"""CTT public tracker API client.

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
from typing import Any
from urllib.parse import unquote

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://appserver.ctt.pt"
SCREEN_PATH = "/CustomerArea/screenservices/CustomerArea/CustomerArea/PublicArea_Detail"
VIEW_NAME = "CustomerArea.PublicArea_Detail"

TRACK_ACTION = "DataActionGetObjectEventsByInputObjectCode"
MAINTENANCE_ACTION = "DataActionCheckIPLocked"

MODULE_VERSION_URL = f"{BASE_URL}/CustomerArea/moduleservices/moduleversioninfo"
MODULE_INFO_URL_TEMPLATE = f"{BASE_URL}/CustomerArea/moduleservices/moduleinfo?{{token}}"
SCREEN_SCRIPT_PATH = "/CustomerArea/scripts/CustomerArea.CustomerArea.PublicArea_Detail.mvc.js"

# A plain aiohttp UA is refused by Cloudflare (error 1010) before it ever
# reaches OutSystems — this string only needs to look like a browser, not be
# any particular one.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

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


class CTTApiError(Exception):
    """Raised when a CTT API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"CTT API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class CTTApiClient:
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
