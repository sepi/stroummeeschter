"""Serves charts on demand over HTTP: one parametrized endpoint used both by
a browser (<img> tag or AJAX) and by rafthercal's ImagePlugin, which fetches
its configured IMAGE_URL with a plain HTTP GET. Local-LAN only, no auth, no
TLS - matches this project's existing trust model (the SlimmeLezer and
Envoy are themselves plain HTTP/self-signed HTTPS on the same network).

Rendering on demand (rather than periodically writing a file, see
chart_cli.py) means the image is always current as of the moment it's
fetched, and every option already supported by the CLI is available as a
query parameter for free - no separate "web" rendering path to keep in sync.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from stroummeeschter import db
from stroummeeschter.chart import (
    DEFAULT_POWER_SIGNALS,
    DEFAULT_PROD_SHIFT_MIN,
    DEFAULT_TREND_SIGNALS,
    PHASE_SIGNALS,
    POWER_SIGNALS,
    TREND_SIGNALS,
)
from stroummeeschter.chart_cli import DEFAULT_DAY_START_HOUR, RENDERERS, render_png
from stroummeeschter.trends import DEFAULT_COUNT as TREND_DEFAULT_COUNT
from stroummeeschter.trends import PERIODS as TREND_PERIODS

logger = logging.getLogger(__name__)

MIN_DIMENSION_PX = 100
MAX_DIMENSION_PX = 3000
MAX_HOURS = 24 * 30
MAX_PROD_SHIFT_MIN = 60.0

REFRESH_MS = 30_000


def _signal_checkboxes(signals: tuple[str, ...], checked: tuple[str, ...] | None = None) -> str:
    """`checked` (default: all of `signals`) controls which boxes start
    ticked - mirrors the backend's own signals=None default, so the page's
    initial view matches what a bare CLI/URL render would produce."""
    checked_set = signals if checked is None else checked
    return "\n".join(
        f'<label><input type="checkbox" data-signal="{s}"{" checked" if s in checked_set else ""}> '
        f'{s.replace("_", " ")}</label>'
        for s in signals
    )


_INDEX_HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>stroummeeschter</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #111; color: #eee;
                font-family: sans-serif; overflow: hidden; }}
  #controls {{ position: fixed; top: 0; left: 0; right: 0; padding: 6px 10px;
               background: rgba(0,0,0,0.65); display: flex; gap: 14px;
               align-items: center; flex-wrap: wrap; font-size: 13px; z-index: 10; }}
  #controls label {{ display: flex; align-items: center; gap: 4px; cursor: pointer;
                      white-space: nowrap; }}
  #chart-wrap {{ display: flex; justify-content: center; align-items: center;
                 height: 100%; padding-top: 42px; box-sizing: border-box; }}
  #chart-img {{ max-width: 100%; max-height: 100%; }}
  select, input[type=number] {{ background: #222; color: #eee; border: 1px solid #555; }}
  .signal-grid {{ display: grid; grid-template-rows: repeat(2, auto);
                  grid-auto-flow: column; gap: 2px 12px; }}
</style></head>
<body>
<div id="controls">
  <label>Chart:
    <select id="chart-type">
      <option value="power">Power</option>
      <option value="phases">Phases</option>
      <option value="trends">Trends</option>
    </select>
  </label>
  <label id="period-label" style="display:none">Period:
    <select id="period">
      <option value="quarter_hour">Quarter-hourly</option>
      <option value="hour">Hourly</option>
      <option value="day">Daily</option>
      <option value="week">Weekly</option>
      <option value="month">Monthly</option>
      <option value="year">Yearly</option>
    </select>
  </label>
  <div id="power-signals" class="signal-grid">
  {_signal_checkboxes(POWER_SIGNALS, checked=DEFAULT_POWER_SIGNALS)}
  </div>
  <div id="phase-signals" class="signal-grid" style="display:none">
  {_signal_checkboxes(PHASE_SIGNALS)}
  </div>
  <div id="trend-signals" class="signal-grid" style="display:none">
  {_signal_checkboxes(TREND_SIGNALS, checked=DEFAULT_TREND_SIGNALS)}
  </div>
  <label id="hours-label">Hours: <input type="number" id="hours" placeholder="full day" style="width:5em"></label>
  <label id="prod-shift-label" title="Experimental diagnostic - shifts the Production line by this many minutes; positive = toward the past. Not a real fix, see chart.py. Remembered per-browser via localStorage.">
    Prod shift (min, experimental):
    <input type="number" id="prod-shift" step="0.1" value="{DEFAULT_PROD_SHIFT_MIN}" style="width:5em">
  </label>
  <label id="price-label" title="Per-kWh prices - min/max bracket a worst/best-case Balance when the actual price tier (e.g. an energy-community favorable rate) can't be determined from meter data alone. A flat single price is just the same value in both boxes. Remembered per-browser via localStorage.">
    Import price (min/max):
    <input type="number" id="import-price-min" step="0.01" placeholder="min" style="width:4.5em">
    <input type="number" id="import-price-max" step="0.01" placeholder="max" style="width:4.5em">
    Export price (min/max):
    <input type="number" id="export-price-min" step="0.01" placeholder="min" style="width:4.5em">
    <input type="number" id="export-price-max" step="0.01" placeholder="max" style="width:4.5em">
  </label>
</div>
<div id="chart-wrap"><img id="chart-img"></div>
<script>
(function () {{
  var img = document.getElementById('chart-img');
  var chartType = document.getElementById('chart-type');
  var powerSignals = document.getElementById('power-signals');
  var phaseSignals = document.getElementById('phase-signals');
  var trendSignals = document.getElementById('trend-signals');
  var hoursLabel = document.getElementById('hours-label');
  var hoursInput = document.getElementById('hours');
  var prodShiftLabel = document.getElementById('prod-shift-label');
  var prodShiftInput = document.getElementById('prod-shift');
  var priceLabel = document.getElementById('price-label');
  var priceInputs = {{
    import_price_min: document.getElementById('import-price-min'),
    import_price_max: document.getElementById('import-price-max'),
    export_price_min: document.getElementById('export-price-min'),
    export_price_max: document.getElementById('export-price-max')
  }};
  var periodLabel = document.getElementById('period-label');
  var period = document.getElementById('period');

  // Remembered per-browser: once a user dials in a shift/price value, it
  // should stick across reloads instead of reverting to the server's
  // default (shift) or blank (price).
  var savedProdShift = localStorage.getItem('prodShiftMin');
  if (savedProdShift !== null) prodShiftInput.value = savedProdShift;
  prodShiftInput.addEventListener('change', function () {{
    localStorage.setItem('prodShiftMin', prodShiftInput.value);
  }});

  Object.keys(priceInputs).forEach(function (key) {{
    var el = priceInputs[key];
    var saved = localStorage.getItem(key);
    if (saved !== null) el.value = saved;
    el.addEventListener('change', function () {{ localStorage.setItem(key, el.value); }});
  }});

  function checkedSignals(container) {{
    return Array.prototype.slice.call(container.querySelectorAll('input[data-signal]:checked'))
      .map(function (el) {{ return el.dataset.signal; }});
  }}

  function updateSrc() {{
    var isPhases = chartType.value === 'phases';
    var isTrends = chartType.value === 'trends';
    powerSignals.style.display = (isPhases || isTrends) ? 'none' : '';
    phaseSignals.style.display = isPhases ? '' : 'none';
    trendSignals.style.display = isTrends ? '' : 'none';
    hoursLabel.style.display = isTrends ? 'none' : '';
    prodShiftLabel.style.display = (isPhases || isTrends) ? 'none' : '';
    priceLabel.style.display = isPhases ? 'none' : '';
    periodLabel.style.display = isTrends ? '' : 'none';

    var signalContainer = isTrends ? trendSignals : (isPhases ? phaseSignals : powerSignals);
    var signals = checkedSignals(signalContainer);
    var params = new URLSearchParams();
    params.set('chart', chartType.value);
    params.set('width', Math.round(window.innerWidth));
    params.set('height', Math.round(window.innerHeight - 42));
    params.set('signals', signals.join(','));
    if (isTrends) {{
      params.set('period', period.value);
    }} else {{
      if (hoursInput.value) params.set('hours', hoursInput.value);
      // Always sent (even 0) once past this guard - an explicit 0 must
      // disable the shift, not silently fall back to the server's own
      // non-zero default.
      if (!isPhases && prodShiftInput.value !== '') {{
        params.set('prod_shift_min', prodShiftInput.value);
      }}
    }}
    if (!isPhases) {{
      // Balance only shows once all four are filled in (see
      // chart.py _money_balance) - sending a partial set is harmless,
      // the backend just won't have enough to compute one.
      Object.keys(priceInputs).forEach(function (key) {{
        if (priceInputs[key].value !== '') params.set(key, priceInputs[key].value);
      }});
    }}
    params.set('t', Date.now());  // cache-bust: always refetch, never a stale cached image
    img.src = '/chart.png?' + params.toString();
  }}

  chartType.addEventListener('change', updateSrc);
  hoursInput.addEventListener('change', updateSrc);
  prodShiftInput.addEventListener('change', updateSrc);
  Object.keys(priceInputs).forEach(function (key) {{
    priceInputs[key].addEventListener('change', updateSrc);
  }});
  period.addEventListener('change', updateSrc);
  Array.prototype.forEach.call(document.querySelectorAll('input[data-signal]'), function (el) {{
    el.addEventListener('change', updateSrc);
  }});

  var resizeTimer;
  window.addEventListener('resize', function () {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(updateSrc, 250);
  }});

  updateSrc();
  setInterval(updateSrc, {REFRESH_MS});
}})();
</script>
</body></html>""".encode("utf-8")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _query_hours(query: dict) -> float | None:
    values = query.get("hours")
    if not values:
        return None
    try:
        value = float(values[0])
    except ValueError:
        return None
    return _clamp(value, 0.1, MAX_HOURS)


def _query_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(query[key][0]))
    except (KeyError, IndexError, ValueError):
        return default
    return int(_clamp(value, lo, hi))


def _query_float(query: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(query[key][0])
    except (KeyError, IndexError, ValueError):
        return default
    return _clamp(value, lo, hi)


def _query_optional_float(query: dict, key: str) -> float | None:
    try:
        return float(query[key][0])
    except (KeyError, IndexError, ValueError):
        return None


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/":
                    self._serve_index()
                elif parsed.path == "/chart.png":
                    self._serve_chart(parse_qs(parsed.query))
                else:
                    self.send_error(404)
            except Exception:
                logger.exception("Failed to handle %s", self.path)
                self.send_error(500, "internal error")

        def _serve_index(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_INDEX_HTML)))
            self.end_headers()
            self.wfile.write(_INDEX_HTML)

        def _serve_chart(self, query: dict) -> None:
            chart = (query.get("chart") or ["power"])[0]
            if chart not in RENDERERS:
                self.send_error(400, f"unknown chart '{chart}', expected one of {sorted(RENDERERS)}")
                return

            on_date = None
            date_raw = query.get("date")
            if date_raw:
                try:
                    on_date = date_cls.fromisoformat(date_raw[0])
                except ValueError:
                    self.send_error(400, f"invalid date '{date_raw[0]}', expected YYYY-MM-DD")
                    return

            width = _query_int(query, "width", 1600, MIN_DIMENSION_PX, MAX_DIMENSION_PX)
            height = _query_int(query, "height", 400, MIN_DIMENSION_PX, MAX_DIMENSION_PX)
            day_start_hour = _query_int(query, "day_start_hour", DEFAULT_DAY_START_HOUR, 0, 23)

            signals = (query.get("signals") or [None])[0]

            period = (query.get("period") or ["day"])[0]
            if period not in TREND_PERIODS:
                self.send_error(400, f"unknown period '{period}', expected one of {TREND_PERIODS}")
                return
            count = _query_int(query, "count", TREND_DEFAULT_COUNT[period], 1, 366)
            prod_shift_min = _query_float(
                query, "prod_shift_min", DEFAULT_PROD_SHIFT_MIN, -MAX_PROD_SHIFT_MIN, MAX_PROD_SHIFT_MIN
            )

            png = render_png(
                db_path,
                width,
                height,
                chart=chart,
                hours=_query_hours(query),
                day_start_hour=day_start_hour,
                on_date=on_date,
                signals=signals,
                period=period,
                count=count,
                prod_shift_min=prod_shift_min,
                import_price_min=_query_optional_float(query, "import_price_min"),
                import_price_max=_query_optional_float(query, "import_price_max"),
                export_price_min=_query_optional_float(query, "export_price_min"),
                export_price_max=_query_optional_float(query, "export_price_max"),
            )

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, format: str, *args) -> None:
            logger.info("%s - %s", self.address_string(), format % args)

    return Handler


def build_server(db_path: str, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(db_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stroummeeschter-web",
        description="Serve charts on demand over HTTP (GET /chart.png?chart=power|phases|trends&hours=&date=&"
        "day_start_hour=&width=&height=&signals=&period=&count=&prod_shift_min=&import_price_min=&"
        "import_price_max=&export_price_min=&export_price_max=).",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("STROUMMEESCHTER_DB", "stroummeeschter.db"),
        help="Path to the SQLite database file (default: %(default)s, env STROUMMEESCHTER_DB)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("STROUMMEESCHTER_WEB_HOST", "0.0.0.0"),
        help="Bind address (default: %(default)s - LAN-reachable, no auth; this is meant to stay "
        "on a trusted local network)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STROUMMEESCHTER_WEB_PORT", "8080")),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = build_server(args.db, args.host, args.port)
    logger.info("Serving on http://%s:%d/chart.png", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
