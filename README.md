# Mood Monitor — oneM2M Ingest → Mood → Postgres Pipeline

Authors: Alper Ramadani, Benjamin Karic, Tahir Toy

Overview
--------
Mood Monitor is an end-to-end telemetry ingestion and analytics pipeline that:
- receives oneM2M Content Instances (CINs) from an ACME CSE,
- normalizes telemetry into canonical metrics,
- persists telemetry in Postgres for analytics,
- computes a compact "mood" score and publishes a derived CIN back to the CSE,
- optionally runs an ML-based mood predictor in parallel (read-only) for experimentation.

This README is the concise, user-facing guide for hackathon delivery and reviewers. For the canonical technical reference and runbook, see docs/TECHNICAL.md.

Quick links
-----------
- Full technical documentation and runbook: docs/TECHNICAL.md
- Experimental ML-only service: mood-service-ml/ (calculation-only; safe for testing)
- Integration test helper: scripts/verify_ingest.sh
- Database migrations: postgres/migrations/
- Compose orchestrator: docker-compose.yml
- WireGuard VPN plan and setup: wireguard-onem2m-setup/docs/network-plan.md

How it works (pipeline)
-----------------------
1) Devices or MN-CSEs write telemetry CINs into the IN-CSE resource tree under:
   /~/in-cse/in-name/cloud-analytics/telemetry/room-<id>/sample

2) The ACME CSE triggers a subscription (SUB) with nct=2 (full representation) to the ingest service.

3) ingest:
   - Extracts `m2m:cin.con` from notification payload variants.
   - Normalizes synonyms and shapes into canonical metrics.
   - Upserts dimension rows (room, device, metric) and inserts normalized telemetry into fact_telemetry.
   - Forwards a normalized oneM2M-style notification to mood-service.

4) mood-service:
   - Computes a heuristic mood score (0..100) and label ∈ {focus, neutral, tired}.
   - Persists results into fact_mood.
   - Posts a mood CIN back to the IN-CSE under:
     /~/in-cse/in-name/cloud-analytics/analytics/mood/score

5) Grafana (optional) visualizes metrics and mood from Postgres. UIs can also read the latest mood from the CSE via /la or via mood-service GET /latest-mood.

High-level architecture
-----------------------
- ACME CSE (IN-CSE) — Receives CINs; delivers subscription notifications.
- ingest — Flask service that handles /notify (or /onem2m), normalizes, persists to Postgres, and forwards to mood-service.
- mood-service — FastAPI service that computes the mood, persists to Postgres, and posts derived mood CINs to the CSE.
- mood-service-ml — Optional, experimental ML predictor service for testing models; calculation-only (no DB writes or CSE posts).
- Postgres — Stores dim tables and the facts fact_telemetry and fact_mood.
- Grafana — Visualizations and dashboards.

Grafana dashboards
------------------
System Overview (gauge and mood)
![Grafana system overview](memory-bank/grafana-dashboard-with-postgres-tables.png)

Postgres tables (raw and dimensions)
![Grafana tables view](memory-bank/postgres-tables-in-grafana.png)

Data flow (ASCII)
-----------------
CIN -> (ACME CSE) -> SUB notify -> ingest (/notify)
   -> normalize -> Postgres (fact_telemetry)
   -> forward to mood-service (/notify)
   -> compute mood -> Postgres (fact_mood)
   -> post mood CIN -> (ACME CSE) -> read via /la

Supported metrics (canonical keys)
----------------------------------
- temperature: temp / tempe / temperature
- humidity: rh / humiy / humidity
- co2: co2 / co2ppm
- lux: lux
- noise: noise
- occupancy: occ / occupancy

Canonical payload examples
--------------------------
Telemetry sample (MN → IN):
```json
{"co2": 935, "noise": 58, "lux": 320, "temp": 23.1, "rh": 41, "occ": 2, "ts": 1738075200}
```

Mood result (IN):
```json
{"score": 78, "label": "focus", "ts": 1738075210}
```

Components and responsibilities
-------------------------------
- ingest/ (Flask)
  - Endpoints: POST /notify, /onem2m, / (accepts SUB notifications), /test-insert (helper)
  - Key functions:
    - normalize_payload(con) — maps synonyms, coerces numeric fields, handles arrays and nested shapes.
    - post_to_mood(normalized, ci_rn, ct, parent) — forwards normalized telemetry to mood-service.
  - Persists: raw_onem2m_ci (raw payload), exploded metrics into fact_telemetry; upserts dim_room, dim_device, dim_metric.
  - Env: DATABASE_URL (libpq or DSN), MOOD_NOTIFY (default http://mood:8088/notify).
- mood-service/ (FastAPI)
  - Endpoints: POST /notify (consume notification/normalized), GET /latest-mood (reads CSE /la).
  - Key functions:
    - compute_mood_score(sample) — heuristic weighting of co2, noise, lux, temp, rh, occ.
    - one_m2m_post_cin(target_path, con_payload) — posts CIN with required oneM2M headers.
  - Persists mood to fact_mood and posts CIN to CSE.
  - Env: CSE_BASE, CSE_ORIGIN, DATABASE_URL, ROOM_IDS, MOOD_NOTIFY.
  - Important: CSE_BASE should be the FULL container resource path for the mood score container (example below).
- mood-service-ml/ (FastAPI)
  - Endpoints: POST /notify
  - Purpose: run in parallel for ML testing. Loads joblib model if provided (MOOD_MODEL_PATH), otherwise falls back to heuristic.
  - No side effects: does NOT write to DB or CSE.
- postgres/ (Postgres 15)
  - init.sql: initial schema bootstrap (executed on first container init).
  - migrations/: SQL migrations (seed metrics, create fact_mood).
- grafana/ (optional)
  - Provisioning under grafana/provisioning for datasources/dashboards.

Database model (key tables)
---------------------------
- dim_metric(metric_id, metric_rn, unit, …) — seeded with canonical metrics.
- dim_room(room_id, room_rn, …)
- dim_device(device_id, device_rn, room_id, …)
- fact_telemetry(ts_cse, device_id, metric_id, value, value_text, quality, parent_path, ci_rn, …)
- fact_mood(parent_path, ci_rn, ts_cse, score, label, confidence, room_id, device, inserted_at, …)

Docker Compose (services, ports, networks)
------------------------------------------
- acme (container_name: cloud-in-cse)
  - Image: r3dpanda1/acme-onem2m-cse
  - Ports: 8080:8080
  - Volumes: ./cse:/data, acme-data:/opt/ACME-oneM2M-CSE/data
  - Network: in-cse-net
- mood (mood-service)
  - Build: ./mood-service
  - Ports: 8088:8088
  - Env: CSE_BASE, CSE_ORIGIN, MOOD_NOTIFY, ROOM_IDS
  - Depends on: acme
  - Network: in-cse-net
- postgres
  - Image: postgres:15
  - Volumes: postgres-data:/var/lib/postgresql/data, ./postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
  - Network: in-cse-net
- grafana
  - Image: grafana/grafana:latest
  - Ports: 3000:3000
  - Env: GF_SECURITY_ADMIN_PASSWORD
  - Depends on: postgres
  - Network: in-cse-net
- ingest
  - Build: ./ingest
  - Ports: 8089:8088 (container listens on 8088; mapped to 8089 on host)
  - Env: DATABASE_URL=postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
  - Volumes: ./logs/ingest:/var/log/ingest
  - Depends on: postgres
  - Network: in-cse-net

Quick start (developer)
-----------------------
Prerequisites:
- Docker & Docker Compose
- Git
- Keep host ports free: 8080 (CSE), 8088 (mood), 8089 (ingest), 3000 (Grafana), 5432 internal (Postgres)

1) Start the stack
```bash
docker compose up -d
```

2) Verify services are running
```bash
docker ps --filter name=onem2m_postgres -a
docker logs --tail 100 ingest
docker logs --tail 100 mood
```

3) Run integration smoke test
```bash
./scripts/verify_ingest.sh
```

4) Manual test: post a CIN to CSE
```bash
curl -v -X POST "http://localhost:8080/~/in-cse/in-name/cloud-analytics/telemetry/room-101/sample" \
  -H "Content-Type: application/json;ty=4" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -d '{"m2m:cin":{"con":{"tempe":22.3,"humiy":50,"co2":820},"cnf":"application/json:0"}}'
```

5) Read latest mood from CSE
```bash
curl -s "http://localhost:8080/~/in-cse/in-name/cloud-analytics/analytics/mood/score/la" | jq
```

6) Or from mood-service thin API
```bash
curl -s "http://localhost:8088/latest-mood" | jq
```

Experimental ML service (optional)
----------------------------------
Build and run locally:
```bash
# Build image
docker build -t mood-service-ml -f mood-service-ml/Dockerfile .

# Create a toy model artifact
docker run --rm -v "$(pwd)":/workdir -w /workdir mood-service-ml python3 create_model.py

# Run calculation-only service (maps host 8090 -> container 8088)
docker run -d --name mood-ml-debug -p 8090:8088 \
  -e MOOD_MODEL_PATH=/models/mood_model.pkl \
  -v "$(pwd)"/mood_model.pkl:/models/mood_model.pkl:ro \
  mood-service-ml

# Test
curl -s -X POST http://localhost:8090/notify -H "Content-Type: application/json" \
  -d '{"m2m:cin":{"con":{"co2":600,"noise":40,"lux":300,"temp":23,"rh":45,"occ":1}}}' | jq
```

Configuration & environment variables
-------------------------------------
Example `.env`:
```ini
# CSE interaction (IMPORTANT: CSE_BASE should be the FULL container path for mood score)
# Example: http://cloud-in-cse:8080/~/in-cse/in-name/cloud-analytics/analytics/mood/score
CSE_BASE=http://cloud-in-cse:8080/~/in-cse/in-name/cloud-analytics/analytics/mood/score
CSE_ORIGIN=admin:admin

# Service interop
MOOD_NOTIFY=http://mood:8088/notify
ROOM_IDS=room-101

# Postgres
POSTGRES_USER=onem2m
POSTGRES_PASSWORD=onem2m_pass
POSTGRES_DB=onem2m

# Grafana
GRAFANA_ADMIN_PASSWORD=changeme
```

Notes:
- mood-service defaults use `acme` in code, but our compose CSE container is named `cloud-in-cse`. Provide CSE_BASE explicitly to avoid name mismatches.
- Postgres init.sql runs only on first initialization. Apply migrations manually if needed after the first run.

ACME resource exploration (real demo server)
----------------------------------
ACME CSE — moodAnalysis container in REST UI
![ACME CSE UI](memory-bank/acme_ressourcetree.png)

These examples use the public VPS ACME CSE at 135.181.198.131:8080. Adjust the IP if your deployment differs.

Example: read an announced resource and reshape with jq to the normalized telemetry fields our pipeline expects:
```bash
root@ubuntu-4gb-hel1-3:~/iot_onem2m# curl -s \
  -H "Accept: application/json" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  "http://135.181.198.131:8080/aiQSrAnncdO6xF8vb4U?rcn=5" \
  | jq '.[\"cod:aiQSrAnnc\"] | {ts: now|tonumber, room: "Room01", temperature: .tempe, humidity: .humiy, co2: .co2}'
{
  "ts": 1762899857.255733,
  "room": "Room01",
  "temperature": 22.5,
  "humidity": 56.0,
  "co2": 4577.99
}
```

Browse common ACME resources:
```bash
# 1) CSE Base with all children
curl -X GET "http://135.181.198.131:8080/id-cloud-in-cse?rcn=4" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"

# 2) Remote CSE
curl -X GET "http://135.181.198.131:8080/id-room-mn-cse" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"

# 3) Cross Resource (cbA) with children
curl -X GET "http://135.181.198.131:8080/cbAnNG65oGsiT?rcn=5" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"

# 4) Air Quality Sensor Announcement
curl -X GET "http://135.181.198.131:8080/aiQSrAnncdO6xF8vb4U" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"

# 5) Subscription
curl -X GET "http://135.181.198.131:8080/subOgqLiAqx0M" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"

# 6) Access Control Policy
curl -X GET "http://135.181.198.131:8080/acpCreateRootResources" \
  -H "X-M2M-Origin: CAdmin" \
  -H "X-M2M-RI: $(uuidgen)" \
  -H "X-M2M-RVI: 4" \
  -H "Accept: application/json"
```

WireGuard VPN and network topology
----------------------------------
- Project VPN subnet: 10.100.0.0/24
- Cloud hub (Hetzner example): 10.100.0.1
- Spoke sites (homes) receive static VPN IPs (e.g., 10.100.0.2/3/4).
- Routing decision: NAT on each Pi (no home router changes required).
- Details: wireguard-onem2m-setup/docs/network-plan.md
- Helper scripts: wireguard-onem2m-setup/scripts/
  - 01-generate-keys.sh, 02-setup-cloud.sh, 03-setup-spoke.sh, 04-test-connection.sh
- Per-user configs under wireguard-onem2m-setup/configs/<name> (wg0.conf samples included).

Security & operations
---------------------
- Lock down `/notify` endpoints: IP allowlist or shared-secret header; ensure CSE ACLs are restrictive.
- Move credentials from `.env` to Docker secrets or a secrets manager for production.
- Set CSE container limits (mni) on high-rate containers to cap history.
- Time sync: include `ts` in payloads and ensure VPS clock is accurate.
- Backups: scripts/backup_postgres.sh writes pg_dump artifacts into ./backups (with retention).
- Monitoring: add Prometheus metrics (ingest_received, mood_post_success/failure, model_load_failures).

Verification & troubleshooting
------------------------------
- End-to-end smoke test: `./scripts/verify_ingest.sh`
- Logs:
  - ingest logs: docker logs -f ingest and ./logs/ingest (mounted)
  - mood-service logs: docker logs -f mood
  - mood-ml logs: docker logs -f mood-ml-debug
- DB sanity:
```bash
docker exec -i onem2m_postgres psql -U onem2m -d onem2m \
  -c "SELECT * FROM fact_mood ORDER BY inserted_at DESC LIMIT 20;"
```
- If mood CIN post fails (CSE error):
  - Inspect mood-service logs for HTTP status and response body.
  - Validate headers: X-M2M-Origin, X-M2M-RI, X-M2M-RVI and Content-Type `application/json;ty=4`.
  - Confirm `CSE_BASE` points to the correct container path.
  - Consider adding retry/backoff (future improvement).

Development & CI notes
----------------------
- Unit tests recommended for: normalize_payload (ingest), parsing, and compute_mood_score.
- Integration CI job: spin up Postgres + CSE + ingest + mood in a test environment; run scripts/verify_ingest.sh.
- Add automated migration execution or document manual steps for operators.

Repository map (files of interest)
----------------------------------
- ingest/ — app.py (normalize, DB insert), Dockerfile, requirements.txt
- mood-service/ — app.py (heuristic compute, DB persistence, oneM2M posting), Dockerfile, requirements.txt
- mood-service-ml/ — app.py (ML experiment, calculation-only), Dockerfile, requirements.txt, create_model.py
- postgres/ — init.sql, migrations/*.sql (seed metrics, create fact_mood)
- grafana/ — provisioning/
- scripts/ — verify_ingest.sh, backup_postgres.sh, pull_airquality.sh
- wireguard-onem2m-setup/ — configs, scripts/, docs/network-plan.md
- docs/TECHNICAL.md — canonical technical reference & runbook (detailed)

Acceptance criteria (recommended)
---------------------------------
- IN-CSE up; cloud-analytics AE and CNTs created.
- SUB delivers notifications to mood-service within ≤ 2 s.
- Mood CIN written back and visible via `/la`.
- Dashboard can read latest mood without custom glue.

License
-------
Add a LICENSE file per your organization/hackathon rules (e.g., MIT).

Contributors & contact
----------------------
Authors: Alper Ramadani, Benjamin Karic, Tahir Toy  
Open issues/PRs for bugs and improvements. PRs welcome.
