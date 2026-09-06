"""Sample CTT tracking records shared by the test modules.

Shapes are taken from real parcels (see ``carrier-research/ctt/api/tracking.md``,
the private research repo, for the annotated captures) but every tracking
code, relabel code and person field below is a fabricated stand-in, not a
real one — nothing in this public repo should let a payload be traced back
to an actual shipment.

Kept in one module rather than inline in each test — when the payload shape
turns out to be different from what was assumed, there is exactly one place
to fix it.
"""
from __future__ import annotations

DELIVERED_CODE = "DW100000001PT"
ACTIVE_CODE = "DS100000002PT"
PICKUP_CODE = "LA100000003PT"
# The derivation counter-example (plan section 6 / 11): went out for
# delivery, failed once, and returned to the network. Its current status is
# `in_transit`, never `problem` — a highest-`Progress`/highest-rank rule
# would get this one wrong.
DERIVATION_CODE = "LW100000004DE"
# StateId 5 ("Devolvido") and StateId 8 ("Em espera") were unmapped at 1.0.0
# and surfaced as `unknown` on real installs on 2026-09-06 — these two
# fixtures reproduce the event sequences that confirmed them.
ON_HOLD_CODE = "DD100000005PT"
RETURNED_CODE = "DD100000006PT"


def event(
    date_time: str,
    state_id: int,
    state: str,
    *,
    event_text: str = "",
    local: str = "",
    progress: int = 0,
    reason: str = "",
    point_code: str = "",
    withdrawal_date: str = "1900-01-01",
) -> dict:
    """One entry of CTT's own ``Events.List`` — see tracking.md's shape."""
    return {
        "DateTime": date_time,
        "DateMonthText": "",
        "State": state,
        "StateId": state_id,
        "Event": event_text,
        "EventCode": "",
        "EventColorType": 0,
        "Local": local,
        "Situation": "",
        "Reason": reason,
        "Progress": progress,
        "PointCode": point_code,
        "ReceptorName": "",
        "LockerName": "",
        "WithdrawalDate": withdrawal_date,
        "WithdrawalDateMonthText": "",
        "TypeOfTimelineEvent": "0",
        "Nexus": "",
        "DestinationNodeName": "",
    }


def _object(
    code: str,
    events: list[dict],
    *,
    relabel_object_code: str = "",
    sender: str = "",
    recipient: str = "",
) -> dict:
    """The ``ObjectEventsFromQuery`` record api.py hands to ``normalize_parcel``."""
    return {
        "ObjectCode": code,
        "ObjectName": code,
        "RelabelObjectCode": relabel_object_code,
        "Found": True,
        "IsTracked": True,
        "CustomsPurposes": False,
        "CTTObject": True,
        "Carrier": "0",
        "CarrierID": 0,
        "CarrierLogoURL": "",
        "Sender": sender,
        "SenderEmail": "",
        "SenderCountryCode": "PT",
        "SenderMobile": "",
        "Recipient": recipient,
        "RecipientEmail": "",
        "RecipientCountryCode": "PT",
        "RecipientAddress": "",
        "RecipientPostalCodeAndTown": "",
        "RecipientTimeSlot": "",
        "IsLocker": False,
        "LockerName": "",
        "IsDeliveryPoint": False,
        "CreationDate": "1900-01-01T00:00:00",
        "ClearanceDate": "1900-01-01T00:00:00",
        "DeliveryTimeSlot": "",
        "DeliveryDeadlineEnd": "1900-01-01T00:00:00",
        "ShipmentProduct": "",
        "ClientReference": "",
        "IsB2C": False,
        "Events": {"List": events},
        "SpecialServices": {
            "List": [{"Description": "2 tentativas e avisar CTT", "Value": ""}]
        },
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A full, real event history — delivered after one failed attempt.

    Real shape (fabricated codes): ten events, newest-first, including the two
    ``Não entregue`` events 41 seconds apart that ``build_history`` must not
    deduplicate.
    """
    events = [
        event(
            "2026-04-02T12:09:12Z", 12, "Entregue",
            event_text="O envio foi entregue. O processo de envio terminou.",
            local="8819009 - (CO) C.O. PALMELA", progress=100,
        ),
        event(
            "2026-04-02T09:31:09Z", 7, "Em entrega",
            local="8819009 - (CO) C.O. PALMELA", progress=80,
        ),
        event(
            "2026-04-01T12:22:30Z", 13, "Não entregue",
            local="8819009 - (CO) C.O. PALMELA", progress=90,
            reason="O destinatário não atendeu.",
        ),
        event(
            "2026-04-01T12:21:49Z", 13, "Não entregue",
            local="8819009 - (CO) C.O. PALMELA", progress=90,
            reason="O destinatário não atendeu.",
        ),
        event(
            "2026-04-01T09:45:43Z", 7, "Em entrega",
            local="8819009 - (CO) C.O. PALMELA", progress=80,
        ),
        event(
            "2026-04-01T04:13:56Z", 11, "Em trânsito",
            local="8819009 - (CO) C.O. PALMELA", progress=50,
        ),
        event(
            "2026-03-31T19:16:48Z", 11, "Em trânsito",
            local="PTMRLP - (CO) C.O. LOURES-MARL (OLX)", progress=50,
        ),
        event(
            "2026-03-31T18:50:13Z", 11, "Em trânsito",
            local="Centro operacional Lisboa", progress=50,
        ),
        event("2026-03-31T18:50:12Z", 2, "Aceite", progress=3),
        event("2026-03-31T16:42:00Z", 1, "Aguarda entrada nos CTT", progress=0),
    ]
    return _object(code, events)


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel, still relabelled (a real relabel case).

    Carries a populated ``RelabelObjectCode`` (a fabricated stand-in) and
    a ``SpecialServices`` entry, so it also does double duty for the
    raw-round-trip test — a key nothing else maps must survive
    ``normalize_parcel`` untouched.
    """
    events = [
        event("2026-05-02T09:31:09Z", 7, "Em entrega", local="Centro operacional Lisboa", progress=80),
        event("2026-05-01T18:00:00Z", 11, "Em trânsito", local="Centro operacional Lisboa", progress=50),
        event("2026-05-01T12:00:00Z", 2, "Aceite", progress=3),
        event("2026-05-01T08:00:00Z", 1, "Aguarda entrada nos CTT", progress=0),
    ]
    return _object(
        code, events, relabel_object_code="0010000000000000000000001"
    )


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """The same parcel as :func:`active_sample`, one step earlier (in transit)."""
    sample = active_sample(code)
    sample["Events"]["List"] = sample["Events"]["List"][1:]
    return sample


def pickup_sample(code: str = PICKUP_CODE) -> dict:
    """A parcel waiting at a CTT shop — pickup detail lives on the event.

    ``IsDeliveryPoint``/``IsLocker`` stay false at the top level on purpose:
    both were observed false on real parcels that genuinely sat at a pickup
    point, so ``normalize_parcel`` must never read them.
    """
    events = [
        event(
            "2026-06-11T10:00:00Z", 14, "No ponto de entrega",
            local="Loja CTT Esgueira (Aveiro)", progress=90,
            point_code="8810266", withdrawal_date="2026-06-12",
        ),
        event("2026-06-11T08:00:00Z", 7, "Em entrega", local="8819009 - (CO) C.O. PALMELA", progress=80),
        event("2026-06-10T12:00:00Z", 11, "Em trânsito", local="Centro operacional Lisboa", progress=50),
        event("2026-06-09T09:00:00Z", 1, "Aguarda entrada nos CTT", progress=0),
    ]
    return _object(code, events)


def derivation_counter_example_sample(code: str = DERIVATION_CODE) -> dict:
    """The newest-event-not-highest-rank counter-example.

    Real event sequence, oldest → newest, as ``(StateId, Progress)``::

        (2,3) (11,50) (11,50) (11,50) (11,50) (10,50) (10,50)
        (11,50) (11,50) (11,50) (7,80) (13,90) (11,50)

    The parcel went out for delivery (StateId 7), failed once (StateId 13,
    ``Progress`` 90 — higher than the 80 of ``Em entrega``), and returned to
    the network (StateId 11). The newest event is the last one: `Em trânsito`
    / `in_transit`. A rule that ranks by ``Progress`` or "highest status
    reached" would wrongly report `problem`.
    """
    oldest_to_newest = [
        event("2026-03-20T08:00:00Z", 2, "Aceite", progress=3),
        event("2026-03-20T14:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-21T08:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-21T20:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-22T08:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-22T20:00:00Z", 10, "Em importação", progress=50),
        event("2026-03-23T08:00:00Z", 10, "Em importação", progress=50),
        event("2026-03-23T20:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-24T08:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-24T20:00:00Z", 11, "Em trânsito", progress=50),
        event("2026-03-25T09:00:00Z", 7, "Em entrega", progress=80),
        event(
            "2026-03-25T12:00:00Z", 13, "Não entregue", progress=90,
            reason="O destinatário não atendeu.",
        ),
        event("2026-03-25T18:00:00Z", 11, "Em trânsito", progress=50),
    ]
    return _object(code, list(reversed(oldest_to_newest)))


def on_hold_pickup_sample(code: str = ON_HOLD_CODE) -> dict:
    """A parcel put ``Em espera`` (StateId 8) mid-transit.

    Went on hold while CTT queried the address, then resumed on its own to
    out-for-delivery and finally a pickup point. ``Em espera`` normalises to
    `in_transit` — a delay, not an exception — and this fixture also
    exercises the `awaiting_pickup`/`en_route_to_pickup_point` split, since
    its newest event is StateId 14.
    """
    events = [
        event(
            "2026-09-02T09:50:58Z", 14, "No ponto de entrega",
            event_text="Chegou ao ponto de entrega.",
            local="Centro operacional Coimbra", progress=90,
        ),
        event(
            "2026-09-02T08:47:58Z", 7, "Em entrega",
            event_text="O envio saiu para entrega. Será entregue durante o dia.",
            local="Centro operacional Coimbra", progress=80,
        ),
        event(
            "2026-09-01T08:22:16Z", 8, "Em espera",
            event_text="O envio encontra-se em espera.",
            local="Centro operacional Coimbra", progress=50,
            reason="Estamos a averiguar a morada junto do cliente.",
        ),
        event(
            "2026-09-01T08:22:15Z", 11, "Em trânsito",
            event_text="Chegou ao centro operacional",
            local="Centro operacional Coimbra", progress=50,
        ),
        event(
            "2026-08-31T16:30:53Z", 11, "Em trânsito",
            event_text="Chegou ao centro operacional",
            local="8811582 - (CDP) 2350 - Cld 2350 Torres Novas 2350", progress=50,
        ),
        event(
            "2026-08-31T16:30:49Z", 2, "Aceite",
            event_text="O envio foi aceite. O processo de envio foi iniciado.",
            progress=3,
        ),
        event(
            "2026-08-31T14:22:33Z", 1, "Aguarda entrada nos CTT",
            event_text="A informação sobre o envio foi recebida.",
            progress=0,
        ),
    ]
    return _object(code, events)


def returned_sample(code: str = RETURNED_CODE) -> dict:
    """A parcel returned to the sender (StateId 5).

    Six failed delivery attempts (``Não entregue``, StateId 13) before CTT
    handed it back. ``Devolvido`` normalises to `returning`, the suite
    convention for both an in-progress and a completed return.
    """
    events = [
        event(
            "2026-08-31T10:47:21Z", 5, "Devolvido",
            event_text="O envio foi entregue ao remetente. Processo de devolução terminado.",
            local="Centro operacional Lisboa", progress=100,
        ),
        event(
            "2026-08-31T09:37:28Z", 7, "Em entrega",
            event_text="O envio saiu para entrega. Será entregue durante o dia.",
            local="Centro operacional Lisboa", progress=80,
        ),
        event(
            "2026-08-27T15:06:32Z", 13, "Não entregue",
            event_text="A entrega do envio não foi conseguida.",
            local="006027 - (PL) CO MAIA", progress=90,
            reason="O envio não foi levantado.",
        ),
        event(
            "2026-08-26T21:08:53Z", 13, "Não entregue",
            event_text="A entrega do envio não foi conseguida.",
            local="006027 - (PL) CO MAIA", progress=90,
            reason="O destinatário não atendeu.",
        ),
        event(
            "2026-08-20T08:32:28Z", 7, "Em entrega",
            event_text="O envio saiu para entrega. Será entregue durante o dia.",
            local="006027 - (PL) CO MAIA", progress=80,
        ),
        event(
            "2026-08-16T16:41:00Z", 1, "Aguarda entrada nos CTT",
            event_text="A informação sobre o envio foi recebida.",
        ),
        event(
            "2026-08-14T19:55:12Z", 2, "Aceite",
            event_text="O envio foi aceite. O processo de envio foi iniciado.",
            progress=3,
        ),
    ]
    return _object(
        code, events, relabel_object_code="0010000000000000000000002"
    )
