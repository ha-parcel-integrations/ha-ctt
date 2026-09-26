"""CTT Portugal tracker client and parcel mapping.

CTT's tracker is an OutSystems Reactive app. There is no credential, but
there is a session: the endpoint answers ``403 Invalid Login`` to a cookie-less
request and *sets* the session cookie on that same response — the 403 is the
bootstrap, not an error. Two more requirements come along for the ride:

* **Version tokens, derived not pinned.** ``moduleVersion`` and the per-action
  ``apiVersion`` change on every CTT deploy. Both are public and
  reconstructable — ``moduleVersion`` from a keyless GET, ``apiVersion`` from
  a literal inside the screen's own JS bundle. A ``200`` response says
  ``versionInfo.hasModuleVersionChanged`` / ``hasApiVersionChanged`` when a
  cached token has gone stale; that is a "re-derive and retry once" signal,
  never an error.
* **A browser ``User-Agent`` is mandatory.** Cloudflare fronts the host and
  rejects the default aiohttp UA with error 1010 before the request reaches
  OutSystems.
* **An outage looks exactly like a not-found.** ``Found: false`` inside a
  ``200`` is CTT's semantic-404 for a genuinely unknown code, but the same
  shape is also what the backend returns while the whole tracker is down.
  The honest signal lives on a sibling action, ``DataActionCheckIPLocked``,
  which is only worth calling once ``Found`` is already false — a found
  parcel already proves the backend is up.

Contract kept for the coordinator: ``async_get_parcel`` returns the raw
``ObjectEventsFromQuery`` dict for a known parcel, ``None`` for a genuine
not-found, and raises :class:`CTTApiError` for anything else — including an
outage, so the coordinator surfaces ``UpdateFailed`` instead of quietly
reporting every parcel as unknown.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import unquote

import aiohttp

from .const import (
    BROWSER_USER_AGENT,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    CTTApiError,
    ParcelStatus,
)
from .parcels import NEW_ISSUE_URL, parse_iso, to_iso_timestamp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://appserver.ctt.pt"
SCREEN_PATH = "/CustomerArea/screenservices/CustomerArea/CustomerArea/PublicArea_Detail"
VIEW_NAME = "CustomerArea.PublicArea_Detail"

TRACK_ACTION = "DataActionGetObjectEventsByInputObjectCode"
MAINTENANCE_ACTION = "DataActionCheckIPLocked"

MODULE_VERSION_URL = f"{BASE_URL}/CustomerArea/moduleservices/moduleversioninfo"
MODULE_INFO_URL_TEMPLATE = f"{BASE_URL}/CustomerArea/moduleservices/moduleinfo?{{token}}"
SCREEN_SCRIPT_PATH = "/CustomerArea/scripts/CustomerArea.CustomerArea.PublicArea_Detail.mvc.js"

_COMMON_HEADERS = {"User-Agent": BROWSER_USER_AGENT}


def _api_version_pattern(action_name: str) -> re.Pattern[str]:
    """Match ``controller.callDataAction("<action>", "<path>", "<apiVersion>"``."""
    return re.compile(
        r'callDataAction\(\s*"' + re.escape(action_name) + r'"\s*,\s*"[^"]*"\s*,\s*"([^"]+)"'
    )


def _parse_nr2_users_cookie(raw_value: str) -> dict[str, str]:
    """Split the URL-encoded ``nr2Users`` cookie value into its fields.

    The raw value looks like ``crf%3d<TOKEN>%3buid%3d0%3bunm%3d`` — URL-decode
    it first (``%3d`` → ``=``, ``%3b`` → ``;``), then split on ``;``.
    """
    decoded = unquote(raw_value)
    fields: dict[str, str] = {}
    for segment in decoded.split(";"):
        if "=" in segment:
            key, _, value = segment.partition("=")
            fields[key] = value
    return fields


# Key on StateId (the stable integer), never on the Portuguese `State`
# literal — the literals carry inconsistent casing in CTT's own rendering
# code ("Dados Alterados" / "Dados alterados") and are display text, not a
# vocabulary. Ten entries, confirmed on the wire across real parcels:
# four on 2026-09-06, plus StateId 5 (Devolvido) and 8 (Em espera) added
# 2026-09-06 after real installs surfaced them as `unknown`. Six further
# `State` literals are known from CTT's own rendering code (Em devolução,
# Cancelado, Em exportação, No cacifo CTT, Aguarda entrada na rede, Dados
# Alterados) but their StateIds have never been seen on the wire, so
# guessing the integers would be mapping *wrongly* rather than mapping too
# little — they arrive as `unknown` with the one-shot warning, which is how
# this map is meant to grow. `ParcelStatus` has no `failed_attempt`/`cancelled`
# member; `Não entregue` (StateId 13, a failed delivery attempt) maps to
# `problem`, and `Devolvido` (StateId 5, delivered back to the sender) maps
# to `returning` — the suite convention collapses both an in-progress and a
# completed return into that one member.
_STATUS_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.REGISTERED,        # Aguarda entrada nos CTT
    2: ParcelStatus.IN_TRANSIT,        # Aceite - handed over, in the network
    5: ParcelStatus.RETURNING,         # Devolvido - delivered back to sender
    7: ParcelStatus.OUT_FOR_DELIVERY,  # Em entrega
    8: ParcelStatus.IN_TRANSIT,        # Em espera - on hold, resumes on its own
    10: ParcelStatus.IN_TRANSIT,       # Em importação
    11: ParcelStatus.IN_TRANSIT,       # Em trânsito
    12: ParcelStatus.DELIVERED,        # Entregue
    13: ParcelStatus.PROBLEM,          # Não entregue - failed delivery attempt
    14: ParcelStatus.AT_PICKUP_POINT,  # No ponto de entrega
}

# StateIds we have already warned about, so each unmapped one is logged only
# once per HA session instead of on every poll.
_unmapped_statuses_logged: set[int] = set()


def _warn_unmapped_status(state_id: int) -> None:
    """Log an unmapped CTT StateId once, with a copy-paste issue link."""
    if state_id in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(state_id)
    _LOGGER.warning(
        "Unrecognised CTT StateId — help us map it. Open an issue "
        "and paste this line: %s\n  StateId=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        state_id,
    )


def map_parcel_status(state_id: int | None) -> ParcelStatus:
    """Map a CTT ``StateId`` to a canonical :class:`ParcelStatus`.

    ``None`` (no events at all) reports ``unknown`` silently; an
    unrecognised ``StateId`` reports ``unknown`` with a one-shot warning
    rather than inheriting whatever status came before it.
    """
    if state_id is None:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(state_id)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(state_id)
    return ParcelStatus.UNKNOWN


def map_event_status(state_id: int | None) -> ParcelStatus | None:
    """Map a history entry's ``StateId`` to a canonical status, or ``None``.

    Unmapped StateIds keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to
    unknown") and warn once, reusing the parcel-status one-shot set.
    """
    if state_id is None:
        return None
    mapped = _STATUS_MAP.get(state_id)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(state_id)
    return None


def ctt_timestamp(value: str | None) -> str | None:
    """Return an ISO 8601 timestamp, treating CTT's null-date sentinel as absent.

    OutSystems represents "no date" as ``1900-01-01T00:00:00`` (events) or
    ``1900-01-01`` (``WithdrawalDate``) rather than an empty string or
    ``null`` — real events always carry a UTC ``Z`` suffix, the sentinel
    never does. Treat either shape as absent so it never reaches a timestamp
    field.
    """
    if not value or str(value).startswith("1900-01-01"):
        return None
    return to_iso_timestamp(value)


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from CTT's ``Events.List``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` prefers ``Event`` (a full
    Portuguese sentence) over ``State`` (the short display literal) — falls
    back to the latter when the former is empty. ``Events.List`` arrives
    newest-first; this function sorts oldest → newest and caps to the most
    recent ``max_events`` itself, so it is passed through unsorted.

    Deliberately **not** deduplicated: CTT genuinely emits duplicate events
    (a real parcel carried two ``Não entregue`` events 41 seconds apart) —
    collapsing on ``(DateTime, StateId)`` would silently drop a legitimate
    row.
    """
    parseable: list[tuple[datetime, dict]] = []
    unparseable: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = ctt_timestamp(event.get("DateTime"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("StateId")),
            "raw_status": event.get("Event") or event.get("State"),
        }
        parsed = parse_iso(timestamp)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    parseable.sort(key=lambda item: item[0])
    ordered = [entry for _, entry in parseable] + unparseable
    return ordered[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _events_list(raw: dict) -> list[dict]:
    """Return CTT's ``Events.List``, newest-first as the API returns it."""
    events = (raw.get("Events") or {}).get("List")
    return [event for event in events or [] if isinstance(event, dict)]


def _newest_event_with_state_id(events: list[dict], state_id: int) -> dict | None:
    """Return the newest event with the given ``StateId``, or ``None``.

    ``events`` is newest-first, so the first match already is the newest one
    — used for ``delivered_at`` (StateId 12) and ``pickup_point`` (StateId
    14), both of which must come from the *event*, never a top-level flag.
    """
    for event in events:
        if event.get("StateId") == state_id:
            return event
    return None


def normalize_ctt_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    Status is derived from ``Events.List[0]`` — the newest event, never the
    highest ``Progress`` or "highest status reached". ``Progress`` is not
    monotonic (``Não entregue`` is 90, ``Em entrega`` is 80) and a real
    parcel proves a rank rule wrong: it went out for delivery, failed, and
    returned to ``Em trânsito`` — a last-event rule reports `in_transit`
    (correct), a highest-rank rule reports `problem` (wrong).

    No delivery window is exposed to an anonymous caller (``planned_from``/
    ``planned_to`` are always ``None``), and CTT never returns ``weight`` or
    ``dimensions`` either — see ``CAPABILITIES_BY_VARIANT`` in const.py.
    """
    events = _events_list(raw)
    newest = events[0] if events else None
    state_id = newest.get("StateId") if newest else None
    status = map_parcel_status(state_id)
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        delivered_event = _newest_event_with_state_id(events, 12)
        if delivered_event is not None:
            delivered_at = ctt_timestamp(delivered_event.get("DateTime"))

    pickup_point = None
    if status is ParcelStatus.AT_PICKUP_POINT:
        pickup_event = _newest_event_with_state_id(events, 14)
        if pickup_event is not None:
            pickup_point = pickup_event.get("Local") or None

    tracking_code = raw.get("ObjectCode")

    return {
        "carrier": "CTT",
        "barcode": tracking_code,
        "sender": raw.get("Sender") or None,
        "receiver": raw.get("Recipient") or None,
        "status": status,
        "raw_status": newest.get("State") if newest else None,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }




class CTTClient:
    """Client for CTT's keyless, session-bootstrapped public tracker.

    Session cookie, CSRF token and both version tokens are cached on the
    instance and re-derived on demand — never hardcoded, since CTT redeploys
    invalidate the version tokens and any 403 can mean the session expired.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session
        self._module_version: str | None = None
        self._api_versions: dict[str, str] = {}
        self._screen_script: str | None = None
        self._cookie_value: str | None = None
        self._csrf_token: str | None = None

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking record.

        Returns the ``ObjectEventsFromQuery`` dict for a known parcel, or
        ``None`` when CTT genuinely reports the code as not found. When
        ``Found`` is false *and* CTT is in maintenance, raises
        :class:`CTTApiError` instead — an outage must never be reported as a
        not-found (see the module docstring).
        """
        payload = await self._call_action(
            TRACK_ACTION,
            {
                "ObjectCodeInput": tracking_code,
                "SearchInput": tracking_code,
                "IsFromPublicArea": True,
                "IPClient": "",
            },
        )
        data = payload.get("data") or {}
        record = data.get("ObjectEventsFromQuery")
        if isinstance(record, dict) and record.get("Found"):
            return record

        if await self._is_maintenance():
            raise CTTApiError("CTT is in maintenance — not a genuine not-found")
        return None

    async def _is_maintenance(self) -> bool:
        """Ask the sibling action whether CTT is currently down.

        Only called on ``Found: false`` — a found parcel already proves the
        backend is up, so this is not worth spending on every poll.
        """
        payload = await self._call_action(MAINTENANCE_ACTION, {})
        data = payload.get("data") or {}
        return bool(data.get("IsMaintenance"))

    async def _call_action(
        self,
        action_name: str,
        variables: dict[str, Any],
        *,
        retry_session: bool = True,
        retry_version: bool = True,
    ) -> dict[str, Any]:
        """POST one OutSystems data action and return the parsed envelope.

        Handles both retryable conditions the module docstring describes,
        each at most once per call: a ``403`` re-bootstraps the anonymous
        session from the cookie the server sets on that same response; a
        stale version token re-derives and retries. Anything else raises.
        """
        api_version = await self._ensure_api_version(action_name)
        url = f"{BASE_URL}{SCREEN_PATH}/{action_name}"
        body = {
            "versionInfo": {
                "moduleVersion": self._module_version,
                "apiVersion": api_version,
            },
            "viewName": VIEW_NAME,
            "screenData": {"variables": variables},
        }
        headers = dict(_COMMON_HEADERS)
        headers["Content-Type"] = "application/json; charset=UTF-8"
        headers["Accept"] = "application/json"
        if self._cookie_value and self._csrf_token:
            headers["Cookie"] = f"nr2Users={self._cookie_value}"
            headers["X-CSRFToken"] = self._csrf_token

        async with self._session.post(url, json=body, headers=headers) as response:
            if response.status == 403:
                bootstrapped = self._capture_session_cookie(response)
                if retry_session and bootstrapped:
                    return await self._call_action(
                        action_name,
                        variables,
                        retry_session=False,
                        retry_version=retry_version,
                    )
                raise CTTApiError(
                    "anonymous session bootstrap failed", status_code=403
                )
            if response.status != 200:
                raise CTTApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise CTTApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise CTTApiError("unexpected body (not a JSON object)")

        version_info = payload.get("versionInfo") or {}
        stale = version_info.get("hasModuleVersionChanged") or version_info.get(
            "hasApiVersionChanged"
        )
        if stale and retry_version:
            self._module_version = None
            self._screen_script = None
            self._api_versions.pop(action_name, None)
            return await self._call_action(
                action_name, variables, retry_session=retry_session, retry_version=False
            )

        return payload

    def _capture_session_cookie(self, response: aiohttp.ClientResponse) -> bool:
        """Extract the anonymous session cookie and CSRF token from a 403.

        Returns whether a usable cookie was found — the 403 is expected the
        first time and carries ``Set-Cookie: nr2Users=...``, but a 403
        without one means something else is wrong and must not loop forever.
        """
        cookie = response.cookies.get("nr2Users")
        if cookie is None:
            return False
        self._cookie_value = cookie.value
        fields = _parse_nr2_users_cookie(cookie.value)
        crf = fields.get("crf")
        if not crf:
            return False
        self._csrf_token = crf
        return True

    async def _ensure_api_version(self, action_name: str) -> str:
        """Return the cached ``apiVersion`` for ``action_name``, deriving it if needed."""
        cached = self._api_versions.get(action_name)
        if cached is not None:
            return cached
        await self._ensure_module_version()
        script = await self._ensure_screen_script()
        match = _api_version_pattern(action_name).search(script)
        if match is None:
            raise CTTApiError(f"could not derive apiVersion for {action_name}")
        version = match.group(1)
        self._api_versions[action_name] = version
        return version

    async def _ensure_module_version(self) -> None:
        """Derive and cache ``moduleVersion`` from the keyless version endpoint."""
        if self._module_version is not None:
            return
        async with self._session.get(
            MODULE_VERSION_URL, headers=_COMMON_HEADERS
        ) as response:
            if response.status != 200:
                raise CTTApiError(
                    f"HTTP {response.status} deriving moduleVersion",
                    status_code=response.status,
                )
            payload = await response.json(content_type=None)
        token = (payload or {}).get("versionToken")
        if not token:
            raise CTTApiError("moduleversioninfo returned no versionToken")
        self._module_version = token

    async def _ensure_screen_script(self) -> str:
        """Return the cached screen bundle JS, fetching it via the manifest if needed."""
        if self._screen_script is not None:
            return self._screen_script
        manifest_url = MODULE_INFO_URL_TEMPLATE.format(token=self._module_version)
        async with self._session.get(manifest_url, headers=_COMMON_HEADERS) as response:
            if response.status != 200:
                raise CTTApiError(
                    f"HTTP {response.status} fetching module manifest",
                    status_code=response.status,
                )
            manifest = await response.json(content_type=None)
        url_versions = ((manifest or {}).get("manifest") or {}).get("urlVersions") or {}
        script_token = url_versions.get(SCREEN_SCRIPT_PATH)
        if not script_token:
            raise CTTApiError("module manifest missing the screen script's version token")
        script_url = f"{BASE_URL}{SCREEN_SCRIPT_PATH}?{script_token}"
        async with self._session.get(script_url, headers=_COMMON_HEADERS) as response:
            if response.status != 200:
                raise CTTApiError(
                    f"HTTP {response.status} fetching screen script",
                    status_code=response.status,
                )
            script = await response.text()
        self._screen_script = script
        return script
