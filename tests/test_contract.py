"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ctt.const import ParcelStatus
from custom_components.ctt.ctt import normalize_ctt_parcel
from custom_components.ctt.express import normalize_express_parcel
from custom_components.ctt.parcels import parse_iso, planned_window

from .express_payloads import (
    EXPRESS_CODE,
    delivered_history,
    pickup_history,
    rescheduled_history,
    shipping_history,
    status_event,
)
from .payloads import (
    active_sample,
    delivered_sample,
    event,
    on_hold_pickup_sample,
    pickup_sample,
    returned_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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


def _collected() -> dict:
    sample = pickup_sample()
    sample["Events"]["List"].insert(
        0, event("2026-06-12T15:00:00Z", 12, "Entregue", progress=100)
    )
    return sample


def _express_collected() -> dict:
    return shipping_history(
        status_event("2026-09-01T09:00:00+02:00", "1000", "Enviado"),
        status_event(
            "2026-09-02T10:00:00+02:00",
            "2310",
            "Disponible",
            delivery_location="Punto Collectt de prueba",
        ),
        status_event("2026-09-03T10:00:00+02:00", "2100", "Entregado"),
    )


CTT_SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "on_hold_pickup": on_hold_pickup_sample,
    "collected": _collected,
    "returned": returned_sample,
}

EXPRESS_SAMPLES = {
    "delivered": delivered_history,
    "at_pickup_point": pickup_history,
    "collected": _express_collected,
    "rescheduled": rescheduled_history,
    "empty": dict,
}


def _ctt(name: str) -> dict:
    return normalize_ctt_parcel(CTT_SAMPLES[name]())


def _express(name: str) -> dict:
    return normalize_express_parcel(EXPRESS_CODE, EXPRESS_SAMPLES[name]())


@pytest.fixture(
    params=[("ctt", n) for n in CTT_SAMPLES] + [("express", n) for n in EXPRESS_SAMPLES]
)
def parcel(request) -> dict:
    kind, name = request.param
    return _ctt(name) if kind == "ctt" else _express(name)


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    back_at_sender = parcel["carrier"] == "CTT" and (
        parcel["status"] is ParcelStatus.RETURNING
    )
    assert parcel["delivered"] is (
        parcel["status"] is ParcelStatus.DELIVERED or back_at_sender
    )


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_ctt_parcel_stays_pickup():
    parcel = _ctt("collected")
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Loja CTT Esgueira (Aveiro)"
    assert parcel["delivered_at"] == "2026-06-12T15:00:00Z"


def test_a_collected_express_parcel_stays_pickup():
    parcel = _express("collected")
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Punto Collectt de prueba"


def test_a_home_delivery_is_not_pickup():
    for parcel in (_ctt("delivered"), _ctt("active"), _express("delivered")):
        assert parcel["pickup"] is False
        assert parcel["pickup_point"] is None


def test_a_returned_ctt_parcel_is_back_at_the_sender():
    parcel = _ctt("returned")
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is True


def test_an_express_return_in_progress_is_not_delivered():
    raw = shipping_history(status_event("2026-09-01T09:00:00+02:00", "2500", "X"))
    parcel = normalize_express_parcel(EXPRESS_CODE, raw)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_a_delivery_date_covers_the_whole_local_day():
    parcel = _express("rescheduled")
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert start.utcoffset() == end.utcoffset()


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (None, None, (None, None)),
        (
            "2099-03-01T10:00:00+01:00",
            None,
            ("2099-03-01T10:00:00+01:00", "2099-03-01T23:59:59+01:00"),
        ),
        (
            "2099-03-01T23:59:59+01:00",
            None,
            ("2099-03-01T23:59:59+01:00", None),
        ),
        (
            "2099-03-01T08:00:00",
            "2099-03-01T12:00:00",
            ("2099-03-01T08:00:00+01:00", "2099-03-01T12:00:00+01:00"),
        ),
        (
            "2099-03-01",
            "2099-03-03",
            ("2099-03-01T00:00:00+01:00", "2099-03-03T23:59:59+01:00"),
        ),
        (
            "2099-03-03",
            "2099-03-01",
            ("2099-03-01T00:00:00+01:00", "2099-03-03T23:59:59+01:00"),
        ),
        ("garbage", None, (None, None)),
    ],
)
def test_planned_window_shapes(start, end, expected):
    assert planned_window(start, end, zone=ZoneInfo("Europe/Madrid")) == expected
