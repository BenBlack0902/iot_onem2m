import os
import time
import json
import logging
from typing import Any, Dict, Optional

import httpx
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
    Post a content instance to the CSE. We do a simple POST with JSON:
    { "con": { ... } } and include basic auth. If the CSE requires specific headers
    (like X-M2M-Origin and Content-Type with ty=4), adapt here later.
    """
    url = target_path
    auth = (CSE_USER, CSE_PASS) if CSE_USER or CSE_PASS else None
    headers = {"Content-Type": "application/json"}
    body = {"m2m:cin": {"con": con_payload}}
    logger.info("Posting mood CIN to %s with body %s", url, body)
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
    try:
        resp = one_m2m_post_cin(target, mood)
        logger.info("Mood CIN posted, status %s", resp.status_code)
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
