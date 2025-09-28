# IN-CSE (Cloud) — Project Brief

## 1) Purpose
Central "system-of-record" and analytics hub:
- Receive **aggregated room samples** from MN-CSE.
- Trigger **mood computation** via subscription callbacks.
- Persist **mood results** and expose **latest** for UI.
- (Optional) Emit **cloud→edge commands** (lighting/HVAC).

---

## 2) Responsibilities (yours)
- Deploy **IN-CSE** (ACME recommended) on VPS and expose HTTP.
- Create and maintain the **cloud resource tree** (below).
- Define **subscriptions** to your **mood-service**.
- Run **mood-service** (HTTP) that writes results back to IN-CSE.
- Provide a simple **read interface** (CSE `/la` or tiny API) to UI.

---

## 3) Resource Tree (IN-CSE)
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

> Add more rooms as `telemetry/room-<id>/sample`.

---

## 4) Data Contracts (JSON)

**Telemetry sample (MN→IN)**
```json
{"co2": 935, "noise": 58, "lux": 320, "temp": 23.1, "rh": 41, "occ": 2, "ts": 1738075200}
```

**Mood result (IN)**
```json
{"score": 78, "label": "focus", "ts": 1738075210}
```

**Command (IN→MN, optional)**
```json
{"scene": "warm_dim", "target": "room", "ts": 1738075215}
```

---

## 5) Subscriptions (IN-CSE → Mood Service)
- **Target**: `telemetry/room-<id>/sample`
- **Event**: `enc.net = [3]` (resource created)
- **Delivery**: `nu = ["http://<mood-service>:8088/notify"]`
- **Payload**: `nct = 2` (full representation with m2m:cin.con)

---

## 6) Mood Service (your cloud app)
- **Input**: oneM2M notification → extract `con` from `m2m:sgn.nev.rep.m2m:cin.con`
- **Process**: compute score 0..100 and label ∈ {focus, neutral, tired} (heuristic first, ML later)
- **Output**: POST cin to `/cloud-analytics/analytics/mood/score`
- **Optional**: write command cin to `/cloud-analytics/commands/*` based on policy

---

## 7) Read Interfaces for UI

**ACME latest:**
```
GET /~/in-cse/in-name/cloud-analytics/analytics/mood/score/la
```

**(Optional) Thin API:**
```
GET /latest-mood?room=101 → {score,label,ts} (service reads from CSE)
```

---

## 8) Deployment (VPS)
- **ACME CSE**: container, expose :8080 HTTP (basic auth OK for hackathon).
- **Mood Service**: FastAPI/Flask on :8088 (same docker network as ACME).

**Env (.env):**
```ini
CSE_BASE=http://acme:8080/~/in-cse/in-name
CSE_ORIGIN=admin:admin
MOOD_NOTIFY=http://mood:8088/notify
ROOM_IDS=room-101
```

---

## 9) Security & Ops (minimal)
- **Firewall**: expose only :8080 (CSE) and :8088 (mood). Keep MQTT private.
- Rotate default creds; consider IP allowlist for `/notify`.
- Set container retention on high-rate CNTs: `mni` (e.g., 500) to cap history.
- Include `ts` in all payloads; use VPS time sync.

---

## 10) Acceptance Criteria (IN-CSE slice)
- [ ] IN-CSE up; cloud-analytics AE and CNTs created.
- [ ] SUB delivers notifications to mood-service within ≤2 s.
- [ ] Mood cin written back and visible via `/la`.
- [ ] (Optional) Command cin appears under `/commands/*` and is consumable by MN.
- [ ] Dashboard can read latest mood without custom glue.

---

## 11) Handoffs to MN-CSE Team

**Where to POST samples:**
```
/~/in-cse/in-name/cloud-analytics/telemetry/room-<id>/sample (ty=4, con = sample JSON)
```

**Command watch:** subscribe MN to:
```
/~/in-cse/in-name/cloud-analytics/commands/** (lighting/hvac)
```