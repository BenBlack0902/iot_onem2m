# Product Context

## Project Name
IN-CSE (Cloud) — Cloud Analytics / Mood Service

## Purpose (why this exists)
Provide a central system-of-record and analytics hub that:
- Receives aggregated room telemetry samples from MN-CSE (edge).
- Triggers mood computation via subscription callbacks.
- Persists mood results and exposes the latest result for a UI.
- Optionally emits cloud→edge commands (lighting / HVAC) based on policy.

## High-level goals
- Reliable ingestion of telemetry (co2, noise, lux, temp, rh, occ, ts) from rooms.
- Fast notification to mood-service on new telemetry (target ≤ 2s delivery).
- Compute a 0..100 mood score and categorical label (focus, neutral, tired).
- Persist mood results as oneM2M content instances under analytics/mood/score and make the "latest" readable by UI.
- Minimal, auditable deployment on a VPS (Docker containers recommended).

## Success / Acceptance Criteria
- IN-CSE (ACME recommended) running and reachable on HTTP (:8080).
- cloud-analytics AE and required CNT resources created.
- Subscription(s) deliver notifications to mood-service within ≤2s.
- Mood content instances are written back and visible via `/la`.
- (Optional) Commands appear under `/commands/*` and can be subscribed to by MN.
