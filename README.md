# stroummeeschter

Streams readings from a SlimmeLezer (ESPHome-based P1 smart-meter reader,
`http://stroum` on the local network) into a SQLite database.

## How sampling works

The device is an ESPHome build (`web_server` component) and exposes a
Server-Sent-Events stream at `/events`. On connect it immediately replays
the *current value of every entity* as a burst of `event: state` messages,
then pushes a new `event: state` message the instant any value actually
changes. There's also `event: ping` (keepalive) and `event: log` (debug
lines), which are ignored.

This means a single long-lived SSE connection is sufficient to capture
every value: no polling interval to guess, and nothing missed between
polls. If the connection drops (Wi-Fi hiccup, device reboot), the client
reconnects with exponential backoff, and the reconnect itself delivers a
fresh full snapshot - so state is never silently stale.

By default only `sensor-power_consumed`, `sensor-power_produced`,
`sensor-energy_consumed_luxembourg` and `sensor-energy_produced_luxembourg`
are recorded (see `--sensors` below) - the instantaneous power sensors for
charting, plus the meter's own cumulative energy counters so totals over a
period can be computed exactly (last - first reading) instead of by
integrating noisy power samples.

## measured_at

The device does **not** currently expose the smart meter's own
measurement timestamp (the P1 telegram's OBIS `0-0:1.0.0` field). We
checked the live entity list and there is no `timestamp` text_sensor
configured. `readings.recorded_at` is therefore our local UTC receipt
time, not a meter-side timestamp.

If you want true meter-side timestamps, uncomment the `timestamp` line
in the SlimmeLezer's ESPHome YAML (it exists in the `dsmr` platform
config but ships commented out) and re-flash; a `text_sensor-timestamp`
entity would then show up in the stream and could be correlated with
readings from the same telegram burst.

## Units

We parse the device's `state` string ourselves (e.g. `"0.591 kW"`) rather
than trust its JSON `value` field: on the live device, `value` was observed
truncating a trailing significant digit (`value=15006.39` vs.
`state="15006.392 kWh"` for the same reading) while `state` always reflects
the sensor's configured decimal accuracy. Numeric results are normalized to
their SI base unit (`kW` -> `W`, `kWh` -> `Wh`) in `units.py`, so nothing
downstream has to reason about metric prefixes. `entities.unit` records the
post-normalization unit. Text sensors (identification strings, Wi-Fi SSID,
...) pass through as plain strings.

## Data accumulation rate

Measured against the live device: with the default 4-sensor set, about
**2.2 MiB/day (~0.8 GiB/year)**. With `--sensors all` (every primary
entity, ~24 sensors), about **25 MiB/day (~9 GiB/year)**. Both figures are
real on-disk size after a WAL checkpoint + `VACUUM`, extrapolated from the
device's actual telegram cadence (~1 push per sensor every ~10-15s).

## Database schema

Managed via numbered migrations in `migrations/` (see below).

- `entities(id, name, unit, category, first_seen, last_seen)` - one row
  per entity, upserted as metadata arrives (delta events don't repeat
  name/unit/category, so they're only ever added, never nulled out).
- `readings(id, entity_id, value, recorded_at)` - one row per observed
  value. `value` has no fixed column type: REAL (SI-normalized) for
  numeric sensors, TEXT for text sensors, `NULL` when the meter reports
  `"NA"`.
- `power_balance` (view) - pivots `sensor-power_consumed` /
  `sensor-power_produced` into one row per `recorded_at` with
  `consumed_w`, `produced_w`, `net_export_w` columns, for querying net
  import/export directly instead of joining at query time.

## Schema migrations

Schema changes live as plain numbered `.sql` files under
`src/stroummeeschter/migrations/`, tracked via SQLite's built-in
`PRAGMA user_version`. Both `stroummeeschter-import-slimmelezer` (the logger) and
`stroummeeschter-chart` apply any pending migrations automatically on
startup - already-applied ones are skipped, so this is safe to run on
every deploy without a separate migrate step.

To add a schema change: drop a new `NNNN_description.sql` file in with
the next number, ship it, restart the service(s).

## Usage

```bash
pip install dist/stroummeeschter-*.whl
stroummeeschter-import-slimmelezer --url http://stroum --db /var/lib/stroummeeschter/stroum.db
```

Options (also settable via `STROUMMEESCHTER_URL` / `STROUMMEESCHTER_DB` /
`STROUMMEESCHTER_SENSORS` env vars):

- `--url` - base URL of the SlimmeLezer (default `http://stroum`)
- `--db` - path to the SQLite file (default `stroummeeschter.db`)
- `--sensors` - comma-separated entity IDs to record, or `all` for every
  primary entity (default: `sensor-power_consumed,sensor-power_produced,
  sensor-energy_consumed_luxembourg,sensor-energy_produced_luxembourg`)
- `--include-diagnostics` - also record Wi-Fi/uptime/etc. Ignored when
  `--sensors` names specific entities explicitly - those are always
  recorded regardless of category.
- `--min-interval SECONDS` - throttle: at most one recorded value per
  entity per interval (default `0`, i.e. record every change)
- `--log-level`

## Viewing a chart

`stroummeeschter-chart` renders a chart to a PNG file - it doesn't serve
anything itself, so point whatever's already serving static files on your
box (nginx, a static dir, the thermal printer's "print image from URL"
integration if it can read a file/local URL) at the output path.

```bash
# once, for the current local day (default - see below)
stroummeeschter-chart --db /var/lib/stroummeeschter/stroum.db \
  --out /var/www/html/power.png --width 576 --height 300

# or keep it fresh continuously (writes are atomic, via a temp file + rename,
# so nothing ever reads a half-written PNG)
stroummeeschter-chart --db /var/lib/stroummeeschter/stroum.db \
  --out /var/www/html/power.png --interval 60
```

**Time window**: by default the chart covers the current *local* calendar
day, from `--day-start-hour` (default `6`, i.e. 6am) to the same hour the
next day - so "today's chart" always means the same thing regardless of
what time you happen to generate it. Use `--date YYYY-MM-DD` to render a
past day instead. `--hours N` overrides this with a rolling window ending
now (e.g. for a quick "last 3 hours" check) - note the title's aggregates
(see below) still always cover the full calendar day even when `--hours`
zooms the plotted lines into a shorter window, since "today's total"
matters regardless of how far you've zoomed in. `--width`/`--height` are
in pixels - match your printer's paper width (e.g. `--width 384 --height
250` for a narrow 58mm printer).

**`--chart {power,phases}`** (default `power`) selects which chart to
render:

- `power`: four lines - **Import** and **Export** (grid, from the
  SlimmeLezer), **Production** (solar, from Envoy if configured), and a
  derived **Consumption** line - `production + import - export`, the
  instantaneous energy balance - plotted *negative*, as a mirror-image
  sink against the three generation-side signals. A green fill on a
  secondary 0-100% axis shows the instantaneous self-consumption index
  (`(production - export) / production`, or see `--assume-netting`
  below). The y-axis isn't anchored at 0 (that stopped making sense once
  Consumption goes negative); a horizontal line at 0 marks the crossing.
- `phases`: net grid power per phase (`produced - consumed` for that
  phase alone) - useful for spotting a phase imbalance, e.g. solar
  landing on phases 2/3 while a load on phase 1 has to import regardless
  of overall surplus (same-phase production/consumption cancels out
  silently before the meter ever sees it, so this is the net residual).

**`--assume-netting`** (power chart only) changes the self-consumption
formula from `(production - export) / production` to
`min(consumption, production) / production` - i.e. assumes a phase-1
import is financially cancelled by simultaneous phase-2/3 export, as if
self-consumed. This is an **unconfirmed hypothesis**, not known billing
reality - the research done for this project (Luxembourg's
autoconsommation scheme) suggests import and export are billed
*separately*, not netted, so the default (no flag) formula reflects that.
Use the flag to visualize "what if it were netted" for comparison.

**Resampling**: Import/export update roughly every 10-15s, Envoy
production only every ~60s by default; since they don't share timestamps,
all raw signals are resampled onto one shared 10-second grid (pandas
`Series.reindex(method="ffill", tolerance=...)`) before any arithmetic
between them, rather than assumed to line up - this is also what makes
plain signal arithmetic (`production_w - export_w`) safe, since pandas
aligns Series by their now-identical index. Forward-filling only holds a
value for up to 5 minutes (`chart.py.MAX_GAP`): if a source stops
updating for longer than that (a stalled process, not just its normal
polling gap), the line breaks instead of holding the stale value forever
- this happened for real once (see git history / conversation) and
silently made a derived signal look like it was tracking a trend it
wasn't.

The chart title bakes in the period's aggregates so a printed copy carries
the numbers, not just the lines:

- Imported / Exported / Net export (kWh) and the share of samples that
  were net-exporting - from the meter's own cumulative energy counters,
  not integrated from the noisier power samples (see `aggregates.py`).
- PV production and **self-consumption %** - only shown when Envoy
  production data is also present in the window. Without it, true
  self-consumption isn't computable from grid meter data alone, since the
  grid meter only sees net import/export, not the panels' raw output.

## Enphase Envoy production data (optional)

`stroummeeschter-import-envoy` polls an Envoy's local `/production.json` for true
solar production, recorded as `envoy-production_w` /
`envoy-production_wh_lifetime` / `envoy-production_wh_today` in the same
database. Combined with the grid meter's export total, this gives a real
self-consumption ratio: `(produced - exported) / produced`.

Current Envoy firmware requires a long-lived access token (generate one
via the Enlighten portal / `entrez.enphaseenergy.com`, tied to your
Envoy's serial number) exchanged for a local session cookie - a bare
`Authorization` header per request isn't accepted. Put the token in a
file you control (any path, your choice):

```bash
echo "YOUR_TOKEN" > /etc/stroummeeschter/envoy_token
chmod 600 /etc/stroummeeschter/envoy_token

stroummeeschter-import-envoy --url https://envoy \
  --token-file /etc/stroummeeschter/envoy_token \
  --db /var/lib/stroummeeschter/stroum.db --interval 60
```

If an install has no production CT clamp (microinverter-only monitoring,
common when there's no separate metering hardware), Envoy still returns
an `eim`-type entry in the response but with `activeCount: 0` and every
field hardcoded to `0` - it's a placeholder, not real data.
`_pick_production_entry` in `envoy_cli.py` only trusts an `eim` entry when
its `activeCount > 0`, and otherwise falls back to the microinverter-summed
`inverters` entry, which is always live.

Use `--dump` to fetch and pretty-print the raw `/production.json` payload
once (no db writes) - useful for checking what your specific
install/firmware actually returns before trusting the parsing.

## Development

This project uses [pip-tools](https://github.com/jazzband/pip-tools) to
pin dependencies.

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

## Building and installing the wheel on the server

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install build
python -m build --wheel
# copy dist/stroummeeschter-*.whl to the server, then there:
pip install stroummeeschter-*.whl
```

### Running as systemd services

The logger, the Envoy poller, and the chart writer are independent
processes - run whichever combination you need.

```ini
# /etc/systemd/system/stroummeeschter-import-slimmelezer.service
[Unit]
Description=stroummeeschter - SlimmeLezer to SQLite logger
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/stroummeeschter/.venv/bin/stroummeeschter-import-slimmelezer --url http://stroum --db /var/lib/stroummeeschter/stroum.db
Restart=on-failure
RestartSec=5
DynamicUser=yes
StateDirectory=stroummeeschter

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/stroummeeschter-import-envoy.service
[Unit]
Description=stroummeeschter - Envoy production poller
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/stroummeeschter/.venv/bin/stroummeeschter-import-envoy --url https://envoy \
  --token-file /etc/stroummeeschter/envoy_token \
  --db /var/lib/stroummeeschter/stroum.db --interval 60
Restart=on-failure
RestartSec=5
DynamicUser=yes
StateDirectory=stroummeeschter
ConfigurationDirectory=stroummeeschter

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/stroummeeschter-chart.service
[Unit]
Description=stroummeeschter - power chart writer
After=stroummeeschter-import-slimmelezer.service

[Service]
ExecStart=/opt/stroummeeschter/.venv/bin/stroummeeschter-chart \
  --db /var/lib/stroummeeschter/stroum.db \
  --out /var/www/html/power.png --interval 60
Restart=on-failure
RestartSec=5
DynamicUser=yes
StateDirectory=stroummeeschter

[Install]
WantedBy=multi-user.target
```
