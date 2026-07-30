"""Aggregates computable from the recorded readings.

Symbols (confirmed with the utility - billing is net-metered, not
gross-metered): Ig/Eg = gross import/export power at the connection point;
In = Ig - Eg = net import (what's actually billed); En = -In = net export;
P = PV production; C = In + P = consumption (energy balance identity,
holds regardless of billing). There's deliberately no self-consumption
ratio here: under confirmed net metering, every Wh of production reduces
In 1:1 whether it was used on-site in the same instant or briefly exported
and re-imported elsewhere - only the net total over the billing period is
ever actually billed, so a "fraction self-consumed" metric doesn't
correspond to anything financially real.
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
    net_import_wh (In) / net_export_wh (En = -In): the billed quantity.
    consumed_wh (C = In + P): energy balance identity, needs both net
    import and PV production known.
    net_exporting_share: fraction of power_balance samples in the window
    where produced > consumed - a proxy for "how much of the time were we
    net-exporting".
    """
    consumed_first, consumed_last = _first_last(conn, ENTITY_ENERGY_CONSUMED, since, until)
    produced_first, produced_last = _first_last(conn, ENTITY_ENERGY_PRODUCED, since, until)

    imported_wh = consumed_last - consumed_first if None not in (consumed_first, consumed_last) else None
    exported_wh = produced_last - produced_first if None not in (produced_first, produced_last) else None
    net_import_wh = imported_wh - exported_wh if None not in (imported_wh, exported_wh) else None
    net_export_wh = -net_import_wh if net_import_wh is not None else None

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

    consumed_wh = pv_production_wh + net_import_wh if None not in (pv_production_wh, net_import_wh) else None

    return {
        "imported_wh": imported_wh,
        "exported_wh": exported_wh,
        "net_import_wh": net_import_wh,
        "net_export_wh": net_export_wh,
        "net_exporting_share": net_exporting_share,
        "pv_production_wh": pv_production_wh,
        "consumed_wh": consumed_wh,
    }
