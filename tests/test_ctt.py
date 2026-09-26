"""Tests for the CTT Portugal backend: the ctt.pt client and its parcel mapping.

CTT's tracker needs three things a plain HTTP client does not usually need
to model: an anonymous session bootstrapped off a `403`, two derived version
tokens, and a maintenance check that turns a `Found: false` into a real
error when the whole backend — not just the tracking code — is down. Each
gets its own section below.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ctt.const import (
    CAPABILITIES_BY_VARIANT,
    CTTApiError,
    ParcelStatus,
)
from custom_components.ctt.ctt import (
    BASE_URL,
    MAINTENANCE_ACTION,
    SCREEN_SCRIPT_PATH,
    TRACK_ACTION,
    CTTClient,
    build_history,
    ctt_timestamp,
    map_event_status,
    map_parcel_status,
    normalize_ctt_parcel,
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

CODE = "RR999999999PT"

MODULE_VERSION_TOKEN = "MODULETOKEN"
SCRIPT_VERSION_TOKEN = "SCRIPTTOKEN"
TRACK_API_VERSION = "TRACKAPIVERSION"
MAINTENANCE_API_VERSION = "MAINTAPIVERSION"

SCREEN_SCRIPT_TEXT = (
    'controller.callDataAction("DataActionGetObjectEventsByInputObjectCode", '
    f'"screenservices/x", "{TRACK_API_VERSION}", []);\n'
    'controller.callDataAction("DataActionCheckIPLocked", '
    f'"screenservices/y", "{MAINTENANCE_API_VERSION}", []);'
)


class _Morsel:
    def __init__(self, value: str) -> None:
        self.value = value


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _response(
    status: int,
    *,
    json_body: object = None,
    text_body: str | None = None,
    cookies: dict | None = None,
    bad_json: bool = False,
) -> AsyncMock:
    response = AsyncMock()
    response.status = status
    if bad_json:
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", "x", 0))
    else:
        response.json = AsyncMock(return_value=json_body)
    response.text = AsyncMock(return_value=text_body or "")
    response.cookies = cookies or {}
    return response


def _bootstrap_cookie() -> dict:
    """A ``403`` response's ``Set-Cookie`` in aiohttp's own ``response.cookies`` shape."""
    return {"nr2Users": _Morsel("crf%3dTOKEN123%3buid%3d0%3bunm%3d")}


def _version_derivation_gets() -> list:
    """The three GETs every fresh client issues once, before its first POST."""
    return [
        _ctx(_response(200, json_body={"versionToken": MODULE_VERSION_TOKEN})),
        _ctx(
            _response(
                200,
                json_body={"manifest": {"urlVersions": {SCREEN_SCRIPT_PATH: SCRIPT_VERSION_TOKEN}}},
            )
        ),
        _ctx(_response(200, text_body=SCREEN_SCRIPT_TEXT)),
    ]


def _found_envelope(object_code: str = CODE) -> dict:
    return {
        "versionInfo": {"hasModuleVersionChanged": False, "hasApiVersionChanged": False},
        "data": {
            "ObjectEventsFromQuery": {
                "ObjectCode": object_code,
                "Found": True,
                "Events": {"List": []},
            },
            "Result": {"Success": True, "ErrorMessage": ""},
        },
    }


def _not_found_envelope() -> dict:
    return {
        "versionInfo": {"hasModuleVersionChanged": False, "hasApiVersionChanged": False},
        "data": {
            "ObjectEventsFromQuery": {"ObjectCode": "", "Found": False, "Events": {"List": []}},
            "Result": {"Success": True, "ErrorMessage": ""},
        },
    }


def _maintenance_envelope(*, is_maintenance: bool) -> dict:
    return {
        "versionInfo": {"hasModuleVersionChanged": False, "hasApiVersionChanged": False},
        "data": {
            "Result": {"Success": not is_maintenance, "ErrorMessage": ""},
            "IsMaintenance": is_maintenance,
            "WebURLRedirect": "https://www.ctt.pt/home/site-em-manutencao"
            if is_maintenance
            else "",
        },
    }


def _client(get_responses: list, post_responses: list) -> tuple[CTTClient, MagicMock]:
    session = MagicMock()
    session.get = MagicMock(side_effect=get_responses)
    session.post = MagicMock(side_effect=post_responses)
    return CTTClient(session), session


# ---------------------------------------------------------------------------
# the happy path: version derivation + session bootstrap + a found parcel
# ---------------------------------------------------------------------------


async def test_get_parcel_returns_record_on_success():
    client, session = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),  # bootstrap
            _ctx(_response(200, json_body=_found_envelope())),  # real request
        ],
    )

    record = await client.async_get_parcel(CODE)

    assert record["ObjectCode"] == CODE
    assert record["Found"] is True
    # tracking code goes in the request body, not the URL
    assert session.post.call_args_list[-1].kwargs["json"]["screenData"][
        "variables"
    ]["ObjectCodeInput"] == CODE


async def test_second_call_reuses_the_session_and_version_tokens():
    """Only the very first call pays for the bootstrap + version derivation."""
    client, session = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=_found_envelope())),
            _ctx(_response(200, json_body=_found_envelope("OTHER"))),
        ],
    )

    await client.async_get_parcel(CODE)
    record = await client.async_get_parcel("OTHER")

    assert record["ObjectCode"] == "OTHER"
    assert session.get.call_count == 3  # not re-derived
    assert session.post.call_count == 3  # one bootstrap 403 + two real posts


# ---------------------------------------------------------------------------
# the outage trap: Found: false is not always a not-found
# ---------------------------------------------------------------------------


async def test_get_parcel_returns_none_on_genuine_not_found():
    client, _ = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=_not_found_envelope())),
            _ctx(_response(200, json_body=_maintenance_envelope(is_maintenance=False))),
        ],
    )

    assert await client.async_get_parcel(CODE) is None


async def test_get_parcel_raises_when_found_false_is_actually_an_outage():
    """The single most important behaviour on this surface: an outage must
    never be reported as a not-found, or the coordinator would silently show
    every parcel as unknown for as long as CTT is down."""
    client, _ = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=_not_found_envelope())),
            _ctx(_response(200, json_body=_maintenance_envelope(is_maintenance=True))),
        ],
    )

    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_maintenance_check_reuses_cached_module_version_and_script():
    """No extra GET for the sibling action — same screen, same bundle."""
    client, session = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=_not_found_envelope())),
            _ctx(_response(200, json_body=_maintenance_envelope(is_maintenance=False))),
        ],
    )

    await client.async_get_parcel(CODE)

    assert session.get.call_count == 3  # module version + manifest + script, once
    maintenance_call = session.post.call_args_list[-1]
    assert MAINTENANCE_ACTION in maintenance_call.args[0]


# ---------------------------------------------------------------------------
# session bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_403_without_a_cookie_raises():
    client, _ = _client(
        _version_derivation_gets(),
        [_ctx(_response(403, cookies={}))],  # no nr2Users -> nothing to retry with
    )

    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_bootstrap_cookie_without_crf_raises():
    client, _ = _client(
        _version_derivation_gets(),
        [_ctx(_response(403, cookies={"nr2Users": _Morsel("uid%3d0")}))],  # no crf field
    )

    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_expired_session_re_bootstraps_on_a_later_403():
    """A 403 mid-session is normal handshake traffic, never a user-facing error."""
    client, session = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=_found_envelope())),
            _ctx(_response(403, cookies=_bootstrap_cookie())),  # session expired
            _ctx(_response(200, json_body=_found_envelope())),
        ],
    )

    await client.async_get_parcel(CODE)
    await client.async_get_parcel(CODE)

    assert session.post.call_count == 4


# ---------------------------------------------------------------------------
# version-token staleness
# ---------------------------------------------------------------------------


async def test_stale_version_token_is_rederived_and_retried_once():
    stale_envelope = {
        "versionInfo": {"hasModuleVersionChanged": False, "hasApiVersionChanged": True},
        "data": {
            "ObjectEventsFromQuery": {"ObjectCode": "", "Found": False, "Events": {"List": []}},
            "Result": {"Success": True, "ErrorMessage": ""},
        },
    }
    client, session = _client(
        _version_derivation_gets() + _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=stale_envelope)),  # stale -> re-derive
            _ctx(_response(200, json_body=_found_envelope())),  # retried with fresh token
        ],
    )

    record = await client.async_get_parcel(CODE)

    assert record["Found"] is True
    assert session.get.call_count == 6  # derived twice: once stale, once fresh


# ---------------------------------------------------------------------------
# transport / envelope errors
# ---------------------------------------------------------------------------


async def test_get_parcel_raises_on_non_200_non_403():
    client, _ = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(500)),
        ],
    )
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_raises_on_unparseable_body():
    client, _ = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, bad_json=True)),
        ],
    )
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_raises_on_non_object_body():
    client, _ = _client(
        _version_derivation_gets(),
        [
            _ctx(_response(403, cookies=_bootstrap_cookie())),
            _ctx(_response(200, json_body=["not", "a", "dict"])),
        ],
    )
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_propagates_network_error():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    client, session = _client(_version_derivation_gets(), [])
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(CODE)


# ---------------------------------------------------------------------------
# version-derivation failures
# ---------------------------------------------------------------------------


async def test_module_version_endpoint_failure_raises():
    client, _ = _client([_ctx(_response(500))], [])
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_module_version_endpoint_missing_token_raises():
    client, _ = _client([_ctx(_response(200, json_body={}))], [])
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_manifest_missing_script_entry_raises():
    client, _ = _client(
        [
            _ctx(_response(200, json_body={"versionToken": MODULE_VERSION_TOKEN})),
            _ctx(_response(200, json_body={"manifest": {"urlVersions": {}}})),
        ],
        [],
    )
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


async def test_script_without_the_action_literal_raises():
    client, _ = _client(
        [
            _ctx(_response(200, json_body={"versionToken": MODULE_VERSION_TOKEN})),
            _ctx(
                _response(
                    200,
                    json_body={"manifest": {"urlVersions": {SCREEN_SCRIPT_PATH: SCRIPT_VERSION_TOKEN}}},
                )
            ),
            _ctx(_response(200, text_body="no callDataAction here")),
        ],
        [],
    )
    with pytest.raises(CTTApiError):
        await client.async_get_parcel(CODE)


def test_track_action_url_targets_the_right_screen():
    assert TRACK_ACTION in (
        f"{BASE_URL}/CustomerArea/screenservices/CustomerArea/CustomerArea/"
        f"PublicArea_Detail/{TRACK_ACTION}"
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


def test_ctt_timestamp_treats_1900_01_01_as_absent():
    """OutSystems' null-date sentinel must never reach a timestamp field."""
    assert ctt_timestamp("1900-01-01T00:00:00") is None
    assert ctt_timestamp("1900-01-01") is None
    assert ctt_timestamp("") is None
    assert ctt_timestamp(None) is None
    assert ctt_timestamp("2026-04-02T12:09:12Z") == "2026-04-02T12:09:12Z"



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



def test_capabilities_never_claims_a_delivery_window_or_weight():
    """CTT does not expose these to an anonymous caller — see const.py."""
    assert "delivery_window" not in CAPABILITIES_BY_VARIANT["CTT"]
    assert "weight" not in CAPABILITIES_BY_VARIANT["CTT"]
    assert "dimensions" not in CAPABILITIES_BY_VARIANT["CTT"]



def test_normalize_delivered_parcel():
    parcel = normalize_ctt_parcel(delivered_sample())
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
    parcel = normalize_ctt_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 10
    assert parcel["history"][0]["status"] == ParcelStatus.REGISTERED


def test_normalize_active_parcel_has_no_window():
    parcel = normalize_ctt_parcel(active_sample())
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
    parcel = normalize_ctt_parcel(derivation_counter_example_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "Em trânsito"


def test_normalize_in_transit_parcel():
    parcel = normalize_ctt_parcel(in_transit_sample())
    assert parcel["status"] == ParcelStatus.IN_TRANSIT


def test_normalize_on_hold_pickup_parcel_ends_up_awaiting_pickup():
    """StateId 8 ('Em espera') is a delay, not an exception — it must not
    surface as `unknown` or `problem`, and the parcel still reaches
    `AT_PICKUP_POINT` once it resumes."""
    parcel = normalize_ctt_parcel(on_hold_pickup_sample(), include_history=True)
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
    parcel = normalize_ctt_parcel(returned_sample())
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["raw_status"] == "Devolvido"
    assert parcel["delivered"] is False


def test_normalize_pickup_parcel():
    parcel = normalize_ctt_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Loja CTT Esgueira (Aveiro)"


def test_normalize_pickup_point_ignores_top_level_flags():
    """IsDeliveryPoint/IsLocker were false even on real pickup-point parcels."""
    raw = pickup_sample()
    raw["IsDeliveryPoint"] = False
    raw["IsLocker"] = False
    parcel = normalize_ctt_parcel(raw)
    assert parcel["pickup_point"] == "Loja CTT Esgueira (Aveiro)"


def test_tracking_url_none_without_a_code():
    assert tracking_url(None) is None
    assert tracking_url("") is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-fetched code still yields a full parcel dict."""
    parcel = normalize_ctt_parcel({"ObjectCode": "RR000000000PT"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_blank_person_fields_become_none():
    """Empty on all four real parcels for an anonymous caller — map, don't
    hardcode, so the first parcel that does carry a name keeps it."""
    parcel = normalize_ctt_parcel(active_sample())
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_maps_sender_and_receiver_when_present():
    raw = active_sample()
    raw["Sender"] = "Loja Exemplo"
    raw["Recipient"] = "Cliente Exemplo"
    parcel = normalize_ctt_parcel(raw)
    assert parcel["sender"] == "Loja Exemplo"
    assert parcel["receiver"] == "Cliente Exemplo"


def test_normalize_keeps_raw_payload():
    raw = active_sample()
    assert normalize_ctt_parcel(raw)["raw"] is raw


def test_normalize_raw_round_trips_fields_nothing_else_maps():
    """A key nothing else maps must survive — guards against a future
    'tidy-up' quietly trimming ``raw``."""
    parcel = normalize_ctt_parcel(active_sample())
    assert parcel["raw"]["RelabelObjectCode"] == "0010000000000000000000001"
    assert parcel["raw"]["SpecialServices"]["List"][0]["Description"] == (
        "2 tentativas e avisar CTT"
    )
