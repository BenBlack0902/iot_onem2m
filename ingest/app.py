from flask import Flask, request
import os, json, datetime
import psycopg2
from psycopg2.extras import Json

app = Flask(__name__)

PG_DSN = os.getenv("DATABASE_URL", "postgresql://onem2m:onem2m_pass@postgres:5432/onem2m")
# Allow libpq style or DSN
conn = psycopg2.connect(PG_DSN)

def parse_ct(ct):
    try:
        return datetime.datetime.strptime(ct, "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None

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

    return ("", 204)

# Helper for testing without CSE
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
