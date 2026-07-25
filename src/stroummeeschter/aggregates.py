"""Aggregates computable from the recorded readings.

A grid smart meter alone can't tell you true self-consumption (solar used
on-site vs. exported) - it only sees net import/export at the connection
point, not the panels' raw output. self_consumption_ratio below only
appears when Envoy production data (envoy-production_wh_lifetime) is also
present in the window; otherwise it's None rather than a guess.
"""

from __future__ import annotations

import sqlite3

ENTITY_ENERGY_CONSUMED = "sensor-energy_consumed_luxembourg"
ENTITY_ENERGY_PRODUCED = "sensor-energy_produced_luxembourg"
ENTITY_PV_PRODUCTION = "envoy-production_wh_lifetime"


def _first_last(conn: sqlite3.Connection, entity_id: str, since: str, until: str) -> tuple[float | None, float | None]:
    row = conn.execute(
        """
        SELECT
            (SELECT value FROM readings
             WHERE entity_id = ? AND recorded_at >= ? AND recorded_at < ?
             ORDER BY recorded_at ASC LIMIT 1),
            (SELECT value FROM readings
             WHERE entity_id = ? AND recorded_at >= ? AND recorded_at < ?
             ORDER BY recorded_at DESC LIMIT 1)
        """,
        (entity_id, since, until, entity_id, since, until),
    ).fetchone()
    return row


def energy_totals(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Wh totals for [since, until), derived from the meter's own cumulative counters.

    imported_wh / exported_wh: last - first cumulative reading in the window
    (exact - no integration of the noisier instantaneous power readings).
    net_exporting_share: fraction of power_balance samples in the window
    where produced > consumed - a proxy for "how much of the time were we
    net-exporting", not a true self-consumption ratio.
    """
    consumed_first, consumed_last = _first_last(conn, ENTITY_ENERGY_CONSUMED, since, until)
    produced_first, produced_last = _first_last(conn, ENTITY_ENERGY_PRODUCED, since, until)

    imported_wh = consumed_last - consumed_first if None not in (consumed_first, consumed_last) else None
    exported_wh = produced_last - produced_first if None not in (produced_first, produced_last) else None
    net_export_wh = exported_wh - imported_wh if None not in (imported_wh, exported_wh) else None

    (net_exporting_share,) = conn.execute(
        """
        SELECT AVG(CASE WHEN net_export_w > 0 THEN 1.0 ELSE 0.0 END)
        FROM power_balance
        WHERE recorded_at >= ? AND recorded_at < ?
        """,
        (since, until),
    ).fetchone()

    pv_first, pv_last = _first_last(conn, ENTITY_PV_PRODUCTION, since, until)
    pv_production_wh = pv_last - pv_first if None not in (pv_first, pv_last) else None

    self_consumption_ratio = None
    if pv_production_wh and exported_wh is not None:
        self_consumption_ratio = (pv_production_wh - exported_wh) / pv_production_wh

    return {
        "imported_wh": imported_wh,
        "exported_wh": exported_wh,
        "net_export_wh": net_export_wh,
        "net_exporting_share": net_exporting_share,
        "pv_production_wh": pv_production_wh,
        "self_consumption_ratio": self_consumption_ratio,
    }
