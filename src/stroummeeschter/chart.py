"""Renders a PNG power chart - reused both for the live web view and for
printing on the thermal printer (fetched by URL, so parameters come in as
plain query strings: hours/width/height)."""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # headless: no display, just render to a buffer

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from stroummeeschter.aggregates import energy_totals

ENTITY_IMPORT_W = "sensor-power_consumed"
ENTITY_EXPORT_W = "sensor-power_produced"
ENTITY_PRODUCTION_W = "envoy-production_w"

# Everything is stored as UTC; charts display in local time so a "daily"
# window (see chart_cli.day_window) reads as an actual local calendar day.
LOCAL_TZ = ZoneInfo("Europe/Luxembourg")

PHASES = (1, 2, 3)
PHASE_LINESTYLES = {1: "-", 2: "--", 3: "-."}

POWER_SIGNALS = ("import", "export", "production", "consumption", "self_consumption", "surplus")
PHASE_SIGNALS = tuple(f"phase{p}_{direction}" for p in PHASES for direction in ("import", "export"))
PHASE_COLORS = {"import": "orange", "export": "blue"}

TREND_SIGNALS = ("imported", "exported", "produced", "consumed", "surplus")
# Import/Export/Production keep their colors from the power chart for
# consistency; Consumption switches from red-line to a distinct bar color
# since red-as-line vs red-as-bar read very differently at a glance, and
# Surplus gets its own color here since it's a full bar (can go negative),
# not the transparent overlay fill it is on the power chart.
TREND_COLORS = {
    "imported": "orange",
    "exported": "blue",
    "produced": "green",
    "consumed": "firebrick",
    "surplus": "gray",
}

# All signals get resampled onto one regular grid at this resolution -
# roughly the SlimmeLezer's own native cadence, so plotting doesn't invent
# precision the data doesn't have. A shared regular index is also what
# makes plain Series arithmetic between differently-sourced signals safe
# (production_w - export_w, etc.) without manual zip/list-comprehension.
RESAMPLE_FREQ = "10s"

# Forward-fill holds the last known value between updates - necessary since
# Envoy (~60s cadence) and the SlimmeLezer (~10-15s) don't share timestamps.
# But if a source stops updating entirely (a stalled process, not just its
# normal polling gap), holding the last value forever silently fabricates
# data - it happened for real: a 32-minute logger stall rendered as a flat
# "current" line, which made a derived signal look like it was tracking a
# real trend it wasn't. Past this gap, show a break (NaN) instead.
MAX_GAP = pd.Timedelta(minutes=5)

# A signal combining multiple sources (Consumption, self-consumption %) is
# only as fresh as its slowest input. Envoy updates far less often than the
# SlimmeLezer, so holding its value for the full MAX_GAP just to keep a
# derived line "continuous" quietly stretches one real reading across many
# grid points that don't actually have fresh data - a milder, everyday
# version of the same fabrication MAX_GAP exists to prevent. Derived signals
# use this much tighter tolerance instead: only render where production is
# genuinely current, not merely "not yet timed out".
#
# 60s, not 20s: measured against real data, consecutive envoy-production_w
# readings have gaps averaging ~18s but ranging up to 46s under completely
# normal operation (network/processing jitter, not outages) - a 20s cutoff
# was cutting ~30% of readings that were actually fine, producing constant
# holes in Consumption with no real cause. 60s gives margin above the
# observed max without falling back to full outage-level tolerance.
DERIVED_MAX_GAP = pd.Timedelta(seconds=60)


def _fmt_kwh(wh: float | None) -> str:
    return f"{wh / 1000:.2f} kWh" if wh is not None else "n/a"


def _fmt_pct(fraction: float | None) -> str:
    return f"{fraction * 100:.0f}%" if fraction is not None else "n/a"


def _time_grid(since: str, until: str) -> pd.DatetimeIndex:
    return pd.date_range(start=pd.Timestamp(since), end=pd.Timestamp(until), freq=RESAMPLE_FREQ, inclusive="left")


def _fetch_series(conn: sqlite3.Connection, entity_id: str, since: str, until: str) -> pd.Series:
    rows = conn.execute(
        """
        SELECT recorded_at, value FROM readings
        WHERE entity_id = ? AND recorded_at >= ? AND recorded_at < ? AND value IS NOT NULL
        ORDER BY recorded_at
        """,
        (entity_id, since, until),
    ).fetchall()
    if not rows:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    index = pd.to_datetime([r[0] for r in rows], utc=True)
    series = pd.Series([r[1] for r in rows], index=index)
    # recorded_at only has second-level precision - two genuinely distinct
    # readings can land in the same second (e.g. an SSE reconnect's full
    # snapshot replay overlapping a real delta update). reindex() requires
    # a unique index, so keep the later of any tie - the more recent value.
    return series[~series.index.duplicated(keep="last")]


def _resample(
    conn: sqlite3.Connection,
    entity_id: str,
    since: str,
    until: str,
    grid: pd.DatetimeIndex,
    max_gap: pd.Timedelta = MAX_GAP,
) -> pd.Series:
    """Fetch `entity_id` and forward-fill it onto `grid`, breaking (NaN) past
    `max_gap` - see the module-level comment on why a real gap must never
    render as a flat hold."""
    series = _fetch_series(conn, entity_id, since, until)
    return series.reindex(grid, method="ffill", tolerance=max_gap)


def _phase_exporting_shares(conn: sqlite3.Connection, since: str, until: str) -> dict[int, float | None]:
    grid = _time_grid(since, until)
    shares = {}
    for p in PHASES:
        consumed_w = _resample(conn, f"sensor-power_consumed_phase_{p}", since, until, grid)
        produced_w = _resample(conn, f"sensor-power_produced_phase_{p}", since, until, grid)
        net_w = (produced_w - consumed_w).dropna()
        shares[p] = float((net_w > 0).mean()) if len(net_w) else None
    return shares


def render_power_chart(
    conn: sqlite3.Connection,
    since: str,
    until: str,
    totals_since: str | None = None,
    totals_until: str | None = None,
    assume_netting: bool = False,
    signals: set[str] | None = None,
    width_px: int = 1600,
    height_px: int = 400,
    dpi: int = 100,
) -> bytes:
    """Plots [since, until), but the title's aggregates always cover
    [totals_since, totals_until) - which defaults to [since, until) but is
    meant to be passed the current calendar day regardless of what's being
    plotted, so "today's totals" stays meaningful even when zoomed into a
    shorter window (see chart_cli.write_chart).

    `signals` (from POWER_SIGNALS) restricts which lines/fills get drawn;
    None (default) draws all of them. Everything is still computed
    regardless - the title's aggregates never depend on what's toggled on
    for display."""
    totals_since = totals_since or since
    totals_until = totals_until or until
    show = POWER_SIGNALS if signals is None else signals

    grid = _time_grid(since, until)
    import_w = _resample(conn, ENTITY_IMPORT_W, since, until, grid)
    export_w = _resample(conn, ENTITY_EXPORT_W, since, until, grid)
    production_w = _resample(conn, ENTITY_PRODUCTION_W, since, until, grid)
    # Tighter tolerance specifically for combining into derived signals below
    # - see DERIVED_MAX_GAP. production_w (the loose version) is still what
    # gets displayed as the raw Production line.
    production_w_fresh = _resample(conn, ENTITY_PRODUCTION_W, since, until, grid, max_gap=DERIVED_MAX_GAP)

    # consumption = production + import - export (energy balance at any instant);
    # plotted positive, alongside Production, so a surplus/deficit shows up
    # directly as which line is on top - no sign-reading required.
    consumption_w = production_w_fresh + import_w - export_w

    if assume_netting:
        # Hypothesis: if import/export were financially netted (unconfirmed -
        # Luxembourg's autoconsommation scheme researched earlier suggests
        # they're NOT), a phase-1 import is effectively "paid for" by
        # simultaneous phase-2/3 export, same as if self-consumed. Then
        # self-consumption is simply how much of total load production
        # covered, capped at 100% since you can't self-consume more than
        # you produced: min(consumption, production) / production.
        self_consumed_w = consumption_w.clip(upper=production_w_fresh)
        self_consumption_label = "Self-consumption % (assuming netting)"
    else:
        # Reality (per current research): export is paid at a separate, much
        # lower feed-in/market rate - only energy that never touched the
        # grid counts as self-consumed.
        self_consumed_w = production_w_fresh - export_w
        self_consumption_label = "Self-consumption %"
    # Undefined (NaN -> gap in the fill) whenever there's no production, e.g. at night.
    self_consumption_pct = (self_consumed_w / production_w_fresh * 100).where(production_w_fresh > 0)

    totals = energy_totals(conn, totals_since, totals_until)

    fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_axisbelow(True)
    ax.grid(True, which="major", linewidth=0.5, alpha=0.5)

    # Self-consumption % fill sits on its own 0-100 axis, underlaid behind
    # the power lines (lower zorder + a transparent ax patch so it shows through).
    ax2 = ax.twinx()
    ax2.set_zorder(ax.get_zorder() - 1)
    ax.patch.set_visible(False)
    if "self_consumption" in show:
        ax2.fill_between(grid, 0, self_consumption_pct, color="green", alpha=0.2, label=self_consumption_label)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Self-consumption %", color="green")
    ax2.tick_params(axis="y", labelcolor="green")

    # Surplus: the gap between Production and Consumption whenever
    # production is ahead - drawn behind the lines so they stay crisp on top.
    if "surplus" in show:
        ax.fill_between(
            grid, consumption_w, production_w,
            where=(production_w > consumption_w), color="blue", alpha=0.15, label="Surplus",
        )

    if "import" in show:
        ax.plot(grid, import_w, label="Import", color="orange")
    if "export" in show:
        ax.plot(grid, export_w, label="Export", color="blue")
    if "production" in show:
        ax.plot(grid, production_w, label="Production", color="green")
    if "consumption" in show:
        ax.plot(grid, consumption_w, label="Consumption", color="red")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("W")
    lines, labels = ax.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labels + labels2, loc="upper right", fontsize="small")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    # Minor ticks every 10 minutes for finer-grained reading, unlabeled so
    # they don't clutter a full-day window (only the major ticks get text).
    ax.xaxis.set_minor_locator(mdates.MinuteLocator(interval=10))
    ax.grid(True, which="minor", axis="x", linewidth=0.3, alpha=0.3)
    # Fixed to the requested window, not the data's own range - a chart
    # generated mid-day (or with gaps) must still show the whole window,
    # not a stretched view of whatever data happens to exist so far.
    ax.set_xlim(datetime.fromisoformat(since), datetime.fromisoformat(until))
    fig.autofmt_xdate()

    title_lines = [
        f"Imported {_fmt_kwh(totals['imported_wh'])}  |  Exported {_fmt_kwh(totals['exported_wh'])}",
        f"Net export {_fmt_kwh(totals['net_export_wh'])}  |  "
        f"Net-exporting {_fmt_pct(totals['net_exporting_share'])} of samples",
    ]
    if totals["pv_production_wh"] is not None:
        title_lines.append(
            f"PV production {_fmt_kwh(totals['pv_production_wh'])}  |  "
            f"Self-consumption {_fmt_pct(totals['self_consumption_ratio'])}"
        )
    ax.set_title("\n".join(title_lines), fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render_trends_chart(
    conn: sqlite3.Connection,
    buckets: list[tuple[str, str, str]],
    signals: set[str] | None = None,
    width_px: int = 1600,
    height_px: int = 400,
    dpi: int = 100,
) -> bytes:
    """Grouped bar chart of energy totals (kWh) per bucket - one bar group
    per (label, since, until) in `buckets` (see trends.trend_buckets), oldest
    first. Unlike the power/phase charts, buckets aren't points on a
    continuous time axis (a "monthly" bucket isn't comparable in width to a
    "yearly" one) so this uses categorical x-ticks, not a datetime axis.

    `signals` (from TREND_SIGNALS) restricts which bars get drawn; None
    (default) draws all five. Consumed is derived the same way as
    stats.compute_stats: produced + imported - exported.
    """
    show = TREND_SIGNALS if signals is None else signals

    labels = []
    values = {name: [] for name in TREND_SIGNALS}
    for label, since, until in buckets:
        totals = energy_totals(conn, since, until)
        imported_wh = totals["imported_wh"]
        exported_wh = totals["exported_wh"]
        produced_wh = totals["pv_production_wh"]
        consumed_wh = (
            produced_wh + imported_wh - exported_wh
            if None not in (produced_wh, imported_wh, exported_wh)
            else None
        )
        labels.append(label)
        values["imported"].append(_kwh_or_nan(imported_wh))
        values["exported"].append(_kwh_or_nan(exported_wh))
        values["produced"].append(_kwh_or_nan(produced_wh))
        values["consumed"].append(_kwh_or_nan(consumed_wh))
        values["surplus"].append(_kwh_or_nan(totals["net_export_wh"]))

    fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_axisbelow(True)
    ax.grid(True, which="major", axis="y", linewidth=0.5, alpha=0.5)
    ax.axhline(0, color="black", linewidth=0.8)

    shown = [name for name in TREND_SIGNALS if name in show]
    x = range(len(labels))
    bar_width = 0.8 / max(len(shown), 1)
    for i, name in enumerate(shown):
        offsets = [xi + (i - (len(shown) - 1) / 2) * bar_width for xi in x]
        ax.bar(offsets, values[name], width=bar_width, label=name.capitalize(), color=TREND_COLORS[name])

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("kWh")
    ax.legend(loc="upper right", fontsize="small")

    totals_line = "  |  ".join(
        f"{name.capitalize()} {sum(v for v in values[name] if v == v):.1f} kWh"  # v == v filters NaN
        for name in shown
    )
    ax.set_title(totals_line, fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _kwh_or_nan(wh: float | None) -> float:
    return wh / 1000 if wh is not None else float("nan")


def render_phase_chart(
    conn: sqlite3.Connection,
    since: str,
    until: str,
    totals_since: str | None = None,
    totals_until: str | None = None,
    signals: set[str] | None = None,
    width_px: int = 1600,
    height_px: int = 400,
    dpi: int = 100,
) -> bytes:
    """Per-phase import and export power - raw signals, not pre-netted.

    Same-phase production and consumption cancel out silently before the
    meter ever sees them, so a phase's import/export lines already reflect
    that netting - this is useful for spotting a phase imbalance (e.g.
    solar landing on phases 2/3 while a load on phase 1 has to import
    regardless of overall surplus).

    Plots [since, until), but the title's exporting-share always covers
    [totals_since, totals_until) - see render_power_chart. `signals` (from
    PHASE_SIGNALS, e.g. "phase1_import") restricts which lines get drawn;
    None draws all six. Color follows direction (orange=import, blue=export,
    matching render_power_chart); linestyle follows phase.
    """
    totals_since = totals_since or since
    totals_until = totals_until or until
    show = PHASE_SIGNALS if signals is None else signals

    grid = _time_grid(since, until)

    fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_axisbelow(True)
    ax.grid(True, which="major", linewidth=0.5, alpha=0.5)

    for p in PHASES:
        consumed_w = _resample(conn, f"sensor-power_consumed_phase_{p}", since, until, grid)
        produced_w = _resample(conn, f"sensor-power_produced_phase_{p}", since, until, grid)
        if f"phase{p}_import" in show:
            ax.plot(grid, consumed_w, label=f"Phase {p} Import", linestyle=PHASE_LINESTYLES[p], color=PHASE_COLORS["import"])
        if f"phase{p}_export" in show:
            ax.plot(grid, produced_w, label=f"Phase {p} Export", linestyle=PHASE_LINESTYLES[p], color=PHASE_COLORS["export"])

    exporting_shares = _phase_exporting_shares(conn, totals_since, totals_until)

    ax.set_ylim(bottom=0)
    ax.set_ylabel("W")
    ax.legend(loc="upper right", fontsize="small")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=LOCAL_TZ))
    # Minor ticks every 10 minutes for finer-grained reading, unlabeled so
    # they don't clutter a full-day window (only the major ticks get text).
    ax.xaxis.set_minor_locator(mdates.MinuteLocator(interval=10))
    ax.grid(True, which="minor", axis="x", linewidth=0.3, alpha=0.3)
    ax.set_xlim(datetime.fromisoformat(since), datetime.fromisoformat(until))
    fig.autofmt_xdate()

    ax.set_title(
        "  |  ".join(
            f"Phase {p} exporting {_fmt_pct(exporting_shares[p])} of samples" for p in PHASES
        ),
        fontsize=8,
    )

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
