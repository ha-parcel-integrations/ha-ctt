"""CTT Express tracker client and parcel mapping.

CTT Express (Spain) runs its own keyless JSON tracker: a plain GET, no session.
It is the only tracker that knows a purely Spanish parcel, and it knows every
all-digit code, so :func:`is_express_code` routes those here and everything
else stays on the ctt.pt backend in :mod:`.ctt`.

Contract kept for the coordinator, same as :class:`.ctt.CTTClient`:
``async_get_parcel`` returns the raw ``shipping_history`` dict for a known
parcel, ``None`` for a not-found, and raises :class:`CTTApiError` for anything
else.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    BROWSER_USER_AGENT,
    EXPRESS_TRACKING_API_URL,
    EXPRESS_TRACKING_URL,
    HISTORY_MAX_EVENTS,
    CTTApiError,
    ParcelStatus,
)
from .parcels import NEW_ISSUE_URL, parse_iso

_LOGGER = logging.getLogger(__name__)

# ``planned_from`` is a date without a time; anchor it to the carrier's own
# midnight rather than UTC so it lands on the right calendar day.
_EXPRESS_TZ = ZoneInfo("Europe/Madrid")

# Keyed on the ``STATUS`` event code — the label is display text. 1700
# (temporarily stored) and 2900 (to be collected at the depot) were seen but
# their meaning is not settled, so they arrive as `unknown` with the one-shot
# warning until a second sighting pins them down.
EXPRESS_STATUS_MAP: dict[str, ParcelStatus] = {
    "0000": ParcelStatus.REGISTERED,        # pending CTT Express receipt
    "0500": ParcelStatus.IN_TRANSIT,        # collected
    "1000": ParcelStatus.IN_TRANSIT,        # sent
    "1500": ParcelStatus.OUT_FOR_DELIVERY,  # out for delivery
    "1600": ParcelStatus.PROBLEM,           # delivery incident
    "2100": ParcelStatus.DELIVERED,         # delivered
    "2310": ParcelStatus.AT_PICKUP_POINT,   # available at a Collectt pickup point
    "2400": ParcelStatus.OUT_FOR_DELIVERY,  # new delivery round
    "2500": ParcelStatus.RETURNING,         # returning
    "3900": ParcelStatus.IN_TRANSIT,        # international transit
}

# Codes we have already warned about, so each unmapped one is logged only once
# per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def is_express_code(code: str) -> bool:
    """Return whether a normalised tracking code belongs on the CTT Express tracker.

    Every all-digit code seen was known there (including a Portuguese Expresso
    parcel's relabel code), every S10 code on ctt.pt — so the shape decides.
    """
    return code.isdigit()


class CTTExpressClient:
    """Client for the keyless CTT Express public tracker."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's ``shipping_history``, or ``None`` when not found."""
        async with self._session.get(
            EXPRESS_TRACKING_API_URL,
            params={"sc": tracking_code},
            headers={"User-Agent": BROWSER_USER_AGENT},
        ) as response:
            if response.status == 429:
                raise CTTApiError(
                    "HTTP 429",
                    status_code=429,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if not 200 <= response.status < 300:
                raise CTTApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            # Not-found is an empty body served as application/json, which
            # ``response.json()`` would reject — parse the text ourselves.
            body = await response.text()

        if not body.strip():
            return None
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise CTTApiError(f"unparseable body ({err})") from err
        if not isinstance(payload, dict):
            raise CTTApiError("unexpected body (not a JSON object)")
        if payload.get("error") is not None:
            raise CTTApiError(f"tracker error: {payload['error']}")
        history = (payload.get("data") or {}).get("shipping_history")
        if not isinstance(history, dict) or not isinstance(
            history.get("events"), list
        ):
            raise CTTApiError("response has no shipping_history events")
        return history


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped CTT Express code once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised CTT Express status — help us map it. Open an issue "
        "and paste this line: %s\n  CTT Express code=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_express_status(code: str | None) -> ParcelStatus | None:
    """Map a ``STATUS`` event code, or ``None`` (warned once) when unmapped."""
    if code is None:
        return None
    mapped = EXPRESS_STATUS_MAP.get(code)
    if mapped is None:
        _warn_unmapped_status(code)
    return mapped


def _ordered_events(raw: dict) -> list[dict]:
    """Return the events oldest → newest by ``event_date``.

    The server sends them oldest-first, but that is observed, not promised, and
    the newest ``STATUS`` event is the parcel state — so sort rather than trust
    the order. Unparseable dates go last in their original order; the code
    number is never a rank.
    """
    events = [event for event in raw.get("events") or [] if isinstance(event, dict)]
    dated: list[tuple[datetime, dict]] = []
    undated: list[dict] = []
    for event in events:
        parsed = parse_iso(event.get("event_date"))
        if parsed is None:
            undated.append(event)
        else:
            dated.append((parsed, event))
    dated.sort(key=lambda item: item[0])
    return [event for _, event in dated] + undated


def _planned_from(events: list[dict]) -> str | None:
    """Return the newest ``71_INAT`` revised delivery date as local midnight."""
    for event in reversed(events):
        if event.get("code") != "71_INAT":
            continue
        detail = event.get("detail")
        value = detail.get("delivery_date") if isinstance(detail, dict) else None
        if not isinstance(value, str):
            continue
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(day, time(0), _EXPRESS_TZ).isoformat()
    return None


def normalize_express_parcel(
    tracking_code: str, raw: dict, *, include_history: bool = False
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``raw`` is the ``shipping_history`` dict, or ``{}`` for a not-found
    placeholder. ``barcode`` is the configured code, never ``item_code``: the
    tracker echoes a different, longer code. Status comes from the newest
    ``STATUS`` event only — ``MANAGEMENTS`` events record actions such as a new
    delivery date and must never replace the state.
    """
    events = _ordered_events(raw)
    status_events = [event for event in events if event.get("type") == "STATUS"]
    newest = status_events[-1] if status_events else None
    status = (
        map_express_status(newest.get("code")) if newest else None
    ) or ParcelStatus.UNKNOWN
    delivered = status is ParcelStatus.DELIVERED

    pickup_point = None
    if status is ParcelStatus.AT_PICKUP_POINT and newest is not None:
        detail = newest.get("detail")
        location = detail.get("delivery_location") if isinstance(detail, dict) else None
        if isinstance(location, str) and location.strip():
            pickup_point = location

    planned_from = None
    if status not in (ParcelStatus.DELIVERED, ParcelStatus.RETURNING):
        planned_from = _planned_from(events)

    history = None
    if include_history:
        history = [
            {
                "timestamp": event.get("event_date"),
                "status": map_express_status(event.get("code")),
                "raw_status": event.get("description"),
            }
            for event in status_events
        ][-HISTORY_MAX_EVENTS:]

    return {
        "carrier": "CTT Express",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": newest.get("description") if newest else None,
        "delivered": delivered,
        "delivered_at": newest.get("event_date") if delivered and newest else None,
        "planned_from": planned_from,
        "planned_to": None,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
        "url": EXPRESS_TRACKING_URL.format(tracking_code=tracking_code),
        # Declared weight and measures carry no unit, so they stay unclaimed.
        "weight": None,
        "dimensions": None,
        "history": history,
        "raw": raw,
    }
