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
import json
import logging
import os
from datetime import date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from stroummeeschter import db
from stroummeeschter.chart import PHASE_SIGNALS, POWER_SIGNALS
from stroummeeschter.chart_cli import DEFAULT_DAY_START_HOUR, RENDERERS, render_png
from stroummeeschter.stats import compute_stats

logger = logging.getLogger(__name__)

MIN_DIMENSION_PX = 100
MAX_DIMENSION_PX = 3000
MAX_HOURS = 24 * 30

REFRESH_MS = 30_000


def _signal_checkboxes(signals: tuple[str, ...]) -> str:
    return "\n".join(
        f'<label><input type="checkbox" data-signal="{s}" checked> {s.replace("_", " ")}</label>'
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
  #stats-panel {{ position: fixed; bottom: 0; left: 0; right: 0; max-height: 40%;
                  overflow: auto; background: rgba(0,0,0,0.8); font-size: 12px;
                  padding: 8px; display: none; z-index: 9; }}
  #stats-panel table {{ border-collapse: collapse; margin: 0 auto; }}
  #stats-panel th, #stats-panel td {{ padding: 2px 8px; text-align: right; white-space: nowrap; }}
  #stats-panel th {{ text-align: left; }}
  #stats-panel td:first-child, #stats-panel th:first-child {{ text-align: left; }}
</style></head>
<body>
<div id="controls">
  <label>Chart:
    <select id="chart-type">
      <option value="power">Power</option>
      <option value="phases">Phases</option>
    </select>
  </label>
  <div id="power-signals" class="signal-grid">
  {_signal_checkboxes(POWER_SIGNALS)}
  </div>
  <div id="phase-signals" class="signal-grid" style="display:none">
  {_signal_checkboxes(PHASE_SIGNALS)}
  </div>
  <label id="netting-label"><input type="checkbox" id="assume-netting"> Assume netting</label>
  <label>Hours: <input type="number" id="hours" placeholder="full day" style="width:5em"></label>
  <label><input type="checkbox" id="show-stats"> Stats</label>
</div>
<div id="chart-wrap"><img id="chart-img"></div>
<div id="stats-panel"></div>
<script>
(function () {{
  var img = document.getElementById('chart-img');
  var chartType = document.getElementById('chart-type');
  var powerSignals = document.getElementById('power-signals');
  var phaseSignals = document.getElementById('phase-signals');
  var nettingLabel = document.getElementById('netting-label');
  var assumeNetting = document.getElementById('assume-netting');
  var hoursInput = document.getElementById('hours');
  var showStats = document.getElementById('show-stats');
  var statsPanel = document.getElementById('stats-panel');

  function checkedSignals(container) {{
    return Array.prototype.slice.call(container.querySelectorAll('input[data-signal]:checked'))
      .map(function (el) {{ return el.dataset.signal; }});
  }}

  function updateSrc() {{
    var isPhases = chartType.value === 'phases';
    powerSignals.style.display = isPhases ? 'none' : '';
    phaseSignals.style.display = isPhases ? '' : 'none';
    nettingLabel.style.display = isPhases ? 'none' : '';

    var signals = checkedSignals(isPhases ? phaseSignals : powerSignals);
    var params = new URLSearchParams();
    params.set('chart', chartType.value);
    params.set('width', Math.round(window.innerWidth));
    params.set('height', Math.round(window.innerHeight - 42));
    params.set('signals', signals.join(','));
    if (!isPhases && assumeNetting.checked) params.set('assume_netting', '1');
    if (hoursInput.value) params.set('hours', hoursInput.value);
    params.set('t', Date.now());  // cache-bust: always refetch, never a stale cached image
    img.src = '/chart.png?' + params.toString();
  }}

  var HORIZON_LABELS = {{last_hour: 'Last hour', last_day: 'Last day', last_week: 'Last week',
                         last_month: 'Last month', total: 'Total'}};

  function fmtW(v) {{ return v === null || v === undefined ? '-' : Math.round(v) + ' W'; }}
  function fmtMinAvgMax(stat) {{
    if (!stat) return '-';
    return fmtW(stat.min_w) + ' / ' + fmtW(stat.avg_w) + ' / ' + fmtW(stat.max_w);
  }}
  function fmtKwh(v) {{ return v === null || v === undefined ? '-' : (v / 1000).toFixed(2) + ' kWh'; }}
  function fmtPct(v) {{ return v === null || v === undefined ? '-' : Math.round(v * 100) + '%'; }}

  function renderStats(data) {{
    // Same shape for every signal: power view (min/avg/max W) then energy
    // view (total kWh) where applicable - Import/Export/Production/
    // Consumption all get both, nothing cherry-picked.
    var rows = ['<tr><th>Horizon</th>' +
                '<th>Import (min/avg/max)</th><th>Imported</th>' +
                '<th>Export (min/avg/max)</th><th>Exported</th>' +
                '<th>Production (min/avg/max)</th><th>Produced</th>' +
                '<th>Consumption (avg)</th><th>Consumed</th>' +
                '<th>Net export</th><th>Self-consumption</th><th>Net-exporting</th></tr>'];
    Object.keys(HORIZON_LABELS).forEach(function (key) {{
      var h = data.horizons[key];
      if (!h) return;
      rows.push('<tr><td>' + HORIZON_LABELS[key] + '</td><td>' +
        fmtMinAvgMax(h.import_w) + '</td><td>' + fmtKwh(h.imported_wh) + '</td><td>' +
        fmtMinAvgMax(h.export_w) + '</td><td>' + fmtKwh(h.exported_wh) + '</td><td>' +
        fmtMinAvgMax(h.production_w) + '</td><td>' + fmtKwh(h.produced_wh) + '</td><td>' +
        fmtW(h.avg_consumption_w) + '</td><td>' + fmtKwh(h.consumed_wh) + '</td><td>' +
        fmtKwh(h.net_export_wh) + '</td><td>' +
        fmtPct(h.self_consumption_ratio) + '</td><td>' +
        fmtPct(h.net_exporting_share) + '</td></tr>');
    }});
    statsPanel.innerHTML = '<table>' + rows.join('') + '</table>';
  }}

  function updateStats() {{
    if (!showStats.checked) return;
    fetch('/stats.json').then(function (r) {{ return r.json(); }}).then(renderStats)
      .catch(function (err) {{ statsPanel.textContent = 'Failed to load stats: ' + err; }});
  }}

  showStats.addEventListener('change', function () {{
    statsPanel.style.display = showStats.checked ? 'block' : 'none';
    updateStats();
  }});

  chartType.addEventListener('change', updateSrc);
  assumeNetting.addEventListener('change', updateSrc);
  hoursInput.addEventListener('change', updateSrc);
  Array.prototype.forEach.call(document.querySelectorAll('input[data-signal]'), function (el) {{
    el.addEventListener('change', updateSrc);
  }});

  var resizeTimer;
  window.addEventListener('resize', function () {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(updateSrc, 250);
  }});

  updateSrc();
  setInterval(function () {{ updateSrc(); updateStats(); }}, {REFRESH_MS});
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


def _query_bool(query: dict, key: str) -> bool:
    values = query.get(key)
    if not values:
        return False
    return values[0].strip().lower() in ("1", "true", "yes", "on")


def make_handler(db_path: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/":
                    self._serve_index()
                elif parsed.path == "/chart.png":
                    self._serve_chart(parse_qs(parsed.query))
                elif parsed.path == "/stats.json":
                    self._serve_stats()
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

        def _serve_stats(self) -> None:
            conn = db.connect(db_path)
            try:
                db.init_db(conn)
                stats = compute_stats(conn)
            finally:
                conn.close()

            body = json.dumps(stats).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

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

            png = render_png(
                db_path,
                width,
                height,
                chart=chart,
                hours=_query_hours(query),
                day_start_hour=day_start_hour,
                on_date=on_date,
                assume_netting=_query_bool(query, "assume_netting"),
                signals=signals,
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
        description="Serve charts on demand over HTTP (GET /chart.png?chart=power|phases&hours=&date=&"
        "day_start_hour=&width=&height=&assume_netting=).",
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
