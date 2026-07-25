from __future__ import annotations

import logging
import sqlite3
from importlib import resources

logger = logging.getLogger(__name__)


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrations() -> list[tuple[int, str, str]]:
    """Return (version, filename, sql) for every bundled migration, sorted by version."""
    migrations_dir = resources.files("stroummeeschter").joinpath("migrations")
    migrations = []
    for entry in migrations_dir.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        version = int(entry.name.split("_", 1)[0])
        migrations.append((version, entry.name, entry.read_text()))
    return sorted(migrations, key=lambda m: m[0])


def init_db(conn: sqlite3.Connection) -> None:
    """Bring the database up to the latest schema version.

    Schema changes are plain numbered .sql files under migrations/, applied
    in order and tracked via SQLite's built-in PRAGMA user_version. Safe to
    call on every startup - already-applied migrations are skipped.
    """
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, filename, sql in _migrations():
        if version <= current_version:
            continue
        logger.info("Applying migration %s", filename)
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()


def upsert_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    now: str,
    name: str | None = None,
    unit: str | None = None,
    category: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO entities (id, name, unit, category, first_seen, last_seen)
        VALUES (:id, :name, :unit, :category, :now, :now)
        ON CONFLICT (id) DO UPDATE SET
            name = COALESCE(excluded.name, entities.name),
            unit = COALESCE(excluded.unit, entities.unit),
            category = COALESCE(excluded.category, entities.category),
            last_seen = excluded.last_seen
        """,
        {"id": entity_id, "name": name, "unit": unit, "category": category, "now": now},
    )


def insert_reading(
    conn: sqlite3.Connection,
    entity_id: str,
    value: float | str | None,
    recorded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO readings (entity_id, value, recorded_at) VALUES (?, ?, ?)",
        (entity_id, value, recorded_at),
    )
