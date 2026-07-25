# stroummeeschter

Records home energy data into SQLite and serves charts/stats from it:
grid import/export from a SlimmeLezer (an ESPHome-based P1 smart-meter
reader), and solar production from an Enphase Envoy. "Stroum" is
Luxembourgish for electricity/current.

## What it's made of

Four independent processes, each its own console script, sharing one
SQLite database:

| Command | What it does |
|---|---|
| `stroummeeschter-import-slimmelezer` | Streams grid import/export (and more) from the SlimmeLezer via Server-Sent Events |
| `stroummeeschter-import-envoy` | Polls an Enphase Envoy for solar production |
| `stroummeeschter-chart` | Renders a chart to a PNG file, once or on a fixed interval |
| `stroummeeschter-web` | Serves charts and stats on demand over HTTP, plus an interactive browser page |

Only `stroummeeschter-import-slimmelezer` is required to get anything
useful; the Envoy poller adds true solar production (and with it, a real
self-consumption ratio); the chart/web tools are just views over
whatever's in the database.

## Quick start (installing on a Raspberry Pi or similar)

No separate build machine needed - `git` and `python3` on the target is
enough, matching the sibling [rafthercal](https://github.com/sepi/rafthercal)
project's deployment approach:

```bash
git clone git@github-sepi:sepi/stroummeeschter.git
cd stroummeeschter
bash scripts/install-stroummeeschter
```

The script creates a project-local venv (`venv/`), installs dependencies
from `requirements.in` (**not** the pinned `requirements.txt` - see
[Development](#development) for why that distinction matters), installs
this package itself, and installs + starts three systemd **user** services:
`stroummeeschter-import-slimmelezer`, `stroummeeschter-import-envoy`, and
`stroummeeschter-web`. (`stroummeeschter-chart`, the periodic file-writer,
isn't started by default - `stroummeeschter-web` supersedes it for most
uses; see [Viewing charts](#viewing-charts).)

Before the Envoy poller does anything useful, put your Envoy's access
token in `envoy_token` (in the project directory) and `chmod 600` it, then
`systemctl --user restart stroummeeschter-import-envoy`. See
[Enphase Envoy setup](#enphase-envoy-setup-optional) for how to get that
token.

To update later:

```bash
scripts/update-stroummeeschter          # pulls latest main, reinstalls, restarts everything
scripts/update-stroummeeschter v0.2.0   # or a specific branch/tag/commit
```

Manage the services directly with `systemctl --user status|restart
<unit>` and `journalctl --user -u <unit> -f`. The unit templates live in
`src/stroummeeschter/*.service`, with a placeholder path
(`/home/pi/stroummeeschter`) that the install script swaps for wherever
you actually cloned it.

## Ingesting SlimmeLezer data

The device is an ESPHome build (confirmed live: `web_server` component,
`esp-app` web component) exposing Server-Sent Events at `/events`. On
connect it immediately replays the *current value of every entity* as a
burst of `event: state` messages, then pushes a new one the instant any
value changes (there's also `event: ping` keepalive and `event: log`
debug lines, both ignored). A single long-lived connection is therefore
enough to capture every value - no polling interval to guess, nothing
missed between polls - and a reconnect (Wi-Fi hiccup, device reboot)
delivers a fresh full snapshot on its own.

```bash
stroummeeschter-import-slimmelezer --url http://stroum --db stroummeeschter.db
```

Options (also settable via `STROUMMEESCHTER_URL` / `STROUMMEESCHTER_DB` /
`STROUMMEESCHTER_SENSORS` env vars):

- `--url` - base URL of the SlimmeLezer (default `http://stroum`)
- `--db` - path to the SQLite file (default `stroummeeschter.db`)
- `--sensors` - comma-separated entity IDs to record, or `all` for every
  primary entity. Default: `sensor-power_consumed`, `sensor-power_produced`,
  `sensor-energy_consumed_luxembourg`, `sensor-energy_produced_luxembourg`,
  and `sensor-power_consumed_phase_{1,2,3}` /
  `sensor-power_produced_phase_{1,2,3}` (10 entities) - the whole-house and
  per-phase power sensors for charting, plus the meter's own cumulative
  energy counters so totals over a period can be computed exactly
  (last - first reading) instead of by integrating noisy power samples.
- `--include-diagnostics` - also record Wi-Fi/uptime/etc. Ignored when
  `--sensors` names specific entities explicitly - those are always
  recorded regardless of category.
- `--min-interval SECONDS` - throttle: at most one recorded value per
  entity per interval (default `0`, i.e. record every change)
- `--log-level`

The connection uses a 30s connect timeout and a 20s **read** timeout
(`client.py`): the device's own telegram-driven updates arrive roughly
every ~10-15s, so 20s gives margin without waiting too long to notice a
truly dead connection. Without a read timeout at all, a silently-dropped
TCP connection (no FIN/RST, just goes quiet) would hang the process
forever with no exception ever raised - discovered the hard way, since
the reconnect-with-backoff loop can only act once something actually
raises. The loop catches `Exception` broadly for the same reason: any
single unexpected error must trigger a retry, not silently kill the
process (which would leave this signal frozen while other pollers, e.g.
Envoy, keep going - corrupting every derived signal that combines them).

### measured_at

The device does **not** expose the smart meter's own measurement
timestamp (the P1 telegram's OBIS `0-0:1.0.0` field) - checked the live
entity list, there's no `timestamp` text_sensor configured. So
`readings.recorded_at` is our local UTC receipt time, not a meter-side
timestamp. (The `dsmr` platform's ESPHome config has a `timestamp` line
that ships commented out; uncommenting it and re-flashing would expose a
`text_sensor-timestamp` entity if true meter-side timestamps are ever
needed.)

### Units and precision

We parse the device's `state` display string ourselves (e.g. `"0.591
kW"`) rather than trust its JSON `value` field: on the live device,
`value` was observed truncating a trailing significant digit
(`value=15006.39` vs. `state="15006.392 kWh"` for the same reading)
while `state` always reflects the sensor's configured decimal accuracy.
Numeric results are normalized to their SI base unit (`kW` -> `W`, `kWh`
-> `Wh`) in `units.py`, so nothing downstream has to reason about metric
prefixes; `entities.unit` records the post-normalization unit. Text
sensors (identification strings, Wi-Fi SSID, ...) pass through as plain
strings - the `readings.value` column deliberately has no fixed type
(REAL for numeric sensors, TEXT for text sensors).

## Enphase Envoy setup (optional)

```bash
echo "YOUR_TOKEN" > envoy_token
chmod 600 envoy_token
stroummeeschter-import-envoy --url https://envoy --token-file envoy_token --db stroummeeschter.db --interval 60
```

Current Envoy firmware requires a long-lived access token (generate one
via the Enlighten portal / `entrez.enphaseenergy.com`, tied to your
Envoy's serial number) exchanged for a local session cookie - a bare
`Authorization` header per request isn't accepted (`envoy.py` posts it to
`/auth/check_jwt`). Options:

- `--url` - base URL of the Envoy (default `https://envoy`, env `STROUMMEESCHTER_ENVOY_URL`)
- `--token-file` - path to the token file (required; env `STROUMMEESCHTER_ENVOY_TOKEN_FILE`)
- `--db` (env `STROUMMEESCHTER_DB`)
- `--interval` - seconds between polls (default `60`)
- `--dump` - fetch `/production.json` once, pretty-print the raw payload,
  and exit (no db writes) - useful for checking what your specific
  install/firmware actually returns before trusting the parsing
- `--log-level`

Recorded as `envoy-production_w`, `envoy-production_wh_lifetime`,
`envoy-production_wh_today`. If an install has no production CT clamp
(microinverter-only monitoring, common when there's no separate metering
hardware), Envoy still returns an `eim`-type entry in the response but
with `activeCount: 0` and every field hardcoded to `0` - a placeholder,
not real data. `_pick_production_entry` in `envoy_cli.py` only trusts an
`eim` entry when its `activeCount > 0`, otherwise falling back to the
microinverter-summed `inverters` entry, which is always live. Confirmed
against a real install exactly matching this case.

Combined with the grid meter's export total, this gives a real
self-consumption ratio - see [Viewing charts](#viewing-charts).

## Database

Schema changes live as plain numbered `.sql` files under
`src/stroummeeschter/migrations/`, tracked via SQLite's built-in `PRAGMA
user_version`. Every entry point that touches the database
(`stroummeeschter-import-slimmelezer`, `stroummeeschter-chart`,
`stroummeeschter-web`, `stroummeeschter-import-envoy`) applies any
pending migrations on startup - already-applied ones are skipped, so
this is always safe to run, no separate migrate step. To add a schema
change: drop in a new `NNNN_description.sql` file with the next number,
ship it, restart the service(s). Currently two migrations exist.

- `entities(id, name, unit, category, first_seen, last_seen)` - one row
  per entity, upserted as metadata arrives (delta events don't repeat
  name/unit/category, so they're only ever added, never nulled out).
- `readings(id, entity_id, value, recorded_at)` - one row per observed
  value, indexed on `(entity_id, recorded_at)`. `value` has no fixed
  column type (see [Units](#units-and-precision) above).
- `power_balance` (view) - pivots `sensor-power_consumed` /
  `sensor-power_produced` into one row per `recorded_at` with
  `consumed_w`, `produced_w`, `net_export_w` columns.

### Data accumulation rate

Measured against real data with the current default 10-sensor set: about
**8.0 MiB/day (~2.9 GiB/year)**. With `--sensors all` (every primary
entity, ~24 sensors, most of which change far less often than the power
sensors), about **11.9 MiB/day (~4.2 GiB/year)** - less than a proportional
scale-up because most of the extra entities are low-frequency diagnostics,
not high-frequency power readings. Both figures are real on-disk size
after a WAL checkpoint + `VACUUM`, extrapolated from the SlimmeLezer's
measured telegram cadence.

## Viewing charts

Two ways to get a chart, sharing all their rendering logic
(`chart_cli.py::render_png`):

**On demand (`stroummeeschter-web`, recommended)** - a stdlib
`http.server`, no nginx or other web server needed:

```bash
stroummeeschter-web --db stroummeeschter.db --port 8080
```

- `GET /chart.png?chart=power|phases&hours=&date=&day_start_hour=&width=&height=&signals=&assume_netting=` -
  the chart itself, generated fresh on every request (always current,
  every CLI option below available as a query param). Point rafthercal's
  `ImagePlugin` (`IMAGE_URL` config) at this for printing, or fetch it
  from a browser/AJAX call.
- `GET /stats.json` - see [Stats](#stats) below.
- `GET /` - an interactive page: chart-type selector, per-signal
  checkboxes (2x3 grid, swaps between power/phase signals), an "assume
  netting" toggle, an hours override, a live stats table (toggleable, at
  the bottom), auto-resizes to the browser window, and refreshes every
  30s (and on resize/control change).

`--host` defaults to `0.0.0.0` (LAN-reachable, no auth) and `--port`
defaults to `8080` (env `STROUMMEESCHTER_WEB_HOST` / `_WEB_PORT`) - this
is meant to stay on a trusted local network, matching the SlimmeLezer/
Envoy's own plain-HTTP trust model.

**Periodic file (`stroummeeschter-chart`)** - writes a PNG to a path
instead of serving it, for when something else needs a plain file rather
than a URL:

```bash
stroummeeschter-chart --db stroummeeschter.db --out power.png --interval 60
```

Writes atomically (temp file + rename) so a concurrent reader never sees
a half-written PNG. Same options as the query params above, as CLI flags
(`--chart`, `--hours`, `--day-start-hour`, `--date`, `--width`, `--height`,
`--signals`, `--assume-netting`), plus `--interval` (omit to render once
and exit).

### Time window

By default, both charts cover the current *local* calendar day, from
`--day-start-hour`/`day_start_hour` (default `6`, i.e. 6am, timezone
`Europe/Luxembourg`) to the same hour the next day - so "today's chart"
means the same thing regardless of when you generate it, and a live chart
stops 2 hours past "now" rather than riding out to the day's actual end
(there's no data in the future anyway; this just keeps the legend's
corner off of real data). `--date`/`date` renders a past day instead.
`--hours`/`hours` overrides both with a rolling window ending now (e.g.
a quick "last 3 hours" check) - the title/stats aggregates still always
cover the full calendar day even then, since "today's total" matters
regardless of how far you've zoomed in.

### Chart types and signals

**`power`** (default): six possible signals, each independently toggleable
via `--signals`/`signals=` (comma-separated; omit for all):

- **Import** (orange), **Export** (blue), **Production** (green) - read
  directly from a sensor.
- **Consumption** (solid red) - derived: `production + import - export`
  (the instantaneous energy balance), plotted *positive* alongside
  Production so a surplus/deficit shows up directly as which line is on
  top, rather than needing to read a sign.
- **Surplus** - a transparent blue fill between Consumption and
  Production wherever production is ahead.
- **Self-consumption %** - a green fill on a secondary 0-100% axis.
  Default formula `(production - export) / production` (only energy that
  never touched the grid counts as self-consumed - matches Luxembourg's
  autoconsommation billing, where export is paid at a separate, lower
  market rate, not netted against import). `--assume-netting` switches to
  `min(consumption, production) / production` instead - an **unconfirmed
  hypothesis** that import and export are financially netted, which the
  autoconsommation research above suggests is *not* how it actually works;
  the flag exists to visualize "what if" for comparison, not as a claim
  about real billing.

**`phases`**: **Phase {1,2,3} Import/Export** (color = direction, orange/
blue, matching the power chart; linestyle = phase) - raw per-phase grid
power, not pre-netted. Same-phase production and consumption already
cancel out silently before the meter ever measures them, so these lines
are the net residual per phase - useful for spotting a phase imbalance
(e.g. solar landing on phases 2/3 while a load on phase 1 has to import
regardless of overall surplus).

Both charts show gridlines (major + 10-minute minor ticks) and bake the
period's aggregates into the title: Imported/Exported/Net export (kWh)
and the share of samples net-exporting for `power` (from the meter's own
cumulative energy counters, not integrated from noisier power samples -
see `aggregates.py`); PV production and self-consumption % additionally,
only when Envoy data is present in the window; per-phase exporting share
for `phases`.

### How signals get aligned

Everything is resampled onto one regular 10-second grid
(`chart.py::_time_grid`) via `pandas.Series.reindex(method="ffill",
tolerance=...)`, rather than assumed to already share timestamps - the
SlimmeLezer updates roughly every 10-15s, Envoy roughly every 60s by
default, and they never land on the same instant. This is also what makes
plain arithmetic between signals (`production_w - export_w`) safe, since
they end up sharing an identical pandas index.

Two different forward-fill tolerances are used, deliberately:

- **`MAX_GAP` (5 minutes)** for anything displayed as its own raw line
  (Import, Export, Production, and both phase-chart signals). Generous on
  purpose - it's an outage detector, not a precision bound. Past this gap,
  the line breaks (shows nothing) instead of holding a stale value
  forever. This matters for real reasons: a SlimmeLezer logger stall once
  went undetected for 32 minutes and got silently forward-filled as a flat
  "current" value, which made a *derived* signal (Consumption) look like
  it was tracking a real trend it wasn't.
- **`DERIVED_MAX_GAP` (60 seconds)** specifically when Production feeds
  into Consumption or Self-consumption %. Envoy updates far less often
  than the SlimmeLezer, so holding its value for the full 5 minutes just
  to keep a *derived* line "continuous" would quietly stretch one real
  Envoy reading across many grid points that don't actually have fresh
  data - a milder, everyday version of the same problem. Tuned from real
  measured data: consecutive Envoy readings have gaps averaging ~18s but
  ranging up to 46s under completely normal operation (not outages, just
  jitter) - the first attempt at 20s was cutting ~30% of perfectly good
  readings, producing constant holes with no real cause.

Also handled: `recorded_at` only has second-level precision, so two
genuinely distinct readings for the same entity can land in the same
second (e.g. an SSE reconnect's full-snapshot replay overlapping a real
delta) - `pandas.reindex()` requires a unique index, so `_fetch_series`
keeps the later of any same-second duplicate.

## Stats

`GET /stats.json` on `stroummeeschter-web` - summary statistics across
five horizons (`last_hour`, `last_day`, `last_week`, `last_month` [30-day
approximation, calendar months vary], `total` [since the earliest actual
reading]), computed symmetrically across Import/Export/Production/
Consumption (`stats.py`):

- **min/avg/max (W)** for Import, Export, Production - these are directly
  stored signals, so exact min/avg/max is one cheap indexed SQL aggregate
  each, no resampling needed.
- **avg (W)** for Consumption only (not min/max) - Consumption isn't
  stored directly; true min/max would mean redoing the full resampled/
  derived series `chart.py` uses, which gets expensive as `total` grows
  over months of history. The average is cheap and exact: avg power =
  energy / time, using the same energy-balance identity as the chart's
  Consumption line, just as a plain total rather than a resampled series.
- **Imported/Exported/Produced/Consumed/Net export (kWh)** for every
  horizon - reusing `aggregates.energy_totals()` (cheap for any window).
- **Self-consumption ratio, net-exporting share** - also from
  `energy_totals()`.

Shown in `stroummeeschter-web`'s browser page as a toggleable table at
the bottom.

Not yet built: wiring this into a rafthercal text report (planned).

## Development

This project uses [pip-tools](https://github.com/jazzband/pip-tools) to
pin dependencies for **development and CI** - `requirements.txt` is a lock
file resolved by `pip-compile` on whatever machine last regenerated it
(typically a modern x86_64 dev box). The install script deliberately does
**not** use it (see [Quick start](#quick-start-installing-on-a-raspberry-pi-or-similar)) -
those exact pins for matplotlib/pandas/numpy may not exist as prebuilt
wheels for an older Pi/Debian/Python combination, which would defeat the
entire point of installing directly on the target. `requirements.in` (loose
bounds) is what actually gets installed there.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
pytest
```

To change dependencies, edit `requirements.in` / `requirements-dev.in`
and recompile:

```bash
pip-compile --strip-extras --output-file=requirements.txt requirements.in
pip-compile --strip-extras --output-file=requirements-dev.txt requirements-dev.in
```
