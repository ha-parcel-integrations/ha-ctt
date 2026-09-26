# Working in this repository

Home Assistant custom integration for **CTT** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/ctt/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
This repo follows it exactly.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **If this carrier can reach `ParcelStatus.AT_PICKUP_POINT` from a real raw
  status/code**, it needs an `awaiting_pickup` sensor — see *Parcel contract*
  in `CONVENTIONS.md`. Say "pickup point", not "ServicePoint"/"parcel
  shop"/"locker", for the generic concept. `ha-dhl-nl`, `ha-dpd`, `ha-gls`,
  `ha-inpost` are reference implementations; CTT's own `StateId 14` ("No
  ponto de entrega") reaches `AT_PICKUP_POINT`, so `sensor.py` ships both
  `awaiting_pickup` and `en_route_to_pickup_point` (added 2026-09-06, gap
  from the original build — see `tests/payloads.py::on_hold_pickup_sample`).

## Carrier-specific notes

**Two backends, routed per code.** CTT Portugal lives in `ctt.py`, CTT Express
(Spain) in `express.py`; `api.py` is only the dispatcher, and
`is_express_code` sends every all-digit code to CTT Express and everything else
to CTT. There is no country or brand setting — one entry holds both kinds of
code, and each parcel's `carrier` says which backend it came from. The
coordinator keeps each raw next to its code so it can pick the matching
normaliser, and never adds CTT's `ObjectCode` to a CTT Express record.

**CTT Express specifics.** Not-found is an empty `200` body, so the client
reads text and parses it itself. `events` is a list; the state is the newest
`STATUS` event by `event_date` (sorted, never trusted in server order) and
`MANAGEMENTS` events never set it — they only feed `planned_from`, from the
newest revised delivery date, while the parcel is not delivered or returning.
`barcode` is the configured code, never the tracker's own `item_code`.

The rest of this section is about the CTT Portugal backend.

CTT is keyless but not stateless: `ctt.py` maintains an anonymous OutSystems
session (cookie + CSRF token, bootstrapped off an expected `403`) and two
derived version tokens (`moduleVersion`, per-action `apiVersion`) on the
client instance. None of the three is ever hardcoded — a stale version token
is a "re-derive and retry once" signal from the server
(`versionInfo.hasModuleVersionChanged`/`hasApiVersionChanged`), and a later
`403` just means the session expired and needs a fresh bootstrap. See the
module docstring in `ctt.py` for the full mechanics; the wire-level
reference (why each step exists, what was actually observed) lives in the
private `carrier-research/ctt/api/` and is not duplicated here.

**The outage trap.** CTT's `Found: false` is CTT's semantic-404, but the
exact same shape is also what the backend returns during a genuine outage —
confirmed live during research on 2026-09-05. `ctt.py` never reports a
not-found without first calling the sibling `DataActionCheckIPLocked`
action and checking `IsMaintenance`; only when that is also false does
`async_get_parcel` return `None`. A found parcel skips the check entirely —
it already proves the backend is up. Do not "simplify" this by trusting
`Found` alone.

**Status is derived from the newest event, never the highest rank.**
`Events.List[0]` (newest-first) is the status; `Progress` is not monotonic
(`Não entregue` is 90, `Em entrega` is 80) and must never be used to rank.
`tests/payloads.py::derivation_counter_example_sample` is the regression test
for this — it went out for delivery, failed once, and returned to
`in_transit`; a highest-rank rule would report `problem`.

**Fields that are `None` on purpose.** `planned_from`/`planned_to`,
`weight` and `dimensions` are always `None` — no observed parcel (four real
ones, 2026-09-06) populates `DeliveryDeadlineEnd`, `DeliveryTimeSlot` or any
weight/dimension field for an anonymous caller. `CAPABILITIES_BY_VARIANT["CTT"]`
in `const.py` reflects this (`{"pickup_point", "url", "history"}`) and must
stay in agreement if a future parcel ever proves otherwise. `pickup_point`
comes from the *event* (`StateId` 14's `Local`), never from the top-level
`IsDeliveryPoint`/`IsLocker` flags — both were `false` on real parcels that
genuinely sat at a CTT shop.

**Tracking-code format is not validated client-side.** All four real codes
seen so far are UPU S10 (`^[A-Z]{2}\d{9}[A-Z]{2}$`), one of them a
German-issued code CTT still tracks — but four samples were never enough to
defend a regex, and CTT's own backend clearly handles other shapes
internally (one real parcel carried a 25-digit RelabelObjectCode).
`config_flow.valid_tracking_code` now accepts any non-empty code; an
unrecognized one simply comes back "not found" on the next poll. The code's
shape is used only to route between the two backends, never to reject.

**API mechanics go in the private research notes, NOT here** — the endpoint,
the two-step session bootstrap, the version-token derivation, the status
vocabulary and the annotated real payloads live in
`carrier-research/ctt/api/` (`tracking.md`, `express.md`), never duplicated
into this repo.

## Running tests

```
python -m pytest tests/ --cov=custom_components.ctt
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
