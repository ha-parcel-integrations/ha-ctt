"""Tests for the suite-wide parcel helpers and the canonical contract.

These need no Home Assistant instance. Both backends' normalisers are checked
here against the one key set and the capabilities each variant declares; their
own mapping tests live in test_ctt.py and test_express.py.
"""
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ctt.const import (
    CAPABILITIES_BY_VARIANT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
)
from custom_components.ctt.ctt import normalize_ctt_parcel
from custom_components.ctt.express import normalize_express_parcel
from custom_components.ctt.parcels import (
    apply_delivered_filter,
    format_dimensions,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .express_payloads import (
    EXPRESS_CODE,
    delivered_history,
    pickup_history,
    rescheduled_history,
)
from .payloads import delivered_sample, pickup_sample

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


def test_to_iso_timestamp_reads_numbers_as_epoch_ms():
    """Suite-wide branch neither backend hits: both stamp ISO strings."""
    assert to_iso_timestamp(0) == "1970-01-01T00:00:00+00:00"
    assert to_iso_timestamp(float("inf")) is None


def test_format_dimensions_needs_all_three_axes():
    """Suite-wide helper, unused by both backends (no variant claims dimensions)."""
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


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
    assert list(normalize_ctt_parcel(delivered_sample())) == CANONICAL_KEYS
    assert (
        list(normalize_express_parcel(EXPRESS_CODE, delivered_history()))
        == CANONICAL_KEYS
    )


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    for variant, fields in CAPABILITIES_BY_VARIANT.items():
        assert fields <= KNOWN_CAPABILITIES, variant


def _assert_capabilities_come_true(
    capabilities: frozenset, delivered: dict, pickup: dict, planned: dict, history: dict
) -> None:
    if "weight" in capabilities:
        assert delivered["weight"] is not None
    if "dimensions" in capabilities:
        assert delivered["dimensions"] is not None
    if "delivery_window" in capabilities:
        assert planned["planned_from"] is not None or planned["planned_to"] is not None
    if "pickup_point" in capabilities:
        assert pickup["pickup_point"] is not None
    if "url" in capabilities:
        assert delivered["url"] is not None
    if "history" in capabilities:
        assert history["history"] is not None


def test_ctt_capabilities_match_what_the_normaliser_returns():
    """Every declared CTT capability must come true somewhere in a sample."""
    delivered = normalize_ctt_parcel(delivered_sample())
    _assert_capabilities_come_true(
        CAPABILITIES_BY_VARIANT["CTT"],
        delivered,
        normalize_ctt_parcel(pickup_sample()),
        delivered,
        normalize_ctt_parcel(delivered_sample(), include_history=True),
    )


def test_express_capabilities_match_what_the_normaliser_returns():
    """Every declared CTT Express capability must come true somewhere in a sample."""
    _assert_capabilities_come_true(
        CAPABILITIES_BY_VARIANT["CTT Express"],
        normalize_express_parcel(EXPRESS_CODE, delivered_history()),
        normalize_express_parcel(EXPRESS_CODE, pickup_history()),
        normalize_express_parcel(EXPRESS_CODE, rescheduled_history()),
        normalize_express_parcel(
            EXPRESS_CODE, delivered_history(), include_history=True
        ),
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
