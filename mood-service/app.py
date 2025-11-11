import os
import time
import json
import logging
import uuid
from typing import Any, Dict, Optional

import httpx
import psycopg2
from psycopg2.extras import Json
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
    Ensure we return a dict telemetry object from 'con' which may be:
      - a JSON string
      - already a dict
      - other scalar (not expected)
    """
    if con_field is None:
        return None
    if isinstance(con_field, dict):
        return con_field
    if isinstance(con_field, str):
        try:
            parsed = json.loads(con_field)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            # not JSON string; ignore
            return None
    return None


def compute_mood_score(sample: Dict[str, Any]) -> Dict[str, Any]:
    """
    Heuristic score 0..100 based on telemetry fields:
    co2 (ppm), noise (dB), lux, temp (C), rh (%), occ (count)
    We normalize each into 0..1 (1 is best) and then weighted average.
    """

    def clamp01(x):
        return max(0.0, min(1.0, x))

    # defaults
    co2 = float(sample.get("co2", 1000))
    noise = float(sample.get("noise", 60))
    lux = float(sample.get("lux", 100))
    temp = float(sample.get("temp", 22.0))
    rh = float(sample.get("rh", 40))
    occ = float(sample.get("occ", 0))

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

    return {"score": score, "label": label, "ts": int(time.time())}


def one_m2m_post_cin(target_path: str, con_payload: Dict[str, Any]) -> httpx.Response:
    """
    Post a content instance to the CSE as an oneM2M CIN (ty=4).
    Adds required oneM2M headers (X-M2M-Origin, X-M2M-RI, X-M2M-RVI) and sets
    Content-Type to include the ty=4 directive. Includes "cnf":"application/json"
    in the posted CIN body.
    """
    url = target_path
    auth = (CSE_USER, CSE_PASS) if CSE_USER or CSE_PASS else None

    # Build oneM2M headers
    headers = {
        "Content-Type": "application/json;ty=4",
        "Accept": "application/json",
        "X-M2M-Origin": CSE_ORIGIN,
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "4",
    }

    body = {"m2m:cin": {"con": con_payload, "cnf": "application/json:0"}}
    logger.info("Posting mood CIN to %s with body %s headers %s", url, body, {k: headers[k] for k in ("X-M2M-Origin", "X-M2M-RI", "X-M2M-RVI")})

    resp = client.post(url, json=body, headers=headers, auth=auth)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        # Log response body for diagnostics then re-raise
        try:
            logger.error("CSE error status %s body: %s", resp.status_code, resp.text)
        except Exception:
            logger.exception("CSE error and failed to read response body")
        raise
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

    # Normalize synonyms so compute_mood_score gets expected keys (temp, rh, co2, lux, noise, occ)
    if isinstance(telemetry, dict):
        # temperature -> temp
        if "temperature" in telemetry and "temp" not in telemetry:
            telemetry["temp"] = telemetry.get("temperature")
        if "tempe" in telemetry and "temp" not in telemetry:
            telemetry["temp"] = telemetry.get("tempe")
        if "temp" in telemetry:
            # also keep a canonical float
            try:
                telemetry["temp"] = float(telemetry["temp"])
            except Exception:
                pass

        # humidity -> rh
        if "humidity" in telemetry and "rh" not in telemetry:
            telemetry["rh"] = telemetry.get("humidity")
        if "humiy" in telemetry and "rh" not in telemetry:
            telemetry["rh"] = telemetry.get("humiy")
        if "rh" in telemetry:
            try:
                telemetry["rh"] = float(telemetry["rh"])
            except Exception:
                logger.debug("Could not convert rh to float: %s", telemetry.get("rh"))

        # occupancy -> occ
        if "occupancy" in telemetry and "occ" not in telemetry:
            telemetry["occ"] = telemetry.get("occupancy")
        if "occ" in telemetry:
            try:
                telemetry["occ"] = float(telemetry["occ"])
            except Exception:
                pass

        # co2 variants
        if "co2" not in telemetry and "co2ppm" in telemetry:
            telemetry["co2"] = telemetry.get("co2ppm")
        if "co2" in telemetry:
            try:
                telemetry["co2"] = float(telemetry["co2"])
            except Exception:
                pass

        # lux / noise keep as-is if present; try to coerce to float
        for k in ("lux", "noise"):
            if k in telemetry:
                try:
                    telemetry[k] = float(telemetry[k])
                except Exception:
                    pass

    mood = compute_mood_score(telemetry)

    # Persist mood to Postgres (room_id = 1)
    try:
        # Try to extract ci_rn and parent_path from the notification payload
        def find_ci_identifiers(obj):
            if isinstance(obj, dict):
                if "m2m:cin" in obj and isinstance(obj["m2m:cin"], dict):
                    cin = obj["m2m:cin"]
                    return (cin.get("rn") or cin.get("ri"), payload.get("sur") or "")
                for v in obj.values():
                    res = find_ci_identifiers(v)
                    if res and (res[0] or res[1]):
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_ci_identifiers(item)
                    if res and (res[0] or res[1]):
                        return res
            return (None, None)

        ci_rn, parent_path = find_ci_identifiers(payload)

        # Timestamp for DB (epoch seconds)
        ts_val = int(mood.get("ts", time.time()))

        # Connect and upsert mood row (room_id fixed to 1)
        PG_DSN = os.getenv("DATABASE_URL", "postgresql://onem2m:onem2m_pass@postgres:5432/onem2m")
        try:
            conn = psycopg2.connect(PG_DSN)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO fact_mood (parent_path, ci_rn, ts_cse, score, label, confidence, room_id, device)
                        VALUES (%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)
                        ON CONFLICT (parent_path, ci_rn) DO UPDATE
                          SET score = EXCLUDED.score,
                              label = EXCLUDED.label,
                              confidence = EXCLUDED.confidence,
                              inserted_at = now()
                    """, (
                        parent_path,
                        ci_rn,
                        ts_val,
                        mood.get("score"),
                        mood.get("label"),
                        float(mood.get("confidence", 0.0)) if mood.get("confidence") is not None else None,
                        1,
                        telemetry.get("device"),
                    ))
        except Exception:
            logger.exception("Failed to persist mood to Postgres")
    except Exception:
        logger.exception("Error during mood DB extraction/persist")

    # Post directly to the CSE container URL provided in CSE_BASE.
    # The CSE_BASE should be the full container resource path when you want to
    # post directly to that container (e.g. http://host:8080/cntrm1iEXHDrA).
    # This avoids appending extra path segments that may not exist in the CSE tree.
    target = os.getenv("CSE_BASE", "http://cloud-in-cse:8080/cntrm1iEXHDrA").rstrip("/")
    try:
        resp = one_m2m_post_cin(target, mood)
        logger.info("Mood CIN posted, status %s (target=%s)", resp.status_code, target)
    except httpx.HTTPStatusError as exc:
        logger.error("Failed to write CIN to CSE: %s - %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Failed to write CIN to CSE")
    except Exception as exc:
        logger.exception("Error posting to CSE: %s", exc)
        raise HTTPException(status_code=502, detail="Error communicating with CSE")

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
        resp = client.get(la_url, auth=auth, timeout=10.0)
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
