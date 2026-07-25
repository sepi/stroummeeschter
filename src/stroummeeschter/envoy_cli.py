"""Polls an Enphase Envoy for production data into the stroummeeschter
database. The Envoy has no push mechanism (unlike the SlimmeLezer's SSE
stream), so this runs its own poll loop on --interval."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from stroummeeschter import db
from stroummeeschter.envoy import EnvoyClient

logger = logging.getLogger(__name__)

ENTITY_PRODUCTION_W = "envoy-production_w"
ENTITY_PRODUCTION_WH_LIFETIME = "envoy-production_wh_lifetime"
ENTITY_PRODUCTION_WH_TODAY = "envoy-production_wh_today"

MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0


def _pick_production_entry(payload: dict) -> dict | None:
    """Prefer the metered ('eim') entry, but only if a CT is actually active -
    installs without a production CT clamp still get an 'eim' entry, just
    with activeCount 0 and every field hardcoded to 0. Fall back to the
    microinverter-summed 'inverters' entry, which is always live."""
    entries = payload.get("production", [])
    for entry in entries:
        if entry.get("type") == "eim" and entry.get("activeCount", 0) > 0:
            return entry
    for entry in entries:
        if entry.get("type") == "inverters":
            return entry
    return None


def poll_once(client: EnvoyClient, conn: sqlite3.Connection) -> None:
    payload = client.production()
    entry = _pick_production_entry(payload)
    if entry is None:
        logger.warning("No production entry in Envoy response: %r", payload)
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    readings = {
        ENTITY_PRODUCTION_W: entry.get("wNow"),
        ENTITY_PRODUCTION_WH_LIFETIME: entry.get("whLifetime"),
        ENTITY_PRODUCTION_WH_TODAY: entry.get("whToday"),
    }
    for entity_id, value in readings.items():
        if value is None:
            continue
        unit = "W" if entity_id.endswith("_w") else "Wh"
        db.upsert_entity(conn, entity_id, now, name=entity_id, unit=unit, category=0)
        db.insert_reading(conn, entity_id, float(value), now)
    conn.commit()
    logger.debug("Envoy production: %r", readings)


def _read_token(token_file: str) -> str:
    with open(token_file) as f:
        return f.read().strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stroummeeschter-import-envoy",
        description="Poll an Enphase Envoy for production data into the stroummeeschter database.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("STROUMMEESCHTER_ENVOY_URL", "https://envoy"),
        help="Base URL of the Envoy (default: %(default)s, env STROUMMEESCHTER_ENVOY_URL)",
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("STROUMMEESCHTER_ENVOY_TOKEN_FILE"),
        help="Path to a file containing the Envoy long-lived access token "
        "(env STROUMMEESCHTER_ENVOY_TOKEN_FILE). Required.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("STROUMMEESCHTER_DB", "stroummeeschter.db"),
        help="Path to the SQLite database file (default: %(default)s, env STROUMMEESCHTER_DB)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between polls (default: %(default)s)",
    )
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Fetch /production.json once, pretty-print the raw payload, and exit (no db writes)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.token_file:
        parser.error("--token-file is required (or set STROUMMEESCHTER_ENVOY_TOKEN_FILE)")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dump:
        token = _read_token(args.token_file)
        client = EnvoyClient(args.url, token)
        print(json.dumps(client.production(), indent=2))
        return

    conn = db.connect(args.db)
    db.init_db(conn)

    backoff = MIN_BACKOFF
    while True:
        try:
            token = _read_token(args.token_file)
            client = EnvoyClient(args.url, token)
            poll_once(client, conn)
            backoff = MIN_BACKOFF
        except Exception:
            logger.exception("Envoy poll failed; retrying in %.0fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
