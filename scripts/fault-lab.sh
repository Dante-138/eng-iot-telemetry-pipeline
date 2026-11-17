#!/usr/bin/env bash
# Fault lab: inject one failure at a time, prove which layer the data stopped at, go back to
# normal and prove the pipeline works again.
#
#   scripts/fault-lab.sh                      all faults, in order
#   scripts/fault-lab.sh bad-mic broker-down  only these
#   LAB_FRESH=1 scripts/fault-lab.sh          start from docker compose down -v
#
# Faults: gateway-unknown, bad-mic, broker-down, ingest-down, db-reject.
# Every fault sends a burst of 5 uplinks of dev-ok and "taps" each hop of the pipeline:
#   gateway topic   mosquitto_sub on <region>/gateway/+/event/up   (bridge -> broker)
#   app topic       mosquitto_sub on application/+/device/+/event/up (ChirpStack -> broker)
#   database        rows in uplinks and in failed_uplinks
#   logs            the line of the component that explains the stop
# Needs: docker compose v2, curl, python3. Local development credentials only.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=${REGION:-eu868}
GRAFANA_URL=${GRAFANA_URL:-http://localhost:3000}
BURST=5
WINDOW=14   # seconds the topic taps listen
TMP=$(mktemp -d)
RESULTS=()

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAULT LAB FAIL: %s\n' "$*" >&2; exit 1; }
strip_ansi() { sed 's/\x1b\[[0-9;]*m//g'; }

# wait_for <description> <timeout seconds> <command...>
wait_for() {
  local what=$1 timeout=$2; shift 2
  local deadline=$((SECONDS + timeout))
  until "$@" > /dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "timeout waiting for $what"
    sleep 2
  done
}

psql_t() { docker compose exec -T postgres psql -U chirpstack -d telemetry -Atc "$1" | tr -d '\r'; }
uplink_rows() { psql_t "SELECT count(*) FROM uplinks"; }
parked_rows() { psql_t "SELECT count(*) FROM failed_uplinks WHERE replayed_at IS NULL"; }
now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# log_hits <since> <pattern> <services...>: lines matching the pattern, ANSI colours removed
log_hits() {
  local since=$1 pattern=$2; shift 2
  docker compose logs --no-color --since "$since" "$@" 2>&1 | strip_ansi | grep -c -F -e "$pattern" || true
}
alert_state() {
  curl -fsS -u admin:admin "$GRAFANA_URL/api/prometheus/grafana/api/v1/rules" \
    | python3 -c 'import json,sys; r=[r for g in json.load(sys.stdin)["data"]["groups"] for r in g["rules"] if r["name"]=="Radio OK, database stalled"][0]; print(r["state"] if r["health"]=="ok" else "error:"+r["health"])'
}

# sub_bg <file> <topic>: tap a topic for $WINDOW seconds in the background, one line per message
sub_bg() {
  docker compose exec -T mosquitto mosquitto_sub -t "$2" -F '%t' -W "$WINDOW" > "$1" 2>/dev/null || true
}

# burst [VAR=value ...]: send $BURST uplinks of dev-ok and tap the two topics meanwhile.
# Sets GW_FRAMES, APP_EVENTS and ROWS_ADDED, SIM_ACKS (PUSH_ACK lines) and T0 (start time).
burst() {
  local before; before=$(uplink_rows)
  T0=$(now_utc)
  rm -f "$TMP/gw" "$TMP/app"
  local pids=()
  if [ "${TAP:-1}" = 1 ]; then
    sub_bg "$TMP/gw" "$REGION/gateway/+/event/up" & pids+=($!)
    sub_bg "$TMP/app" "application/+/device/+/event/up" & pids+=($!)
    sleep 1
  fi
  local envs=()
  for kv in "$@"; do envs+=(-e "$kv"); done
  SIM_OUT=$(docker compose run --rm -T -e SIM_DEVICE=dev-ok -e SIM_COUNT=$BURST "${envs[@]}" simulator 2>&1)
  SIM_ACKS=$(grep -o '[0-9]* without PUSH_ACK' <<< "$SIM_OUT" | cut -d' ' -f1)
  [ "${#pids[@]}" -eq 0 ] || wait "${pids[@]}"
  GW_FRAMES=n/a APP_EVENTS=n/a
  if [ "${TAP:-1}" = 1 ]; then
    GW_FRAMES=$(grep -c . "$TMP/gw" || true)
    APP_EVENTS=$(grep -c . "$TMP/app" || true)
  fi
  ROWS_ADDED=$(( $(uplink_rows) - before ))
}

expect() { # expect <label> <actual> <expected>
  [ "$2" = "$3" ] || fail "$FAULT: $1 is $2, expected $3"
}

record() { # record <fault> <layer where it stopped> <evidence>
  RESULTS+=("| $1 | $2 | gateway topic $GW_FRAMES, app topic $APP_EVENTS, db rows +$ROWS_ADDED | $3 |")
  printf 'result: gateway topic %s, app topic %s, db rows +%s | %s\n' \
    "$GW_FRAMES" "$APP_EVENTS" "$ROWS_ADDED" "$3"
}

normal_check() { # proves the pipeline is back to normal
  FAULT="$FAULT (recovery)"
  burst
  expect "gateway topic frames" "$GW_FRAMES" "$BURST"
  expect "app topic events" "$APP_EVENTS" "$BURST"
  expect "db rows added" "$ROWS_ADDED" "$BURST"
  printf 'back to normal: %s frames, %s events, +%s rows\n' "$GW_FRAMES" "$APP_EVENTS" "$ROWS_ADDED"
}

cleanup() {
  docker compose start mosquitto ingest > /dev/null 2>&1 || true
  psql_t "ALTER TABLE uplinks DROP CONSTRAINT IF EXISTS lab_reject" > /dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

fault_gateway_unknown() {
  step "gateway-unknown: frames come from a gateway ChirpStack does not know"
  burst SIM_GATEWAY_EUI=ffffffffffffff01
  local hits; hits=$(log_hits "$T0" "Getting gateway meta-data failed" chirpstack)
  expect "gateway topic frames" "$GW_FRAMES" "$BURST"
  expect "app topic events" "$APP_EVENTS" 0
  expect "db rows added" "$ROWS_ADDED" 0
  [ "$hits" -ge 1 ] || fail "$FAULT: no 'Getting gateway meta-data failed' in the ChirpStack log"
  record gateway-unknown "4 ChirpStack (bridge published, nothing on the app topic)" \
    "chirpstack log: 'Getting gateway meta-data failed' x$hits"
  normal_check
}

fault_bad_mic() {
  step "bad-mic: a device signs its frames with the wrong key"
  burst SIM_WRONG_KEY=1
  local hits; hits=$(log_hits "$T0" "resulted in valid MIC" chirpstack)
  expect "gateway topic frames" "$GW_FRAMES" "$BURST"
  expect "app topic events" "$APP_EVENTS" 0
  expect "db rows added" "$ROWS_ADDED" 0
  [ "$hits" -ge 1 ] || fail "$FAULT: no MIC error in the ChirpStack log"
  record bad-mic "4 ChirpStack (MIC check)" \
    "chirpstack log: 'None of the device-sessions for dev_addr resulted in valid MIC' x$hits"
  normal_check
}

fault_broker_down() {
  step "broker-down: Mosquitto is stopped"
  local t_fault; t_fault=$(now_utc)
  docker compose stop mosquitto > /dev/null
  TAP=0 burst
  local refused; refused=$(log_hits "$t_fault" "mqtt: connection error" chirpstack-gateway-bridge)
  expect "simulator frames without PUSH_ACK" "$SIM_ACKS" 0   # the bridge still acknowledges UDP
  expect "db rows added" "$ROWS_ADDED" 0
  [ "$refused" -ge 1 ] || fail "$FAULT: no 'mqtt: connection error' in the bridge log"
  record broker-down "2/3 bridge to broker (UDP is acknowledged, MQTT is not)" \
    "bridge log: 'mqtt: connection error' and 'dial tcp ...:1883: connect: connection refused' (x$refused); simulator: $SIM_ACKS frames without PUSH_ACK"
  local t1; t1=$(now_utc)
  docker compose up -d --wait mosquitto > /dev/null
  wait_for "bridge reconnected" 90 bash -c "[ \"\$(docker compose logs --no-color --since $t1 chirpstack-gateway-bridge 2>&1 | grep -c 'connected to mqtt broker')\" -ge 1 ]"
  wait_for "ingest reconnected" 90 bash -c "[ \"\$(docker compose logs --no-color --since $t1 ingest 2>&1 | grep -c 'MQTT connected')\" -ge 1 ]"
  sleep 5
  local after; after=$(uplink_rows)
  printf 'frames sent while the broker was down are not recovered (QoS 0 on the radio side): rows %s\n' "$after"
  normal_check
}

fault_ingest_down() {
  step "ingest-down: the ingest service is stopped, messages wait in the broker session"
  docker compose stop ingest > /dev/null
  local before; before=$(uplink_rows)
  burst
  expect "gateway topic frames" "$GW_FRAMES" "$BURST"
  expect "app topic events" "$APP_EVENTS" "$BURST"
  expect "db rows added" "$ROWS_ADDED" 0
  record ingest-down "6 ingest (the broker holds the events)" \
    "ingest container stopped; events queued in the persistent session"
  local t1; t1=$(now_utc)
  docker compose start ingest > /dev/null
  rows_back() { [ "$(uplink_rows)" -ge $((before + BURST)) ]; }
  wait_for "queued messages stored after ingest restart" 90 rows_back
  sleep 3
  expect "rows stored after the restart (no loss, no duplicate)" "$(( $(uplink_rows) - before ))" "$BURST"
  local resumed; resumed=$(log_hits "$t1" "session present: True" ingest)
  [ "$resumed" -ge 1 ] || fail "$FAULT: ingest did not resume the persistent session"
  printf 'ingest log: "session present: True"; +%s rows arrived from the session\n' "$BURST"
  normal_check
}

fault_db_reject() {
  step "db-reject: PostgreSQL refuses the writes to uplinks"
  local parked_before; parked_before=$(parked_rows)
  psql_t "ALTER TABLE uplinks ADD CONSTRAINT lab_reject CHECK (false) NOT VALID" > /dev/null
  burst
  parked_ok() { [ "$(parked_rows)" -ge $((parked_before + BURST)) ]; }
  wait_for "messages parked in failed_uplinks" 90 parked_ok
  expect "gateway topic frames" "$GW_FRAMES" "$BURST"
  expect "app topic events" "$APP_EVENTS" "$BURST"
  expect "db rows added" "$ROWS_ADDED" 0
  firing() { [ "$(alert_state)" = firing ]; }
  # frames at the gateway, no row in uplinks in the same window: the alert fires
  if ! firing; then
    # the window may have moved on while the retries ran: send one more burst and look again
    TAP=0 burst
    wait_for "alert 'Radio OK, database stalled' firing" 60 firing
  fi
  local errors; errors=$(log_hits "$T0" "parked" ingest)
  record db-reject "6 ingest to database (rows parked, alert firing)" \
    "failed_uplinks +$BURST, ingest log 'parked ... in failed_uplinks' x$errors, grafana alert: firing"
  psql_t "ALTER TABLE uplinks DROP CONSTRAINT lab_reject" > /dev/null
  docker compose run --rm -T ingest python -m ingest.replay | tee "$TMP/replay"
  grep -q 'still failing 0' "$TMP/replay" || fail "$FAULT: replay left failing rows"
  expect "pending rows in failed_uplinks after replay" "$(parked_rows)" "$parked_before"
  idle() { [ "$(alert_state)" = inactive ]; }
  wait_for "alert back to inactive" 90 idle
  printf 'replay recovered the parked rows; alert: %s\n' "$(alert_state)"
  normal_check
}

step "Start the stack and provision it"
[ "${LAB_FRESH:-0}" != 1 ] || docker compose down -v --remove-orphans
docker compose up -d --build --wait
docker compose run --rm -T provision > /dev/null

FAULT=control
step "control: no fault, to prove the taps see a healthy pipeline"
burst
expect "gateway topic frames" "$GW_FRAMES" "$BURST"
expect "app topic events" "$APP_EVENTS" "$BURST"
expect "db rows added" "$ROWS_ADDED" "$BURST"
printf 'result: gateway topic %s, app topic %s, db rows +%s\n' "$GW_FRAMES" "$APP_EVENTS" "$ROWS_ADDED"

if [ "$#" -eq 0 ]; then set -- gateway-unknown bad-mic broker-down ingest-down db-reject; fi
for fault in "$@"; do
  FAULT=$fault
  case "$fault" in
    gateway-unknown) fault_gateway_unknown ;;
    bad-mic) fault_bad_mic ;;
    broker-down) fault_broker_down ;;
    ingest-down) fault_ingest_down ;;
    db-reject) fault_db_reject ;;
    *) fail "unknown fault '$fault'" ;;
  esac
done

step "Summary (symptom -> layer -> evidence)"
printf '%s\n' "${RESULTS[@]}"
printf '\nFAULT LAB OK\n'
