"""Diagnostics support for the CTT parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CTTConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# Both trackers' own payload names, on top of the canonical fields we publish
# ourselves. The object code is included: it dereferences everything else on
# this record, even for an anonymous caller.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    # CTT payload fields
    "ObjectCode",
    "ObjectName",
    "RelabelObjectCode",
    "RecipientPostalCodeAndTown",
    "SenderMobile",
    "ContractNumber",
    "ClientNumber",
    "ClientContractCompanyName",
    "ReceptorName",
    "PointCode",
    "Recipient",
    "RecipientEmail",
    "RecipientAddress",
    "RecipientTimeSlot",
    "Sender",
    "SenderEmail",
    "ClientReference",
    # CTT Express payload fields — free event text and pickup addresses can
    # carry names, so every ``detail`` value goes too.
    "item_code",
    "description",
    "source",
    "event_date",
    "detail",
    "event_courier_code",
    "item_event_datetime",
    "item_event_text",
    "External_event_text",
    "incident_type_name",
    "delivery_location",
    "delivery_date",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CTTConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the CTT config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
