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

-- Dead letter table of the ingest service: an up event that could not be stored after the
-- retries is kept here, raw, with the error. "python -m ingest.replay" reprocesses it.
CREATE TABLE IF NOT EXISTS failed_uplinks (
    id           BIGSERIAL PRIMARY KEY,
    failed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    topic        TEXT NOT NULL,
    payload      BYTEA NOT NULL,
    error        TEXT NOT NULL,
    attempts     INTEGER NOT NULL,
    replayed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS failed_uplinks_pending_idx ON failed_uplinks (id) WHERE replayed_at IS NULL;
