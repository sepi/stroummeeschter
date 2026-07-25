from __future__ import annotations

import argparse
import logging
import os

from stroummeeschter.logger import ReadingLogger

DEFAULT_SENSORS = (
    "sensor-power_consumed,sensor-power_produced,"
    "sensor-energy_consumed_luxembourg,sensor-energy_produced_luxembourg,"
    "sensor-power_consumed_phase_1,sensor-power_consumed_phase_2,sensor-power_consumed_phase_3,"
    "sensor-power_produced_phase_1,sensor-power_produced_phase_2,sensor-power_produced_phase_3"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stroummeeschter-import-slimmelezer",
        description="Stream readings from a SlimmeLezer into a SQLite database.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("STROUMMEESCHTER_URL", "http://stroum"),
        help="Base URL of the SlimmeLezer (default: %(default)s, env STROUMMEESCHTER_URL)",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("STROUMMEESCHTER_DB", "stroummeeschter.db"),
        help="Path to the SQLite database file (default: %(default)s, env STROUMMEESCHTER_DB)",
    )
    parser.add_argument(
        "--sensors",
        default=os.environ.get("STROUMMEESCHTER_SENSORS", DEFAULT_SENSORS),
        help="Comma-separated entity IDs to record, or 'all' for every primary "
        "entity (default: %(default)s, env STROUMMEESCHTER_SENSORS)",
    )
    parser.add_argument(
        "--include-diagnostics",
        action="store_true",
        help="Also record diagnostic entities (Wi-Fi signal, uptime, ...). Ignored when --sensors "
        "names specific entities explicitly - those are always recorded regardless of category.",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.0,
        help="Minimum seconds between recorded values per entity (default: 0, record every change)",
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

    sensors = None if args.sensors.strip().lower() == "all" else {
        s.strip() for s in args.sensors.split(",") if s.strip()
    }

    reading_logger = ReadingLogger(
        base_url=args.url,
        db_path=args.db,
        sensors=sensors,
        include_diagnostics=args.include_diagnostics,
        min_interval=args.min_interval,
    )
    reading_logger.run()


if __name__ == "__main__":
    main()
