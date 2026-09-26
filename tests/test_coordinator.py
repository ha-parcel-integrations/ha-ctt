"""Tests for the CTT coordinator: fetching, caching and events.

The parcel mapping itself is covered by ``test_ctt.py`` and ``test_express.py``.
"""
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ctt.api import CTTApiError
from custom_components.ctt.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
    ParcelStatus,
)
from custom_components.ctt.coordinator import CTTCoordinator

from .express_payloads import EXPRESS_CODE, delivered_history, pickup_history
from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    delivered_sample,
    in_transit_sample,
)

OTHER_CODE = "RR999999999PT"


def _entry_with(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        # Keep-most-recent-100 so the delivered-retention filter never trims
        # the (old, fixed-date) sample parcels these tests assert on.
        options={
            CONF_PARCELS: parcels,
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
        unique_id=DOMAIN,
    )


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_update_merges_multiple_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample() if code == ACTIVE_CODE else delivered_sample()
    )
    coordinator = CTTCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1  # one active
    assert data[0]["barcode"] == ACTIVE_CODE
    assert len(coordinator.delivered) == 1
    assert coordinator.last_success_time is not None


async def test_update_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None  # not found
    coordinator = CTTCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert data[0]["barcode"] == OTHER_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN


async def test_update_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = CTTCoordinator(hass, client, entry)
    await coordinator._async_update_data()  # populates the cache

    client.async_get_parcel.side_effect = CTTApiError("HTTP 500")
    await coordinator._async_update_data()  # error -> cached raw reused
    assert len(coordinator.delivered) == 1


async def test_update_raises_when_every_parcel_fails(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = CTTApiError("HTTP 500")
    coordinator = CTTCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_update_reraises_unexpected_exceptions(hass):
    """Only API and network errors are tolerated; a bug must not be swallowed."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = ValueError("boom")
    coordinator = CTTCoordinator(hass, client, entry)

    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_update_skips_items_missing_a_tracking_code(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ""}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = CTTCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert client.async_get_parcel.await_count == 1  # empty item never fetched


async def test_update_backfills_missing_tracking_number(hass):
    """An edge payload without ``ObjectCode`` keeps the requested code."""
    entry = _entry_with([{CONF_TRACKING_CODE: OTHER_CODE}])
    entry.add_to_hass(hass)
    sample = active_sample()
    del sample["ObjectCode"]
    client = AsyncMock()
    client.async_get_parcel.return_value = sample
    coordinator = CTTCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == OTHER_CODE


async def test_update_prunes_cache_for_untracked_parcels(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = CTTCoordinator(hass, client, entry)
    coordinator._raw_cache["GONE"] = {"ObjectCode": "GONE"}

    await coordinator._async_update_data()

    assert "GONE" not in coordinator._raw_cache
    assert DELIVERED_CODE in coordinator._raw_cache


async def test_update_fetches_parcels_concurrently(hass):
    """All tracked parcels go out in one gather, not one-by-one."""
    import asyncio

    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: DELIVERED_CODE}]
    )
    entry.add_to_hass(hass)
    in_flight = 0
    peak = 0

    async def _slow_fetch(code):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return active_sample(code)

    client = AsyncMock()
    client.async_get_parcel.side_effect = _slow_fetch
    coordinator = CTTCoordinator(hass, client, entry)

    await coordinator._async_update_data()
    assert peak == 2


async def test_cache_only_poll_does_not_stamp_last_success(hass):
    """A poll served entirely from cache must not look like a success."""
    entry = _entry_with([{CONF_TRACKING_CODE: DELIVERED_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_sample()
    coordinator = CTTCoordinator(hass, client, entry)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    assert stamp is not None

    client.async_get_parcel.side_effect = CTTApiError("HTTP 500")
    await coordinator._async_update_data()  # served from cache
    assert coordinator.last_success_time == stamp



# ---------------------------------------------------------------------------
# two backends in one entry — the code's shape picks the normaliser
# ---------------------------------------------------------------------------


async def test_update_merges_ctt_and_express_parcels(hass):
    entry = _entry_with(
        [{CONF_TRACKING_CODE: ACTIVE_CODE}, {CONF_TRACKING_CODE: EXPRESS_CODE}]
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample() if code == ACTIVE_CODE else pickup_history()
    )
    coordinator = CTTCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    carriers = {parcel["barcode"]: parcel["carrier"] for parcel in data}
    assert carriers == {ACTIVE_CODE: "CTT", EXPRESS_CODE: "CTT Express"}


async def test_update_express_result_never_gets_a_ctt_key(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: EXPRESS_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_history()
    coordinator = CTTCoordinator(hass, client, entry)

    await coordinator._async_update_data()

    assert "ObjectCode" not in coordinator._raw_cache[EXPRESS_CODE]
    assert coordinator.delivered[0]["barcode"] == EXPRESS_CODE


async def test_update_express_not_found_shows_pending_placeholder(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: EXPRESS_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = None
    coordinator = CTTCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert data[0]["carrier"] == "CTT Express"
    assert data[0]["barcode"] == EXPRESS_CODE
    assert data[0]["status"] == ParcelStatus.UNKNOWN
    assert data[0]["raw"] == {}


async def test_update_express_keeps_cached_payload_on_error(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: EXPRESS_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = delivered_history()
    coordinator = CTTCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_parcel.side_effect = CTTApiError("HTTP 500")
    await coordinator._async_update_data()
    assert coordinator.delivered[0]["carrier"] == "CTT Express"


async def test_update_express_rate_limit_backs_off(hass):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry_with([{CONF_TRACKING_CODE: EXPRESS_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = CTTApiError(
        "HTTP 429", status_code=429, retry_after=90
    )
    coordinator = CTTCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed) as excinfo:
        await coordinator._async_update_data()
    assert excinfo.value.retry_after == 90

# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    """Otherwise every restart floods the user with "registered" events."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample()
    coordinator = CTTCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = AsyncMock()
    coordinator = CTTCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = in_transit_sample()
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = active_sample()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_fires_status_changed_event(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = CTTCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    client.async_get_parcel.return_value = in_transit_sample()
    await coordinator._async_update_data()  # first refresh: suppressed

    client.async_get_parcel.return_value = active_sample()  # out for delivery
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_and_not_status_changed(hass):
    """The hop to delivered fires exactly one, dedicated event."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = CTTCoordinator(hass, client, entry)

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    client.async_get_parcel.return_value = delivered_sample(ACTIVE_CODE)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1
    assert delivered[0].data["barcode"] == ACTIVE_CODE
    assert delivered[0].data["status"] == ParcelStatus.DELIVERED


async def test_no_events_for_parcel_first_seen_delivered(hass):
    """A parcel already delivered when first tracked fires nothing at all."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.side_effect = lambda code: (
        active_sample(code) if code == ACTIVE_CODE else delivered_sample(code)
    )
    coordinator = CTTCoordinator(hass, client, entry)

    fired = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: fired.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: fired.append(e))

    await coordinator._async_update_data()  # first refresh seeds the state

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: DELIVERED_CODE},
            ],
        },
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_get_parcel.return_value = active_sample(ACTIVE_CODE)
    coordinator = CTTCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [
                {CONF_TRACKING_CODE: ACTIVE_CODE},
                {CONF_TRACKING_CODE: OTHER_CODE},
            ],
        },
    )
    client.async_get_parcel.side_effect = lambda code: active_sample(code)
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == OTHER_CODE


async def test_fires_delivery_time_changed_event(hass):
    """CTT itself never populates a window — this exercises the shared
    coordinator machinery directly (as every carrier's tests do), not a
    CTT-specific capability. See ``normalize_parcel``'s docstring: CTT's
    ``planned_from``/``planned_to`` are always ``None``."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = CTTCoordinator(hass, client, entry)
    coordinator._known_state = {ACTIVE_CODE: ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        ACTIVE_CODE: ("2026-04-29T13:00:00Z", "2026-04-29T15:00:00Z")
    }

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    parcel = {
        "barcode": ACTIVE_CODE,
        "status": ParcelStatus.IN_TRANSIT,
        "planned_from": "2026-04-29T16:00:00Z",
        "planned_to": "2026-04-29T18:00:00Z",
    }
    coordinator._fire_change_events([parcel])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_planned_from"] == "2026-04-29T13:00:00Z"
    assert events[0].data["new_planned_from"] == "2026-04-29T16:00:00Z"


async def test_losing_the_eta_is_silent(hass):
    """value -> null just means the carrier lost the window; not worth an alert."""
    entry = _entry_with([{CONF_TRACKING_CODE: ACTIVE_CODE}])
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = CTTCoordinator(hass, client, entry)
    coordinator._known_state = {ACTIVE_CODE: ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {
        ACTIVE_CODE: ("2026-04-29T13:00:00Z", "2026-04-29T15:00:00Z")
    }

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    parcel = {
        "barcode": ACTIVE_CODE,
        "status": ParcelStatus.IN_TRANSIT,
        "planned_from": None,
        "planned_to": None,
    }
    coordinator._fire_change_events([parcel])
    await hass.async_block_till_done()

    assert events == []
