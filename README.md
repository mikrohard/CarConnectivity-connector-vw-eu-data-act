# CarConnectivity Connector for the Volkswagen EU Data Act Portal

A [CarConnectivity](https://github.com/tillsteinbach/CarConnectivity) connector that reads vehicle
data from the **Volkswagen EU Data Act portal** (`eu-data-act.drivesomethinggreater.com`).

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
| `country` / `language` / `brand` | `si` / `sl` / `VOLKSWAGEN_PASSENGER_CARS` | OIDC `state` components. |
| `vin` | — | Optional. Restrict to a single VIN; otherwise all consented vehicles are used. |
| `hide_vins` | `[]` | VINs to exclude. |

## Exposed data points (read-only)

| EU Data Act field | CarConnectivity attribute |
|---|---|
| `mileage.value` | `vehicle.odometer` (km) |
| `battery_state_report.soc` | electric `drive.level` (%) |
| `battery_state_report.charge_power` | `vehicle.charging.power` (kW) |
| `charging_state_report.current_charge_state` | `vehicle.charging.state` |
| `settings.target_soc` | `vehicle.charging.settings.target_level` (%) |
| `range` *(when present)* | electric `drive.range` (km) |
| `min_temperature` / `max_temperature` | `battery.temperature_min` / `temperature_max` (°C) |
| `window_heating_state` | `vehicle.window_heatings.heating_state` |
| `locked` | `vehicle.doors.lock_state` |

Fields without a native CarConnectivity home (parking brake, charge-mode options, raw diagnostics)
are not exposed.

## License

MIT
