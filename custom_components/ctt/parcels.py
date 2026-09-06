"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Carrier-specific: :data:`_STATUS_MAP` (keyed on CTT's integer ``StateId``)
and :func:`normalize_parcel`. Everything else — the timestamp parsing, the
history builder, the sort contract, the delivered filter, the one-shot
warning for unmapped statuses — is suite-wide machinery and should be left
alone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ctt/issues/new"
    "?template=unrecognised_status.yml"
)

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


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


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


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


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


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    Status is derived from ``Events.List[0]`` — the newest event, never the
    highest ``Progress`` or "highest status reached". ``Progress`` is not
    monotonic (``Não entregue`` is 90, ``Em entrega`` is 80) and a real
    parcel proves a rank rule wrong: it went out for delivery, failed, and
    returned to ``Em trânsito`` — a last-event rule reports `in_transit`
    (correct), a highest-rank rule reports `problem` (wrong).

    No delivery window is exposed to an anonymous caller (``planned_from``/
    ``planned_to`` are always ``None``), and CTT never returns ``weight`` or
    ``dimensions`` either — see ``CAPABILITIES`` in const.py.
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


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
