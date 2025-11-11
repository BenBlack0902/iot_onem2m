#!/bin/bash
# pull_airquality.sh — periodic oneM2M announced resource poller
# Benjamin Karic, adapted by Cline, 2025-11-11
set -euo pipefail

# Configuration
ENDPOINT="http://135.181.198.131:8080/aiQSrAnncdO6xF8vb4U?rcn=5"
ROOM="Room01"
CSE_ORIGIN="CAdmin"
# In this compose the ingest service maps host port 8089 -> container 8088 for test-insert
INGEST_URL="http://127.0.0.1:8089/test-insert"
DB_USER="onem2m"
DB_NAME="onem2m"
POSTGRES_CONTAINER="onem2m_postgres"

LOG_PREFIX="$(date -Is)"

# Fetch announced resource
RESP=$(curl -s -H "Accept: application/json" -H "X-M2M-Origin: $CSE_ORIGIN" -H "X-M2M-RI: $(uuidgen)" -H "X-M2M-RVI: 4" "$ENDPOINT")

if [ -z "$RESP" ]; then
  echo "$LOG_PREFIX Empty response from $ENDPOINT" >&2
  exit 1
fi

# Extract the cod:* object (first cod: entry)
COD=$(echo "$RESP" | jq -c 'to_entries[] | select(.key|startswith("cod:")) | .value' || true)

if [ -z "$COD" ] || [ "$COD" = "null" ]; then
  echo "$LOG_PREFIX No cod:* object found in response" >&2
  exit 1
fi

# Build normalized payload
NORMALIZED=$(echo "$COD" | jq -c --arg room "$ROOM" '{ts:(now|tonumber), room: $room, temperature: .tempe, humidity: .humiy, co2: .co2}')

echo "$LOG_PREFIX POLL -> $NORMALIZED"

# Create a test-insert body to reuse ingest logic (onem2m handler)
CT=$(date +%Y%m%dT%H%M%S)
RN="cin-pull-$(date +%s)"
POST_BODY=$(jq -n --arg rn "$RN" --arg ct "$CT" --argjson con "$NORMALIZED" '{rn:$rn, ct:$ct, con:$con}')

# Post to ingest test-insert endpoint (local host mapping)
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$INGEST_URL" -H "Content-Type: application/json" -d "$POST_BODY" || true)
echo "$LOG_PREFIX Posted to ingest as $RN (HTTP $HTTP_CODE)"

# Optional: also insert raw payload into Postgres raw_onem2m_ci for traceability
# We use docker exec psql to insert the payload as text/jsonb
INSERT_SQL="INSERT INTO raw_onem2m_ci (created_at,parent_path,ci_rn,payload) VALUES (NOW(), '/aiQSrAnncdO6xF8vb4U', 'cin-pull-${RN}', '$NORMALIZED'::jsonb) ON CONFLICT DO NOTHING;"
docker exec -i $POSTGRES_CONTAINER psql -U $DB_USER -d $DB_NAME -c "$INSERT_SQL" > /dev/null 2>&1 || echo "$LOG_PREFIX Warning: failed to insert raw_onem2m_ci via docker exec"

echo "$LOG_PREFIX Done."
