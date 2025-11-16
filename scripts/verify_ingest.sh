#!/bin/bash
# Simple integration test for ingest -> DB -> mood-service flow
set -euo pipefail

INGEST_URL="http://127.0.0.1:8088/test-insert"
PSQL="docker exec -i onem2m_postgres psql -U onem2m -d onem2m -t -c"

echo "Seeding dim_metric (idempotent)..."
cat postgres/migrations/001_seed_dim_metric.sql | docker exec -i onem2m_postgres psql -U onem2m -d onem2m

TEST_RN="cin-verify-$(date +%s)"
echo "Posting test notification rn=${TEST_RN} ..."
curl -s -X POST "${INGEST_URL}" -H "Content-Type: application/json" -d "{\"rn\":\"${TEST_RN}\",\"ct\":\"$(date +%Y%m%dT%H%M%S)\",\"con\":{\"tempe\":21.2,\"humiy\":56.0,\"co2\":1237.98,\"ts\":$(date +%s)}}"

echo "Checking raw_onem2m_ci..."
${PSQL} "SELECT parent_path, ci_rn, payload FROM raw_onem2m_ci WHERE ci_rn='${TEST_RN}' LIMIT 1;"

echo "Checking fact_telemetry..."
${PSQL} "SELECT m.metric_rn, f.value FROM fact_telemetry f JOIN dim_metric m ON f.metric_id=m.metric_id WHERE f.ci_rn='${TEST_RN}' ORDER BY m.metric_rn;"

echo "Done."
