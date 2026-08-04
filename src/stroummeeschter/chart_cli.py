"""Writes the power chart to a PNG file - once, or repeatedly with --interval.

Serving the image (over HTTP, to a thermal printer, wherever) is left to
whatever's already doing that job on the box; this just produces the file.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from stroummeeschter import db
from stroummeeschter.chart import (
    DEFAULT_PROD_SHIFT_MIN,
    LOCAL_TZ,
    PHASE_SIGNALS,
    POWER_SIGNALS,
    TREND_SIGNALS,
    render_phase_chart,
    render_power_chart,
    render_trends_chart,
)
from stroummeeschter.trends import DEFAULT_COUNT as TREND_DEFAULT_COUNT
from stroummeeschter.trends import PERIODS as TREND_PERIODS
from stroummeeschter.trends import trend_buckets

logger = logging.getLogger(__name__)

RENDERERS = {
    "power": render_power_chart,
    "phases": render_phase_chart,
    "trends": render_trends_chart,
}

DEFAULT_DAY_START_HOUR = 6


def day_window(day_start_hour: int = DEFAULT_DAY_START_HOUR, on_date: date_cls | None = None) -> tuple[datetime, datetime]:
    """Return (since, until) in UTC for one local day_start_hour-to-day_start_hour
    window: the one `on_date` falls in, or - if on_date is omitted - whichever
    one "now" currently falls in (so before 6am local, that's still yesterday's
    window)."""
    if on_date is not None:
        start_local = datetime(on_date.year, on_date.month, on_date.day, day_start_hour, tzinfo=LOCAL_TZ)
    else:
        now_local = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
        start_local = now_local.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
        if now_local < start_local:
            start_local -= timedelta(days=1)

    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def render_png(
    db_path: str,
    width_px: int,
    height_px: int,
    chart: str = "power",
    hours: float | None = None,
    day_start_hour: int = DEFAULT_DAY_START_HOUR,
    on_date: date_cls | None = None,
    signals: str | None = None,
    period: str = "day",
    count: int | None = None,
    prod_shift_min: float = DEFAULT_PROD_SHIFT_MIN,
) -> bytes:
    """Resolve the time window and render one chart to PNG bytes. Shared by
    the file-writing CLI below and webapp.py's on-demand HTTP endpoint, so
    the window-resolution rules only live in one place.

    `signals` is a comma-separated string (e.g. "import,export") rather
    than a set, since both call sites (CLI args, URL query strings) hand
    this through as plain text.

    `period`/`count` only apply to --chart trends: buckets aren't points on
    a since/until timeline like power/phases are, so that chart bypasses
    the day-window resolution below entirely.

    `prod_shift_min` only applies to --chart power - see
    render_power_chart's docstring."""
    signal_set = {s.strip() for s in signals.split(",") if s.strip()} if signals else None

    if chart == "trends":
        buckets = trend_buckets(period, count or TREND_DEFAULT_COUNT[period], day_start_hour=day_start_hour)
        conn = db.connect(db_path)
        db.init_db(conn)
        try:
            return render_trends_chart(
                conn, buckets, signals=signal_set, width_px=width_px, height_px=height_px
            )
        finally:
            conn.close()

    render = RENDERERS[chart]

    # Totals always cover the full calendar day, regardless of --hours - a
    # "3 hour total" isn't a useful stat; "today's total" is what matters,
    # even while zoomed into a shorter window on the plotted lines.
    totals_since, totals_until = day_window(day_start_hour=day_start_hour, on_date=on_date)

    if hours is not None:
        until = datetime.now(timezone.utc)
        since = until - timedelta(hours=hours)
    elif on_date is None:
        # Viewing "today" (not a past --date): stop 2h past now rather than
        # riding out to the day's actual end. A live chart's data never
        # reaches the far end of the window anyway (that's the future), so
        # extending all the way there just leaves the legend's usual corner
        # sitting over real data instead of the empty space past "now".
        since = totals_since
        until = min(datetime.now(timezone.utc) + timedelta(hours=2), totals_until)
    else:
        since, until = totals_since, totals_until

    extra_kwargs = {"prod_shift_min": prod_shift_min} if chart == "power" else {}

    conn = db.connect(db_path)
    db.init_db(conn)
    try:
        return render(
            conn,
            since.isoformat(timespec="seconds"),
            until.isoformat(timespec="seconds"),
            totals_since=totals_since.isoformat(timespec="seconds"),
            totals_until=totals_until.isoformat(timespec="seconds"),
            signals=signal_set,
            width_px=width_px,
            height_px=height_px,
            **extra_kwargs,
        )
    finally:
        conn.close()


def write_chart(
    db_path: str,
    out_path: str,
    width_px: int,
    height_px: int,
    chart: str = "power",
    hours: float | None = None,
    day_start_hour: int = DEFAULT_DAY_START_HOUR,
    on_date: date_cls | None = None,
    signals: str | None = None,
    period: str = "day",
    count: int | None = None,
    prod_shift_min: float = DEFAULT_PROD_SHIFT_MIN,
) -> None:
    png = render_png(
        db_path,
        width_px,
        height_px,
        chart=chart,
        hours=hours,
        day_start_hour=day_start_hour,
        on_date=on_date,
        signals=signals,
        period=period,
        count=count,
        prod_shift_min=prod_shift_min,
    )

    # Write via a temp file + atomic rename so a concurrent reader (a static
    # web server, the thermal printer fetch) never sees a half-written PNG.
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(png)
    os.replace(tmp_path, out_path)
    logger.info("Wrote %s (%d bytes)", out_path, len(png))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stroummeeschter-chart",
        description="Render the power chart to a PNG file.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("STROUMMEESCHTER_DB", "stroummeeschter.db"),
        help="Path to the SQLite database file (default: %(default)s, env STROUMMEESCHTER_DB)",
    )
    parser.add_argument("--out", required=True, help="Path to write the PNG to")
    parser.add_argument(
        "--chart",
        choices=sorted(RENDERERS),
        default="power",
        help="Which chart to render (default: %(default)s)",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Rolling window in hours ending now. If omitted (default), charts the current "
        "local day instead (see --day-start-hour/--date).",
    )
    parser.add_argument(
        "--day-start-hour",
        type=int,
        default=DEFAULT_DAY_START_HOUR,
        help="Local hour (0-23) a 'day' starts at (default: %(default)s). Ignored if --hours is given.",
    )
    parser.add_argument(
        "--date",
        type=date_cls.fromisoformat,
        default=None,
        help="Chart the day_start_hour window for this date (YYYY-MM-DD) instead of the current "
        "one. Ignored if --hours is given.",
    )
    parser.add_argument(
        "--signals",
        default=None,
        help="Comma-separated list of signals to draw (default: gross import/export hidden, "
        "everything else shown - see chart.py DEFAULT_POWER_SIGNALS/DEFAULT_TREND_SIGNALS). "
        f"Power chart: {','.join(POWER_SIGNALS)}. Phase chart: {','.join(PHASE_SIGNALS)}. "
        f"Trends chart: {','.join(TREND_SIGNALS)}.",
    )
    parser.add_argument(
        "--period",
        choices=TREND_PERIODS,
        default="day",
        help="Bucket size for --chart trends (default: %(default)s). Ignored otherwise.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of buckets for --chart trends (default depends on --period: "
        f"{TREND_DEFAULT_COUNT}). Ignored otherwise.",
    )
    parser.add_argument(
        "--prod-shift-min",
        type=float,
        default=DEFAULT_PROD_SHIFT_MIN,
        help="EXPERIMENTAL diagnostic knob, --chart power only: shift the Production line by this "
        "many minutes (float; positive shifts it left/toward the past - i.e. a currently-recorded "
        "reading is displayed as if it happened this long ago). Not a verified fix for the "
        "Production-lags-the-grid artifact (see chart.py render_power_chart docstring) - grid-searching "
        "this by hand found no clean single value that corrects it, just one judged reasonable. "
        f"Default {DEFAULT_PROD_SHIFT_MIN} min - pass 0 to see the raw, unshifted data.",
    )
    parser.add_argument("--width", type=int, default=1600, help="Image width in pixels (default: %(default)s)")
    parser.add_argument("--height", type=int, default=400, help="Image height in pixels (default: %(default)s)")
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="If set, keep running and rewrite --out every N seconds instead of exiting after one render",
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

    kwargs = dict(
        hours=args.hours,
        day_start_hour=args.day_start_hour,
        on_date=args.date,
        chart=args.chart,
        signals=args.signals,
        period=args.period,
        count=args.count,
        prod_shift_min=args.prod_shift_min,
    )

    if args.interval is None:
        write_chart(args.db, args.out, args.width, args.height, **kwargs)
        return

    while True:
        try:
            write_chart(args.db, args.out, args.width, args.height, **kwargs)
        except Exception:
            logger.exception("Failed to render chart")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
