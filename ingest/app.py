from flask import Flask, request
import os, json, datetime
import time
import httpx
import psycopg2
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool
import re

app = Flask(__name__)

@app.before_request
def log_raw_body():
    try:
        raw = request.get_data(cache=True, as_text=True)
        app.logger.warning("==== RAW REQUEST BODY START ====")
        app.logger.warning(raw)
        app.logger.warning("==== RAW REQUEST BODY END ====")
    except Exception as e:
        app.logger.error(f"Could not read body: {e}")


PG_DSN = os.getenv("DATABASE_URL", "postgresql://onem2m:onem2m_pass@postgres:5432/onem2m")
BUFFERED = os.getenv("INGEST_BUFFERED", "1") == "1"



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

# Initialize a threaded connection pool to avoid re-entrancy on a single connection
DB_POOL_MIN = int(os.getenv("INGEST_DB_MIN_CONN", "1"))
DB_POOL_MAX = int(os.getenv("INGEST_DB_MAX_CONN", "10"))

def init_pool_with_retry(dsn, minconn, maxconn, retries=10, delay=2):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            pool = ThreadedConnectionPool(minconn, maxconn, dsn)
            app.logger.info("Created Postgres connection pool on attempt %d/%d", attempt, retries)
            return pool
        except Exception as exc:
            last_exc = exc
            app.logger.warning("Postgres pool creation attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(delay)
    app.logger.error("Could not create Postgres pool after %d attempts, raising.", retries)
    raise last_exc

pool = init_pool_with_retry(PG_DSN, DB_POOL_MIN, DB_POOL_MAX)

def borrow_conn():
    return pool.getconn()

def release_conn(c):
    try:
        pool.putconn(c)
    except Exception:
        pass


def parse_ct(ct):
    """
    Parse oneM2M creation timestamp into a datetime object.
    Handles formats:
    - 20251114T215730 (basic)
    - 20251114T215730,684403 (with microseconds and comma)
    - 20251114T215730.684403 (with microseconds and dot)
    """
    print(f"DEBUG parse_ct: received ct='{ct}' type={type(ct)}")

    if not ct:
        print(f"DEBUG parse_ct: ct is empty/None")
        return None

    try:
        # Remove microseconds if present (after comma or dot)
        clean_ct = str(ct).split(',')[0].split('.')[0]
        print(f"DEBUG parse_ct: cleaned ct='{clean_ct}'")
        result = datetime.datetime.strptime(clean_ct, "%Y%m%dT%H%M%S").replace(tzinfo=datetime.timezone.utc)
        print(f"DEBUG parse_ct: parsed result={result}")
        return result
    except Exception as e:
        # Log the error for debugging
        print(f"DEBUG parse_ct: ERROR - {e}")
        app.logger.warning(f"Failed to parse timestamp '{ct}': {e}")
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
        "louds": "noise",
        "noise": "noise",
        "occ": "occupancy",
        "occupancy": "occupancy",
    }

    def as_number(v):
        try:
            # Explicit boolean handling
            if isinstance(v, bool):
                return 1.0 if v else 0.0
            # Fast-path numbers
            if isinstance(v, (int, float)):
                return float(v)
            # Strings that might be booleans or numbers
            if isinstance(v, str):
                s = v.strip().lower()
                if s in ("true", "false"):
                    return 1.0 if s == "true" else 0.0
                try:
                    return float(s)
                except Exception:
                    return None
            return float(v)
        except Exception:
            return None

    out = {"metrics": [], "device": None, "room": None, "qos": {}, "ts": None, "labels": {}}
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
            num = as_number(val)
            if num is None and txt is None and val is not None:
                try:
                    txt = str(val)
                except Exception:
                    pass
            out["metrics"].append({"name": canon, "value": num, "text": txt, "unit": unit})
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
                _num = as_number(v)
                _txt = None
                if _num is None and v is not None:
                    try:
                        _txt = str(v)
                    except Exception:
                        _txt = None
                out["metrics"].append({"name": canonical_map[lk], "value": _num, "text": _txt, "unit": None})

    # recursive extractor for nested structures (handles cod:*, m2m:cbA and similar)
    def extract_from(obj):
        if isinstance(obj, dict):
            # check for metric-like keys at this level
            for k, v in obj.items():
                lk = k.lower()
                if lk in canonical_map:
                    _num2 = as_number(v)
                    _txt2 = None
                    if _num2 is None and v is not None:
                        try:
                            _txt2 = str(v)
                        except Exception:
                            _txt2 = None
                    out["metrics"].append({"name": canonical_map[lk], "value": _num2, "text": _txt2, "unit": None})
                # room label extraction
                if k == "lbl" and isinstance(v, list):
                    # Parse label entries like "key:value" into a labels dict
                    if "labels" not in out or not isinstance(out.get("labels"), dict):
                        out["labels"] = {}
                    for entry in v:
                        if isinstance(entry, str) and ":" in entry:
                            key, val = entry.split(":", 1)
                            out["labels"][key] = val
                            if key == "room" and not out.get("room"):
                                out["room"] = val
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

    # Device fallback: if no device was discovered, use labels.desk when available
    try:
        if not out.get("device"):
            labels = out.get("labels") or {}
            if isinstance(labels, dict) and labels.get("desk"):
                out["device"] = labels.get("desk")
    except Exception:
        pass

    return out


def post_to_mood(normalized, ci_rn=None, ct=None, parent=None):
    """
    Post normalized telemetry as a oneM2M-style notification to one or more mood endpoints.
    Builds the same oneM2M-shaped payload and POSTs to:
      - MOOD_NOTIFY (default http://mood:8088/notify)
      - MOOD_NOTIFY_ML (optional)
      - MOOD_NOTIFY_TARGETS (optional, comma/space separated list)
    Failures are logged per-target and do not abort ingest processing.
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
        for m in normalized.get("metrics", []):
            if m.get("name") and m.get("value") is not None:
                telemetry[m["name"]] = m["value"]

        if normalized.get("room"):
            telemetry["room"] = normalized.get("room")
        if normalized.get("device"):
            telemetry["device"] = normalized.get("device")
        if normalized.get("ts"):
            telemetry["ts"] = normalized.get("ts")

        labels = normalized.get("labels") or {}
        if isinstance(labels, dict) and labels:
            telemetry["labels"] = labels
            if "desk" in labels:
                telemetry["desk"] = labels["desk"]
            if "sensor" in labels:
                telemetry["sensor"] = labels["sensor"]

        payload["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"] = telemetry

        # Build target list
        targets = []
        primary = os.getenv("MOOD_NOTIFY", "http://mood:8088/notify")
        if primary:
            targets.append(primary.strip())
        ml = os.getenv("MOOD_NOTIFY_ML")
        if ml:
            targets.append(ml.strip())
        others = os.getenv("MOOD_NOTIFY_TARGETS", "")
        if others:
            # split on commas or whitespace
            for t in re.split(r"[,\s]+", others.strip()):
                if t:
                    targets.append(t)

        # dedupe while preserving order
        seen = set()
        uniq_targets = []
        for t in targets:
            if not t:
                continue
            if t in seen:
                continue
            seen.add(t)
            uniq_targets.append(t)

        client = httpx.Client(timeout=5.0)
        for url in uniq_targets:
            try:
                resp = client.post(url, json=payload)
                if resp is None:
                    app.logger.warning("post_to_mood: no response from %s for ci_rn=%s", url, ci_rn)
                else:
                    if resp.status_code >= 400:
                        app.logger.warning("post_to_mood: mood service %s returned status %s body=%s", url, resp.status_code, resp.text)
                    else:
                        app.logger.debug("post_to_mood: mood service %s accepted payload, status=%s", url, resp.status_code)
            except Exception as exc:
                app.logger.exception("post_to_mood: failed to post to %s: %s", url, exc)
    except Exception as exc:
        app.logger.exception("post_to_mood failed: %s", exc)


def process_record(db_conn, parent, ci_rn, ct, con):
    """
    Core processing logic: insert raw_onem2m_ci, upsert dims, insert facts from normalized payload,
    and forward to mood-service.
    """
    # CSE sometimes stores the content as a JSON-encoded string; parse if necessary
    if isinstance(con, str):
        try:
            con = json.loads(con)
        except Exception:
            pass

    ts_cse = parse_ct(ct)

    with db_conn, db_conn.cursor() as cur:
        # Raw (idempotent)
        cur.execute(
            """
          INSERT INTO raw_onem2m_ci (parent_path, ci_rn, created_at, payload)
          VALUES (%s, %s, %s, %s)
          RETURNING id
        """,
            (parent or "unknown", ci_rn, ts_cse, Json(con)),
        )
        row = cur.fetchone()
        raw_id = row[0] if row else None

        # Explode if payload matches our compact format or needs normalization
        device = None
        room = None
        room_id = None
        device_id = None

        if isinstance(con, dict):
            device = con.get("device")
            room = con.get("room")
            qos = con.get("qos", {})
            metrics = con.get("metrics", [])
            if room:
                cur.execute("INSERT INTO dim_room(room_rn) VALUES (%s) ON CONFLICT (room_rn) DO NOTHING", (room,))
                cur.execute("SELECT room_id FROM dim_room WHERE room_rn=%s", (room,))
                row = cur.fetchone()
                room_id = row[0] if row else None

            if device:
                cur.execute(
                    """
                  INSERT INTO dim_device(device_rn, room_id) VALUES (%s,%s)
                  ON CONFLICT (device_rn) DO UPDATE SET room_id=COALESCE(EXCLUDED.room_id, dim_device.room_id)
                  RETURNING device_id
                """,
                    (device, room_id),
                )
                row = cur.fetchone()
                device_id = row[0] if row else None

            # existing compact metrics handling
            for m in metrics:
                name = m.get("name")
                val = m.get("value")
                txt = m.get("text")
                unit = m.get("unit")
                if not name:
                    continue
                cur.execute(
                    """
                  INSERT INTO dim_metric(metric_rn, unit) VALUES (%s,%s)
                  ON CONFLICT (metric_rn) DO UPDATE SET unit = COALESCE(EXCLUDED.unit, dim_metric.unit)
                  RETURNING metric_id
                """,
                    (name, unit),
                )
                row = cur.fetchone()
                metric_id = row[0] if row else None

                cur.execute(
                    """
                  INSERT INTO fact_telemetry (ts_cse, device_id, room_id, metric_id, value, value_text, quality, parent_path, ci_rn, raw_id)
                  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (ts_cse, device_id, room_id, metric_id, val, txt, Json(qos), parent or "unknown", ci_rn, raw_id),
                )

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
                    cur.execute(
                        """
                      INSERT INTO dim_device(device_rn, room_id) VALUES (%s,%s)
                      ON CONFLICT (device_rn) DO UPDATE SET room_id=COALESCE(EXCLUDED.room_id, dim_device.room_id)
                      RETURNING device_id
                    """,
                        (device, room_id),
                    )
                    row = cur.fetchone()
                    device_id = row[0] if row else None

                for m in normalized.get("metrics", []):
                    name = m.get("name")
                    val = m.get("value")
                    txt = m.get("text")
                    unit = m.get("unit")
                    app.logger.info(
                        "ingest: normalized metric for ci_rn=%s -> name=%s value=%s unit=%s",
                        ci_rn,
                        name,
                        val,
                        unit,
                    )
                    if (not name) or (val is None and (txt is None)):
                        app.logger.info(
                            "ingest: skipping metric (missing name or usable value) for ci_rn=%s : %s",
                            ci_rn,
                            m,
                        )
                        continue
                    try:
                        cur.execute(
                            """
                          INSERT INTO dim_metric(metric_rn, unit) VALUES (%s,%s)
                          ON CONFLICT (metric_rn) DO UPDATE SET unit = COALESCE(EXCLUDED.unit, dim_metric.unit)
                          RETURNING metric_id
                        """,
                            (name, unit),
                        )
                        row = cur.fetchone()
                        metric_id = row[0] if row else None

                        cur.execute(
                            """
                          INSERT INTO fact_telemetry (ts_cse, device_id, room_id, metric_id, value, value_text, quality, parent_path, ci_rn, raw_id)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                            (ts_cse, device_id, room_id, metric_id, val, txt, Json(normalized.get("qos", {})), parent or "unknown", ci_rn, raw_id),
                        )
                    except Exception:
                        app.logger.exception(
                            "ingest: failed inserting normalized metric for ci_rn=%s name=%s",
                            ci_rn,
                            name,
                        )

                # After inserting, forward normalized to mood-service
                try:
                    post_to_mood(normalized, ci_rn=ci_rn, ct=ct, parent=parent)
                    app.logger.info(
                        "ingest: forwarded normalized payload to mood-service for ci_rn=%s",
                        ci_rn,
                    )
                except Exception:
                    app.logger.exception("Failed to post normalized payload to mood-service")


def extract_fields_for_queue(body: dict):
    """Return (parent, ci_rn, ct, con) from either oneM2M sgn or raw cod:* object or bare payload."""
    parent = None
    ci_rn = None
    ct = None
    con = None

    if not isinstance(body, dict):
        return (None, None, None, body)

    if "m2m:sgn" in body:
        s = body.get("m2m:sgn", {})
        if s.get("vrq") is True:
            return (None, None, None, None)
        rep = s.get("nev", {}).get("rep", {})
        parent = s.get("sur") or "unknown"
        ci_rn = None
        ct = None
        con = None
        # Case 1: Standard CIN
        if isinstance(rep, dict) and "m2m:cin" in rep:
            cin = rep.get("m2m:cin", {}) or {}
            ci_rn = cin.get("rn")
            ct = cin.get("ct")
            con = cin.get("con")
        else:
            # Case 2: Rep contains a single namespaced announcement object (e.g. mio:*, cod:*, aco:*)
            if isinstance(rep, dict):
                namespaced = [k for k in rep.keys() if isinstance(k, str) and ":" in k]
                if len(namespaced) == 1 and isinstance(rep.get(namespaced[0]), dict):
                    inner = rep.get(namespaced[0])
                    con = inner
                    ci_rn = inner.get("rn")
                    ct = inner.get("ct")
                else:
                    # Case 3: recursively find first dict that looks like an announcement (has rn/ct or metric-like keys)
                    metric_keys = {"tempe","temp","temperature","humiy","rh","humidity","co2","co2ppm","lux","noise","louds","occ","occupancy"}
                    found = []
                    def find_ann(obj):
                        if isinstance(obj, dict):
                            if ("rn" in obj and "ct" in obj) or any(k in obj for k in metric_keys):
                                found.append(obj)
                                return
                            for v in obj.values():
                                find_ann(v)
                        elif isinstance(obj, list):
                            for it in obj:
                                find_ann(it)
                    find_ann(rep)
                    if found:
                        con = found[0]
                        ci_rn = con.get("rn")
                        ct = con.get("ct")
    else:
        # Raw cod:* body or other
        cod_keys = [k for k in body.keys() if isinstance(k, str) and k.startswith("cod:")]
        if len(cod_keys) == 1:
            con = body[cod_keys[0]]
            if isinstance(con, dict):
                parent = con.get("lnk") or "unknown"
                ci_rn = con.get("rn") or "cin-raw"
                ct = con.get("ct")
        else:
            # treat entire body as con
            con = body

    # If con is a JSON-encoded string, try to decode
    if isinstance(con, str):
        try:
            con = json.loads(con)
        except Exception:
            pass

    return (parent, ci_rn, ct, con)


def enqueue(parent, ci_rn, ct, con):
    c = borrow_conn()
    try:
        with c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_queue (parent_path, ci_rn, ct, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (parent or "unknown", ci_rn, ct, Json(con)),
            )
            row = cur.fetchone()
            qid = row[0]
            app.logger.info("ingest: enqueued message id=%s ci_rn=%s parent=%s", qid, ci_rn, parent)
            return qid
    finally:
        release_conn(c)


@app.post("/onem2m")
def onem2m():
    body = request.get_json(force=True)

    parent, ci_rn, ct, con = extract_fields_for_queue(body)

    # If VRQ (verification request) skip with 200 OK
    if body.get("m2m:sgn", {}).get("vrq") is True:
        return ("", 200)

    if BUFFERED:
        enqueue(parent, ci_rn, ct, con)
        return ("", 202)

    # direct processing (fallback)
    app.logger.info("ingest: direct process ci rn=%s parent=%s", ci_rn, parent)
    c = borrow_conn()
    try:
        process_record(c, parent, ci_rn, ct, con)
    finally:
        release_conn(c)
    return ("", 204)


# Helper for testing without CSE and to accept subscription-delivered notifications
@app.post("/notify")
def notify():
    body = request.get_json(force=True)
    parent, ci_rn, ct, con = extract_fields_for_queue(body)
    if BUFFERED:
        enqueue(parent, ci_rn, ct, con)
        return ("", 202)
    # direct processing
    c = borrow_conn()
    try:
        process_record(c, parent, ci_rn, ct, con)
    finally:
        release_conn(c)
    return ("", 204)


@app.post("/")
def root_notify():
    # Accept raw cod:* payloads or oneM2M payloads
    body = request.get_json(force=True)
    parent, ci_rn, ct, con = extract_fields_for_queue(body)
    if BUFFERED:
        enqueue(parent, ci_rn, ct, con)
        return ("", 202)
    # direct processing
    c = borrow_conn()
    try:
        process_record(c, parent, ci_rn, ct, con)
    finally:
        release_conn(c)
    return ("", 204)


@app.post("/test-insert")
def test_insert():
    data = request.get_json(force=True)
    # Build a oneM2M-like sgn payload for compatibility then enqueue/process
    sgn = {
      "m2m:sgn": {
        "nev": {"rep": {"m2m:cin": {
          "rn": data.get("rn","cin-test"),
          "ct": data.get("ct"),  # like 20251009T153210
          "con": data.get("con")
        }}} ,
        "sur": data.get("parent","/cloud-analytics/telemetry/room-101/sample")
      }
    }
    parent, ci_rn, ct, con = extract_fields_for_queue(sgn)
    if BUFFERED:
        enqueue(parent, ci_rn, ct, con)
        return ("", 202)
    # direct
    c = borrow_conn()
    try:
        process_record(c, parent, ci_rn, ct, con)
    finally:
        release_conn(c)
    return ("", 204)


if __name__ == "__main__":
    # Listen on internal port 8088; docker-compose will map host port as configured
    app.run(host="0.0.0.0", port=8088)
