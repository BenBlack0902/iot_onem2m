# IoT OneM2M Ingest → Mood → Postgres — Full Project Overview (Hackathon Report)

Authors: Ben Black, Alper, Tahir, Benjamin Karic  
Prepared by: Cline (engineering) — November 2025

Purpose and scope
-----------------
This document is the canonical project README and hackathon report for the iot_onem2m repository. It explains the complete sensor → network → ingest → analytics pipeline we built, why we built it, what each component does, what we changed during the hackathon, how to run and verify the system, and operational guidance for persistence and recovery.

Brief project statement (one sentence)
- We built a robust end-to-end oneM2M telemetry pipeline that ingests sensor CINs, normalizes metrics, computes a "mood" score, posts derived CINs back to the CSE, and persists both raw/normalized telemetry and computed mood in Postgres for analytics and dashboards.

Executive summary
-----------------
- The ingest service receives oneM2M notifications (m2m:sgn) and persists raw CINs and parsed metrics into Postgres (`raw_onem2m_ci`, `fact_telemetry`).
- The mood-service computes a compact heuristic mood score for each telemetry snapshot, posts a mood CIN to the IN‑CSE, and persists the score into a new `fact_mood` table (room_id = 1).
- We added a poller for announced resources, nightly logical backups (pg_dump), persistent logs, and ensured Docker volumes preserve Postgres and Grafana state across container restarts.
- All changes are committed to the repository for reproducibility and hackathon submission.

What we implemented during the hackathon
---------------------------------------
This section explains the concrete work items and where to find them.

1. Sensor & announced resource handling
   - The MN‑CSE mirrors sensor containers into an announced resource on the IN‑CSE.
   - We added logic in ingest to handle announced resource shapes (examining `cod:*` wrappers) and extract telemetry fields (tempe/temperature, humiy/humidity, co2, etc.).

2. Secure networking (WireGuard)
   - Included WireGuard config packages and sample `wg0.conf` files in `wireguard-onem2m-setup/`.
   - Scripts to generate keys and set up spoke/cloud exist for secure transport between distributed nodes.

3. Ingest service (ingest/)
   - Accepts HTTP POST notifications at `/notify` (m2m:sgn).
   - Persists raw payloads in `raw_onem2m_ci`.
   - Normalizes telemetry into `dim_metric` / `fact_telemetry`.
   - Forwards normalized payloads to mood-service for analytics.
   - Key file: `ingest/app.py`.

4. Mood service (mood-service/)
   - Robust recursive extraction of `m2m:cin.con`.
   - Normalizes synonyms and numeric coercion.
   - `compute_mood_score(sample)` returns `{score,label,ts}`.
   - Posts computed CINs back to the IN‑CSE and persists the score in Postgres (`fact_mood`).
   - Key file: `mood-service/app.py`, requirements updated to include `psycopg2-binary`.

5. Database & migrations (postgres/)
   - Added migration `002_create_fact_mood.sql` to create `fact_mood` table with UNIQUE(parent_path, ci_rn) for idempotency.
   - Postgres persists in a named volume `postgres-data`.

6. Grafana & dashboards
   - Grafana provisioning located in `grafana/provisioning/` and data persisted to `grafana-data` volume.

7. Poller & verification scripts
   - `scripts/pull_airquality.sh`: optional poller for announced resources (`?rcn=5`) that posts normalized payloads to ingest.
   - `scripts/verify_ingest.sh`: helper used during development to post a test CIN and validate raw & fact inserts.

8. Backups and persistence
   - `scripts/backup_postgres.sh`: nightly pg_dump to `./backups` with retention (last 7).
   - Cron entry was installed to run this script at 02:00 server time.
   - Ingest logs are persisted to host at `./logs/ingest`.

Operational architecture (summary)
----------------------------------
- Docker Compose orchestrates services on a single host or VM (compose file: `docker-compose.yml`).
- Named volumes preserve state:
  - `postgres-data` => Postgres DB files
  - `grafana-data` => Grafana state
  - `acme-data` => CSE data
- Host mounts for logs and backups:
  - `./logs/ingest` — ingest logs
  - `./backups` — backups from `backup_postgres.sh`

Reproducible steps — run & verify
---------------------------------
1. Configure environment variables in `.env` (POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, CSE_BASE, CSE_ORIGIN, etc.).
2. Start stack:
   - docker compose up -d
3. Verify end-to-end:
   - ./scripts/verify_ingest.sh
     - Posts a test CIN to the IN‑CSE container path (or MN‑CSE when required)
     - Verifies `raw_onem2m_ci` and `fact_telemetry` rows exist
     - Confirms mood-service posted a mood CIN and inserted a row in `fact_mood`
4. Inspect DB:
   - docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT * FROM fact_mood ORDER BY inserted_at DESC LIMIT 20;"

Key commands used during verification (examples)
-----------------------------------------------
- Post a CIN to MN or IN container (use the container path you have):
  curl -v -X POST "http://<CSE_HOST>:8080/<container_path>" \
    -H "Content-Type: application/json;ty=4" \
    -H "X-M2M-Origin: CAdmin" \
    -H "X-M2M-RI: $(uuidgen)" \
    -H "X-M2M-RVI: 4" \
    -d '{"m2m:cin":{"con":{"tempe":22.3,"humiy":50,"co2":820},"cnf":"application/json:0"}}'

- Check RAW CINs:
  docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT created_at,parent_path,ci_rn,payload FROM raw_onem2m_ci ORDER BY created_at DESC LIMIT 10;"

- Check mood rows:
  docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT mood_id,parent_path,ci_rn,score,label,room_id,inserted_at FROM fact_mood ORDER BY inserted_at DESC LIMIT 20;"

Hackathon narrative — who did what (short)
------------------------------------------
- Ben Black: primary repo owner and high-level architecture, verification, and composition
- Alper: network / WireGuard configuration and CSE adaptation
- Tahir: sample data sources and environment setup
- Benjamin Karic: polling script, backups, and operational scripts
- Cline (engineering): implemented mood persistence, fixed ingest parsing edge-cases, added backups, documentation, and performed verification and packaging for submission

Design decisions & rationale
---------------------------
- Keep derived analytics (mood) in the same Postgres DB as normalized telemetry to allow efficient joins and dashboards.
- Use ON CONFLICT idempotent upserts to avoid duplicates from notification retries.
- Provide a poller for announced resources for environments where SUB configuration is constrained.
- Add scheduled backups (logical) for portability and easy restore.

Research questions enabled
--------------------------
- Correlate environmental conditions to computed mood over time.
- Compare oneM2M push (SUB) vs poller (GET) delivery in terms of latency and reliability.
- Evaluate robustness to repeated notifications and restarts.

Next steps (short list)
-----------------------
- Export Grafana dashboards and include them in provisioning for reproducible dashboards.
- Add connection pooling to mood-service and ingest for performance.
- Add basic Prometheus metrics and alerts for monitoring.
- Run a recorded resilience test: restart containers while simulating high notification volumes and verify data integrity.

Appendices (references to memory bank)
-------------------------------------
- See `memory-bank/` for: short plan, changelog, active context, system patterns, and technical context — these files contain the detailed narrative you asked to preserve in the README.

Contact & submission
--------------------
- For hackathon submission, we should attach this README, the memory-bank summary files, and a short runbook to the repository release/package.
- If you want, I will create a release tag and an archive ready for submission.

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
