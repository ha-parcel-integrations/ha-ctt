"""Tests for CTT diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ctt.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "RR999999999PT"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "RR999999999PT",
            "sender": "Loja Exemplo",
            "receiver": "Cliente Exemplo",
            "status": "out_for_delivery",
            "raw": {
                "ObjectCode": "RR999999999PT",
                "Recipient": "Cliente Exemplo",
                "RecipientAddress": "Rua Exemplo 1",
                "Events": {"List": [{"ReceptorName": "Cliente Exemplo"}]},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {"incoming_active": 1, "delivered": 0}
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["receiver"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["Recipient"] == "**REDACTED**"
    assert result["incoming"][0]["raw"]["RecipientAddress"] == "**REDACTED**"
    assert (
        result["incoming"][0]["raw"]["Events"]["List"][0]["ReceptorName"]
        == "**REDACTED**"
    )
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "out_for_delivery"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }


async def test_diagnostics_redacts_a_ctt_express_record(hass):
    """Free event text and pickup addresses can carry names."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "0000000000000000000001"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "0000000000000000000001",
            "url": "https://example.invalid/?sc=0000000000000000000001",
            "status": "at_pickup_point",
            "raw": {
                "item_code": "0000000000000000000001001",
                "events": [
                    {
                        "code": "2310",
                        "type": "STATUS",
                        "description": "Disponible",
                        "event_date": "2026-09-02T10:00:00+02:00",
                        "detail": {"delivery_location": "Calle Ejemplo 1"},
                    }
                ],
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    parcel = result["incoming"][0]
    assert parcel["barcode"] == "**REDACTED**"
    assert parcel["url"] == "**REDACTED**"
    assert parcel["raw"]["item_code"] == "**REDACTED**"
    event = parcel["raw"]["events"][0]
    assert event["detail"] == "**REDACTED**"
    assert event["description"] == "**REDACTED**"
    assert event["event_date"] == "**REDACTED**"
    # the code and type are what a bug report needs to map a status
    assert event["code"] == "2310"
    assert event["type"] == "STATUS"
