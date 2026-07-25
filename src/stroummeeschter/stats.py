"""Summary statistics across several rolling time horizons, for /stats.json
and (eventually) a rafthercal text report.

Metrics included, and why - kept symmetric across Import/Export/Production/
Consumption on purpose, each gets both a power view and an energy view:
- min/avg/max (W) for Import, Export, Production: these are directly
  stored signals, so exact min/avg/max is one cheap indexed SQL aggregate
  each - no resampling needed. Consumption isn't stored directly (see
  chart.py) - true min/max would mean redoing the full resampled/derived
  series chart.py uses, expensive as "total" grows over months of history,
  so only its average is included (see below); min/max consumption is a
  future addition if it's wanted badly enough to justify the cost.
- imported/exported/produced/consumed/net-export (Wh) for every horizon:
  imported/exported/produced reused as-is from aggregates.energy_totals()
  (cheap for any window); consumed_wh = produced + imported - exported
  (the same energy-balance identity chart.py's Consumption line uses,
  here as a plain total rather than a resampled series). avg_consumption_w
  is just consumed_wh / horizon duration - avg power = energy / time.
- self-consumption ratio, net-exporting share: reused as-is from
  aggregates.energy_totals().
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from stroummeeschter.aggregates import energy_totals
from stroummeeschter.chart import ENTITY_EXPORT_W, ENTITY_IMPORT_W, ENTITY_PRODUCTION_W

HORIZONS = {
    "last_hour": timedelta(hours=1),
    "last_day": timedelta(days=1),
    "last_week": timedelta(days=7),
    "last_month": timedelta(days=30),  # calendar months vary in length; 30 days is an approximation
    "total": None,
}

# Sentinel lower bound if there's truly no data yet to find an earliest
# reading from - plain lexicographic ISO8601 string comparison, same as
# every other recorded_at range query in this codebase (no date parsing).
_EPOCH = "1970-01-01T00:00:00+00:00"


def _power_stats(conn: sqlite3.Connection, entity_id: str, since: str, until: str) -> dict:
    row = conn.execute(
        """
        SELECT MIN(value), AVG(value), MAX(value) FROM readings
        WHERE entity_id = ? AND recorded_at >= ? AND recorded_at < ? AND value IS NOT NULL
        """,
        (entity_id, since, until),
    ).fetchone()
    return {"min_w": row[0], "avg_w": row[1], "max_w": row[2]}


def _earliest_recorded_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MIN(recorded_at) FROM readings WHERE entity_id IN (?, ?, ?)",
        (ENTITY_IMPORT_W, ENTITY_EXPORT_W, ENTITY_PRODUCTION_W),
    ).fetchone()
    return row[0]


def _horizon_stats(conn: sqlite3.Connection, since: str, until: str, hours: float | None) -> dict:
    totals = energy_totals(conn, since, until)

    consumed_wh = None
    if None not in (totals["pv_production_wh"], totals["imported_wh"], totals["exported_wh"]):
        consumed_wh = totals["pv_production_wh"] + totals["imported_wh"] - totals["exported_wh"]
    avg_consumption_w = consumed_wh / hours if (consumed_wh is not None and hours) else None

    return {
        "since": since,
        "until": until,
        "import_w": _power_stats(conn, ENTITY_IMPORT_W, since, until),
        "export_w": _power_stats(conn, ENTITY_EXPORT_W, since, until),
        "production_w": _power_stats(conn, ENTITY_PRODUCTION_W, since, until),
        "avg_consumption_w": avg_consumption_w,
        "imported_wh": totals["imported_wh"],
        "exported_wh": totals["exported_wh"],
        "produced_wh": totals["pv_production_wh"],
        "consumed_wh": consumed_wh,
        "net_export_wh": totals["net_export_wh"],
        "net_exporting_share": totals["net_exporting_share"],
        "self_consumption_ratio": totals["self_consumption_ratio"],
    }


def compute_stats(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    until = now.isoformat(timespec="seconds")
    earliest: str | None = None

    horizons = {}
    for name, delta in HORIZONS.items():
        if delta is None:
            earliest = _earliest_recorded_at(conn)
            since = earliest or _EPOCH
            hours = (now - datetime.fromisoformat(since)).total_seconds() / 3600 if earliest else None
        else:
            since = (now - delta).isoformat(timespec="seconds")
            hours = delta.total_seconds() / 3600
        horizons[name] = _horizon_stats(conn, since, until, hours)

    return {"generated_at": until, "horizons": horizons}
