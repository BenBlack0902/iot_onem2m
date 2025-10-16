#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# Simple E2E smoke test for the iot_onem2m project
# - Starts services with docker-compose (assumes docker & docker-compose are available)
# - Waits for key HTTP ports
# - Exercises ingest -> DB and mood -> CSE -> DB flows
# - Exits non-zero on failure
#
# Usage: ./scripts/smoke-test.sh
# Notes:
# - Script will source .env if present to pick up credentials and CSE_BASE.
# - This script does not change containers; it only starts them and queries them.
# - If you prefer not to bring up containers, run the curl/psql commands manually.

# Load .env if present
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -o allexport
  # shellcheck disable=SC1091
  source .env
  set +o allexport
fi

# Defaults (in case env missing)
POSTGRES_USER="${POSTGRES_USER:-onem2m}"
POSTGRES_DB="${POSTGRES_DB:-onem2m}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-onem2m_postgres}"

CSE_BASE="${CSE_BASE:-http://localhost:8080/cse-in}"
MOOD_HOST="${MOOD_HOST:-http://localhost:8088}"
INGEST_HOST="${INGEST_HOST:-http://localhost:8089}"

# If .env set CSE_BASE to internal docker hostnames, try to normalize to localhost for host-side checks
if [[ "${CSE_BASE}" == *"acme-onem2m-cse"* ]] || [[ "${CSE_BASE}" == *"acme:8080"* ]]; then
  CSE_HOST_URL="http://localhost:8080/cse-in"
else
  CSE_HOST_URL="${CSE_BASE}"
fi

echo "Using:"
echo "  CSE_HOST_URL = ${CSE_HOST_URL}"
echo "  MOOD_HOST    = ${MOOD_HOST}"
echo "  INGEST_HOST  = ${INGEST_HOST}"
echo "  POSTGRES container = ${POSTGRES_CONTAINER}"
echo

# Helper: wait for TCP port to be open on localhost
wait_for_port() {
  local port=$1
  local timeout=${2:-60}
  local start_ts
  start_ts=$(date +%s)
  echo "Waiting for localhost:${port} to be available (timeout ${timeout}s)..."
  while true; do
    if bash -c "cat < /dev/tcp/localhost/${port}" >/dev/null 2>&1; then
      echo "localhost:${port} is accepting connections"
      return 0
    fi
    if (( $(date +%s) - start_ts > timeout )); then
      echo "Timed out waiting for localhost:${port}"
      return 1
    fi
    sleep 1
  done
}

# Preferred helper: wait for HTTP endpoint to return 200 (useful for web UIs)
wait_for_http() {
  local url="$1"
  local timeout=${2:-60}
  local start_ts
  start_ts=$(date +%s)
  echo "Waiting for ${url} to return HTTP 200 (timeout ${timeout}s)..."
  while true; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "${url}" || true)
    if [ "${code}" = "200" ] || [ "${code}" = "301" ] || [ "${code}" = "302" ]; then
      echo "${url} returned HTTP ${code}"
      return 0
    fi
    # Fallback: if the host:port is accepting TCP, consider the service up (useful in some environments)
    host=$(echo "${url}" | sed -n 's|^[^:]*://\([^:/]*\).*|\1|p')
    port=$(echo "${url}" | sed -n 's|^[^:]*://[^:/]*:\([0-9]*\).*|\1|p')
    if [ -n "${port}" ] && bash -c "cat < /dev/tcp/${host}/${port}" >/dev/null 2>&1; then
      echo "TCP ${host}:${port} is open (HTTP not 200 yet)"
      return 0
    fi
    if (( $(date +%s) - start_ts > timeout )); then
      echo "Timed out waiting for ${url}"
      return 1
    fi
    sleep 1
  done
}

# Start services
echo "Starting docker-compose services..."
docker-compose up -d

# Wait for HTTP services to bind (CSE 8080, mood 8088, ingest 8089, grafana 3000)
# Prefer an HTTP probe for the CSE web UI (longer timeout). Fallback to TCP if needed.
wait_for_http "http://localhost:8080/webui/index.html" 180 || { echo "CSE (8080) did not become available"; exit 2; }
wait_for_port 8088 60 || { echo "Mood service (8088) did not become available"; exit 3; }
wait_for_port 8089 60 || { echo "Ingest service (8089) did not become available"; exit 4; }
wait_for_port 3000 60 || echo "Warning: Grafana (3000) did not become available within timeout (non-fatal)"

echo
echo "Running ingest/test-insert smoke test..."
read -r -d '' INGEST_PAYLOAD <<'EOF' || true
{
  "rn": "cin-test",
  "ct": "20251009T153210",
  "con": {
    "device": "dev-smoke-1",
    "room": "room-101",
    "qos": {"r": "good"},
    "metrics": [
      {"name": "co2", "value": 600},
      {"name": "noise", "value": 40}
    ]
  },
  "parent": "/cloud-analytics/telemetry/room-101/sample"
}
EOF

ingest_status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${INGEST_HOST}/test-insert" -H "Content-Type: application/json" -d "${INGEST_PAYLOAD}" || true)
echo "ingest /test-insert returned HTTP ${ingest_status}"
if [ "${ingest_status}" != "204" ]; then
  echo "ERROR: ingest test did not return 204"
  docker-compose logs ingest --tail=200
  exit 5
fi
echo "ingest test-insert OK"

# Wait briefly for DB writes
sleep 2

echo "Checking Postgres for raw_onem2m_ci entry (ci_rn = 'cin-test')..."
raw_count=$(docker exec -i "${POSTGRES_CONTAINER}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc "SELECT count(*) FROM raw_onem2m_ci WHERE ci_rn='cin-test';" || echo "0")
raw_count="$(echo "${raw_count}" | tr -d '[:space:]')"
echo "raw_onem2m_ci count = ${raw_count}"
if [ -z "${raw_count}" ] || [ "${raw_count}" = "0" ]; then
  echo "ERROR: raw_onem2m_ci row not found"
  docker-compose logs ingest --tail=200
  docker exec -i "${POSTGRES_CONTAINER}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c "SELECT * FROM raw_onem2m_ci ORDER BY created_at DESC LIMIT 5;"
  exit 6
fi
echo "Raw ingest recorded OK"

echo
echo "Running mood/notify smoke test..."
read -r -d '' NOTIFY_PAYLOAD <<'EOF' || true
{
  "m2m:sgn": {
    "nev": {
      "rep": {
        "m2m:cin": {
          "rn": "ci-smoke-1",
          "ct": "20251009T153310",
          "con": {
            "co2": 700,
            "noise": 45,
            "lux": 200,
            "temp": 22,
            "rh": 45,
            "occ": 1
          }
        }
      }
    },
    "sur": "/cloud-analytics/telemetry/room-101/sample"
  }
}
EOF

notify_resp=$(curl -s -w "\n%{http_code}" -X POST "${MOOD_HOST}/notify" -H "Content-Type: application/json" -d "${NOTIFY_PAYLOAD}" || true)
# The last line is the HTTP code; everything before is body.
notify_body=$(echo "${notify_resp}" | sed '$d' || true)
notify_code=$(echo "${notify_resp}" | tail -n1 || true)
echo "notify returned HTTP ${notify_code}"
if [ "${notify_code}" != "200" ]; then
  echo "ERROR: /notify failed (code ${notify_code})"
  echo "Response body:"
  echo "${notify_body}"
  docker-compose logs mood --tail=200
  exit 7
fi

echo "notify response body:"
echo "${notify_body}"
# quick check that 'mood' and 'score' are in the returned JSON
if ! echo "${notify_body}" | grep -q '"mood"' || ! echo "${notify_body}" | grep -q '"score"'; then
  echo "ERROR: /notify response missing expected mood/score content"
  exit 8
fi
echo "/notify returned mood OK"

# Wait for CSE to record the posted CIN and mood service to optionally insert into DB
sleep 2

echo "Querying mood/latest-mood endpoint..."
latest_resp=$(curl -s -w "\n%{http_code}" "${MOOD_HOST}/latest-mood" || true)
latest_body=$(echo "${latest_resp}" | sed '$d' || true)
latest_code=$(echo "${latest_resp}" | tail -n1 || true)
echo "latest-mood HTTP ${latest_code}"
echo "${latest_body}"
if [ "${latest_code}" != "200" ]; then
  echo "ERROR: /latest-mood failed (code ${latest_code})"
  exit 9
fi
if ! echo "${latest_body}" | grep -q '"score"'; then
  echo "WARNING: /latest-mood did not include score; checking CSE /la directly..."
fi

# Query CSE /la (host-mapped URL)
cse_la_url="${CSE_HOST_URL}/cloud-analytics/analytics/mood/score/la"
echo "Querying CSE /la at ${cse_la_url} ..."
cse_la_resp=$(curl -s -w "\n%{http_code}" "${cse_la_url}" || true)
cse_la_body=$(echo "${cse_la_resp}" | sed '$d' || true)
cse_la_code=$(echo "${cse_la_resp}" | tail -n1 || true)
echo "CSE /la HTTP ${cse_la_code}"
echo "${cse_la_body}"
if [ "${cse_la_code}" != "200" ] && [ "${cse_la_code}" != "201" ]; then
  echo "ERROR: CSE /la did not return 200/201 (code ${cse_la_code})"
  exit 10
fi
if ! echo "${cse_la_body}" | grep -q '"con"' && ! echo "${cse_la_body}" | grep -q '"m2m:cin"'; then
  echo "ERROR: CSE /la response did not include expected content instance"
  exit 11
fi
echo "CSE /la shows mood CIN OK"

# Check DB fact_mood_scores for recent entries
echo "Checking Postgres for fact_mood_scores entries..."
mood_count=$(docker exec -i "${POSTGRES_CONTAINER}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc "SELECT count(*) FROM fact_mood_scores;" || echo "0")
mood_count="$(echo "${mood_count}" | tr -d '[:space:]')"
echo "fact_mood_scores total rows = ${mood_count}"
if [ -z "${mood_count}" ] || [ "${mood_count}" = "0" ]; then
  echo "WARNING: No rows found in fact_mood_scores. It may be that the DB insert is best-effort and failed."
  docker-compose logs mood --tail=200
else
  echo "Mood persisted to DB (fact_mood_scores) OK"
fi

echo
echo "SMOKE TEST PASSED"
exit 0
