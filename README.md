# CarConnectivity Connector for the Volkswagen EU Data Act Portal

[![GitHub sourcecode](https://img.shields.io/badge/Source-GitHub-green)](https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act/)
[![GitHub release (latest by date)](https://img.shields.io/github/v/release/mikrohard/CarConnectivity-connector-vw-eu-data-act)](https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act/releases/latest)
[![GitHub](https://img.shields.io/github/license/mikrohard/CarConnectivity-connector-vw-eu-data-act)](https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act/blob/main/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/mikrohard/CarConnectivity-connector-vw-eu-data-act)](https://github.com/mikrohard/CarConnectivity-connector-vw-eu-data-act/issues)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/carconnectivity-connector-vw-eu-data-act?label=PyPI%20Downloads)](https://pypi.org/project/carconnectivity-connector-vw-eu-data-act/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/carconnectivity-connector-vw-eu-data-act)](https://pypi.org/project/carconnectivity-connector-vw-eu-data-act/)

A [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) connector that reads vehicle
data from the **VW Group EU Data Act portal** (`eu-data-act.drivesomethinggreater.com`). It supports
every VW Group brand on the portal (Volkswagen, Audi, Škoda, SEAT, Cupra, Bentley) and models pure
EVs, combustion and **PHEV** drivetrains.

## Why this connector exists

Volkswagen has blocked WeConnect API access for all 3rd-party integrations, which breaks the
`carconnectivity-connector-volkswagen` connector. For personal use, the EU Data Act portal is the
remaining access path. Its data has important limitations:

- **Read-only.** No commands are possible (lock/unlock, climatization, charge start/stop, etc.).
- **~15-minute cadence.** Data is delivered as batch datasets roughly every 15 minutes, not live.
- **Partial data.** Some WeConnect data points are absent (e.g. GPS location; estimated range is
  often missing). Only the data points present in a dataset are exposed.

This connector authenticates against the portal (OIDC login, reusing the proven flow from the
[homeassistant-vw-eu-data-act](https://github.com/) integration), downloads the newest dataset per
vehicle, and maps the available data points onto native CarConnectivity attributes as **read-only**
values.

## Prerequisites — enable continuous data on the portal first

Before adding the connector, you must enable a **continuous 15-minute data request** for your
vehicle on the EU Data Act portal. The connector only *downloads* the datasets the portal
generates — it cannot create the data request for you, and without an active request there will be
nothing to fetch.

1. Open <https://eu-data-act.drivesomethinggreater.com/> and **log in** with your Volkswagen ID
   (the same email/password you'll use in the connector config).
2. Go to **Data clusters → Vehicle overview**.
3. **Connect your car** to the site if it isn't already listed (follow the on-screen
   pairing/consent steps for your VIN).
4. Click **Get customised data** for the vehicle and follow the instructions to configure a
   **continuous** data request with a **15-minute** frequency.
5. Wait until the portal starts producing datasets (you'll see ZIP files appear in the vehicle's
   data delivery list, roughly every 15 minutes). The first file can take a little while to show up.

Once datasets are being generated, continue with the installation below.

> The connector polls at most every 15 minutes because that is how often the portal publishes new
> data — a shorter interval cannot produce fresher values.

## Installation

```bash
pip install -e .
```

Requires `carconnectivity>=0.11.9`.

## Configuration

Add a connector of type `vw_eu_data_act` to your `carconnectivity.json`:

```json
{
  "carConnectivity": {
    "connectors": [
      {
        "type": "vw_eu_data_act",
        "config": {
          "username": "you@example.com",
          "password": "your-portal-password",
          "interval": 900,
          "country": "si",
          "language": "sl",
          "brand": "VOLKSWAGEN_PASSENGER_CARS"
        }
      }
    ]
  }
}
```

| Key | Default | Description |
|---|---|---|
| `username` / `password` | — | VW ID credentials. May instead be provided in `.netrc` under machine `vw_eu_data_act`. |
| `netrc` | `~/.netrc` | Path to a netrc file (used when `username`/`password` are omitted). |
| `interval` | `900` | Base poll interval (seconds, min 60). The connector also auto-schedules ~15 min after the newest dataset. |
| `country` / `language` / `brand` | `si` / `sl` / `VOLKSWAGEN_PASSENGER_CARS` | OIDC `state` components. Each brand authenticates with its own OIDC `client_id` — set `brand` to match your car (see below). |
| `vin` | — | Optional. Restrict to a single VIN; otherwise all consented vehicles are used. |
| `hide_vins` | `[]` | VINs to exclude. |

### Supported brands

Set `brand` to the canonical key (or a friendly alias) for your vehicle. Matching is
case-insensitive; an unknown value falls back to Volkswagen Passenger Cars.

| `brand` key | Aliases | Manufacturer |
|---|---|---|
| `VOLKSWAGEN_PASSENGER_CARS` | `VW`, `VOLKSWAGEN` | Volkswagen |
| `VOLKSWAGEN_COMMERCIAL_VEHICLES` | `VWN`, `VW_COMMERCIAL` | Volkswagen Commercial Vehicles |
| `AUDI` | | Audi |
| `SKODA` | `ŠKODA` | Škoda |
| `SEAT` | | SEAT |
| `CUPRA` | | Cupra |
| `BENTLEY` | | Bentley |

### Drivetrains (EV / combustion / PHEV)

The connector detects the drivetrain from the data points present (battery vs fuel) and promotes the
vehicle to electric, combustion or hybrid accordingly, following the same conventions as the official
`carconnectivity-connector-seatcupra`:

- **Pure EV** — a single `primary` (electric) drive.
- **PHEV** — the `primary` drive is the **combustion** engine and the `secondary` drive is the
  **electric** one (so the petrol range is no longer assigned to the electric drive).

## Exposed data points (read-only)

Both the dotted (nested) and flat portal field names are accepted. Drive slots follow the drivetrain
(see *Drivetrains* above): on a PHEV the electric drive is the `secondary` slot and combustion is
`primary`; on a pure EV the electric drive is the single `primary` slot.

### Vehicle

| EU Data Act field | CarConnectivity attribute | Notes |
|---|---|---|
| `mileage.value` / `mileage` | `vehicle.odometer` | unit from `mileage.unit` |
| `outside_temperature` | `vehicle.outside_temperature` | deci-Kelvin → °C |
| `locked` / `lock_state` | `vehicle.doors.lock_state` | |
| `open_state_*` (per door) | `vehicle.doors[*].open_state` | front/rear L/R, bonnet, tailgate |
| `locked_state_*` (per door) | `vehicle.doors[*].lock_state` | |
| `position_*_window_lifter` (fallback `state_*_window_lifter`) | `vehicle.windows[*].open_state` | opening % → closed / ajar / open; incl. sunroof |
| `window_heating_state` (+ `_front` / `_rear`) | `vehicle.window_heatings` (overall + per element) | |
| `parking_lights` | `vehicle.lights[parking].light_state` | |
| `cruising_range_combined` | `vehicle.drives.total_range` (km) | |

### Maintenance & climatisation

| EU Data Act field | CarConnectivity attribute | Notes |
|---|---|---|
| `maintenance_interval__time_until_inspection` | `maintenance.inspection_due_at` | signed countdown → due date |
| `maintenance_interval__time_until_oil_change` | `maintenance.oil_service_due_at` | |
| `maintenance_interval_distance_until_*` | `maintenance.*_due_after` (km) | |
| `remaining_climate_time` / `remaining_climatisation_time` | `climatization.estimated_date_reached` | seconds (dotted) / minutes (flat) |

### Electric drive (`secondary` on PHEV, `primary` on EV)

| EU Data Act field | CarConnectivity attribute | Notes |
|---|---|---|
| `battery_state_report.soc` / `state_of_charge` | `drive.level` (%) | |
| `range` / `cruising_range_secondary_engine` | `drive.range` (km) | |
| `long_term_data_average_electr_engine_consumption` | `drive.consumption` | kWh/1000km → kWh/100km |
| `min_temperature` / `max_temperature` | `battery.temperature_min` / `temperature_max` (°C) | |

### Combustion drive (`primary` on PHEV)

| EU Data Act field | CarConnectivity attribute | Notes |
|---|---|---|
| `fuel_level_current_level` / `tank_current_level` | `drive.level` (%) | |
| `cruising_range_primary_engine` | `drive.range` (km) | |
| `long_term_data_average_fuel_consumption` | `drive.consumption` | L/1000km → L/100km |
| `scr_range` | `drive.adblue_range` (km) | diesel / SCR |
| `oil_level_actual_level` | `drive.oil_level` (%) | requires a carconnectivity core with `oil_level`; skipped on older cores |

### Charging

| EU Data Act field | CarConnectivity attribute | Notes |
|---|---|---|
| `battery_state_report.charge_power` | `charging.power` (kW) | |
| `charging_state_report.current_charge_state` / `charging_state` | `charging.state` | |
| `charging_state_report.charge_type` / `charging_mode` | `charging.type` (AC / DC / off) | |
| `battery_state_report.charge_rate` (+ unit) | `charging.rate` (km/h or mph) | |
| `…remaining_charging_time_complete` / `remaining_charging_time` | `charging.estimated_date_reached` | |
| `settings.target_soc` | `charging.settings.target_level` (%) | |
| `plug_state` | `charging.connector.connection_state` | |
| `external_power_supply_state` | `charging.connector.external_power` | |

## Not exposed (and why)

These portal fields have **no native CarConnectivity model**, so they are left as the connector's
"unmapped sensor" log entries rather than forced into an ill-fitting attribute:

- **Tyre pressures** (`tyre_pressure_actual/required/differential_*`) — no tyre-pressure model.
- **Instrument-cluster warnings** (`active_warnings_in_instrument_cluster_*`) — no warning model; the
  value is also a latched hex bitmask that does not clear reliably.
- **Oil level extras** (`oil_level_additional_oil_level`, `oil_level_dipstick_indicator_function`,
  `oil_level_total_max`) — VW-specific quirks with no native model; the actual oil level
  (`oil_level_actual_level`) *is* mapped to `drive.oil_level` (see above).
- **Trip statistics** (`short_/long_term_data_*`: average speed, trip mileage, travel time,
  recuperation, aux/gas consumption, zero-emission distance) — no trip-statistics model.
- **"Safe" / secured states** (`safe_state_*`), `parking_brake`, `state_spoiler`,
  `state_service_hatch`, `state_of_hood`, `bem_level`, `energy_flow` — no attribute.
- **Exact window opening percentage** — used to derive the window `open_state` (closed / ajar / open),
  but the `Window` model has no numeric position attribute, so the percentage itself is not stored.
- **Raw HV SoC** (`hv_soc`) — the displayed SoC is already mapped to `drive.level`.
- **Diagnostics / triggers** (`echo`, `trueness`, `charging_state_error_code`,
  `window_heating_error_code`, `charging_reason_trigger`, `led_state` / `led_color`, `cng_gas_level`,
  `fuel_level__accuracy`, …).

## License

MIT
