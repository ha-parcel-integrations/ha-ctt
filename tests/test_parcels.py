"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ctt.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ctt.parcels import (
    apply_delivered_filter,
    build_history,
    ctt_timestamp,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
    tracking_url,
)

from .payloads import (
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    derivation_counter_example_sample,
    event,
    in_transit_sample,
    on_hold_pickup_sample,
    pickup_sample,
    returned_sample,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state_id,expected",
    [
        (1, ParcelStatus.REGISTERED),
        (2, ParcelStatus.IN_TRANSIT),
        (5, ParcelStatus.RETURNING),
        (7, ParcelStatus.OUT_FOR_DELIVERY),
        (8, ParcelStatus.IN_TRANSIT),
        (10, ParcelStatus.IN_TRANSIT),
        (11, ParcelStatus.IN_TRANSIT),
        (12, ParcelStatus.DELIVERED),
        (13, ParcelStatus.PROBLEM),
        (14, ParcelStatus.AT_PICKUP_POINT),
    ],
)
def test_map_parcel_status_known(state_id, expected):
    assert map_parcel_status(state_id) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    """One of the six State literals seen only in CTT's own rendering code."""
    assert map_parcel_status(99) == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status(99) is None
    assert map_event_status(12) == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status(42) == ParcelStatus.UNKNOWN
    assert map_parcel_status(42) == ParcelStatus.UNKNOWN
    assert caplog.text.count("StateId=42") == 1
    assert "issues/new" in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_passes_ctts_own_iso_strings_through():
    """CTT stamps ``DateTime`` as ISO 8601 with a ``Z`` suffix, not epoch ms."""
    assert to_iso_timestamp("2026-04-02T12:09:12Z") == "2026-04-02T12:09:12Z"
    assert to_iso_timestamp(None) is None


def test_ctt_timestamp_treats_1900_01_01_as_absent():
    """OutSystems' null-date sentinel must never reach a timestamp field."""
    assert ctt_timestamp("1900-01-01T00:00:00") is None
    assert ctt_timestamp("1900-01-01") is None
    assert ctt_timestamp("") is None
    assert ctt_timestamp(None) is None
    assert ctt_timestamp("2026-04-02T12:09:12Z") == "2026-04-02T12:09:12Z"


def test_format_dimensions_needs_all_three_axes():
    """Suite-wide helper, unused by CTT (CAPABILITIES excludes dimensions)."""
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_oldest_to_newest():
    events = delivered_sample()["Events"]["List"]
    history = build_history(events)
    assert len(history) == 10
    assert history[0]["status"] == ParcelStatus.REGISTERED
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_does_not_deduplicate():
    """The delivered sample carries two legitimate 'Não entregue' events 41s apart."""
    events = delivered_sample()["Events"]["List"]
    history = build_history(events)
    failed = [entry for entry in history if entry["status"] == ParcelStatus.PROBLEM]
    assert len(failed) == 2
    assert history[3]["timestamp"] != history[4]["timestamp"]


def test_build_history_caps_to_max_events():
    events = [
        event(f"2026-04-{day:02d}T10:00:00Z", 11, "Em trânsito")
        for day in range(1, 26)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"StateId": 11}]) == []  # no DateTime
    assert build_history(["not-a-dict"]) == []


def test_build_history_drops_the_null_date_sentinel():
    history = build_history(
        [
            event("2026-04-24T10:00:00Z", 1, "Aguarda entrada nos CTT"),
            event("1900-01-01T00:00:00", 0, ""),
        ]
    )
    assert len(history) == 1


def test_build_history_keeps_unparseable_timestamp_last():
    history = build_history(
        [
            event("2026-04-24T10:00:00Z", 1, "Aguarda entrada nos CTT"),
            {**event("not-a-date", 11, "Em trânsito"), "DateTime": "not-a-date"},
        ]
    )
    assert [entry["status"] for entry in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
    ]


def test_build_history_prefers_event_text_falls_back_to_state():
    history = build_history(
        [event("2026-04-24T10:00:00Z", 12, "Entregue", event_text="O envio foi entregue.")]
    )
    assert history[0]["raw_status"] == "O envio foi entregue."

    history = build_history([event("2026-04-24T10:00:00Z", 12, "Entregue")])
    assert history[0]["raw_status"] == "Entregue"


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert delivered["planned_from"] is not None or delivered["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_capabilities_never_claims_a_delivery_window_or_weight():
    """CTT does not expose these to an anonymous caller — see const.py."""
    assert "delivery_window" not in CAPABILITIES
    assert "weight" not in CAPABILITIES
    assert "dimensions" not in CAPABILITIES


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "CTT"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Entregue"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-02T12:09:12Z"
    # A delivered parcel still has no window — CTT never exposes one.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] == (
        "https://www.ctt.pt/feapl_2/app/open/objectSearch/objectSearch.jspx"
        f"?objects={DELIVERED_CODE}"
    )
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 10
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_no_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_derivation_counter_example_is_in_transit_not_problem():
    """The single most important status-mapping test on this carrier.

    This parcel went out for delivery, failed once (StateId 13, Progress
    90 — higher than the 80 of `Em entrega`), and returned to the network.
    ``Events.List[0]`` (the newest event) is the current status: this must
    normalise to `in_transit`, never `problem` — a rank/highest-`Progress`
    rule would get this wrong.
    """
    parcel = normalize_parcel(derivation_counter_example_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Em trânsito"


def test_normalize_in_transit_parcel():
    parcel = normalize_parcel(in_transit_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT


def test_normalize_on_hold_pickup_parcel_ends_up_awaiting_pickup():
    """StateId 8 ('Em espera') is a delay, not an exception — it must not
    surface as `unknown` or `problem`, and the parcel still reaches
    `AT_PICKUP_POINT` once it resumes."""
    parcel = normalize_parcel(on_hold_pickup_sample(), include_history=True)
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    on_hold_statuses = {
        entry["status"] for entry in parcel["history"]
        if entry["raw_status"] == "O envio encontra-se em espera."
    }
    assert on_hold_statuses == {ParcelStatus.IN_TRANSIT}


def test_normalize_returned_parcel_is_returning():
    """StateId 5 ('Devolvido') is the suite's `returning` member, not a
    one-off `returned`/`cancelled` status the enum doesn't have."""
    parcel = normalize_parcel(returned_sample())
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["raw_status"] == "Devolvido"
    assert parcel["delivered"] is False


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Loja CTT Esgueira (Aveiro)"


def test_normalize_pickup_point_ignores_top_level_flags():
    """IsDeliveryPoint/IsLocker were false even on real pickup-point parcels."""
    raw = pickup_sample()
    raw["IsDeliveryPoint"] = False
    raw["IsLocker"] = False
    parcel = normalize_parcel(raw)
    assert parcel["pickup_point"] == "Loja CTT Esgueira (Aveiro)"


def test_tracking_url_none_without_a_code():
    assert tracking_url(None) is None
    assert tracking_url("") is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-fetched code still yields a full parcel dict."""
    parcel = normalize_parcel({"ObjectCode": "RR000000000PT"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_blank_person_fields_become_none():
    """Empty on all four real parcels for an anonymous caller — map, don't
    hardcode, so the first parcel that does carry a name keeps it."""
    parcel = normalize_parcel(active_sample())
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_maps_sender_and_receiver_when_present():
    raw = active_sample()
    raw["Sender"] = "Loja Exemplo"
    raw["Recipient"] = "Cliente Exemplo"
    parcel = normalize_parcel(raw)
    assert parcel["sender"] == "Loja Exemplo"
    assert parcel["receiver"] == "Cliente Exemplo"


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_parcel(raw)["raw"] is raw


def test_normalize_raw_round_trips_fields_nothing_else_maps():
    """A key nothing else maps must survive — guards against a future
    'tidy-up' quietly trimming ``raw``."""
    parcel = normalize_parcel(active_sample())
    assert parcel["raw"]["RelabelObjectCode"] == "0010000000000000000000001"
    assert parcel["raw"]["SpecialServices"]["List"][0]["Description"] == (
        "2 tentativas e avisar CTT"
    )


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
