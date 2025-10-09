# Progress

## Summary
This file tracks the current status of the IN-CSE Cloud Analytics / Mood Service project and records progress across sessions.

## Completed
- Project brief reviewed and stored in `memory-bank/project-brief.md`.
- `productContext.md`, `activeContext.md`, `systemPatterns.md`, and `techContext.md` created and populated from the project brief.

## In progress
- Scaffolding mood-service (FastAPI) and docker-compose (planned).
- Starting ACME CSE container and creating AE/CNT resource tree (planned).
- Creating subscription(s) and testing end-to-end flow (planned).
- Integrating PostgreSQL and Grafana for telemetry storage and visualization (in progress — init SQL, docker-compose, and Grafana provisioning added).
- Adding ingestion service `ingest/` to process oneM2M notifications and populate the database (added and smoke-tested).

## Blockers / Risks
- ACME CSE container image availability (need image name or build instructions).
- VPS networking / firewall configuration to expose ports 8080 and 8088.
- Security: current plan uses basic auth for CSE; must be hardened before production.
- Secrets: `.env` is used for local development; move to Docker secrets or a secrets manager for production.

## Next steps
- Create mood-service scaffold under `mood-service/` and add Dockerfile (existing).
- Add ingestion logic in `mood-service` and add Dockerfile (existing):
  - Write incoming oneM2M ContentInstance payloads into `raw_onem2m_ci`.
  - Parse and upsert `dim_room`, `dim_device`, `dim_metric`.
  - Insert parsed rows into `fact_telemetry`.
  - Optionally refresh `mv_latest_5m` periodically (helper function `refresh_mv_latest_5m` exists).
- Add Grafana dashboards and optionally provision them under `grafana/provisioning/dashboards`.
- Harden security and move credentials out of `.env`.
- Test end-to-end data flow: POST a sample ContentInstance to the CSE, ensure it appears in the DB and in Grafana queries.

## Notes
- Update this file after each major session so Cline can track ongoing progress across conversations.

## Recent changes
- 2025-10-02: Persisted net.ipv4.ip_forward=1 and installed iptables-persistent on the cloud VPS (debian-8gb-fsn1-1). Saved current iptables rules so WireGuard NAT (wg0) and forwarding persist across reboots. Commands executed: `echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-wireguard.conf; sudo sysctl --system; sudo apt-get install -y iptables-persistent; sudo netfilter-persistent save`.
- 2025-10-09: Added PostgreSQL + Grafana to the local development stack on branch `feature/database-dashboarding`:
  - Created `postgres/init.sql` which defines `dim_room`, `dim_device`, `dim_metric`, `raw_onem2m_ci`, `fact_telemetry`, helpful indexes, `v_room_metrics`, `v_room_comfort`, and materialized view `mv_latest_5m`. Provided a helper function `refresh_mv_latest_5m` and created role `onem2m_app`.
  - Updated `docker-compose.yml` to add `postgres` and `grafana` services and persistent volumes.
  - Added Grafana provisioning file `grafana/provisioning/datasources/datasource.yml` to auto-provision the PostgreSQL datasource.
  - Created local `.env` (excluded from git) for DB and Grafana credentials used in local development.
  - Started `postgres` and `grafana` containers and verified:
    - Database objects exist (tables, view, materialized view).
    - Grafana successfully provisions and connects to the Postgres datasource (`Database Connection OK`).
  - Left next steps: implement ingestion in `mood-service` and add dashboards/provisioning.
- 2025-10-09: Added `ingest` service (Flask) and Dockerfile; updated `docker-compose.yml`. Performed smoke test:
  - Sent test payload via `POST /test-insert` to http://localhost:8089/test-insert.
  - Database rows were inserted into `fact_telemetry` for `cin-smoketest-001` (temperature & humidity).
- 2025-10-09: Restricted public access to ACME web UI
  - Bound ACME container to localhost by changing `docker-compose.yml` mapping from `8080:8080` to `127.0.0.1:8080:8080` so the container no longer exposes the UI on the host public interface.
  - Updated WireGuard hub config `wireguard-onem2m-setup/configs/cloud/wg0.conf` PostUp/PostDown to add iptables rules that ACCEPT TCP/8080 from the VPN peers:
    - Alper: 10.100.0.2
    - Tahir: 10.100.0.4
    and DROP other TCP/8080 traffic.
  - Applied runtime iptables rules on the host to immediately enforce the allowlist (INPUT ACCEPT for 10.100.0.2 and 10.100.0.4, and a default DROP for TCP/8080).
  - Added an iptables NAT PREROUTING rule to allow wg0 peers to reach the localhost-bound service (so VPN peers can access the GUI).
  - Restarted the ACME container so the new localhost binding took effect.
  - Verification: ACME now listens on 127.0.0.1:8080; public requests to http://91.98.80.99:8080 are blocked while VPN peers should be able to reach the UI. Final verification from Tahir/Alper is recommended.
Action items:
- (Optional) Replace runtime NAT redirect with nginx reverse proxy for better control, TLS, and logging.
- (Optional) Rotate ACME admin credentials and ensure GUI endpoints require app-level auth if needed.
- (Optional) Persist NAT PREROUTING in wg PostUp/PostDown or move redirect logic to a small systemd unit so it survives reboots.
