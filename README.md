# CTT Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ctt.svg)](https://github.com/ha-parcel-integrations/ha-ctt/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-ctt/total.svg)](https://github.com/ha-parcel-integrations/ha-ctt/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [CTT](https://www.ctt.pt) (Portugal's national post) parcels. No account is needed — you enter the tracking code yourself, just like on the CTT website.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of CTT parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), CTT's own status text, pickup-point detail and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- `ctt.track_parcel` / `ctt.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A CTT parcel and its tracking code (from the shipping confirmation
  email or the missed-delivery card) — no account needed

CTT does not expose an estimated delivery window, weight or dimensions to an
anonymous caller, so this integration does not claim them (see
`CAPABILITIES` in `const.py`). The **Deliveries** calendar entity still
exists for parity with the rest of the suite, but stays empty on CTT since
it has nothing to show without a window.

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ctt` as an **Integration**.
3. Install **CTT** and restart Home Assistant.

### Manual

Copy `custom_components/ctt` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → CTT**. There is nothing to fill in: the hub is created immediately (CTT tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`ctt.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule (quiet overnight window, faster when a parcel is out
for delivery, stopped entirely once nothing is left to track) with nothing to
configure. See [CLAUDE.md](CLAUDE.md) for the details.

## Removal

Standard HA removal applies: **Settings → Devices & Services → CTT → ⋮ → Delete**. Nothing is stored on CTT's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.ctt_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.ctt_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ctt_next_delivery` | Earliest expected delivery moment across all active parcels (always empty — CTT exposes no delivery window) |
| `sensor.ctt_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.ctt_last_successful_update` | Diagnostic: when CTT was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family. CTT reports eight distinct `StateId`s, mapped as follows:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by CTT (`Aguarda entrada nos CTT`) |
| `in_transit` | In the sorting network, including import/customs (`Aceite`, `Em importação`, `Em trânsito`) |
| `out_for_delivery` | With the courier today (`Em entrega`) |
| `at_pickup_point` | Waiting for you at a CTT shop (`No ponto de entrega`) |
| `delivered` | Delivered (`Entregue`) |
| `problem` | A failed delivery attempt (`Não entregue`) |
| `unknown` | Not yet scanned, or a status CTT reports that this integration does not map yet |

`returning` is part of the shared enum but CTT has no confirmed `StateId` for
a return, a cancellation or a locker delivery yet (nine further status
literals are known from CTT's own website code but have never been observed
on the wire) — they arrive as `unknown` with a one-shot log line asking you
to report it, rather than being guessed at.

CTT's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the CTT device):

| Event | When |
|---|---|
| `ctt_parcel_registered` | A new parcel appears in the active list |
| `ctt_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `ctt_parcel_delivered` | A parcel is delivered |
| `ctt_parcel_delivery_time_changed` | The expected delivery window changes (never fires on CTT — see [Requirements](#requirements)) |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `ctt.track_parcel` | `tracking_code` | Start tracking a parcel |
| `ctt.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ctt: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — CTT has not scanned it yet (their API answers `not_found` until the first scan), or the code is wrong. It will pick up automatically once scanned.
- **A status logs "Unrecognised CTT status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-ctt/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same public tracking endpoint as the CTT consumer website. It is not affiliated with, endorsed by, or supported by CTT.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
