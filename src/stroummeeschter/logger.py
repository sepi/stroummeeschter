from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone

from stroummeeschter import db
from stroummeeschter.client import CATEGORY_PRIMARY, SlimmelezerClient

logger = logging.getLogger(__name__)

MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReadingLogger:
    """Consumes the SlimmeLezer SSE stream and persists readings to SQLite.

    Reconnects with exponential backoff on any connection failure. Because a
    fresh connection replays every entity's current value, no readings are
    lost across a reconnect - only the (unknown) values that changed while
    disconnected are unobserved, same as if the device itself was offline.
    """

    def __init__(
        self,
        base_url: str,
        db_path: str,
        sensors: set[str] | None = None,
        include_diagnostics: bool = False,
        min_interval: float = 0.0,
    ):
        self.client = SlimmelezerClient(base_url)
        self.db_path = db_path
        self.sensors = sensors  # None = no allowlist, fall back to category filtering
        self.include_diagnostics = include_diagnostics
        self.min_interval = min_interval
        self._last_recorded: dict[str, float] = {}

    def run(self) -> None:
        conn = db.connect(self.db_path)
        db.init_db(conn)

        backoff = MIN_BACKOFF
        while True:
            try:
                logger.info("Connecting to %s/events", self.client.base_url)
                for reading in self.client.stream_readings():
                    backoff = MIN_BACKOFF  # connection is healthy again
                    self._handle_reading(conn, reading)
            except Exception as exc:
                # Deliberately broad: this loop is meant to run forever, so a
                # single unexpected error (a malformed payload, a transient
                # db hiccup, anything) must never silently kill the process -
                # that leaves import/export frozen while other pollers (e.g.
                # Envoy) keep going, corrupting every derived signal that
                # combines them. Only KeyboardInterrupt/SystemExit (not
                # Exception subclasses) still stop the process, as intended.
                logger.warning("Stream interrupted (%r); reconnecting in %.0fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    def _handle_reading(self, conn: sqlite3.Connection, reading) -> None:
        logger.debug("Received %s = %r %s", reading.entity_id, reading.value, reading.unit or "")

        if self.sensors is not None:
            if reading.entity_id not in self.sensors:
                logger.debug("Skipping %s: not in --sensors allowlist", reading.entity_id)
                return
        else:
            category = reading.category if reading.category is not None else CATEGORY_PRIMARY
            if category != CATEGORY_PRIMARY and not self.include_diagnostics:
                logger.debug("Skipping %s: diagnostic entity", reading.entity_id)
                return

        if self.min_interval > 0:
            last = self._last_recorded.get(reading.entity_id)
            now_monotonic = time.monotonic()
            if last is not None and now_monotonic - last < self.min_interval:
                logger.debug("Skipping %s: throttled by --min-interval", reading.entity_id)
                return
            self._last_recorded[reading.entity_id] = now_monotonic

        now = _utcnow_iso()
        db.upsert_entity(
            conn,
            reading.entity_id,
            now,
            name=reading.name,
            unit=reading.unit,
            category=reading.category,
        )
        db.insert_reading(conn, reading.entity_id, reading.value, now)
        conn.commit()
