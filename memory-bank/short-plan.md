Short Plan — Minimal, trackable steps

Goal
- Finish three focused deliverables:
  1) Replace the old public IP (91.98.80.99) with the new IP (135.181.198.131) in config/docs.
  2) Make the ingest → mood end-to-end flow work reliably (normalize incoming payloads, store raw + normalized metrics, POST normalized data to mood-service).
  3) Ensure Postgres schema and migrations support the ingest and mood flow (seed canonical metrics, idempotency, indexes).

Why this short plan
- Keeps work small and verifiable.
- Memory-bank will be the single source of truth (plan, migrations, test vectors, changelog).
- Each change will be small: update docs/config, implement parser/normalizer, add safe migration.

Deliverables (three chunks)
- Chunk 1 — IP update
  - Replace occurrences of 91.98.80.99 → 135.181.198.131 in configuration files and docs (wireguard configs, network-plan, docker/.env if present).
  - Verify reachability with:
    curl -X GET "http://135.181.198.131:8080/id-cloud-in-cse?rcn=4" -H "X-M2M-Origin: CAdmin" -H "X-M2M-RI: $(uuidgen)" -H "X-M2M-RVI: 4" -H "Accept: application/json"
  - Acceptance: curl returns JSON and updated files are committed; memory-bank/progress.md updated.

- Chunk 2 — Ingest normalizer + mood-service integration
  - Implement a normalize_payload(con, ct, parent) helper in ingest/app.py to accept multiple payload shapes and map alternate field names to canonical metric names (temperature, humidity, co2, lux, noise, occupancy).
  - Keep raw payload in raw_onem2m_ci (already done).
  - Upsert dim_* rows and insert into fact_telemetry (reuse existing SQL pattern).
  - POST normalized payload to mood-service endpoint (http://mood:8088/compute or /notify) and log response.
  - Acceptance: test vectors inserted into DB as expected and mood-service returns score,label.

- Chunk 3 — Postgres migration: seed metrics & verify idempotency
  - Add idempotent migration script to insert canonical dim_metric rows (temperature, humidity, co2, lux, noise, occupancy).
  - Verify unique constraint UNIQUE(parent_path, ci_rn, metric_id) prevents duplicates.
  - Acceptance: dim_metric contains canonical rows and re-sent notifications do not duplicate fact rows.

Testing & traceability
- For each chunk:
  - Update memory-bank/changelog.md with a brief entry (date, author, summary, files changed).
  - Update memory-bank/progress.md to mark the chunk complete and add verification commands.
  - Add minimal test vector(s) to memory-bank/test-plan.md for quick verification (curl /test-insert or psql queries).

Next action (what I'll do if you say "Proceed: apply short-plan")
1. Implement Chunk 1: update config/docs to new IP and create/change memory-bank entries (short-plan, changelog, progress).
2. Proceed to Chunk 2: implement normalize_payload in ingest/app.py and wire to mood-service.
3. Proceed to Chunk 3: add migration script to postgres/migrations/ and run/verify.

Minimal task progress
- [x] Review memory-bank docs and current state
- [x] Inspect postgres/init.sql to map schema
- [x] Inspect ingest/app.py to find insertion points
- [ ] Chunk 1 — IP update (replace 91.98.80.99 → 135.181.198.131)
- [ ] Chunk 2 — Ingest normalizer + mood-service integration
- [ ] Chunk 3 — Postgres metric seeding migration and verification
