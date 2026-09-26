"""Builders for invented CTT Express ``shipping_history`` records.

Codes, texts and locations are made up; only the envelope shape is real.
``events`` is a list, oldest first, with ``STATUS`` events carrying the parcel
state and ``MANAGEMENTS`` events recording delivery-management actions.
"""
from __future__ import annotations

from typing import Any

EXPRESS_CODE = "0000000000000000000001"
EXPRESS_CODE_25 = "0000000000000000000000001"


def status_event(
    event_date: str, code: str, description: str, **detail: Any
) -> dict[str, Any]:
    return {
        "code": code,
        "description": description,
        "type": "STATUS",
        "source": "ITEM_STATUS_V2",
        "event_date": event_date,
        "detail": {"event_courier_code": "X0", **detail},
    }


def management_event(
    event_date: str, code: str, description: str, **detail: Any
) -> dict[str, Any]:
    return {
        "code": code,
        "description": description,
        "type": "MANAGEMENTS",
        "source": "CASE_V1",
        "event_date": event_date,
        "detail": dict(detail),
    }


def shipping_history(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_code": EXPRESS_CODE + "001",
        "item_length_declared": "10",
        "item_width_declared": "10",
        "item_height_declared": "10",
        "declared_weight": "1",
        "events": list(events),
    }


def envelope(history: dict[str, Any] | None) -> dict[str, Any]:
    return {"error": None, "data": {"shipping_history": history}}


def delivered_history() -> dict[str, Any]:
    return shipping_history(
        status_event("2026-09-01T09:00:00+02:00", "0000", "Pendiente de recepción"),
        status_event("2026-09-01T18:00:00+02:00", "0500", "Recogido"),
        status_event("2026-09-02T08:00:00+02:00", "1500", "En reparto"),
        status_event("2026-09-02T14:00:00+02:00", "1600", "Incidencia en el reparto"),
        management_event(
            "2026-09-02T15:00:00+02:00",
            "71_INAT",
            "Nueva fecha de entrega",
            delivery_date="2026-09-04",
        ),
        status_event("2026-09-04T08:30:00+02:00", "2400", "Nuevo reparto"),
        status_event("2026-09-04T12:15:00+02:00", "2100", "Entregado"),
        management_event("2026-09-04T13:00:00+02:00", "75_INAT", "Gestión cerrada"),
    )


def pickup_history(location: str | None = "Punto Collectt de prueba") -> dict[str, Any]:
    detail = {} if location is None else {"delivery_location": location}
    return shipping_history(
        status_event("2026-09-01T09:00:00+02:00", "1000", "Enviado"),
        status_event("2026-09-02T10:00:00+02:00", "2310", "Disponible", **detail),
    )


def rescheduled_history() -> dict[str, Any]:
    return shipping_history(
        status_event("2026-11-01T09:00:00+01:00", "1500", "En reparto"),
        status_event("2026-11-01T14:00:00+01:00", "1600", "Incidencia en el reparto"),
        management_event(
            "2026-11-01T15:00:00+01:00",
            "71_INAT",
            "Nueva fecha de entrega",
            delivery_date="2026-11-03",
        ),
    )
