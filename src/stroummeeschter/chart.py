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

POWER_SIGNALS = ("import", "export", "net_import", "net_export", "production", "consumption")
# Billing is confirmed net-metered (net_import = import - export is what's
# actually billed) - the gross Import/Export lines aren't very interesting
# to look at day to day, so they're hidden unless explicitly requested via
# --signals; net_import is shown instead. net_export = -net_import is the
# same line mirrored around 0 - also hidden by default (redundant with
# net_import when both would just show), available for anyone who thinks
# in terms of export rather than import.
DEFAULT_POWER_SIGNALS = tuple(s for s in POWER_SIGNALS if s not in ("import", "export", "net_export"))
PHASE_SIGNALS = tuple(f"phase{p}_{direction}" for p in PHASES for direction in ("import", "export"))
PHASE_COLORS = {"import": "orange", "export": "blue"}

TREND_SIGNALS = ("imported", "exported", "net_import", "produced", "consumed", "surplus")
DEFAULT_TREND_SIGNALS = tuple(s for s in TREND_SIGNALS if s not in ("imported", "exported"))
# Import/Export/Production/net_import keep their colors from the power
# chart for consistency; Consumption switches from red-line to a distinct
# bar color since red-as-line vs red-as-bar read very differently at a
# glance, and Surplus gets its own color here since it's a full bar (can go
# negative), not the transparent overlay fill it is on the power chart.
TREND_COLORS = {
    "imported": "orange",
    "exported": "blue",
    "net_import": "purple",
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

# A signal combining multiple sources (Consumption) is only as fresh as its
# slowest input. Envoy updates far less often than the
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

# Grid-searched by hand against 5.5 days of real data (see render_power_chart's
# prod_shift_min docstring) - not a verified correction, just judged "not bad"
# for this specific install's lag. Still experimental: no clean single value
# was found that actually corrects the underlying artifact.
DEFAULT_PROD_SHIFT_MIN = 13.0


def _fmt_kwh(wh: float | None) -> str:
    return f"{wh / 1000:.2f} kWh" if wh is not None else "n/a"


def _fmt_pct(fraction: float | None) -> str:
    return f"{fraction * 100:.0f}%" if fraction is not None else "n/a"


def _money_balance(
    imported_wh: float | None,
    exported_wh: float | None,
    import_price_min: float | None,
    import_price_max: float | None,
    export_price_min: float | None,
    export_price_max: float | None,
) -> tuple[float, float] | None:
    """Conservative worst/best-case balance (whatever currency unit the
    prices are given in) from already-netted imported_wh/exported_wh -
    None if any input is missing.

    We can't tell from our own data which price tier (e.g. an
    energy-community favorable rate) applied to any given kWh, so this
    brackets the real figure instead of guessing at it: worst case pays
    the max import price and earns the min export price; best case pays
    the min import price and earns the max export price. A single flat
    price for a direction is just min == max for that direction - the
    two cases then agree for that side."""
    values = (imported_wh, exported_wh, import_price_min, import_price_max, export_price_min, export_price_max)
    if None in values:
        return None
    imported_kwh = imported_wh / 1000
    exported_kwh = exported_wh / 1000
    worst = exported_kwh * export_price_min - imported_kwh * import_price_max
    best = exported_kwh * export_price_max - imported_kwh * import_price_min
    return worst, best


def _fmt_balance(balance: tuple[float, float] | None) -> str | None:
    if balance is None:
        return None
    worst, best = balance
    if worst == best:
        return f"Balance {worst:+.2f}"
    return f"Balance {worst:+.2f} (worst) to {best:+.2f} (best)"


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


def _shift_by_minutes(series: pd.Series, minutes: float) -> pd.Series:
    """Shift `series` (already resampled onto the regular RESAMPLE_FREQ grid)
    by `minutes` - a no-op at minutes=0. Positive minutes moves the series
    left/toward the past: shift(periods=-n) moves a value that was at
    position i+n back to position i, i.e. pulls a later ("more future")
    reading backward in time. See render_power_chart's prod_shift_min
    docstring for why (and why this is diagnostic, not a real fix)."""
    if not minutes:
        return series
    grid_step_s = pd.Timedelta(RESAMPLE_FREQ).total_seconds()
    steps = round(minutes * 60 / grid_step_s)
    return series.shift(-steps)


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
    signals: set[str] | None = None,
    prod_shift_min: float = DEFAULT_PROD_SHIFT_MIN,
    import_price_min: float | None = None,
    import_price_max: float | None = None,
    export_price_min: float | None = None,
    export_price_max: float | None = None,
    width_px: int = 1600,
    height_px: int = 400,
    dpi: int = 100,
) -> bytes:
    """Plots [since, until), but the title's aggregates always cover
    [totals_since, totals_until) - which defaults to [since, until) but is
    meant to be passed the current calendar day regardless of what's being
    plotted, so "today's totals" stays meaningful even when zoomed into a
    shorter window (see chart_cli.write_chart).

    `signals` (from POWER_SIGNALS) restricts which lines get drawn;
    None (default) draws DEFAULT_POWER_SIGNALS (gross import/export hidden -
    pass them explicitly via --signals to see them). Everything is still
    computed regardless - the title's aggregates never depend on what's
    toggled on for display.

    `prod_shift_min` is an experimental diagnostic knob (not a verified fix
    - grid-searching it by hand found no clean single value that actually
    corrects the lag/under-response artifact, just one judged "not bad" -
    see DEFAULT_PROD_SHIFT_MIN): shifts the Production line (and what feeds
    Consumption) by this many minutes. Positive shifts it left/toward the
    past - i.e. a currently-recorded reading is displayed as if it happened
    this long ago, which is what you'd want if Production is lagging behind
    the near-instant grid readings. Defaults to DEFAULT_PROD_SHIFT_MIN, not
    0 - pass 0 explicitly to see the raw, unshifted data.

    `import_price_min/max`/`export_price_min/max` (all optional, same
    currency unit and per-kWh basis you'd naturally quote a tariff in): if
    all four are given, a worst/best-case Balance line is added to the
    title - see _money_balance. A flat single price for a direction is
    just min == max for that direction."""
    totals_since = totals_since or since
    totals_until = totals_until or until
    show = DEFAULT_POWER_SIGNALS if signals is None else signals

    grid = _time_grid(since, until)
    import_w = _resample(conn, ENTITY_IMPORT_W, since, until, grid)  # Ig
    export_w = _resample(conn, ENTITY_EXPORT_W, since, until, grid)  # Eg
    # In = Ig - Eg: the net-metered quantity actually billed (confirmed with
    # the utility) - the headline signal, unlike either gross line alone.
    net_import_w = import_w - export_w
    production_w = _resample(conn, ENTITY_PRODUCTION_W, since, until, grid)
    # Tighter tolerance specifically for combining into derived signals below
    # - see DERIVED_MAX_GAP. production_w (the loose version) is still what
    # gets displayed as the raw Production line.
    production_w_fresh = _resample(conn, ENTITY_PRODUCTION_W, since, until, grid, max_gap=DERIVED_MAX_GAP)

    production_w = _shift_by_minutes(production_w, prod_shift_min)
    production_w_fresh = _shift_by_minutes(production_w_fresh, prod_shift_min)

    # C = In + P (energy balance at any instant); plotted positive, alongside
    # Production, so a surplus/deficit shows up directly as which line is on
    # top - no sign-reading required.
    consumption_w = production_w_fresh + net_import_w

    totals = energy_totals(conn, totals_since, totals_until)

    fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_axisbelow(True)
    ax.grid(True, which="major", linewidth=0.5, alpha=0.5)

    if "import" in show:
        ax.plot(grid, import_w, label="Import", color="orange")
    if "export" in show:
        ax.plot(grid, export_w, label="Export", color="blue")
    if "net_import" in show:
        ax.plot(grid, net_import_w, label="Net import", color="purple")
    if "net_export" in show:
        ax.plot(grid, -net_import_w, label="Net export", color="teal")
    if "production" in show:
        ax.plot(grid, production_w, label="Production", color="green")
    if "consumption" in show:
        ax.plot(grid, consumption_w, label="Consumption", color="red")
    # Net import can go negative (net-exporting), so the axis can't be
    # floored at 0 the way the all-non-negative gross signals used to allow
    # - a zero line instead marks the boundary.
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("W")
    ax.legend(loc="upper right", fontsize="small")
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
        title_lines.append(f"PV production {_fmt_kwh(totals['pv_production_wh'])}")
    balance_line = _fmt_balance(
        _money_balance(
            totals["imported_wh"], totals["exported_wh"],
            import_price_min, import_price_max, export_price_min, export_price_max,
        )
    )
    if balance_line:
        title_lines.append(balance_line)
    if prod_shift_min:
        # Self-documenting: a shifted chart must never look indistinguishable
        # from an unshifted one - see prod_shift_min's docstring note.
        title_lines.append(f"[experimental: Production shifted {prod_shift_min:+.1f} min]")
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
    import_price_min: float | None = None,
    import_price_max: float | None = None,
    export_price_min: float | None = None,
    export_price_max: float | None = None,
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
    (default) draws DEFAULT_TREND_SIGNALS (gross imported/exported hidden,
    same rationale as the power chart - net_import is what's actually
    billed). consumed_wh/net_import_wh are read straight from
    aggregates.energy_totals(), which already derives them from the
    confirmed net-metering identity.

    `import_price_min/max`/`export_price_min/max` (optional, see
    render_power_chart): if all four are given, a worst/best-case Balance
    for the *whole* window (summed imported/exported across every bucket,
    regardless of which bars are toggled for display) is added to the
    title - a flat price times a per-bucket-netted total isn't the same
    as pricing each bucket separately and summing, but since the price
    itself is assumed constant across the window here, it is exactly
    equivalent (only time-varying pricing would need to be applied
    per-bucket).
    """
    show = DEFAULT_TREND_SIGNALS if signals is None else signals

    labels = []
    values = {name: [] for name in TREND_SIGNALS}
    total_imported_wh = 0.0
    total_exported_wh = 0.0
    for label, since, until in buckets:
        totals = energy_totals(conn, since, until)
        labels.append(label)
        values["imported"].append(_kwh_or_nan(totals["imported_wh"]))
        values["exported"].append(_kwh_or_nan(totals["exported_wh"]))
        values["net_import"].append(_kwh_or_nan(totals["net_import_wh"]))
        values["produced"].append(_kwh_or_nan(totals["pv_production_wh"]))
        values["consumed"].append(_kwh_or_nan(totals["consumed_wh"]))
        values["surplus"].append(_kwh_or_nan(totals["net_export_wh"]))
        total_imported_wh += totals["imported_wh"] or 0.0
        total_exported_wh += totals["exported_wh"] or 0.0

    fig, ax = plt.subplots(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    ax.set_axisbelow(True)
    ax.grid(True, which="major", axis="y", linewidth=0.5, alpha=0.5)
    ax.axhline(0, color="black", linewidth=0.8)

    shown = [name for name in TREND_SIGNALS if name in show]
    x = range(len(labels))
    bar_width = 0.8 / max(len(shown), 1)
    for i, name in enumerate(shown):
        offsets = [xi + (i - (len(shown) - 1) / 2) * bar_width for xi in x]
        ax.bar(offsets, values[name], width=bar_width, label=_trend_label(name), color=TREND_COLORS[name])

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("kWh")
    ax.legend(loc="upper right", fontsize="small")

    totals_line = "  |  ".join(
        f"{_trend_label(name)} {sum(v for v in values[name] if v == v):.1f} kWh"  # v == v filters NaN
        for name in shown
    )
    balance_line = _fmt_balance(
        _money_balance(
            total_imported_wh, total_exported_wh,
            import_price_min, import_price_max, export_price_min, export_price_max,
        )
    )
    title = f"{totals_line}\n{balance_line}" if balance_line else totals_line
    ax.set_title(title, fontsize=8)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _kwh_or_nan(wh: float | None) -> float:
    return wh / 1000 if wh is not None else float("nan")


def _trend_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


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
