"""Tests for the CTT API client.

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

from custom_components.ctt.api import (
    BASE_URL,
    MAINTENANCE_ACTION,
    SCREEN_SCRIPT_PATH,
    TRACK_ACTION,
    CTTApiClient,
    CTTApiError,
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


def _client(get_responses: list, post_responses: list) -> tuple[CTTApiClient, MagicMock]:
    session = MagicMock()
    session.get = MagicMock(side_effect=get_responses)
    session.post = MagicMock(side_effect=post_responses)
    return CTTApiClient(session), session


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
