#!/bin/sh
# Runs once, on first start of the PostgreSQL volume. Local development only.
# One instance, two databases: "chirpstack" (network server) and "telemetry".
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE EXTENSION IF NOT EXISTS hstore;
    CREATE DATABASE telemetry;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname telemetry -f /schema/schema.sql
