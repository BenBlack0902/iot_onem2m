# System Patterns

## Overview
This document records the system architecture and recurring design patterns for the IN-CSE cloud analytics / mood-service project. It captures the oneM2M resource layout, integration patterns, subscription flow, and operational constraints.

## Architecture (high-level)
- ACME CSE (IN-CSE) — containerized, exposes HTTP on :8080
- Mood Service — lightweight HTTP service (FastAPI) on :8088
- Network: Both run in same Docker network for service discovery (`acme` and `mood`)
- UI (optional) — reads latest mood via CSE `/la` or thin API on the mood-service

## Resource Tree (canonical)
```
/in-cse/in-name/
└── cloud-analytics (AE)
    ├── telemetry/ (cnt)
    │   └── room-101/ (cnt)
    │       └── sample/ (cnt) → cin {co2, noise, lux, temp, rh, occ, ts}
    ├── analytics/ (cnt)
    │   └── mood/ (cnt)
    │       └── score/ (cnt) → cin {score, label, ts}
    ├── commands/ (cnt) (optional)
    │   ├── lighting/ (cnt) → cin {scene, target, ts}
    │   └── hvac/ (cnt) → cin {fan, reason, ts}
    └── subs/ (cnt)
        └── sub_room101_mood (sub) → notify mood-service on new telemetry/.../sample cin
```

## Integration Pattern: Telemetry → Mood-Service → mood CIN
1. Edge (MN-CSE) posts telemetry CIN to:
   - POST /~/in-cse/in-name/cloud-analytics/telemetry/room-101/sample (ty=4)
2. IN-CSE SUB configured on `telemetry/room-101/sample` triggers on resource-created (enc.net = [3]) and sends notification (nct=2) to:
   - http://mood:8088/notify (full representation containing m2m:cin.con)
3. Mood Service extracts `con` from `m2m:sgn.nev.rep.m2m:cin.con`, computes score/label, and writes a new CIN to:
   - POST /~/in-cse/in-name/cloud-analytics/analytics/mood/score (ty=4, con = {"score": n, "label": "...", "ts": ...})

## Subscription details
- Target: telemetry/room-<id>/sample
- Event filter: resource created (enc.net = [3])
- Delivery: nu = ["http://mood:8088/notify"]
- Payload: nct = 2 (full representation; notification contains m2m:cin with `con` payload)

## Data Contracts (JSON)
- Telemetry sample:
  {"co2": 935, "noise": 58, "lux": 320, "temp": 23.1, "rh": 41, "occ": 2, "ts": 1738075200}
- Mood result:
  {"score": 78, "label": "focus", "ts": 1738075210}
- Command (optional):
  {"scene": "warm_dim", "target": "room", "ts": 1738075215}

## Design & System Patterns
- Push-based ingestion: edge systems push CINs; CSE stores and SUB forwards.
- Full-representation notifications (nct=2) simplify payload parsing on mood-service.
- Idempotent writes: mood-service should ensure repeated notifications don't create duplicate mood CINs (e.g., de-dupe by telemetry ts or request id).
- Keep `mni` (max instances) for high-rate CNTs to bound storage (e.g., mni = 500).
- Thin-read pattern: expose `/la` on CNTs for latest CIN retrieval; optionally create a thin API that reads and translates CSE data for the UI.
- Security: restrict `/notify` by IP allowlist and use basic auth for CSE write operations in early stages.

## Operational notes
- Prioritize time-synchronization on VPS for consistent `ts` values.
- Monitor subscription delivery latency and retry behavior.
- Use container labels and logs for tracing notification flows (CSE logs + mood-service logs).
- For development/hackathon: basic auth is acceptable; plan to rotate credentials before production.

## Links / References
- project-brief.md (source of truth)
- activeContext.md (current work focus)
