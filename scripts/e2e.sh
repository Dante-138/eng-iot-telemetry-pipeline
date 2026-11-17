#!/usr/bin/env bash
# End-to-end check of the whole bench. Runs locally and in CI.
#
#   scripts/e2e.sh            fresh start (docker compose down -v), run, leave the stack up
#   E2E_DOWN=1 scripts/e2e.sh also tear the stack down at the end
#   REGION=au915_0 scripts/e2e.sh   same scenario on AU915 sub-band 1 (default eu868)
#
# Needs: docker compose v2, curl. Local development credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."
export REGION=${REGION:-eu868}   # read by docker compose (bridge topics, ChirpStack, simulator)

# dev-ok 12 + dev-gap 9 + dev-weak 12 + dev-quiet 8 + dev-battery 12 + dev-otaa 12
# (see telemetry/scenario.py). The gateways also count the OTAA JoinRequest and the 4 copies the second gateway
# hears: 70 frames.
EXPECTED_UPLINKS=65
EXPECTED_FRAMES=70
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

# Frames counted by gateway-tap (radio side), summed over all gateways.
tap_frames() {
  curl -fsS -X POST "$INFLUX_URL/api/v2/query?org=local-org"     -H "Authorization: Token local-dev-token"     -H "Content-Type: application/vnd.flux"     -H "Accept: application/csv"     --data 'from(bucket: "telemetry") |> range(start: -7d) |> filter(fn: (r) => r._measurement == "gateway_frames" and r._field == "frames") |> group() |> sum()'     | tr -d '' | awk -F, '!/^#/ && NF > 1 && $NF ~ /^[0-9]+$/ { print $NF }' | tail -n 1
}

# Alert rule state from Grafana: "inactive", "firing" or "pending".
alert_state() {
  curl -fsS -u admin:admin "$GRAFANA_URL/api/prometheus/grafana/api/v1/rules"     | python3 -c 'import json,sys; r=[r for g in json.load(sys.stdin)["data"]["groups"] for r in g["rules"] if r["name"]=="Radio OK, database stalled"][0]; print(r["state"] if r["health"]=="ok" else "error:"+r["health"])'
}

# Predicates for wait_for: they query again on every retry.
pg_full() { [ "$(pg_count)" = "$EXPECTED_UPLINKS" ]; }
tap_full() { [ "$(tap_frames)" = "$EXPECTED_FRAMES" ]; }
alert_idle() { [ "$(alert_state)" = inactive ]; }
influx_full() { [ "$(influx_count)" = "$EXPECTED_UPLINKS" ]; }

step "Fresh start"
docker compose down -v --remove-orphans

step "docker compose up"
docker compose up -d --build --wait

step "Provision ChirpStack (gateway, application, profile, ABP devices)"
docker compose run --rm -T provision

step "Run the simulator scenario (dev-otaa joins, and gets a downlink queued through the API)"
sim_log=$(mktemp)
SIM_EXPECT_DOWNLINK=1 docker compose run --rm -T simulator > "$sim_log" 2>&1 &
sim_pid=$!
wait_for "dev-otaa join" 120 grep -q 'joined dev-otaa' "$sim_log"
docker compose run --rm -T provision python -m provision.enqueue
sim_status=0
wait "$sim_pid" || sim_status=$?
tail -n 8 "$sim_log"
[ "$sim_status" = 0 ] || fail "simulator exited with status $sim_status (join or downlink missing)"
grep -Eq ', 0 without PUSH_ACK' "$sim_log" || fail "gateway bridge did not acknowledge every PUSH_DATA"
grep -q 'downlink dev-otaa .*text=bench-downlink' "$sim_log" || fail "dev-otaa did not decrypt the queued downlink"
echo "dev-otaa joined, uplinks acknowledged, downlink decrypted: $(grep -o 'text=bench-downlink' "$sim_log" | head -n 1)"

step "Wait for $EXPECTED_UPLINKS rows in PostgreSQL uplinks"
wait_for "uplinks rows" 90 pg_full
echo "uplinks rows: $(pg_count)"

step "Two gateways: one row per uplink, gateway_count 2 only where both heard it, best reception stored"
dups=$(docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT count(*) - count(DISTINCT (dev_eui, f_cnt)) FROM uplinks" | tr -d '')
[ "$dups" = 0 ] || fail "$dups duplicated uplink row(s)"
dual=$(docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT string_agg(f_cnt || ':' || gateway_id || ':' || rssi, ',' ORDER BY f_cnt) FROM uplinks u JOIN devices d USING (dev_eui) WHERE d.name = 'dev-ok' AND u.gateway_count = 2" | tr -d '')
want=$(PYTHONPATH=. python3 -c "from telemetry import scenario; print(','.join(f'{f}:{g}:{r}' for f, g, r in scenario.dual_gateway_rows()))")
[ "$dual" = "$want" ] || fail "rows with gateway_count 2 are '$dual', expected '$want'"
others=$(docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT count(*) FROM uplinks WHERE gateway_count <> 1" | tr -d '')
[ "$others" = "$(tr ',' '
' <<< "$want" | wc -l | tr -d ' ')" ] || fail "gateway_count <> 1 on $others row(s)"
echo "no duplicates; gateway_count 2 and best reception (fCnt:gateway:rssi): $dual"

step "Nothing parked in failed_uplinks, replay has nothing to do"
parked=$(docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "SELECT count(*) FROM failed_uplinks" | tr -d '\r')
[ "$parked" = 0 ] || fail "failed_uplinks has $parked row(s)"
# Capture first: grep -q closes the pipe early and pipefail would turn the SIGPIPE into a failure.
replay=$(docker compose run --rm -T ingest python -m ingest.replay)
grep -q 'replayed 0, still failing 0' <<< "$replay" || fail "replay output"
ingest_log=$(docker compose logs ingest)
grep -q 'session present' <<< "$ingest_log" || fail "ingest did not log its MQTT session"

step "Check InfluxDB points"
wait_for "influx points" 60 influx_full
echo "influx points: $(influx_count)"

step "Gateway tap and the alert rule"
wait_for "gateway-tap frames" 60 tap_full
echo "frames counted at the gateway: $(tap_frames)"
wait_for "alert rule 'Radio OK, database stalled' inactive and healthy" 90 alert_idle
echo "alert rule: $(alert_state)"

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
grep -Eq '^dev-otaa +PASS' <<< "$report" || fail "dev-otaa should PASS"
[ "$(echo "$report" | grep -c -E '^dev-')" = 6 ] || fail "expected exactly 6 verdicts"

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
