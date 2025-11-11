from flask import Flask, request
import os, json, datetime
import time
import httpx
import psycopg2
from psycopg2.extras import Json

app = Flask(__name__)

PG_DSN = os.getenv("DATABASE_URL", "postgresql://onem2m:onem2m_pass@postgres:5432/onem2m")

def connect_with_retry(dsn, retries=10, delay=2):
    """
    Attempt to connect to Postgres with a retry loop to tolerate DB startup delays.
    Returns a psycopg2 connection or raises the last exception.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(dsn)
            app.logger.info("Connected to Postgres on attempt %d/%d", attempt, retries)
            return conn
        except Exception as exc:
            last_exc = exc
            app.logger.warning("Postgres connection attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(delay)
    app.logger.error("Could not connect to Postgres after %d attempts, raising.", retries)
    raise last_exc

# Allow libpq style or DSN, but connect with retry to avoid startup race
conn = connect_with_retry(PG_DSN)

def parse_ct(ct):
    try:
        return datetime.datetime.strptime(ct, "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None

# Normalize incoming content instances into a canonical structure
def normalize_payload(con):
    """
    Accepts various incoming shapes and returns a dict:
      {
        "metrics": [{"name": "temperature","value": 21.2, "text": None, "unit": None}, ...],
        "device": <device_rn or None>,
        "room": <room_rn or None>,
        "qos": <qos dict or {}>,
        "ts": <timestamp int or None>
      }
    This implementation is more robust: it will:
      - handle compact "metrics" array,
      - handle flat keys like tempe/humiy/co2,
      - recursively scan nested announcement objects (cod:*, m2m:cbA, etc.)
      - extract room label from "lbl" entries like "room:Room01"
    """
    canonical_map = {
        "tempe": "temperature",
        "temp": "temperature",
        "temperature": "temperature",
        "humiy": "humidity",
        "rh": "humidity",
        "humidity": "humidity",
        "co2": "co2",
        "co2ppm": "co2",
        "lux": "lux",
        "noise": "noise",
        "occ": "occupancy",
        "occupancy": "occupancy",
    }

    def as_number(v):
        try:
            return float(v)
        except Exception:
            return None

    out = {"metrics": [], "device": None, "room": None, "qos": {}, "ts": None}
    if con is None:
        return out

    # Quick path: compact metrics array
    if isinstance(con, dict) and "metrics" in con and isinstance(con["metrics"], list):
        out["device"] = con.get("device")
        out["room"] = con.get("room")
        out["qos"] = con.get("qos", {})
        out["ts"] = con.get("ts")
        for m in con["metrics"]:
            name = m.get("name")
            if not name:
                continue
            canon = canonical_map.get(name, name)
            val = m.get("value")
            txt = m.get("text")
            unit = m.get("unit")
            out["metrics"].append({"name": canon, "value": as_number(val), "text": txt, "unit": unit})
        return out

    # Try flat keys and then a recursive scan for nested announcement structures
    if isinstance(con, dict):
        # flat metadata
        out["device"] = con.get("device")
        out["room"] = con.get("room")
        out["qos"] = con.get("qos", {})
        if "ts" in con:
            out["ts"] = con.get("ts")
        elif "ct" in con:
            out["ts"] = con.get("ct")

        # collect metrics from top-level flat keys
        for k, v in con.items():
            lk = k.lower()
            if lk in canonical_map:
                out["metrics"].append({"name": canonical_map[lk], "value": as_number(v), "text": None, "unit": None})

    # recursive extractor for nested structures (handles cod:*, m2m:cbA and similar)
    def extract_from(obj):
        if isinstance(obj, dict):
            # check for metric-like keys at this level
            for k, v in obj.items():
                lk = k.lower()
                if lk in canonical_map:
                    out["metrics"].append({"name": canonical_map[lk], "value": as_number(v), "text": None, "unit": None})
                # room label extraction
                if k == "lbl" and isinstance(v, list):
                    for entry in v:
                        if isinstance(entry, str) and entry.startswith("room:") and not out.get("room"):
                            out["room"] = entry.split("room:")[-1]
                # device/resource name
                if k == "rn" and isinstance(v, str) and not out.get("device"):
                    out["device"] = v
                # dive into nested structures (including cod:* keys)
                if isinstance(v, (dict, list)):
                    extract_from(v)
        elif isinstance(obj, list):
            for item in obj:
                extract_from(item)

    extract_from(con)

    # Final: dedupe metrics by name keeping first encountered value
    seen = set()
    deduped = []
    for m in out["metrics"]:
        key = (m.get("name"))
        if key in seen:
            continue
        if m.get("value") is None:
            continue
        seen.add(key)
        deduped.append(m)
    out["metrics"] = deduped

    return out

def post_to_mood(normalized, ci_rn=None, ct=None, parent=None):
    """
    Post normalized telemetry as a oneM2M-style notification to the mood-service notify endpoint.
    Mood service expects a notification containing m2m:cin.con somewhere; we'll send:
    {"m2m:sgn": {"nev": {"rep": {"m2m:cin": {"rn": <ci_rn>, "ct": <ct>, "con": <telemetry dict>}}}}, "sur": <parent>}
    """
    try:
        payload = {
            "m2m:sgn": {
                "nev": {
                    "rep": {
                        "m2m:cin": {
                            "rn": ci_rn or "ingest-cin",
                            "ct": ct or datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S"),
                            "con": {}
                        }
                    }
                },
                "sur": parent or ""
            }
        }
        # Build a telemetry dict from normalized
        telemetry = {}
        # prefer metric names directly (temperature, humidity, co2, lux, noise, occupancy)
        for m in normalized.get("metrics", []):
            if m.get("name") and m.get("value") is not None:
                telemetry[m["name"]] = m["value"]
        # copy some metadata
        if normalized.get("room"):
            telemetry["room"] = normalized.get("room")
        if normalized.get("device"):
            telemetry["device"] = normalized.get("device")
        if normalized.get("ts"):
            telemetry["ts"] = normalized.get("ts")
        payload["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"] = telemetry

        # send to mood service
        client = httpx.Client(timeout=5.0)
        url = os.getenv("MOOD_NOTIFY", "http://mood:8088/notify")
        resp = client.post(url, json=payload)
        if resp is None:
            app.logger.warning("post_to_mood: no response from mood service for ci_rn=%s", ci_rn)
        else:
            if resp.status_code >= 400:
                app.logger.warning("post_to_mood: mood service returned status %s body=%s", resp.status_code, resp.text)
            else:
                app.logger.debug("post_to_mood: mood service accepted payload, status=%s", resp.status_code)
    except Exception as exc:
        app.logger.exception("post_to_mood failed: %s", exc)

@app.post("/onem2m")
def onem2m():
    sgn = request.get_json(force=True)

    if sgn.get("m2m:sgn", {}).get("vrq") is True:
        return ("", 200)

    s = sgn.get("m2m:sgn", {})
    rep = s.get("nev", {}).get("rep", {})
    cin = rep.get("m2m:cin", {}) or rep

    ci_rn = cin.get("rn")
    ct = cin.get("ct")
    con = cin.get("con")
    parent = s.get("sur") or "unknown"

    # CSE sometimes stores the content as a JSON-encoded string; parse if necessary
    if isinstance(con, str):
        try:
            con = json.loads(con)
        except Exception:
            # leave as-is if it's not valid JSON
            pass

    # Log receipt for debugging purposes
    app.logger.info("ingest: received ci rn=%s parent=%s", ci_rn, parent)

    ts_cse = parse_ct(ct)

    with conn, conn.cursor() as cur:
        # Raw (idempotent)
        cur.execute("""
          INSERT INTO raw_onem2m_ci (parent_path, ci_rn, created_at, payload)
          VALUES (%s, %s, %s, %s)
          ON CONFLICT (parent_path, ci_rn) DO NOTHING
        """, (parent, ci_rn, ts_cse, Json(con)))

        # Explode if payload matches our compact format
        if isinstance(con, dict):
            device = con.get("device")
            room   = con.get("room")
            qos    = con.get("qos", {})
            metrics = con.get("metrics", [])
            if room:
                cur.execute("INSERT INTO dim_room(room_rn) VALUES (%s) ON CONFLICT (room_rn) DO NOTHING", (room,))
                cur.execute("SELECT room_id FROM dim_room WHERE room_rn=%s", (room,))
                row = cur.fetchone()
                room_id = row[0] if row else None
            else:
                room_id = None

            if device:
                cur.execute("""
                  INSERT INTO dim_device(device_rn, room_id) VALUES (%s,%s)
                  ON CONFLICT (device_rn) DO UPDATE SET room_id=COALESCE(EXCLUDED.room_id, dim_device.room_id)
                  RETURNING device_id
                """, (device, room_id))
                row = cur.fetchone()
                device_id = row[0] if row else None
            else:
                device_id = None

            # existing compact metrics handling
            for m in metrics:
                name = m.get("name"); val = m.get("value"); txt = m.get("text"); unit = m.get("unit")
                if not name:
                    continue
                cur.execute("""
                  INSERT INTO dim_metric(metric_rn, unit) VALUES (%s,%s)
                  ON CONFLICT (metric_rn) DO UPDATE SET unit = COALESCE(EXCLUDED.unit, dim_metric.unit)
                  RETURNING metric_id
                """, (name, unit))
                row = cur.fetchone()
                metric_id = row[0] if row else None

                cur.execute("""
                  INSERT INTO fact_telemetry (ts_cse, device_id, metric_id, value, value_text, quality, parent_path, ci_rn)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                  ON CONFLICT (parent_path, ci_rn, metric_id) DO NOTHING
                """, (ts_cse, device_id, metric_id, val, txt, Json(qos), parent, ci_rn))

            # New: handle normalized payloads from other shapes
            normalized = normalize_payload(con)
            app.logger.info("ingest: normalize_payload result for ci_rn=%s: %s", ci_rn, normalized)
            if normalized and normalized.get("metrics"):
                # attempt to extract room/device if not already set
                if not room and normalized.get("room"):
                    room = normalized.get("room")
                    cur.execute("INSERT INTO dim_room(room_rn) VALUES (%s) ON CONFLICT (room_rn) DO NOTHING", (room,))
                    cur.execute("SELECT room_id FROM dim_room WHERE room_rn=%s", (room,))
                    row = cur.fetchone()
                    room_id = row[0] if row else None

                if not device and normalized.get("device"):
                    device = normalized.get("device")
                    cur.execute("""
                      INSERT INTO dim_device(device_rn, room_id) VALUES (%s,%s)
                      ON CONFLICT (device_rn) DO UPDATE SET room_id=COALESCE(EXCLUDED.room_id, dim_device.room_id)
                      RETURNING device_id
                    """, (device, room_id))
                    row = cur.fetchone()
                    device_id = row[0] if row else None

                for m in normalized.get("metrics", []):
                    name = m.get("name"); val = m.get("value"); txt = m.get("text"); unit = m.get("unit")
                    app.logger.info("ingest: normalized metric for ci_rn=%s -> name=%s value=%s unit=%s", ci_rn, name, val, unit)
                    if not name or val is None:
                        app.logger.info("ingest: skipping metric (missing name or value) for ci_rn=%s : %s", ci_rn, m)
                        continue
                    try:
                        cur.execute("""
                          INSERT INTO dim_metric(metric_rn, unit) VALUES (%s,%s)
                          ON CONFLICT (metric_rn) DO UPDATE SET unit = COALESCE(EXCLUDED.unit, dim_metric.unit)
                          RETURNING metric_id
                        """, (name, unit))
                        row = cur.fetchone()
                        metric_id = row[0] if row else None

                        cur.execute("""
                          INSERT INTO fact_telemetry (ts_cse, device_id, metric_id, value, value_text, quality, parent_path, ci_rn)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (parent_path, ci_rn, metric_id) DO NOTHING
                        """, (ts_cse, device_id, metric_id, val, txt, Json(normalized.get("qos", {})), parent, ci_rn))
                    except Exception:
                        app.logger.exception("ingest: failed inserting normalized metric for ci_rn=%s name=%s", ci_rn, name)

                # After inserting, forward normalized to mood-service
                try:
                    post_to_mood(normalized, ci_rn=ci_rn, ct=ct, parent=parent)
                    app.logger.info("ingest: forwarded normalized payload to mood-service for ci_rn=%s", ci_rn)
                except Exception:
                    app.logger.exception("Failed to post normalized payload to mood-service")

    return ("", 204)

# Helper for testing without CSE and to accept subscription-delivered notifications
@app.post("/notify")
def notify():
    # ACME subscriptions commonly POST to /notify; reuse existing onem2m() processing
    return onem2m()

@app.post("/")
def root_notify():
    # Some subscriptions may point at the root path; accept and reuse onem2m()
    return onem2m()

@app.post("/test-insert")
def test_insert():
    data = request.get_json(force=True)
    sgn = {
      "m2m:sgn": {
        "nev": {"rep": {"m2m:cin": {
          "rn": data.get("rn","cin-test"),
          "ct": data.get("ct"),  # like 20251009T153210
          "con": data["con"]
        }}},
        "sur": data.get("parent","/cloud-analytics/telemetry/room-101/sample")
      }
    }
    with app.test_request_context(json=sgn):
        return onem2m()

if __name__ == "__main__":
    # Listen on internal port 8088; docker-compose will map host port 8089 to this container port
    app.run(host="0.0.0.0", port=8088)
