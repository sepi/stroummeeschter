CREATE TABLE entities (
    id TEXT PRIMARY KEY,           -- e.g. "sensor-power_consumed"
    name TEXT,
    unit TEXT,                     -- SI base unit after normalization (W, Wh, V, A, ...); NULL for text sensors
    category INTEGER,              -- ESPHome entity_category: 0 primary, 2 diagnostic
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id TEXT NOT NULL REFERENCES entities (id),
    value,                          -- REAL, normalized to its SI base unit (kW->W, kWh->Wh) for numeric
                                     -- sensors; TEXT for text sensors (identification strings, SSIDs, ...);
                                     -- NULL when the meter reports "NA". No column affinity: deliberately
                                     -- untyped since both REAL and TEXT are legitimate contents here.
    recorded_at TEXT NOT NULL      -- ISO 8601 UTC receipt time (device does not expose meter-side timestamps)
);

CREATE INDEX idx_readings_entity_time ON readings (entity_id, recorded_at);
