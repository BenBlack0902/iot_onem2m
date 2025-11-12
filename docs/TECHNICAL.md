# IoT OneM2M — Technical Reference & Runbook

This document is the canonical technical reference for the iot_onem2m repository. It describes architecture, component responsibilities, data flows, deployment and verification steps, operational playbooks, and developer notes for the key parts of the project.

Status
- Date: 2025-11-12
- Branch: feature/mood-ml-experiment (contains the experimental ML service)
- Purpose: capture the final, runnable state of the stack and provide a single document operators and developers can use to run, test, and extend the system.

1. Executive summary
- The system ingests oneM2M Content Instances (CINs), normalizes telemetry into canonical metrics and persists them to Postgres (`fact_telemetry`). A separate service computes a "mood" score from the telemetry and publishes the result as oneM2M CINs (and persists scores to `fact_mood`).
- An experimental, read-only ML service `mood-service-ml` was added to test ML-based computation in parallel without affecting production flows.

2. High-level architecture
- Components:
  - CSE (ACME oneM2M) — receives and stores CINs; provides subscription notifications.
  - ingest (HTTP service) — receives oneM2M notifications (m2m:sgn) at `/notify`, extracts `m2m:cin.con`, normalizes fields, persists raw payloads and normalized metrics to Postgres, and forwards normalized telemetry to `mood-service`.
  - mood-service (production heuristic) — computes heuristic mood score and posts a mood CIN to the CSE; persists to `fact_mood`.
  - mood-service-ml (experimental, optional) — drop-safe ML predictor; calculation-only (no DB writes, no CSE posts), used for testing ML models.
  - Postgres — DB schema holds `dim_metric`, `fact_telemetry`, and `fact_mood`.
  - Grafana — dashboards and visualizations.
  - Scripts — poller, verification, backup helpers.
- Data flow:
  CIN -> (CSE) -> subscription notification -> ingest (/notify) -> normalized metrics -> Postgres fact_telemetry -> mood-service (/notify) -> mood CIN -> CSE

3. oneM2M shapes we handle
- Notifications often contain:
  - Full representation: payload["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"]
  - Simpler shapes: {"m2m:cin": {"con": {...}}} or {"con": {...}}
- ingest searches recursively for `m2m:cin` or a leaf `con` to extract telemetry.
- Recognized metrics (canonical keys): `temp`, `rh`, `co2`, `lux`, `noise`, `occ`
  - ingest normalizes synonyms: temperature/tempe -> temp; humidity/humiy -> rh; occupancy -> occ; co2ppm -> co2

4. Database model (important tables)
- dim_metric (dimension of metrics): canonical metrics seeded via migration (temperature, humidity, co2, lux, noise, occupancy).
- fact_telemetry: stores normalized metric values per content instance with metric_id, value, ci_rn, parent_path, inserted_at.
- fact_mood: stores computed mood scores with score, label, confidence (optional), ts_cse, and room_id.
- Migrations:
  - postgres/migrations/001_seed_dim_metric.sql — seeds dim_metric.
  - postgres/migrations/002_create_fact_mood.sql — creates fact_mood.

5. ingest service (ingest/app.py) — responsibilities and important functions
- Entrypoints:
  - POST /notify — main notification receiver.
- Key functions:
  - extract_con_from_notification(payload) — recursive search returning `con` payload.
  - parse_con(con_field) — ensures `con` is a dict (parses JSON strings).
  - normalize_payload(con) — maps synonyms, coerces numeric fields, handles metrics array shapes.
  - persist raw payload to `raw_onem2m_ci` and normalized metrics to `fact_telemetry` (upserting `dim_metric` as necessary).
  - post_to_mood(normalized, ci_rn, ct, parent) — forwards normalized telemetry to mood-service via HTTP (MOOD_NOTIFY env var).
- Environment variables:
  - DATABASE_URL — connection string for Postgres
  - MOOD_NOTIFY — URL to forward normalized telemetry (default: http://mood:8088/notify)
- Logging:
  - detailed normalize logs and DB insert logs are present for debugging.

6. mood-service (mood-service/app.py) — responsibilities and important details
- Entrypoints:
  - POST /notify — consumes normalized telemetry (or oneM2M CIN notifications) and computes mood.
  - GET /latest-mood — reads the latest mood CIN from CSE /la endpoint.
- compute_mood_score(sample):
  - Heuristic normalizations:
    - co2: 400 best -> 1200 bad
    - noise: 30 -> 80
    - lux: 100 -> 800
    - temp: ideal 20..25 (1.0); linear degrade to 10..35
    - rh: ideal 30..50
    - occ: min(1.0, occ / 5.0)
  - Weighted average: co2(0.25), noise(0.2), lux(0.2), temp(0.15), rh(0.1), occ(0.1)
  - Score scaled to 0..100 and labeled: >=75 "focus", >=50 "neutral", else "tired".
- one_m2m_post_cin(target_path, con_payload) builds required headers:
  - Content-Type: application/json;ty=4
  - X-M2M-Origin, X-M2M-RI (uuid), X-M2M-RVI:4
  - Body: {"m2m:cin":{"con": con_payload, "cnf":"application/json:0"}}
- DB persistence:
  - Inserts/updates into fact_mood using ts from mood result.
  - Room_id currently fixed to 1 in code (can be generalized).

7. mood-service-ml (mood-service-ml/app.py) — experimental ML service
- Purpose: run in parallel for testing ML-based mood prediction without side effects.
- Behavior:
  - Lazy loads model from MOOD_MODEL_PATH (joblib).
  - Default fallback values used for missing/invalid features:
    - co2 800, noise 50, lux 200, temp 22, rh 45, occ 0
  - If model missing or prediction fails, falls back to the heuristic formula (same weighting).
  - Always returns {"score", "label", "ts"} and optional "confidence".
  - Does NOT persist to DB and does NOT post to CSE.
- How to test locally:
  - Build image: docker build -t mood-service-ml -f mood-service-ml/Dockerfile .
  - Create test model (create_model.py included)
  - Run: docker run -d --name mood-ml-debug -p 8090:8088 -e MOOD_MODEL_PATH=/models/mood_model.pkl -v $(pwd)/mood_model.pkl:/models/mood_model.pkl:ro mood-service-ml

8. Docker & compose
- docker-compose.yml orchestrates acme (CSE), mood (production), ingest, postgres, grafana, ingest.
- The `mood-ml` experimental service can be added; an image and files exist under mood-service-ml.
- Common ports:
  - CSE: 8080
  - mood (prod): 8088
  - ingest (when run standalone): typically 8088 mapped differently
  - mood-ml (test): map container 8088 to host 8090

9. Verification & typical commands
- Verify ingest end-to-end:
  - ./scripts/verify_ingest.sh
- Manual POST to CIN (example):
  - curl -v -X POST "http://<CSE_HOST>:8080/<container>" -H "Content-Type: application/json;ty=4" -H "X-M2M-Origin: CAdmin" -H "X-M2M-RI: $(uuidgen)" -H "X-M2M-RVI: 4" -d '{"m2m:cin":{"con":{"tempe":22.3,"humiy":50,"co2":820},"cnf":"application/json:0"}}'
- Test mood-ml:
  - curl -s -X POST http://localhost:8090/notify -H "Content-Type: application/json" -d '{"m2m:cin":{"con":{"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1}}}' | jq
- DB checks:
  - docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT * FROM fact_mood ORDER BY inserted_at DESC LIMIT 20;"

10. Operational playbook (runbook highlights)
- Logs:
  - ingest logs persisted in ./logs/ingest (container mount).
  - mood and mood-ml logs: docker logs -f mood / mood-ml-debug.
- If mood CIN POST fails (CSE error 4xx/5xx):
  - Inspect mood-service logs for the HTTP error and response body.
  - Check CSE ACLs / origin permissions; verify X-M2M-Origin header is allowed to post.
  - Retry with backoff (recommended code improvement).
- DB backups:
  - scripts/backup_postgres.sh produces pg_dump into ./backups with retention.
  - Restore via pg_restore or psql depending on backup format.
- Security:
  - Move CSE origin credentials out of .env to Docker secrets or a secrets manager.
  - Limit access to Postgres; use connection pooling.

11. Development & CI recommendations
- Add a lightweight integration CI job that:
  - Spins up postgres + CSE + ingest + mood (or mocks CSE) in a test environment.
  - Runs scripts/verify_ingest.sh
- Add unit tests around normalize_payload, parse_con and compute_mood_score.
- Add Prometheus metrics for key counters: ingest_received, mood_post_success, mood_post_failure, model_load_failures.

12. Files of interest (quick map)
- ingest/
  - app.py — notification receiver, normalizer, DB insert logic
  - Dockerfile, requirements
- mood-service/
  - app.py — heuristic compute, DB persistence, oneM2M posting
  - Dockerfile, requirements
- mood-service-ml/
  - app.py — ML experiment (read-only)
  - Dockerfile, requirements, create_model.py
- postgres/
  - migrations/*.sql — DB migration scripts
- scripts/
  - verify_ingest.sh — integration smoke test
  - pull_airquality.sh — optional poller
  - backup_postgres.sh — logical backup
- memory-bank/
  - ingest-mood-summary.md — short experiment notes
  - progress.md, changelog.md — project tracking

Appendix: sample payloads
- Canonical notification body (full form)
  {
    "m2m:sgn": {
      "nev": {
        "rep": {
          "m2m:cin": {
            "con": {"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1},
            "rn":"cin-12345",
            "ri":"ri-12345"
          }
        }
      },
      "sur": "/.../cntrm1iEXHDrA"
    }
  }
- Simple object:
  {"con":{"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1}}

Contact & follow-ups
- If you want, I will:
  - Commit this file docs/TECHNICAL.md, update README to link to it, and push to `feature/mood-ml-experiment`.
  - Create a PR for review and optionally tag the release.
- Recommended default next step: commit docs/TECHNICAL.md and push it to the feature branch so the repository contains the canonical technical reference.
