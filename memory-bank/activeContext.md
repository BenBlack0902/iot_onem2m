# Active Context

## Current focus
- Initialize IN-CSE (ACME) on the VPS and expose HTTP (:8080).
- Create the cloud resource tree under `/in-cse/in-name/cloud-analytics`.
- Implement a lightweight Mood Service (FastAPI) that:
  - Accepts oneM2M notifications at `/notify`.
  - Extracts telemetry `con` from `m2m:sgn.nev.rep.m2m:cin.con`.
  - Computes a mood score (0..100) + label {focus, neutral, tired}.
  - Posts mood CIN to `/cloud-analytics/analytics/mood/score` on the CSE.
- Create subscription(s) from telemetry `.../sample` to the mood-service notify URL.

## Immediate tasks completed
- Project brief reviewed and summarized into `productContext.md`.

## Active decisions / short notes
- Use Docker / docker-compose to run ACME CSE and Mood Service in the same network.
- Mood Service will expose port 8088 and CSE will be on 8080.
- Use basic auth (CSE_ORIGIN=admin:admin) for CSE calls for hackathon simplicity.
- Notifications will request full representation (nct=2) so mood-service receives `m2m:cin` con in the payload.

## Room IDs / scope
- Initial ROOM_IDS: `room-101` (add more later under `telemetry/room-<id>/sample`).

## Next actions
1. Create `systemPatterns.md`, `techContext.md`, `progress.md`.
2. Scaffold mood-service (FastAPI) and docker-compose.
3. Start ACME CSE container and create AE/CNT structure via CSE HTTP API.
4. Create subscription(s) targeting mood-service `/notify`.
5. Validate end-to-end: POST sample → SUB notify → mood-service computes → CIN written → `la` returns latest.

## Notes for later sessions
- Consider adding thin API `/latest-mood?room=` that reads CSE `/la`.
- Add retention limits (`mni`) on high-rate CNTs.
- Secure `/notify` with IP allowlist and rotate default credentials.

---

## Explainability & supported telemetry shapes (added 2025-10-16)

We added explainability support to the Mood Service and acceptance of multiple telemetry shapes. Key points:

- Supported telemetry shapes
  - Flat content instance (preferred for scoring):
    - con contains top-level metric keys, e.g.
      {
        "m2m:cin": {
          "con": {
            "device": "dev-1",
            "room": "room-101",
            "co2": 720,
            "noise": 38,
            "lux": 450,
            "temp": 23.0,
            "rh": 42,
            "occ": 2
          }
        }
      }
  - Envelope with a `metrics` list:
    - con.metrics = [ { "name": "co2", "value": 720 }, ... ]
    - The Mood Service now normalizes this list into a flat dict before scoring.

- Explainability output
  - The scorer returns a `components` object along with the score and label. Example components:
    {
      "co2": 0.6,
      "noise": 0.84,
      "lux": 0.5,
      "temp": 1.0,
      "rh": 1.0,
      "occ": 0.4,
      "weights": { "co2":0.25, "noise":0.2, ... },
      "combined": 0.708
    }
  - The `components` object is included in the mood CIN posted to the CSE under `con.components`, is logged by the Mood Service, and is persisted as part of the telemetry snapshot when the DB insert occurs.

- How scoring works (brief)
  - Each metric is normalized to 0..1 by heuristics (CO2: 400..1200, noise: 30..80, lux: 100..800, temp ideal 20..25, rh ideal 30..50, occ normalized by count).
  - Weights are applied (default: co2 0.25, noise 0.2, lux 0.2, temp 0.15, rh 0.1, occ 0.1).
  - Combined weighted score multiplied by 100 -> integer score, mapped to label {focus, neutral, tired}.

## How to demo / where to look (quick guide)
- Run the demo helper (creates a temporary subscription that notifies both mood and ingest, posts a full telemetry CI, tails logs and prints DB rows):
  - From repo root:
    bash ./scripts/e2e-watch.sh --demo --timeout 30 --co2 720
- GUI locations (CSE web UI)
  - Telemetry CINs: cse-in → cloud-analytics → telemetry → room-101 → sample → select CIN → JSON tab (see posted metrics)
  - Mood CINs: cse-in → cloud-analytics → analytics → mood → score → select CIN → JSON tab (see `con.score`, `con.label`, and `con.components`)
- Logs to tail for live observation
  - docker-compose logs --follow acme-onem2m-cse
  - docker-compose logs --follow mood
  - docker-compose logs --follow ingest
- DB checks
  - fact_mood_scores: docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT ts, score, label FROM fact_mood_scores ORDER BY ts DESC LIMIT 10;"
  - raw_onem2m_ci: docker exec -i onem2m_postgres psql -U onem2m -d onem2m -c "SELECT ci_rn, parent_path, created_at FROM raw_onem2m_ci ORDER BY created_at DESC LIMIT 10;"

## Branch / PR reference
- Feature branch with the interactive changes: `feature/mood-explainability-e2e`
- PR candidate: https://github.com/BenBlack0902/iot_onem2m/pull/new/feature/mood-explainability-e2e
