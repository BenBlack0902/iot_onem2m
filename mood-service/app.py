import os
import time
import json
import logging
from typing import Any, Dict, Optional
import re
from contextlib import contextmanager

import httpx
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from fastapi import FastAPI, Request, HTTPException, Query
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mood-service")

app = FastAPI(title="Mood Service")

CSE_BASE = os.getenv("CSE_BASE", "http://acme:8080/~/in-cse/in-name")
CSE_ORIGIN = os.getenv("CSE_ORIGIN", "admin:admin")
# Keep legacy env var for notify but not required for posting back.
MOOD_NOTIFY = os.getenv("MOOD_NOTIFY", "http://mood:8088/notify")

# Parse CSE_ORIGIN user:pass for BasicAuth
if ":" in CSE_ORIGIN:
    CSE_USER, CSE_PASS = CSE_ORIGIN.split(":", 1)
else:
    CSE_USER, CSE_PASS = CSE_ORIGIN, ""

client = httpx.Client(timeout=10.0)

# Database connection pool (psycopg2)
DB_POOL: Optional[SimpleConnectionPool] = None
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://onem2m_app:onem2m_pass@postgres:5432/onem2m")
DB_MINCONN = int(os.getenv("DB_POOL_MIN", "1"))
DB_MAXCONN = int(os.getenv("DB_POOL_MAX", "5"))

def init_db_pool():
    global DB_POOL
    if DB_POOL is None:
        try:
            DB_POOL = SimpleConnectionPool(DB_MINCONN, DB_MAXCONN, dsn=DATABASE_URL)
            logger.info("Initialized DB pool")
        except Exception as exc:
            logger.exception("Failed to initialize DB pool: %s", exc)
            DB_POOL = None

@contextmanager
def get_db_conn():
    if DB_POOL is None:
        init_db_pool()
    if DB_POOL is None:
        raise RuntimeError("DB pool not available")
    conn = DB_POOL.getconn()
    try:
        yield conn
    finally:
        try:
            DB_POOL.putconn(conn)
        except Exception:
            pass

def extract_room_rn(obj: Any) -> Optional[str]:
    """
    Recursively search for a string like 'room-101' in the payload or resource paths.
    """
    pattern = re.compile(r'room-[A-Za-z0-9_-]+')
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                m = pattern.search(k)
                if m:
                    return m.group(0)
            if isinstance(v, str):
                m = pattern.search(v)
                if m:
                    return m.group(0)
            res = extract_room_rn(v)
            if res:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = extract_room_rn(item)
            if res:
                return res
    elif isinstance(obj, str):
        m = pattern.search(obj)
        if m:
            return m.group(0)
    return None

def ensure_room(room_rn: str, conn) -> Optional[int]:
    """
    Ensure a dim_room exists; return room_id.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dim_room (room_rn) VALUES (%s) ON CONFLICT (room_rn) DO NOTHING", (room_rn,))
        conn.commit()
        cur.execute("SELECT room_id FROM dim_room WHERE room_rn = %s", (room_rn,))
        row = cur.fetchone()
        return row[0] if row else None

def insert_mood_to_db(room_rn: str, mood: Dict[str, Any], telemetry: Dict[str, Any]) -> bool:
    """
    Insert mood into fact_mood_scores, creating room if necessary.
    Uses simple retry/backoff for transient DB errors and is best-effort:
    - If DB pool is unavailable, skip with a warning.
    - Retries a few times for transient failures.
    """
    MAX_RETRIES = 3
    BACKOFF_BASE = 0.5  # seconds

    if DB_POOL is None:
        init_db_pool()
    if DB_POOL is None:
        logger.warning("DB pool unavailable; skipping DB insert")
        return False

    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            with get_db_conn() as conn:
                # Use a transaction block; psycopg2 will manage rollback on exceptions
                room_id = ensure_room(room_rn, conn)
                if room_id is None:
                    logger.error("Could not ensure room for %s", room_rn)
                    return False
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO fact_mood_scores (room_id, ts, score, label, telemetry_snapshot) VALUES (%s, to_timestamp(%s), %s, %s, %s)",
                        (room_id, mood.get("ts"), mood.get("score"), mood.get("label"), json.dumps(telemetry)),
                    )
                conn.commit()
            return True
        except psycopg2.OperationalError as exc:
            # Operational errors (connection issues) may be transient
            logger.warning("OperationalError inserting mood (attempt %s/%s): %s", attempt, MAX_RETRIES, exc)
            try:
                time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
            except Exception:
                pass
            # re-init pool on next attempt
            init_db_pool()
            continue
        except Exception as exc:
            # For other errors, log and decide whether to retry (we don't retry for data errors)
            logger.exception("Error inserting mood into DB on attempt %s: %s", attempt, exc)
            # If it's the last attempt, return False
            try:
                time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
            except Exception:
                pass
            continue

    logger.error("Failed to insert mood into DB after %s attempts; skipping", MAX_RETRIES)
    return False


def extract_con_from_notification(payload: Dict[str, Any]) -> Optional[Any]:
    """
    Try to locate the telemetry content value in common oneM2M notification shapes.

    Expected useful path:
      payload["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"]

    But notifications vary, so search recursively for a dict with key 'm2m:cin'
    or a leaf key 'con'.
    """
    # helper recursive search
    def find(obj):
        if isinstance(obj, dict):
            # direct m2m:cin
            if "m2m:cin" in obj and isinstance(obj["m2m:cin"], dict) and "con" in obj["m2m:cin"]:
                return obj["m2m:cin"]["con"]
            # direct con
            if "con" in obj:
                return obj["con"]
            for v in obj.values():
                res = find(v)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = find(item)
                if res is not None:
                    return res
        return None

    return find(payload)


def parse_con(con_field: Any) -> Optional[Dict[str, Any]]:
    """
    Ensure we return a flat dict telemetry object from 'con' which may be:
      - a JSON string
      - already a flat dict (co2, noise, lux, temp, rh, occ)
      - an envelope with a 'metrics' list of {name,value} pairs
    This helper will normalize the 'metrics' list into a flat dict so compute_mood_score
    can read values via sample.get("co2") etc.
    """
    if con_field is None:
        return None

    # If it's already a dict, try to normalize metrics list if present
    if isinstance(con_field, dict):
        # If payload uses a 'metrics' list, extract into flat dict
        metrics_list = con_field.get("metrics")
        if isinstance(metrics_list, list):
            flat = {}
            # Copy simple top-level scalar keys (device, room) through as context
            for k, v in con_field.items():
                if k == "metrics":
                    continue
                if isinstance(v, (str, int, float, bool)):
                    flat[k] = v
            # Extract named metrics
            for m in metrics_list:
                if not isinstance(m, dict):
                    continue
                name = m.get("name")
                # prefer numeric 'value' field, fall back to 'text'
                if name:
                    if "value" in m:
                        val = m.get("value")
                        # try to coerce numeric strings to float/int
                        try:
                            if isinstance(val, str):
                                if "." in val:
                                    valf = float(val)
                                else:
                                    valf = int(val)
                                val = valf
                        except Exception:
                            try:
                                val = float(val)
                            except Exception:
                                pass
                        flat[name] = val
                    elif "text" in m:
                        flat[name] = m.get("text")
            return flat
        # If no metrics list, assume it's already the expected flat dict
        return con_field

    # If it's a JSON string, parse it and then normalize recursively
    if isinstance(con_field, str):
        try:
            parsed = json.loads(con_field)
            if isinstance(parsed, dict):
                return parse_con(parsed)
        except Exception:
            # not JSON string; ignore
            return None

    return None


def compute_mood_score(sample: Dict[str, Any]) -> Dict[str, Any]:
    """
    Heuristic score 0..100 based on telemetry fields:
    co2 (ppm), noise (dB), lux, temp (C), rh (%), occ (count)
    We normalize each into 0..1 (1 is best) and then weighted average.

    Returns a dict that includes 'components' with per-metric normalized scores
    to aid debugging and explainability.
    """

    def clamp01(x):
        return max(0.0, min(1.0, x))

    # Safely coerce sample values to floats/ints with defaults
    def get_num(key, default):
        val = sample.get(key, default)
        try:
            return float(val)
        except Exception:
            return float(default)

    co2 = get_num("co2", 1000)
    noise = get_num("noise", 60)
    lux = get_num("lux", 100)
    temp = get_num("temp", 22.0)
    rh = get_num("rh", 40)
    occ = get_num("occ", 0)

    # Normalizations (tuned heuristics)
    # CO2: 400 (best) -> 1200 (bad)
    co2_score = clamp01((1200 - co2) / (1200 - 400))
    # Noise: 30 (quiet) -> 80 (loud)
    noise_score = clamp01((80 - noise) / (80 - 30))
    # Lux: 100 (low) -> 800 (good)
    lux_score = clamp01((lux - 100) / (800 - 100))
    # Temp: ideal band 20..25
    if 20 <= temp <= 25:
        temp_score = 1.0
    else:
        # drop off linearly outside range to 10..35
        if temp < 20:
            temp_score = clamp01((temp - 10) / (20 - 10))
        else:
            temp_score = clamp01((35 - temp) / (35 - 25))
    # RH: ideal 30..50
    if 30 <= rh <= 50:
        rh_score = 1.0
    else:
        # degrade up to 10..70
        if rh < 30:
            rh_score = clamp01((rh - 10) / (30 - 10))
        else:
            rh_score = clamp01((70 - rh) / (70 - 50))
    # Occupancy: presence generally helps focus but neutral weight
    occ_score = clamp01(min(1.0, occ / 5.0))

    # weights (sum 1)
    weights = {
        "co2": 0.25,
        "noise": 0.20,
        "lux": 0.20,
        "temp": 0.15,
        "rh": 0.10,
        "occ": 0.10,
    }

    combined = (
        co2_score * weights["co2"]
        + noise_score * weights["noise"]
        + lux_score * weights["lux"]
        + temp_score * weights["temp"]
        + rh_score * weights["rh"]
        + occ_score * weights["occ"]
    )
    score = int(round(combined * 100))

    # label mapping
    if score >= 70:
        label = "focus"
    elif score >= 40:
        label = "neutral"
    else:
        label = "tired"

    components = {
        "co2": co2_score,
        "noise": noise_score,
        "lux": lux_score,
        "temp": temp_score,
        "rh": rh_score,
        "occ": occ_score,
        "weights": weights,
        "combined": combined,
    }

    # Log the components for debugging
    logger.info("Computed mood components: %s -> score=%s label=%s", components, score, label)

    return {"score": score, "label": label, "ts": int(time.time()), "components": components}


import uuid

def one_m2m_post_cin(target_path: str, con_payload: Dict[str, Any]) -> httpx.Response:
    """
    Post a content instance to the CSE using required oneM2M headers.

    Adds:
    - X-M2M-Origin: originator (uses CSE_USER if available)
    - X-M2M-RI: request identifier (UUID)
    - Content-Type: application/vnd.onem2m-res+json;ty=4

    Keeps basic auth if CSE_ORIGIN contained credentials.
    """
    # Ensure the target URL contains a type parameter for creating a ContentInstance (ty=4)
    url = target_path
    if "?" not in url:
        url = f"{url}?ty=4"
    elif "ty=" not in url:
        url = f"{url}&ty=4"

    auth = (CSE_USER, CSE_PASS) if CSE_USER or CSE_PASS else None
    ri = str(uuid.uuid4())
    origin = CSE_USER if CSE_USER else "CAdmin"
    headers = {
        "Content-Type": "application/vnd.onem2m-res+json;ty=4",
        "X-M2M-Origin": origin,
        "X-M2M-RI": ri,
        "X-M2M-RVI": "3",
        "Accept": "application/json",
    }
    body = {"m2m:cin": {"con": con_payload}}
    logger.info("Posting mood CIN to %s (RI=%s) with body %s", url, ri, body)
    resp = client.post(url, json=body, auth=auth, headers=headers)
    resp.raise_for_status()
    return resp


@app.post("/notify")
async def notify(request: Request):
    """
    Receive oneM2M notification from IN-CSE subscriptions.
    Expected to include telemetry under m2m:cin.con (full representation nct=2).
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Received notification payload: %s", payload)
    con_field = extract_con_from_notification(payload)
    telemetry = parse_con(con_field)
    if telemetry is None:
        logger.warning("Could not find telemetry 'con' in notification")
        raise HTTPException(status_code=400, detail="No telemetry 'con' found in notification")

    mood = compute_mood_score(telemetry)

    # Construct CSE target path for posting mood CIN
    target = f"{CSE_BASE}/cloud-analytics/analytics/mood/score"

    # First: write mood CIN back to IN-CSE (required by brief)
    try:
        resp = one_m2m_post_cin(target, mood)
        logger.info("Mood CIN posted, status %s", resp.status_code)
    except httpx.HTTPStatusError as exc:
        logger.error("Failed to write CIN to CSE: %s - %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Failed to write CIN to CSE")
    except Exception as exc:
        logger.exception("Error posting to CSE: %s", exc)
        raise HTTPException(status_code=502, detail="Error communicating with CSE")

    # Second: persist mood in Postgres (best-effort)
    try:
        room_rn = extract_room_rn(payload)
        if room_rn:
            ok = insert_mood_to_db(room_rn, mood, telemetry)
            if not ok:
                logger.warning("DB insert for mood failed for room %s", room_rn)
        else:
            logger.info("No room identifier found in notification payload; skipping DB insert")
    except Exception as exc:
        logger.exception("Unexpected error while inserting mood into DB: %s", exc)

    return {"result": "ok", "mood": mood}


@app.get("/latest-mood")
def latest_mood(room: Optional[str] = Query(None, description="room id, e.g. room-101")):
    """
    Read the latest mood CIN using the CSE latest (/la) endpoint for the mood/score container.
    If room is provided, in a fuller design we'd query by room; current brief uses a single analytics/mood/score container.
    """
    la_url = f"{CSE_BASE}/cloud-analytics/analytics/mood/score/la"
    auth = (CSE_USER, CSE_PASS) if CSE_USER or CSE_PASS else None
    try:
        # Include mandatory oneM2M headers so the CSE accepts the read request
        ri = str(uuid.uuid4())
        origin = CSE_USER if CSE_USER else "CAdmin"
        headers = {
            "X-M2M-RI": ri,
            "X-M2M-RVI": "3",
            "X-M2M-Origin": origin,
            "Accept": "application/json",
        }
        resp = client.get(la_url, auth=auth, timeout=10.0, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error("CSE /la returned %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Failed to read latest from CSE")
    except Exception as exc:
        logger.exception("Error reading /la from CSE: %s", exc)
        raise HTTPException(status_code=502, detail="Error communicating with CSE")

    # Try to extract 'con' from response (it may be an object or wrapped in m2m:cin)
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON from CSE /la")

    # Try common shapes: {"m2m:cin": {"con": {...}}} or {"con": {...}} or direct object
    def extract_con(data):
        if isinstance(data, dict):
            if "m2m:cin" in data and isinstance(data["m2m:cin"], dict) and "con" in data["m2m:cin"]:
                return data["m2m:cin"]["con"]
            if "con" in data:
                return data["con"]
        return data

    con = extract_con(data)
    return {"latest": con}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
