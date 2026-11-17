-- Schema of the "telemetry" database. Loaded on first start of PostgreSQL.

CREATE TABLE IF NOT EXISTS devices (
    dev_eui     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL,
    last_seen   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS uplinks (
    id             BIGSERIAL PRIMARY KEY,
    dev_eui        TEXT NOT NULL REFERENCES devices (dev_eui),
    received_at    TIMESTAMPTZ NOT NULL,
    f_cnt          BIGINT NOT NULL,
    rssi           INTEGER,
    snr            DOUBLE PRECISION,
    temperature_c  DOUBLE PRECISION,
    humidity_pct   DOUBLE PRECISION,
    battery_mv     INTEGER,
    gateway_id     TEXT,
    UNIQUE (dev_eui, f_cnt, received_at)
);

CREATE INDEX IF NOT EXISTS uplinks_dev_time_idx ON uplinks (dev_eui, received_at);
