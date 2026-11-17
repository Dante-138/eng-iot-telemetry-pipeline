#!/usr/bin/env bash
# End-to-end check of the whole bench. Runs locally and in CI.
#
#   scripts/e2e.sh            fresh start (docker compose down -v), run, leave the stack up
#   E2E_DOWN=1 scripts/e2e.sh also tear the stack down at the end
#
# Needs: docker compose v2, curl. Local development credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."

# dev-ok 12 + dev-gap 9 + dev-weak 12 + dev-quiet 8 + dev-battery 12 (see telemetry/scenario.py)
EXPECTED_UPLINKS=53
INFLUX_URL=${INFLUX_URL:-http://localhost:8086}
GRAFANA_URL=${GRAFANA_URL:-http://localhost:3000}

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'E2E FAIL: %s\n' "$*" >&2; exit 1; }

# wait_for <description> <timeout seconds> <command...>
wait_for() {
  local what=$1 timeout=$2; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@" > /dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "timeout waiting for $what"
    sleep 2
  done
}

pg_count() {
  docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT count(*) FROM uplinks" | tr -d '\r'
}

influx_count() {
  curl -fsS -X POST "$INFLUX_URL/api/v2/query?org=local-org" \
    -H "Authorization: Token local-dev-token" \
    -H "Content-Type: application/vnd.flux" \
    -H "Accept: application/csv" \
    --data 'from(bucket: "telemetry") |> range(start: -7d) |> filter(fn: (r) => r._measurement == "uplink" and r._field == "f_cnt") |> group() |> count()' \
    | tr -d '\r' | awk -F, '!/^#/ && NF > 1 && $NF ~ /^[0-9]+$/ { print $NF }' | tail -n 1
}

# Predicates for wait_for: they query again on every retry.
pg_full() { [ "$(pg_count)" = "$EXPECTED_UPLINKS" ]; }
influx_full() { [ "$(influx_count)" = "$EXPECTED_UPLINKS" ]; }

step "Fresh start"
docker compose down -v --remove-orphans

step "docker compose up"
docker compose up -d --build --wait

step "Provision ChirpStack (gateway, application, profile, ABP devices)"
docker compose run --rm -T provision

step "Run the simulator scenario"
sim_out=$(docker compose run --rm -T simulator 2>&1)
echo "$sim_out" | tail -n 3
grep -Eq ', 0 without PUSH_ACK' <<< "$sim_out" || fail "gateway bridge did not acknowledge every PUSH_DATA"

step "Wait for $EXPECTED_UPLINKS rows in PostgreSQL uplinks"
wait_for "uplinks rows" 90 pg_full
echo "uplinks rows: $(pg_count)"

step "Nothing parked in failed_uplinks, replay has nothing to do"
parked=$(docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT count(*) FROM failed_uplinks" | tr -d '\r')
[ "$parked" = 0 ] || fail "failed_uplinks has $parked row(s)"
docker compose run --rm -T ingest python -m ingest.replay | grep -q 'replayed 0, still failing 0' || fail "replay output"
docker compose logs ingest | grep -q 'session present' || fail "ingest did not log its MQTT session"

step "Check InfluxDB points"
wait_for "influx points" 60 influx_full
echo "influx points: $(influx_count)"

step "Provisioning is idempotent (a second run must not activate the devices again)"
second=$(docker compose run --rm -T provision 2>&1)
grep -q 'already activated' <<< "$second" || fail "second provision run did not skip the activation"
grep -Eq ' activated \(DevAddr' <<< "$(grep -v 'already activated' <<< "$second")"   && fail "second provision run activated a device again"

step "Approval report (config/thresholds.e2e.toml: max_interval_s 5)"
report=$(docker compose run --rm -T -e THRESHOLDS=config/thresholds.e2e.toml approve)
echo "$report"
grep -Eq '^dev-ok +PASS' <<< "$report" || fail "dev-ok should PASS"
grep -Eq '^dev-gap +FAIL.*fcnt gaps' <<< "$report" || fail "dev-gap should FAIL with fcnt gaps"
grep -Eq '^dev-weak +FAIL.*rssi' <<< "$report" || fail "dev-weak should FAIL with rssi"
grep -Eq '^dev-quiet +FAIL +time gaps: 1 interval' <<< "$report" || fail "dev-quiet should FAIL with a time gap"
grep -Eq '^dev-battery +FAIL +battery drop' <<< "$report" || fail "dev-battery should FAIL with battery drop"
[ "$(echo "$report" | grep -c -E '^dev-')" = 5 ] || fail "expected exactly 5 verdicts"

step "Grafana"
wait_for "grafana health" 60 curl -fsS "$GRAFANA_URL/api/health"
wait_for "dashboard listed" 60 bash -c "curl -fsS -u admin:admin '$GRAFANA_URL/api/search?query=Telemetry' | grep -q 'Telemetry overview'"
for uid in pg-telemetry influx-telemetry; do
  health=$(curl -fsS -u admin:admin "$GRAFANA_URL/api/datasources/uid/$uid/health")
  grep -q '"status":"OK"' <<< "$health" || fail "Grafana datasource $uid is not healthy: $health"
done
echo "dashboard 'Telemetry overview' and both datasources are up"

if [ "${E2E_DOWN:-0}" = 1 ]; then
  step "Tear down"
  docker compose down -v
fi

printf '\nE2E OK\n'
