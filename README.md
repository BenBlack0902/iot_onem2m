# OneM2M Ingest → Mood → Postgres Pipeline

Authors: Alper Ramadani, Benjamin Karic, Tahir Toy  
November 2025

## Executive summary

This project implements an end-to-end telemetry ingestion and analytics pipeline that receives oneM2M notifications (CIN), normalizes telemetry, computes a compact "mood" score, persists results in PostgreSQL for analytics, and posts derived CINs back to the CSE. The design balances standard oneM2M interactions with pragmatic engineering choices (Docker-based services, persistent volumes, backup policy). The pipeline is intended for research and operational evaluation of environmental sensing (CO2, temperature, humidity, etc.) and lightweight crowd-sensed wellbeing metrics.

Key outcomes:
- The ingest service accepts oneM2M notification POSTs and writes raw payloads and parsed metrics to Postgres.
- The mood-service computes a 0–100 heuristic "mood" score and:
  - Posts a mood CIN to the IN-CSE (oneM2M),
  - Persists the computed mood into Postgres `fact_mood` with `room_id = 1`.
- Persistence is robust to container restarts via Docker named volumes, and nightly logical backups (pg_dump) are installed.

This README documents architecture, components, how to reproduce and verify results, data model, operational guidance (backups), and next steps.

---

## Architecture (concise)

- Docker Compose orchestrates the system:
  - `acme` (CSE): r3dpanda1/acme-onem2m-cse — the oneM2M container.
  - `ingest`: Python service that receives m2m:sgn POSTs, writes `raw_onem2m_ci` and `fact_telemetry`.
  - `mood`: Python FastAPI service that computes mood from telemetry, posts mood CINs to CSE and writes `fact_mood`.
  - `postgres`: Postgres 15 with named volume `postgres-data`.
  - `grafana`: Grafana for visualization (named volume `grafana-data`).
- Data flow:
  1. CSE subscription sends m2m:sgn → ingest (`/notify`).
  2. ingest stores the raw CIN in `raw_onem2m_ci`, normalizes metrics into `dim_metric`/`fact_telemetry`.
  3. ingest forwards normalized payload to mood-service.
  4. mood-service computes score, POSTS mood CIN back to CSE and INSERTs/UPSERTs into `fact_mood` (room_id = 1).
  5. Grafana reads Postgres for dashboards.

---

## What changed / implemented

- New migration: `postgres/migrations/002_create_fact_mood.sql`
  - Creates `fact_mood` with UNIQUE (parent_path, ci_rn) and `room_id` FK to `dim_room`.
- mood-service:
  - Added Postgres persistence (psycopg2-binary).
  - Insert/Upsert pattern: ON CONFLICT (parent_path, ci_rn) DO UPDATE to ensure idempotency.
  - Fixed parsing/robustness issues for varying oneM2M payload shapes.
- Poller:
  - `scripts/pull_airquality.sh` — periodic poller for announced resources (rcn=5) that posts into ingest's `/test-insert` to reuse ingest logic.
- Backups:
  - `scripts/backup_postgres.sh` — nightly pg_dump to `./backups` with retention (last 7).
  - Cron entry installed to run backup daily at 02:00 (server local time).
- Operational helpers:
  - `scripts/verify_ingest.sh` — helper to post a sample CIN and check `raw_onem2m_ci` + `fact_telemetry`.
- Docker compose:
  - Ensured persistent named volumes exist for Postgres and Grafana.
  - Added ingest log mount `./logs/ingest:/var/log/ingest` to persist service logs across container restarts.

All changes have been committed and pushed to the remote repository.

---

## Data model (short)

- raw_onem2m_ci (existing): stores raw notification payload JSON.
- dim_metric / fact_telemetry (existing): normalized metrics extracted from CINs.
- fact_mood (new):
  - mood_id BIGSERIAL PRIMARY KEY
  - parent_path TEXT
  - ci_rn TEXT
  - ts_cse TIMESTAMPTZ
  - score INTEGER
  - label TEXT
  - confidence DOUBLE PRECISION
  - room_id INTEGER REFERENCES dim_room(room_id)
  - device TEXT
  - inserted_at TIMESTAMPTZ DEFAULT now()
  - UNIQUE(parent_path, ci_rn) for idempotency

Design rationale: allow fast analytics queries over mood time series while preventing duplicates from notification retries.

---

## How to run (developers / operators)

Prereqs:
- Docker, Docker Compose
- Ports: 8080 (CSE), 8088 (mood), 8089 (ingest host mapping), 5432 (Postgres internal)

Start services:
1. From repo root:
   docker compose up -d

2. Verify services:
   docker ps --filter name=onem2m_postgres -a
   docker logs --tail 100 ingest
   docker logs --tail 100 mood

Run the verification helper (automated test scenario):
- ./scripts/verify_ingest.sh
  - It creates a CIN and checks that:
    - raw_onem2m_ci contains the payload,
    - fact_telemetry contains parsed metrics,
    - mood-service posts a mood CIN back to the CSE,
    - fact_mood contains a persisted mood row.

Check Postgres directly (examples):
- List last raw CINs:
  docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT created_at,parent_path,ci_rn,payload FROM raw_onem2m_ci ORDER BY created_at DESC LIMIT 10;"
- Check mood rows:
  docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT mood_id, parent_path, ci_rn, score, label, room_id, inserted_at FROM fact_mood ORDER BY inserted_at DESC LIMIT 20;"

---

## Backup & restore (operational)

Nightly backup:
- `scripts/backup_postgres.sh` runs inside the host and performs:
  docker exec -i onem2m_postgres pg_dump -U ${POSTGRES_USER} ${POSTGRES_DB} | gzip > backups/onem2m-db-<ts>.sql.gz
- Retention: last 7 backups kept.

To restore:
- Stop Postgres container.
- Restore via:
  zcat backups/onem2m-db-YYYY-MM-DD-HHMMSS.sql.gz | docker exec -i onem2m_postgres psql -U onem2m -d onem2m
- Alternatively, use filesystem snapshot/volume restore for full binary-level recovery (stop container, restore volume, start).

Grafana:
- Grafana data is persisted to `grafana-data` volume. You can also copy `grafana.db` from container:
  docker cp grafana:/var/lib/grafana/grafana.db backups/grafana-<ts>.db

---

## Resilience and non-loss on restart

- Docker Compose uses named volumes:
  - `postgres-data` mounted at `/var/lib/postgresql/data` — DB files survive container recreation and restarts.
  - `grafana-data` persists Grafana state.
- ingest logs are persisted via host mount `./logs/ingest` to retain logs across restarts.
- Backups provide an added layer to recover from data corruption or accidental deletes.

Operational recommendations:
- Ensure the host volume directory (`./backups`, `./logs`) is included in host-level backups and has appropriate retention.
- Monitor cron logs at `backups/cron.log`.

---

## Testing and verification performed

- Verified ingest receives notifications (by creating a test CIN); confirmed raw payload and fact_telemetry rows appear.
- Verified mood-service computed a mood, posted CIN to the CSE and inserted rows into `fact_mood` (room_id=1).
- Confirmed that `psycopg2-binary` was added and the mood container can connect and write to Postgres.
- Verified backup script runs (manual execution recommended to test immediate effect).

---

## Troubleshooting notes (common issues & fixes)

- psycopg2 errors in container at startup:
  - Ensure `psycopg2-binary` is present in `mood-service/requirements.txt` and that the image was rebuilt.
- No CINs in Postgres:
  - Check ingestion logs: `docker logs ingest` and check `raw_onem2m_ci`.
- Subscription notifications not arriving:
  - Verify `nu` matches ingest reachable address (e.g., `http://ingest:8088/notify` when CSE and ingest are inside the same Docker network, or host IP with `/notify` path).
- Backups failing:
  - Check `backups/cron.log` and run `./scripts/backup_postgres.sh` manually to see errors.

---

## Files and important locations

- Compose: `docker-compose.yml`
- DB migrations: `postgres/migrations/001_seed_dim_metric.sql`, `postgres/migrations/002_create_fact_mood.sql`
- Mood service:
  - Code: `mood-service/app.py`
  - Requirements: `mood-service/requirements.txt`
- Ingest service: `ingest/app.py`, `ingest/Dockerfile`
- Scripts:
  - `scripts/pull_airquality.sh` — poller (optional)
  - `scripts/verify_ingest.sh` — test helper
  - `scripts/backup_postgres.sh` — nightly backup
- Backups directory: `./backups` (pg_dump outputs)
- Logs directory: `./logs/ingest` (ingest logs persisted)

---

## Future work and research directions

- Replace heuristic mood function with a learned model (time-series or multimodal).
- Add automated tests and CI (integration tests that exercise CSE → ingest → mood → DB).
- Add monitoring and alerting for backup health, Postgres errors, and queue lengths.
- Add secure remote backup (S3) and encryption.
- Add connection pooling and performance tuning for high-throughput scenarios.

---

## How to cite / attribution

If you use or adapt this code for academic work, please cite this repository and acknowledge the authors. Proposed citation format:

---

## Contact & support

For implementation questions, open an issue on the repository or contact the maintainers listed in `memory-bank/team-info` (if available).


## Experimental: mood-service-ml (ML-only)

A new parallel, calculation-only service `mood-service-ml` was added to enable safe experiments with an ML-based mood estimator without touching the production `mood-service`. This service computes mood scores from telemetry but intentionally does not persist results or post CINs to the CSE.

What was added
- mood-service-ml/app.py — FastAPI app implementing a drop-safe ML `compute_mood_score`.
- mood-service-ml/requirements.txt — runtime dependencies (numpy, scikit-learn, joblib, FastAPI/uvicorn).
- mood-service-ml/Dockerfile — image build config for standalone testing.

Behavior summary
- Drop-safe ML:
  - Missing or invalid sensor fields are replaced with sensible mid-range defaults before prediction.
  - The model is loaded lazily from the path defined in `MOOD_MODEL_PATH`.
  - If the model is unavailable or prediction fails, the service falls back to the existing heuristic so it always returns a valid score.
- Output:
  - POST /notify returns JSON `{ "score": int, "label": str, "ts": epoch_seconds }` and includes an optional `confidence` when the model provides it.
- Safety:
  - The experimental service does not write to Postgres and does not post to the CSE — it is read-only with respect to external systems.

Quick start (copy/paste)
- Build the image:
  docker build -t mood-service-ml -f mood-service-ml/Dockerfile .

- Create a test model (optional; useful for ML predictions):
  docker run --rm -v $(pwd):/workdir -w /workdir mood-service-ml python3 create_model.py

- Run the experimental service with a mounted model:
  docker run -d --name mood-ml-debug -p 8090:8088 \
    -e MOOD_MODEL_PATH=/models/mood_model.pkl \
    -v $(pwd)/mood_model.pkl:/models/mood_model.pkl:ro \
    mood-service-ml

- Test the /notify endpoint:
  curl -s -X POST http://localhost:8090/notify \
    -H "Content-Type: application/json" \
    -d '{"m2m:cin":{"con":{"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1}}}' | jq

<<<<<<< HEAD
Notes
- If `MOOD_MODEL_PATH` is not set or the model fails to load, the service will return a heuristic-based score.
- To add this service to the compose stack, add a `mood-ml` service in `docker-compose.yml` (the repository already contains the `mood-service-ml` directory and Dockerfile).
- See `memory-bank/ingest-mood-summary.md` for full experimental notes and test commands.
  
<task_progress>
- [x] Create mood-service-ml directory and add ML calculation app.py
- [x] Add requirements.txt and Dockerfile to mood-service-ml
- [x] Build Docker image mood-service-ml
- [x] Fix Dockerfile and run/test the mood-ml service
- [x] Document experiment in memory-bank/ingest-mood-summary.md
- [x] Update README.md with experimental notes and quick start
</task_progress>
=======
- psycopg2 errors in container at startup:
  - Ensure `psycopg2-binary` is present in `mood-service/requirements.txt` and that the image was rebuilt.
- No CINs in Postgres:
  - Check ingestion logs: `docker logs ingest` and check `raw_onem2m_ci`.
- Subscription notifications not arriving:
  - Verify `nu` matches ingest reachable address (e.g., `http://ingest:8088/notify` when CSE and ingest are inside the same Docker network, or host IP with `/notify` path).
- Backups failing:
  - Check `backups/cron.log` and run `./scripts/backup_postgres.sh` manually to see errors.

---

## Files and important locations

- Compose: `docker-compose.yml`
- DB migrations: `postgres/migrations/001_seed_dim_metric.sql`, `postgres/migrations/002_create_fact_mood.sql`
- Mood service:
  - Code: `mood-service/app.py`
  - Requirements: `mood-service/requirements.txt`
- Ingest service: `ingest/app.py`, `ingest/Dockerfile`
- Scripts:
  - `scripts/pull_airquality.sh` — poller (optional)
  - `scripts/verify_ingest.sh` — test helper
  - `scripts/backup_postgres.sh` — nightly backup
- Backups directory: `./backups` (pg_dump outputs)
- Logs directory: `./logs/ingest` (ingest logs persisted)

---

## Future work and research directions

- Replace heuristic mood function with a learned model (time-series or multimodal).
- Add automated tests and CI (integration tests that exercise CSE → ingest → mood → DB).
- Add monitoring and alerting for backup health, Postgres errors, and queue lengths.
- Add secure remote backup (S3) and encryption.
- Add connection pooling and performance tuning for high-throughput scenarios.

---

## How to cite / attribution

If you use or adapt this code for academic work, please cite this repository and acknowledge the authors. Proposed citation format:

---

## Contact & support

For implementation questions, open an issue on the repository or contact the maintainers listed in `memory-bank/team-info` (if available).
>>>>>>> origin/main
