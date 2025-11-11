# Changelog

All entries are append-only. Each entry: YYYY-MM-DD — Author — Files changed — Summary.

2025-11-11 — Cline — wireguard-onem2m-setup/configs/*, wireguard-onem2m-setup/docs/network-plan.md, memory-bank/progress.md  
- Updated public IP references from 91.98.80.99 → 135.181.198.131 in WireGuard client configs (alper/tahir/benjamin) and network-plan. Marked Chunk 1 (IP update) complete in memory-bank/progress.md.

2025-11-11 — Cline — ingest/app.py  
- Added a `normalize_payload(con)` helper that accepts multiple incoming content shapes and maps alternate metric names to canonical names (temperature, humidity, co2, lux, noise, occupancy).  
- Added `post_to_mood(normalized, ci_rn, ct, parent)` to POST a normalized telemetry notification to the mood-service `/notify` endpoint.  
- This change keeps raw payloads saved to `raw_onem2m_ci`, then enables normalization and forwarding to mood-service. (Chunk 2 in progress.)

2025-11-11 — Cline — ingest/app.py, scripts/verify_ingest.sh, memory-bank/progress.md  
- Implemented normalization insert path and added debug/info logs showing normalize_payload results and per-metric insert attempts.  
- Added `scripts/verify_ingest.sh` to run an end-to-end smoke test: seed metrics, post a test notification, and verify DB inserts.  
- Marked Chunk 2 as complete in memory-bank/progress.md.
