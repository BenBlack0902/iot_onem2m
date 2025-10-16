#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# scripts/e2e-watch.sh (demo mode)
# - Creates a temporary subscription on the telemetry container that notifies both mood and ingest
# - Posts a ContentInstance into the CSE (telemetry container)
# - Tails mood + ingest logs (background)
# - Polls CSE /la until a new mood CIN appears (or timeout)
# - Prints DB rows for inspection
# - Removes the temporary subscription on exit
#
# Usage: ./scripts/e2e-watch.sh [--timeout seconds] [--co2 value] [--demo]
#   --demo : enable creating a temporary subscription that notifies both mood and ingest
# Examples:
#   ./scripts/e2e-watch.sh --timeout 30 --co2 750 --demo

CSE_BASE="${CSE_BASE:-http://localhost:8080/cse-in}"
MOOD_HOST="${MOOD_HOST:-http://localhost:8088}"
INGEST_HOST="${INGEST_HOST:-http://localhost:8089}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-onem2m_postgres}"
TIMEOUT=60
CO2_VAL=750
DEMO=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2;;
    --co2) CO2_VAL="$2"; shift 2;;
    --demo) DEMO=true; shift 1;;
    -h|--help) echo "Usage: $0 [--timeout seconds] [--co2 value] [--demo]"; exit 0;;
    *) echo "Unknown arg: $1"; exit 2;;
  esac
done

TS=$(date +%s)
RI="e2e-watch-${TS}"
RVI="3"
ORIGIN="CAdmin"

LA_URL="${CSE_BASE}/cloud-analytics/analytics/mood/score/la"
CSE_TELE_URL="${CSE_BASE}/cloud-analytics/telemetry/room-101/sample?ty=4"
CSE_SUB_BASE="${CSE_BASE}/cloud-analytics/telemetry/room-101/sample"

SUB_RN=""
cleanup_subscription() {
  if [ -n "${SUB_RN}" ]; then
    echo "Deleting temporary subscription ${SUB_RN}..."
    curl -sS -X DELETE -H "X-M2M-Origin: ${ORIGIN}" -H "X-M2M-RI: del-$(date +%s)" -H "X-M2M-RVI: ${RVI}" "${CSE_SUB_BASE}/${SUB_RN}" >/dev/null || true
    echo "Subscription delete requested."
  fi
}

trap cleanup_subscription EXIT

echo "E2E watch starting (timeout=${TIMEOUT}s)."
echo "CSE LA URL: ${LA_URL}"
echo "Telemetry POST URL: ${CSE_TELE_URL}"
echo

if [ "${DEMO}" = true ]; then
  echo "Creating temporary multi-target subscription (mood + ingest) on telemetry container..."
  SUB_RN="sub-e2e-demo-${TS}"
  cat > /tmp/sub_payload_demo.json <<JSON
{
  "m2m:sub": {
    "rn": "${SUB_RN}",
    "nu": ["http://mood:8088/notify","http://ingest:8088/onem2m"],
    "nct": 1,
    "enc": {"net": [3]}
  }
}
JSON
  # create subscription
  resp=$(curl -sS -i -X POST "${CSE_SUB_BASE}?ty=23" \
    -H "Content-Type: application/vnd.onem2m-res+json;ty=23" \
    -H "X-M2M-Origin: ${ORIGIN}" \
    -H "X-M2M-RI: sub-${TS}" \
    -H "X-M2M-RVI: ${RVI}" \
    --data-binary @/tmp/sub_payload_demo.json || true)
  echo "Subscription create response:"
  echo "${resp}"
  echo
  # Note: cleanup_subscription trap will remove it on exit
fi

# Helper to get latest mood ri from CSE /la (returns empty if none)
get_latest_ri() {
  curl -sS -H "X-M2M-RI: check-$(date +%s)" -H "X-M2M-RVI: ${RVI}" -H "X-M2M-Origin: ${ORIGIN}" "${LA_URL}" \
    | jq -r 'if .["m2m:cin"] then .["m2m:cin"].ri elif .ri then .ri elif .con then "unknown" else empty end' 2>/dev/null || true
}

# Capture previous latest RI (so we can detect a new one)
prev_ri=$(get_latest_ri)
echo "Previous latest mood RI: ${prev_ri:-<none>}"
echo

# Start tailing mood and ingest logs into temp files and background processes
MOOD_LOG=$(mktemp /tmp/mood-logs.XXXXXX)
INGEST_LOG=$(mktemp /tmp/ingest-logs.XXXXXX)
echo "Tailing mood logs to ${MOOD_LOG} and ingest logs to ${INGEST_LOG} (background)..."
docker-compose logs --no-color --tail=0 -f mood > "${MOOD_LOG}" 2>&1 &
MOOD_TAIL_PID=$!
docker-compose logs --no-color --tail=0 -f ingest > "${INGEST_LOG}" 2>&1 &
INGEST_TAIL_PID=$!
# Give tails a moment to start
sleep 0.5

# Post a CI to the CSE (omit rn so CSE assigns it)
cat > /tmp/e2e_full_ci.json <<JSON
{
  "m2m:cin": {
    "con": {
      "device": "dev-e2e-demo",
      "room": "room-101",
      "metrics": [
        {"name":"co2","value":${CO2_VAL}},
        {"name":"noise","value":38},
        {"name":"lux","value":450},
        {"name":"temp","value":23.0},
        {"name":"rh","value":42},
        {"name":"occ","value":2}
      ]
    }
  }
}
JSON

echo "Posting CI to CSE (RI=${RI}, RVI=${RVI})..."
curl -sS -i -X POST "${CSE_TELE_URL}" \
  -H "Content-Type: application/vnd.onem2m-res+json;ty=4" \
  -H "X-M2M-Origin: ${ORIGIN}" \
  -H "X-M2M-RI: ${RI}" \
  -H "X-M2M-RVI: ${RVI}" \
  --data-binary @/tmp/e2e_full_ci.json || true
echo
echo "Posted CI, now waiting for mood CIN to appear (timeout ${TIMEOUT}s)..."
echo

start_ts=$(date +%s)
found_ri=""
while true; do
  now=$(date +%s)
  elapsed=$((now - start_ts))
  if (( elapsed > TIMEOUT )); then
    echo "Timed out after ${TIMEOUT}s waiting for a new mood CIN."
    break
  fi

  latest_ri=$(get_latest_ri)
  if [ -n "${latest_ri}" ] && [ "${latest_ri}" != "${prev_ri}" ]; then
    echo "Detected new mood RI: ${latest_ri}"
    found_ri="${latest_ri}"
    break
  fi

  # Also scan the mood log for evidence of posting
  if grep -q "Posting mood CIN to" "${MOOD_LOG}" 2>/dev/null; then
    echo "Mood service log shows it posted a mood CIN (check logs below)."
  fi

  # Check ingest log for receipt evidence
  if grep -q "ingest: received ci" "${INGEST_LOG}" 2>/dev/null; then
    echo "Ingest log shows it received a CI (check logs below)."
  fi

  sleep 1
done

echo
echo "---- Mood service logs (last 200 lines) ----"
tail -n 200 "${MOOD_LOG}" || true
echo "---- end mood logs ----"
echo

echo
echo "---- Ingest service logs (last 200 lines) ----"
tail -n 200 "${INGEST_LOG}" || true
echo "---- end ingest logs ----"
echo

if [ -n "${found_ri}" ]; then
  echo "Fetching the mood CIN from CSE /la:"
  curl -sS -H "X-M2M-RI: fetch-$(date +%s)" -H "X-M2M-RVI: ${RVI}" -H "X-M2M-Origin: ${ORIGIN}" "${LA_URL}" | jq . || true
else
  echo "No new mood CIN detected in CSE /la within timeout."
fi

echo
echo "---- Postgres: raw_onem2m_ci (last 10 rows) ----"
docker exec -i "${POSTGRES_CONTAINER}" psql -U onem2m -d onem2m -c "SELECT ci_rn, parent_path, created_at FROM raw_onem2m_ci ORDER BY created_at DESC LIMIT 10;" || true
echo
echo "---- Postgres: fact_mood_scores (last 5 rows) ----"
docker exec -i "${POSTGRES_CONTAINER}" psql -U onem2m -d onem2m -c "SELECT ts, score, label FROM fact_mood_scores ORDER BY ts DESC LIMIT 5;" || true
echo

# Clean up
echo "Stopping log tails (pids ${MOOD_TAIL_PID}, ${INGEST_TAIL_PID})..."
kill "${MOOD_TAIL_PID}" >/dev/null 2>&1 || true
kill "${INGEST_TAIL_PID}" >/dev/null 2>&1 || true
rm -f "${MOOD_LOG}" "${INGEST_LOG}" /tmp/e2e_full_ci.json /tmp/sub_payload_demo.json /tmp/sub_payload_fixed.json /tmp/sub_payload_fixed2.json
echo "Done. If demo subscription was created it will be removed on exit."
