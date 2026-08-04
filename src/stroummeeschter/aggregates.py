"""Aggregates computable from the recorded readings.

Symbols (confirmed with the utility - billing is net-metered, not
gross-metered, and settled per DEFAULT_BUCKET_SECONDS-wide window -
Luxembourg's actual regulatory framework, Reglement ILR/E24/1 and Creos's
Smarty meters, both confirm 15 minutes): Ig/Eg = gross import/export power
at the connection point; In = Ig - Eg = net import (what's actually
billed); En = -In = net export; P = PV production; C = In + P =
consumption (energy balance identity, holds regardless of billing).

imported_wh/exported_wh below are NOT plain gross Ig/Eg totals (that would
be bucket-width-independent and, as it turns out, isn't what's billed).
Billing nets Ig against Eg *within* each settlement bucket first, and only
that bucket's resulting sign/magnitude counts - a bucket that both
imported and exported has the smaller one cancelled before it's ever
billed. Summing only the positive per-bucket nets (In+) and only the
negative ones flipped positive (En+) does NOT telescope down to a single
lump-diff figure the way plain signed netting does
(sum(max(0, net_i)) != max(0, sum(net_i))) - measured against 5.5 days of
real data, computing this at 15-min buckets vs. 1-min buckets gives
totals 3-7x apart, every single day. So these have to be computed from
per-bucket nets, not first/last readings alone.

There's deliberately no self-consumption ratio here: under confirmed net
metering, every Wh of production reduces In 1:1 whether it was used
on-site in the same instant or briefly exported and re-imported
elsewhere - only the net total over the billing period is ever actually
billed, so a "fraction self-consumed" metric doesn't correspond to
anything financially real.
"""

from __future__ import annotations

import sqlite3

ENTITY_ENERGY_CONSUMED = "sensor-energy_consumed_luxembourg"
ENTITY_ENERGY_PRODUCED = "sensor-energy_produced_luxembourg"
ENTITY_PV_PRODUCTION = "envoy-production_wh_lifetime"

# 15 minutes - confirmed as the real settlement period both for general
# Creos billing (Smarty transmits 15-min averages, used directly for
# billing) and specifically for energy-community sharing (Reglement
# ILR/E24/1: DSO computes a quarter-hourly energy balance). Passed as a
# parameter rather than hardcoded so a different width (e.g. 3600 for 1h)
# is trivially available later without new plumbing.
DEFAULT_BUCKET_SECONDS = 900


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


def _bucket_last_values(
    conn: sqlite3.Connection, entity_id: str, since: str, until: str, bucket_seconds: int
) -> list[tuple[int, float]]:
    """Last reading per bucket_seconds-wide, epoch-aligned bucket in
    [since, until) - one row per bucket that actually has data, as
    (bucket_start_epoch, value).

    Deliberately uses LEAD() ordered by recorded_at (the index's own
    order - see idx_readings_entity_time) rather than PARTITION BY the
    computed bucket expression: partitioning by a computed column forces
    SQLite to sort every matching raw reading before it can group them,
    which measured ~4.5x slower and doesn't scale (a full year of dense
    10s-cadence data pushed a single entity's query past a minute).
    LEAD() only needs recorded_at order, which comes straight from the
    index, so this is a single linear scan - measured ~8s/entity for a
    full year of dense synthetic data (3.15M rows), ~0.15s for this
    project's actual 5.5 days so far. Only the small per-bucket result
    (one row per bucket, not per raw reading) gets sorted at the end.
    """
    return conn.execute(
        """
        WITH ordered AS (
            SELECT
                recorded_at,
                value,
                (CAST(unixepoch(recorded_at) AS INTEGER) / ?) AS bucket,
                LEAD((CAST(unixepoch(recorded_at) AS INTEGER) / ?)) OVER (ORDER BY recorded_at) AS next_bucket
            FROM readings
            WHERE entity_id = ? AND recorded_at >= ? AND recorded_at < ? AND value IS NOT NULL
        )
        SELECT bucket * ? AS bucket_start, value
        FROM ordered
        WHERE next_bucket IS NULL OR next_bucket != bucket
        ORDER BY bucket_start
        """,
        (bucket_seconds, bucket_seconds, entity_id, since, until, bucket_seconds),
    ).fetchall()


def _value_before(conn: sqlite3.Connection, entity_id: str, at: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM readings WHERE entity_id = ? AND recorded_at < ? AND value IS NOT NULL "
        "ORDER BY recorded_at DESC LIMIT 1",
        (entity_id, at),
    ).fetchone()
    return row[0] if row else None


def _netted_positive_totals(
    conn: sqlite3.Connection,
    import_entity: str,
    export_entity: str,
    since: str,
    until: str,
    bucket_seconds: int,
) -> tuple[float | None, float | None]:
    """In+/En+ over [since, until): net import_entity against
    export_entity within each bucket_seconds-wide bucket, then sum only
    each bucket's positive part (In+) and negative part flipped positive
    (En+) separately - see the module docstring for why this can't be
    derived from a single lump first/last diff."""
    ig_rows = _bucket_last_values(conn, import_entity, since, until, bucket_seconds)
    eg_rows = _bucket_last_values(conn, export_entity, since, until, bucket_seconds)
    if not ig_rows or not eg_rows:
        return None, None

    ig_by_bucket = dict(ig_rows)
    eg_by_bucket = dict(eg_rows)

    # Seed the first bucket's delta from whatever came just before `since`;
    # falling back to that bucket's own value (a zero first delta) if
    # there's nothing earlier - same "can't know what happened before data
    # started" situation chart.py's own forward-fill already accepts.
    ig_seed = _value_before(conn, import_entity, since)
    if ig_seed is None:
        ig_seed = ig_rows[0][1]
    eg_seed = _value_before(conn, export_entity, since)
    if eg_seed is None:
        eg_seed = eg_rows[0][1]

    in_plus = 0.0
    en_plus = 0.0
    prev_ig, prev_eg = ig_seed, eg_seed
    for bucket_start in sorted(set(ig_by_bucket) | set(eg_by_bucket)):
        ig = ig_by_bucket.get(bucket_start, prev_ig)
        eg = eg_by_bucket.get(bucket_start, prev_eg)
        net = (ig - prev_ig) - (eg - prev_eg)
        if net > 0:
            in_plus += net
        elif net < 0:
            en_plus += -net
        prev_ig, prev_eg = ig, eg

    return in_plus, en_plus


def energy_totals(
    conn: sqlite3.Connection, since: str, until: str, bucket_seconds: int = DEFAULT_BUCKET_SECONDS
) -> dict:
    """Wh totals for [since, until), derived from the meter's own cumulative counters.

    imported_wh (In+) / exported_wh (En+): properly netted per
    bucket_seconds-wide bucket (default 15 min, the confirmed real
    settlement period) - see the module docstring for why this isn't a
    plain first/last diff.
    net_import_wh (In) / net_export_wh (En = -In): the billed quantity -
    imported_wh - exported_wh, which does telescope correctly regardless
    of bucket width (only the split between the two, not their
    difference, depends on bucket_seconds).
    consumed_wh (C = In + P): energy balance identity, needs both net
    import and PV production known.
    net_exporting_share: fraction of power_balance samples in the window
    where produced > consumed - a proxy for "how much of the time were we
    net-exporting".
    """
    imported_wh, exported_wh = _netted_positive_totals(
        conn, ENTITY_ENERGY_CONSUMED, ENTITY_ENERGY_PRODUCED, since, until, bucket_seconds
    )
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
