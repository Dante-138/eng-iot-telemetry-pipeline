#!/usr/bin/env bash
# End-to-end check of the whole bench. Runs locally and in CI.
#
#   scripts/e2e.sh            fresh start (docker compose down -v), run, leave the stack up
#   E2E_DOWN=1 scripts/e2e.sh also tear the stack down at the end
#
# Needs: docker compose v2, curl. Local development credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."

EXPECTED_UPLINKS=33   # dev-ok 12 + dev-gap 9 + dev-weak 12 (see telemetry/scenario.py)
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

step "Fresh start"
docker compose down -v --remove-orphans

step "docker compose up"
docker compose up -d --build --wait

step "Provision ChirpStack (gateway, application, profile, 3 ABP devices)"
docker compose run --rm -T provision

step "Run the simulator scenario"
sim_out=$(docker compose run --rm -T simulator)
echo "$sim_out" | tail -n 3
echo "$sim_out" | grep -q "0 without PUSH_ACK" || fail "gateway bridge did not acknowledge every PUSH_DATA"

step "Wait for $EXPECTED_UPLINKS rows in PostgreSQL uplinks"
wait_for "uplinks rows" 90 test "$(pg_count)" = "$EXPECTED_UPLINKS"
echo "uplinks rows: $(pg_count)"

step "Check InfluxDB points"
wait_for "influx points" 60 test "$(influx_count)" = "$EXPECTED_UPLINKS"
echo "influx points: $(influx_count)"

step "Approval report"
report=$(docker compose run --rm -T approve)
echo "$report"
echo "$report" | grep -Eq '^dev-ok +PASS' || fail "dev-ok should PASS"
echo "$report" | grep -Eq '^dev-gap +FAIL.*fcnt gaps' || fail "dev-gap should FAIL with fcnt gaps"
echo "$report" | grep -Eq '^dev-weak +FAIL.*rssi' || fail "dev-weak should FAIL with rssi"
[ "$(echo "$report" | grep -c -E '^dev-')" = 3 ] || fail "expected exactly 3 verdicts"

step "Grafana"
wait_for "grafana health" 60 curl -fsS "$GRAFANA_URL/api/health"
wait_for "dashboard listed" 60 bash -c "curl -fsS -u admin:admin '$GRAFANA_URL/api/search?query=Telemetry' | grep -q 'Telemetry overview'"
for uid in pg-telemetry influx-telemetry; do
  curl -fsS -u admin:admin "$GRAFANA_URL/api/datasources/uid/$uid/health" | grep -q '"status":"OK"' \
    || fail "Grafana datasource $uid is not healthy"
done
echo "dashboard 'Telemetry overview' and both datasources are up"

if [ "${E2E_DOWN:-0}" = 1 ]; then
  step "Tear down"
  docker compose down -v
fi

printf '\nE2E OK\n'
