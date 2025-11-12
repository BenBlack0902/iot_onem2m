# Ingest → Postgres → Mood → oneM2M (moodAnalysis) — Summary

This document captures what we've implemented, verified, and the next steps for the ingest → postgres → mood → oneM2M pipeline. It is intended for developers and operators who need a concise reference of the work done and the commands to reproduce and verify the flow.

## High level goal
Compute a "mood" score from telemetry (ingest), store telemetry facts in Postgres, compute mood in a Python service (mood-service), and publish the mood as oneM2M ContentInstances (CINs) into the ACME IN‑CSE container `cntrm1iEXHDrA` (a.k.a. "moodAnalysis").

This enables mood analytics to be stored, versioned and consumed via standard oneM2M APIs.

---

## What we implemented (done)
1. Ingest
   - `ingest/app.py` updated to:
     - Normalize a variety of incoming `con` shapes (flat map, `metrics` array).
     - Special handling for announced / nested "cod:*" structures — descend into announcement arrays and extract telemetry.
     - Insert normalized metrics into `fact_telemetry` and upsert `dim_metric` as needed.
   - Verified: posting a normalized metrics array (via `/test-insert`) created `fact_telemetry` rows.

2. Mood service
   - `mood-service/app.py` updated to:
     - Extract telemetry from oneM2M notifications robustly (recursive search for `m2m:cin` / `con`).
     - Normalize synonyms (tempe/temperature → temp, humiy/humidity → rh, etc.).
     - compute_mood_score(sample) — deterministic heuristic producing `{score,label,ts}`.
     - `one_m2m_post_cin()`:
       - Builds oneM2M headers: `Content-Type: application/json;ty=4`, `Accept: application/json`, `X-M2M-Origin`, `X-M2M-RI` (uuid), `X-M2M-RVI: 4`.
       - Posts body `{"m2m:cin": {"con": <mood>, "cnf": "application/json:0"}}` (ACME requires `cnf = "application/json:0"`).
       - Logging of successful posts and error responses.
   - Environment-driven configuration:
     - `.env` updated with:
       - `CSE_BASE=http://cloud-in-cse:8080/cntrm1iEXHDrA`
       - `CSE_ORIGIN=CAdmin`
   - Rebuilt & restarted `mood` container to pick up changes.

3. Verification & testing
   - Manual curl POST of a CIN to `http://135.181.198.131:8080/cntrm1iEXHDrA`:
     - `Content-Type: application/json;ty=4`
     - `X-M2M-Origin: CAdmin`
     - `X-M2M-RVI: 4`
     - Body `{"m2m:cin":{"con":{...},"cnf":"application/json:0"}}`
     - returned HTTP 201 (Resource Created) and response body included the created `m2m:cin`.
   - Confirmed `/cntrm1iEXHDrA/la` returns the posted mood CIN.
   - Verified end-to-end: ingest normalizer inserts fact rows; mood-service computed mood and posted CIN to CSE.

4. Files changed (key)
   - ingest/app.py — normalizer + cod:* extractor + DB inserts
   - mood-service/app.py — compute_mood_score, one_m2m_post_cin (headers + cnf), robust extraction
   - .env — CSE_BASE and CSE_ORIGIN updated
   - postgres/migrations/001_seed_dim_metric.sql — metric seeding (exists; ensure applied)
   - scripts/verify_ingest.sh — verification helper (exists)

---

## Commands / How to reproduce locally (copy/paste)

1. Rebuild mood (after code changes)
   docker compose up -d --no-deps --build mood

2. Manual CIN POST test
   curl -v -X POST "http://135.181.198.131:8080/cntrm1iEXHDrA" \
     -H "Content-Type: application/json;ty=4" \
     -H "Accept: application/json" \
     -H "X-M2M-Origin: CAdmin" \
     -H "X-M2M-RI: $(uuidgen)" \
     -H "X-M2M-RVI: 4" \
     -d '{"m2m:cin":{"con":{"mood":"happy","confidence":0.94,"room":"Room01","timestamp":'$(date +%s)'},"cnf":"application/json:0"}}' -w "\nHTTP_CODE:%{http_code}\n"

3. Check latest on container
   curl -s -X GET "http://135.181.198.131:8080/cntrm1iEXHDrA/la" \
     -H "X-M2M-Origin: CAdmin" \
     -H "X-M2M-RI: checkMood" \
     -H "X-M2M-RVI: 4" \
     -H "Accept: application/json" | jq .

4. Verify fact_telemetry rows (Postgres)
   docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT f.ci_rn,m.metric_rn,f.value FROM fact_telemetry f JOIN dim_metric m ON f.metric_id=m.metric_id WHERE f.ci_rn='cin-final-02' ORDER BY m.metric_rn;"

5. Tail mood logs
   docker logs --tail 200 mood

---

## Recommended next / production hardening tasks (developer action items)

1. Retries & backoff for POST to CSE
   - Wrap `one_m2m_post_cin` in 2–3 retries with exponential backoff for transient failures.

2. Metrics & monitoring
   - Add Counters: `mood_cin_post_success`, `mood_cin_post_failure`.
   - Add a healthcheck that verifies CSE `/la` returns a mood CIN within last N minutes.

3. Secrets & policy
   - Move CSE origin credentials into a secrets manager (avoid plaintext in `.env`).
   - Confirm ACME ACPs permit the chosen origin (CAdmin) to create CINs under the container. If not, use the appropriate AE origin.

4. CI / Integration test
   - Add a scripted integration test to `scripts/verify_ingest.sh` that:
     - posts metrics to ingest,
     - checks `fact_telemetry`,
     - waits and verifies `cntrm1iEXHDrA/la` returns mood CIN.

5. Idempotency & audit trail
   - Include source CI identifiers (e.g. `ci_rn`) in the mood CIN `con` payload so CINs can be correlated and deduplicated if needed.

6. Documentation
   - Add the final request/response example (headers + `cnf: "application/json:0"`) to the API docs and README.
   - Add a short runbook for operators: "If CIN POST fails with 4xx/502, check mood logs → check /la → check ACPs."

---

## Acceptance criteria (what we consider "done")
- Any telemetry processed by ingest either from MN-CSE or synthetic POST results in `fact_telemetry` rows.
- mood-service computes mood for telemetry and posts a valid oneM2M CIN into `cntrm1iEXHDrA`:
  - POST returns 201 with `m2m:cin` body
  - `/cntrm1iEXHDrA/la` returns the new mood CIN
- Logs/metrics show success rate and failures are retryable/observable.

---

If you want I will:
- Update `memory-bank/progress.md` and `memory-bank/changelog.md` with short entries reflecting the steps above, or
- Create a PR branch and push these changes (you will need to run git/push locally because this environment does not have repo push auth).

Which do you prefer? I can apply the memory-bank updates now (Act mode) — say "Update memory bank" and I'll write the progress + changelog entries.

---

## Experimental: mood-service-ml (ML-only)

This section documents the parallel ML-only service created for safe experimentation.

What
- A new calculation-only service `mood-service-ml` was added to allow testing of an ML-based compute_mood_score without affecting the production `mood-service`.
- Files added:
  - mood-service-ml/app.py
  - mood-service-ml/requirements.txt
  - mood-service-ml/Dockerfile

Why
- To evaluate an ML approach that can be drop-safe (handles missing/invalid sensor fields) and still fall back to the existing heuristic when needed.
- To ensure experiments do not write to Postgres or post CINs to the CSE.

Key behavior
- Drop-safe ML: missing or invalid fields are replaced with mid-range defaults before prediction.
- Lazy model loading: the model is loaded from the path given by the MOOD_MODEL_PATH environment variable when needed.
- Fallback heuristic: if the model is missing/unreadable or the prediction fails, the service computes a heuristic score mirroring the original logic.
- Output: `/notify` returns a JSON object with `{"score": int, "label": str, "ts": epoch_seconds}` and optional `confidence` when available.

How we tested
1. Built the Docker image `mood-service-ml`.
2. Created a toy model `mood_model.pkl` by running a small script inside the built image and saving it to the repo root.
3. Ran the container `mood-ml-debug` with the model mounted:
   - Container: mood-ml-debug
   - Host port: 8090 -> container 8088
   - Env: MOOD_MODEL_PATH=/models/mood_model.pkl
   - Mount: /root/iot_onem2m/mood_model.pkl -> /models/mood_model.pkl:ro
4. Verified Uvicorn started and that POST /notify returns a mood result. The ML model loaded when present and the service returned predictions.

Quick commands (copy/paste)
- Build:
  docker build -t mood-service-ml -f mood-service-ml/Dockerfile .
- Create a test model (inside the image):
  docker run --rm -v $(pwd):/workdir -w /workdir mood-service-ml python3 create_model.py
- Run:
  docker run -d --name mood-ml-debug -p 8090:8088 \
    -e MOOD_MODEL_PATH=/models/mood_model.pkl \
    -v $(pwd)/mood_model.pkl:/models/mood_model.pkl:ro \
    mood-service-ml
- Test:
  curl -s -X POST http://localhost:8090/notify -H "Content-Type: application/json" -d '{"m2m:cin":{"con":{"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1}}}' | jq

Notes
- The experimental service is intentionally read-only with respect to external systems (no DB/CSE writes).
- To run the service as part of the compose stack, add a `mood-ml` service to `docker-compose.yml` (optional).
- Model path is controlled by `MOOD_MODEL_PATH`. If not set or model missing, the service falls back to the heuristic.
