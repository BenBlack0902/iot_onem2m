# Tech Context

## Runtime & Deployment
- Target host: VPS (example IP provided by owner)
- Containerization: Docker + docker-compose recommended for reproducible deploys
- Ports:
  - ACME CSE (IN-CSE): 8080 (HTTP)
  - Mood Service: 8088 (HTTP)
- Network: single docker-compose network so services can refer to each other by name (`acme`, `mood`)

## Components
- ACME CSE (oneM2M IN-CSE)
  - Run as a container (ACME distribution or equivalent)
  - Expose HTTP API for resource creation and subscription management
  - Basic auth acceptable for development (CSE_ORIGIN=admin:admin)
- Mood Service
  - Minimal FastAPI application (Python) with endpoints:
    - POST /notify — receives oneM2M notifications from IN-CSE
    - (Optional) GET /latest-mood?room=101 — reads latest mood from CSE and returns JSON
  - Responsible for computing mood score/label and writing mood CINs back to IN-CSE
- Local testing utilities
  - curl / httpie for POSTing sample telemetry
  - small Python script to simulate MN-CSE posting telemetry CINs for testing

## Environment variables (.env suggested)
CSE_BASE=http://acme:8080/~/in-cse/in-name
CSE_ORIGIN=admin:admin
MOOD_NOTIFY=http://mood:8088/notify
ROOM_IDS=room-101

## Data formats
- Telemetry sample (oneM2M CIN con): JSON
  {"co2": 935, "noise": 58, "lux": 320, "temp": 23.1, "rh": 41, "occ": 2, "ts": 1738075200}
- Mood result (oneM2M CIN con): JSON
  {"score": 78, "label": "focus", "ts": 1738075210}

## Tooling & Dependencies
- Python 3.10+ (for FastAPI)
- pip dependencies: fastapi, uvicorn, httpx, pydantic
- Docker Engine + docker-compose
- Optional: Postman or HTTP client for manual tests

## Sample docker-compose design (high level)
services:
  acme:
    image: acme-cse:latest
    ports: ["8080:8080"]
    environment:
      - PAYLOADS, ...
  mood:
    build: ./mood-service
    ports: ["8088:8088"]
    environment:
      - CSE_BASE=http://acme:8080/~/in-cse/in-name
      - CSE_ORIGIN=admin:admin
    depends_on:
      - acme

## Operational notes
- Use `mni` (max instances) on high-rate containers to avoid unbounded storage growth.
- Keep VPS time synced (ntp/systemd-timesyncd) to ensure `ts` alignment.
- For production, replace basic auth with TLS + proper auth mechanisms and restrict `/notify` access.
