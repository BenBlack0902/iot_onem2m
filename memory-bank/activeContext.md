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
