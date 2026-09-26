"""Tests for the CTT Express backend: routing, client and parcel mapping.

The two traps this tracker sets: a not-found is an empty ``200`` body rather
than an error object, and ``MANAGEMENTS`` events sit between ``STATUS`` events
without ever being the parcel state.
"""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ctt.const import (
    BROWSER_USER_AGENT,
    EXPRESS_TRACKING_API_URL,
    HISTORY_MAX_EVENTS,
    CTTApiError,
    ParcelStatus,
)
from custom_components.ctt.express import (
    CTTExpressClient,
    is_express_code,
    map_express_status,
    normalize_express_parcel,
)

from .express_payloads import (
    EXPRESS_CODE,
    EXPRESS_CODE_25,
    delivered_history,
    envelope,
    management_event,
    pickup_history,
    rescheduled_history,
    shipping_history,
    status_event,
)


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _response(status: int, text_body: str = "", headers: dict | None = None):
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=text_body)
    # The tracker labels an empty not-found body application/json, so the
    # client must never go through response.json().
    response.json = AsyncMock(side_effect=AssertionError("use response.text()"))
    return response


def _client(response) -> tuple[CTTExpressClient, MagicMock]:
    session = MagicMock()
    session.get = MagicMock(return_value=_ctx(response))
    return CTTExpressClient(session), session


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        (EXPRESS_CODE, True),
        (EXPRESS_CODE_25, True),
        ("DW000000000PT", False),
        ("LW000000000DE", False),
    ],
)
def test_is_express_code_routes_on_shape(code, expected):
    assert is_express_code(code) is expected


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


async def test_client_returns_shipping_history_untouched():
    history = delivered_history()
    client, session = _client(_response(200, json.dumps(envelope(history))))

    assert await client.async_get_parcel(EXPRESS_CODE) == history
    args, kwargs = session.get.call_args
    assert args == (EXPRESS_TRACKING_API_URL,)
    assert kwargs["params"] == {"sc": EXPRESS_CODE}
    assert kwargs["headers"] == {"User-Agent": BROWSER_USER_AGENT}


@pytest.mark.parametrize("body", ["", "  \n"])
async def test_client_empty_body_is_not_found(body):
    client, _ = _client(_response(200, body))
    assert await client.async_get_parcel(EXPRESS_CODE) is None


async def test_client_rate_limit_carries_retry_after():
    client, _ = _client(_response(429, headers={"Retry-After": "30"}))
    with pytest.raises(CTTApiError) as err:
        await client.async_get_parcel(EXPRESS_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == 30.0


@pytest.mark.parametrize("header", [None, "Wed, 21 Oct 2026 07:28:00 GMT"])
async def test_client_rate_limit_without_usable_retry_after(header):
    headers = {"Retry-After": header} if header else {}
    client, _ = _client(_response(429, headers=headers))
    with pytest.raises(CTTApiError) as err:
        await client.async_get_parcel(EXPRESS_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after is None


async def test_client_server_error_raises_with_status():
    client, _ = _client(_response(500, "oops"))
    with pytest.raises(CTTApiError) as err:
        await client.async_get_parcel(EXPRESS_CODE)
    assert err.value.status_code == 500


@pytest.mark.parametrize(
    "body",
    [
        "<html>not json</html>",
        json.dumps(["not", "an", "object"]),
        json.dumps({"error": "boom", "data": None}),
        json.dumps({"error": None, "data": {}}),
        json.dumps({"error": None, "data": {"shipping_history": {"item_code": "1"}}}),
        json.dumps(
            {"error": None, "data": {"shipping_history": {"events": {"0": {}}}}}
        ),
    ],
)
async def test_client_malformed_success_raises(body):
    client, _ = _client(_response(200, body))
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(EXPRESS_CODE)


async def test_client_lets_transport_errors_through():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("down"))
    with pytest.raises(aiohttp.ClientError):
        await CTTExpressClient(session).async_get_parcel(EXPRESS_CODE)


# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("0000", ParcelStatus.REGISTERED),
        ("0500", ParcelStatus.IN_TRANSIT),
        ("1000", ParcelStatus.IN_TRANSIT),
        ("1500", ParcelStatus.OUT_FOR_DELIVERY),
        ("1600", ParcelStatus.PROBLEM),
        ("2100", ParcelStatus.DELIVERED),
        ("2310", ParcelStatus.AT_PICKUP_POINT),
        ("2400", ParcelStatus.OUT_FOR_DELIVERY),
        ("2500", ParcelStatus.RETURNING),
        ("3900", ParcelStatus.IN_TRANSIT),
    ],
)
def test_map_express_status_known(code, expected):
    assert map_express_status(code) is expected


@pytest.mark.parametrize("code", ["1700", "2900", None])
def test_unsettled_or_missing_codes_stay_unmapped(code):
    assert map_express_status(code) is None


# ---------------------------------------------------------------------------
# normalize_express_parcel
# ---------------------------------------------------------------------------


def test_normalize_delivered_ignores_interleaved_managements():
    parcel = normalize_express_parcel(EXPRESS_CODE, delivered_history())

    assert parcel["carrier"] == "CTT Express"
    assert parcel["barcode"] == EXPRESS_CODE
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Entregado"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-09-04T12:15:00+02:00"
    assert parcel["planned_from"] is None
    assert parcel["sender"] is None and parcel["receiver"] is None
    assert parcel["weight"] is None and parcel["dimensions"] is None
    assert parcel["url"].endswith(f"sc={EXPRESS_CODE}")


def test_normalize_barcode_is_the_configured_code_not_item_code():
    raw = delivered_history()
    parcel = normalize_express_parcel(EXPRESS_CODE, raw)
    assert parcel["barcode"] == EXPRESS_CODE != raw["item_code"]
    assert parcel["raw"] is raw


def test_normalize_newest_managements_never_sets_state():
    raw = shipping_history(
        status_event("2026-09-01T09:00:00+02:00", "1500", "En reparto"),
        management_event("2026-09-01T11:00:00+02:00", "75_INAT", "Cambio de dirección"),
    )
    parcel = normalize_express_parcel(EXPRESS_CODE, raw)
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "En reparto"


def test_normalize_does_not_trust_server_order():
    oldest_first = delivered_history()
    newest_first = dict(oldest_first, events=list(reversed(oldest_first["events"])))

    expected = normalize_express_parcel(EXPRESS_CODE, oldest_first, include_history=True)
    actual = normalize_express_parcel(EXPRESS_CODE, newest_first, include_history=True)
    for key in ("status", "raw_status", "delivered_at", "planned_from", "history"):
        assert actual[key] == expected[key], key


def test_normalize_puts_unparseable_dates_last():
    raw = shipping_history(
        status_event("garbage", "2100", "Entregado"),
        status_event("2026-09-01T09:00:00+02:00", "1500", "En reparto"),
    )
    parcel = normalize_express_parcel(EXPRESS_CODE, raw)
    assert parcel["status"] is ParcelStatus.DELIVERED


def test_normalize_pickup_point_from_the_pickup_event():
    parcel = normalize_express_parcel(EXPRESS_CODE, pickup_history())
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Punto Collectt de prueba"


@pytest.mark.parametrize("location", [None, "", "   "])
def test_normalize_pickup_without_location(location):
    parcel = normalize_express_parcel(EXPRESS_CODE, pickup_history(location))
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_normalize_unsettled_code_is_unknown_and_warns_once(caplog):
    raw = shipping_history(
        status_event("2026-09-01T09:00:00+02:00", "1500", "En reparto"),
        status_event("2026-09-01T18:00:00+02:00", "1700", "Almacenado"),
    )
    with caplog.at_level(logging.WARNING):
        first = normalize_express_parcel(EXPRESS_CODE, raw)
        normalize_express_parcel(EXPRESS_CODE, raw)

    assert first["status"] is ParcelStatus.UNKNOWN
    assert first["raw_status"] == "Almacenado"
    warnings = [r for r in caplog.records if "CTT Express code=1700" in r.getMessage()]
    assert len(warnings) == 1
    assert "issues/new" in warnings[0].getMessage()


def test_normalize_planned_from_is_madrid_midnight_on_an_active_parcel():
    parcel = normalize_express_parcel(EXPRESS_CODE, rescheduled_history())
    assert parcel["status"] is ParcelStatus.PROBLEM
    assert parcel["planned_from"] == "2026-11-03T00:00:00+01:00"
    assert parcel["planned_to"] is None


def test_normalize_planned_from_takes_the_newest_revision():
    raw = rescheduled_history()
    raw["events"].append(
        management_event(
            "2026-11-02T09:00:00+01:00",
            "71_INAT",
            "Nueva fecha de entrega",
            delivery_date="2026-11-05",
        )
    )
    parcel = normalize_express_parcel(EXPRESS_CODE, raw)
    assert parcel["planned_from"] == "2026-11-05T00:00:00+01:00"


@pytest.mark.parametrize("value", ["not-a-date", None])
def test_normalize_unusable_delivery_date_gives_no_window(value):
    raw = shipping_history(
        status_event("2026-11-01T09:00:00+01:00", "1600", "Incidencia"),
        management_event(
            "2026-11-01T15:00:00+01:00", "71_INAT", "Nueva fecha", delivery_date=value
        ),
    )
    assert normalize_express_parcel(EXPRESS_CODE, raw)["planned_from"] is None


@pytest.mark.parametrize("final_code", ["2100", "2500"])
def test_normalize_terminal_parcel_drops_the_window(final_code):
    raw = rescheduled_history()
    raw["events"].append(
        status_event("2026-11-03T12:00:00+01:00", final_code, "Final")
    )
    assert normalize_express_parcel(EXPRESS_CODE, raw)["planned_from"] is None


def test_normalize_placeholder_for_not_found():
    parcel = normalize_express_parcel(EXPRESS_CODE, {})
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] is None
    assert parcel["barcode"] == EXPRESS_CODE
    assert parcel["url"].endswith(f"sc={EXPRESS_CODE}")
    assert parcel["history"] is None
    assert parcel["raw"] == {}


def test_normalize_history_is_opt_in_and_status_only():
    assert normalize_express_parcel(EXPRESS_CODE, delivered_history())["history"] is None

    history = normalize_express_parcel(
        EXPRESS_CODE, delivered_history(), include_history=True
    )["history"]
    assert [entry["status"] for entry in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.PROBLEM,
        ParcelStatus.OUT_FOR_DELIVERY,
        ParcelStatus.DELIVERED,
    ]
    assert history[0] == {
        "timestamp": "2026-09-01T09:00:00+02:00",
        "status": ParcelStatus.REGISTERED,
        "raw_status": "Pendiente de recepción",
    }


def test_normalize_history_caps_and_keeps_unmapped_as_none():
    events = [
        status_event(f"2026-09-{day:02d}T10:00:00+02:00", "1000", "Enviado")
        for day in range(1, 26)
    ]
    events.append(status_event("2026-09-26T10:00:00+02:00", "2900", "Recogerán"))
    history = normalize_express_parcel(
        EXPRESS_CODE, shipping_history(*events), include_history=True
    )["history"]
    assert len(history) == HISTORY_MAX_EVENTS
    assert history[-1]["status"] is None
    assert history[-1]["raw_status"] == "Recogerán"
