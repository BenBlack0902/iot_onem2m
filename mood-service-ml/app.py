import os
import time
import json
import logging
import uuid
from typing import Any, Dict, Optional

import numpy as np
import joblib
import httpx
import psycopg2
from psycopg2.extras import Json
from fastapi import FastAPI, Request, HTTPException, Query

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mood-service-ml")

app = FastAPI(title="Mood Service ML (mirrors mood-service, writes to fact_mood_ml)")

# Lazy ML model and path (can be overridden with MOOD_MODEL_PATH env var)
_model = None
_model_path = os.getenv("MOOD_MODEL_PATH", "mood_model.pkl")

# Default "typical" mid-range values (safe fallback)
_DEFAULTS = {
    "co2": 800.0,
    "noise": 50.0,
    "lux": 200.0,
    "temp": 22.0,
    "rh": 45.0,
    "occ": 0.0,
}

# CSE / PUT config (parity with mood-service)
CSE_BASE = os.getenv("CSE_BASE", "http://acme:8080/~/in-cse/in-name")
CSE_ORIGIN = os.getenv("CSE_ORIGIN", "admin:admin")
CSE_PUT_BASE = os.getenv("CSE_PUT_BASE") or CSE_BASE
CSE_PUT_RVI = os.getenv("CSE_PUT_RVI", "3")
CSE_PUT_CSEID = os.getenv("CSE_PUT_CSEID", "id-room-mn-cse")
CSE_PUT_AE = os.getenv("CSE_PUT_AE", "moodMonitorAE")

# DB
PG_DSN = os.getenv("DATABASE_URL", "postgresql://onem2m:onem2m_pass@postgres:5432/onem2m")

# Parse CSE_ORIGIN user:pass for BasicAuth
if ":" in CSE_ORIGIN:
    CSE_USER, CSE_PASS = CSE_ORIGIN.split(":", 1)
else:
    CSE_USER, CSE_PASS = CSE_ORIGIN, ""

client = httpx.Client(timeout=10.0)

# In-memory latest-value cache keyed by (room::desk).
# Stores metric -> (value, ts). TTL controls staleness.
LATEST_CACHE: Dict[str, Dict[str, tuple]] = {}
LATEST_TTL_SEC = int(os.getenv("LATEST_TTL_SEC", "900"))  # default 15 minutes

def _make_key(room: Optional[str], desk: Optional[str]) -> str:
    if room or desk:
        return f"{room or ''}::{desk or ''}"
    return "global"

def _update_latest_cache(telemetry: Optional[Dict[str, Any]], key: str):
    now_ts = int(time.time())
    if key not in LATEST_CACHE:
        LATEST_CACHE[key] = {}
    if not isinstance(telemetry, dict):
        return
    for k in ("co2", "rh", "temp", "lux", "noise", "occ"):
        if k in telemetry and telemetry[k] is not None:
            try:
                v = float(telemetry[k])
                LATEST_CACHE[key][k] = (v, now_ts)
            except Exception:
                # ignore conversion errors
                pass

def _merged_latest_features(key: str) -> Dict[str, Any]:
    now_ts = int(time.time())
    cache = LATEST_CACHE.get(key, {})
    merged: Dict[str, Any] = {}
    for k, default in _DEFAULTS.items():
        entry = cache.get(k)
        if entry and (now_ts - entry[1] <= LATEST_TTL_SEC):
            merged[k] = entry[0]
        else:
            merged[k] = default
    return merged


def extract_con_from_notification(payload: Dict[str, Any]) -> Optional[Any]:
    """
    Try to locate the telemetry content value in common oneM2M notification shapes.
    """
    def find(obj):
        if isinstance(obj, dict):
            if "m2m:cin" in obj and isinstance(obj["m2m:cin"], dict) and "con" in obj["m2m:cin"]:
                return obj["m2m:cin"]["con"]
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
            return None
    return None


def score_to_led_color(score: int) -> str:
    try:
        s = max(0, min(100, int(score)))
    except Exception:
        s = 0
    
    if s < 65:
        # Red to Yellow (0-65)
        t = s / 65.0
        r = 255
        g = int(round(255 * t))
    else:
        # Yellow to Green (65-100)
        t = (s - 65) / 35.0
        r = int(round(255 * (1 - t)))
        g = 255
    
    b = 0
    return f"#{r:02X}{g:02X}{b:02X}"

def hex_to_rg(hex_color: str) -> Dict[str, int]:
    try:
        s = hex_color.strip()
        if s.startswith('#'):
            s = s[1:]
        if len(s) == 3:
            s = ''.join(c*2 for c in s)
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return {"red": r, "green": g, "blue": b}
    except Exception:
        return {"red": 0, "green": 0, "blue": 0}


def put_lamp_color(room: Optional[str], desk: Optional[str], led_hex: str):
    """
    Send a oneM2M PUT to the lamp color resource using red/green channels derived from the led_hex.
    URL pattern: {CSE_PUT_BASE}/~/CSEID/-/AE/{room}/{desk}/lamp/color
    """
    try:
        if not room or not desk:
            logger.warning("Skipping lamp color PUT: missing room or desk (room=%s, desk=%s)", room, desk)
            return
        base = CSE_PUT_BASE.rstrip('/')
        if '://' in base:
            parts = base.split('/')
            if len(parts) > 3:
                base = '/'.join(parts[:3])
        url = f"{base}/~/{CSE_PUT_CSEID}/-/{CSE_PUT_AE}/{room}/{desk}/lamp/color"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-M2M-Origin": CSE_ORIGIN,
            "X-M2M-RI": str(uuid.uuid4()),
            "X-M2M-RVI": CSE_PUT_RVI,
        }
        rgb = hex_to_rg(led_hex or "#000000")
        body = {"cod:color": {"red": int(rgb["red"]), "green": int(rgb["green"])}}
        logger.info("PUT lamp color -> %s body=%s", url, body)
        resp = client.put(url, json=body, headers=headers)
        if resp.status_code >= 300:
            logger.warning("Lamp color PUT returned %s: %s", resp.status_code, resp.text)
    except Exception:
        logger.exception("Failed to PUT lamp color")


def compute_mood_score(sample: Dict[str, Any]) -> Dict[str, Any]:
    """
    ML-powered mood estimator using a saved model (if present).
    Robust against missing values; falls back to a heuristic if model unavailable.
    Adds runtime calibration and blending to avoid harsh low scores.
    Returns a dict {score, label, ts, confidence?, led_color}
    """
    global _model

    # Lazy load model
    if _model is None and os.path.exists(_model_path):
        try:
            _model = joblib.load(_model_path)
            logger.info("Loaded ML mood model from %s", _model_path)
        except Exception as e:
            logger.error("Failed to load ML model: %s", e)
            _model = None

    # Runtime tuning (env vars)
    try:
        ML_BLEND_HEURISTIC = float(os.getenv("ML_BLEND_HEURISTIC", "0.5"))  # 0..1
    except Exception:
        ML_BLEND_HEURISTIC = 0.5
    try:
        SOFTENING_CENTER = float(os.getenv("SOFTENING_CENTER", "60.0"))
    except Exception:
        SOFTENING_CENTER = 60.0
    try:
        SOFTENING_FACTOR = float(os.getenv("SOFTENING_FACTOR", "0.7"))  # multiplies distance-from-center
    except Exception:
        SOFTENING_FACTOR = 0.7
    try:
        SCORE_BIAS = float(os.getenv("SCORE_BIAS", "6.0"))  # small positive bias to lift borderline lows
    except Exception:
        SCORE_BIAS = 6.0
    try:
        THRESHOLD_FOCUS = int(os.getenv("THRESHOLD_FOCUS", "70"))
        THRESHOLD_NEUTRAL = int(os.getenv("THRESHOLD_NEUTRAL", "40"))
    except Exception:
        THRESHOLD_FOCUS = 70
        THRESHOLD_NEUTRAL = 40

    # Ensure every feature has a numeric value (use defaults on missing/invalid)
    features = []
    for key in ["co2", "noise", "lux", "temp", "rh", "occ"]:
        try:
            val = float(sample.get(key, _DEFAULTS[key]))
            if np.isnan(val):
                raise ValueError("nan")
        except Exception:
            val = _DEFAULTS[key]
        features.append(val)

    X = np.array([features])  # shape (1,6)

    ml_score = None
    confidence = None
    if _model is not None:
        try:
            pred = _model.predict(X)
            ml_score = float(pred[0])
            if hasattr(_model, "predict_proba"):
                try:
                    probs = _model.predict_proba(X)
                    confidence = float(np.max(probs[0]))
                except Exception:
                    confidence = None
        except Exception as e:
            logger.error("Model prediction failed: %s", e)
            ml_score = None

    # --- Heuristic estimate (always available) ---
    co2, noise, lux, temp, rh, occ = features
    heuristic_score = 100 * (
        (1200 - co2) / 800 * 0.25
        + (80 - noise) / 50 * 0.20
        + (lux - 100) / 700 * 0.20
        + 1.0 * 0.15
        + 1.0 * 0.10
        + min(1.0, occ / 5.0) * 0.10
    )

    # If ML exists, blend ML + heuristic (blend weight pulls toward heuristic when desired)
    if ml_score is not None and not np.isnan(ml_score):
        try:
            blend = float(ML_BLEND_HEURISTIC)
        except Exception:
            blend = 0.5
        blended = (1.0 - blend) * float(ml_score) + blend * float(heuristic_score)
        pre_calibrated = blended
    else:
        pre_calibrated = heuristic_score

    # --- Softening calibration ---
    # Pull score toward SOFTENING_CENTER then add small positive bias to avoid many low-40/50 outputs.
    try:
        s = float(pre_calibrated)
        # soften distance from center
        s = (s - SOFTENING_CENTER) * SOFTENING_FACTOR + SOFTENING_CENTER
        # small bias
        s = s + SCORE_BIAS
    except Exception:
        s = float(pre_calibrated if pre_calibrated is not None else 0.0)

    # Final normalize and rounding
    score = max(0, min(int(round(float(s))), 100))

    # Adjusted label thresholds (tunable)
    if score >= THRESHOLD_FOCUS:
        label = "focus"
    elif score >= THRESHOLD_NEUTRAL:
        label = "neutral"
    else:
        label = "tired"

    led_color = score_to_led_color(score)
    result = {"score": score, "label": label, "ts": int(time.time()), "led_color": led_color}
    if confidence is not None:
        result["confidence"] = float(confidence)
    # Include some debug fields to help tuning when needed (only present in logs/CIN if desired)
    if os.getenv("MOOD_ML_DEBUG", "0") == "1":
        result["_debug"] = {
            "ml_score": None if ml_score is None else float(ml_score),
            "heuristic_score": float(heuristic_score),
            "pre_calibrated": float(pre_calibrated),
            "softened": float(s),
            "blend": float(ML_BLEND_HEURISTIC),
            "center": float(SOFTENING_CENTER),
            "factor": float(SOFTENING_FACTOR),
            "bias": float(SCORE_BIAS),
            "threshold_focus": THRESHOLD_FOCUS,
            "threshold_neutral": THRESHOLD_NEUTRAL,
        }
    return result


def one_m2m_post_cin(target_path: str, con_payload: Dict[str, Any]) -> httpx.Response:
    """
    Post a content instance to the CSE as an oneM2M CIN (ty=4).
    """
    url = target_path
    auth = (CSE_USER, CSE_PASS) if CSE_USER or CSE_PASS else None

    headers = {
        "Content-Type": "application/json;ty=4",
        "Accept": "application/json",
        "X-M2M-Origin": CSE_ORIGIN,
        "X-M2M-RI": str(uuid.uuid4()),
        "X-M2M-RVI": "4",
    }

    body = {"m2m:cin": {"con": con_payload, "cnf": "application/json:0"}}
    logger.info("Posting mood CIN to %s with body %s", url, body)

    resp = client.post(url, json=body, headers=headers, auth=auth)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
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
    Mirrors mood-service: compute mood (ML), persist to fact_mood_ml, PUT lamp color, post CIN back to CSE.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Received notification payload (ML): %s", payload)
    con_field = extract_con_from_notification(payload)
    telemetry = parse_con(con_field)
    if telemetry is None:
        logger.warning("Could not find telemetry 'con' in notification")
        raise HTTPException(status_code=400, detail="No telemetry 'con' found in notification")

    # Normalize synonyms so compute_mood_score gets expected keys
    if isinstance(telemetry, dict):
        if "temperature" in telemetry and "temp" not in telemetry:
            telemetry["temp"] = telemetry.get("temperature")
        if "tempe" in telemetry and "temp" not in telemetry:
            telemetry["temp"] = telemetry.get("tempe")
        if "temp" in telemetry:
            try:
                telemetry["temp"] = float(telemetry["temp"])
            except Exception:
                pass

        if "humidity" in telemetry and "rh" not in telemetry:
            telemetry["rh"] = telemetry.get("humidity")
        if "humiy" in telemetry and "rh" not in telemetry:
            telemetry["rh"] = telemetry.get("humiy")
        if "rh" in telemetry:
            try:
                telemetry["rh"] = float(telemetry["rh"])
            except Exception:
                logger.debug("Could not convert rh to float: %s", telemetry.get("rh"))

        if "occupancy" in telemetry and "occ" not in telemetry:
            telemetry["occ"] = telemetry.get("occupancy")
        if "occ" in telemetry:
            try:
                telemetry["occ"] = float(telemetry["occ"])
            except Exception:
                pass

        if "co2" not in telemetry and "co2ppm" in telemetry:
            telemetry["co2"] = telemetry.get("co2ppm")
        if "co2" in telemetry:
            try:
                telemetry["co2"] = float(telemetry["co2"])
            except Exception:
                pass

        for k in ("lux", "noise"):
            if k in telemetry:
                try:
                    telemetry[k] = float(telemetry[k])
                except Exception:
                    pass

    # Determine room/desk for per-context latest-value cache
    room_lbl = None
    desk_lbl = None
    if isinstance(telemetry, dict):
        labels = telemetry.get("labels") if isinstance(telemetry.get("labels"), dict) else {}
        room_lbl = telemetry.get("room") or labels.get("room")
        desk_lbl = telemetry.get("desk") or labels.get("desk")

    key = _make_key(room_lbl, desk_lbl)
    _update_latest_cache(telemetry, key)
    merged = _merged_latest_features(key)
    # Preserve device if present
    if isinstance(telemetry, dict) and telemetry.get("device"):
        merged["device"] = telemetry.get("device")

    mood = compute_mood_score(merged)

    # room_lbl and desk_lbl were determined earlier from telemetry and used to build the merged features

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

        ts_val = int(mood.get("ts", time.time()))

        try:
            conn = psycopg2.connect(PG_DSN)
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO fact_mood_ml (parent_path, ci_rn, ts_cse, score, label, confidence, room_id, device, led_color)
                        VALUES (%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (parent_path, ci_rn) DO UPDATE
                          SET score = EXCLUDED.score,
                              label = EXCLUDED.label,
                              confidence = EXCLUDED.confidence,
                              led_color = EXCLUDED.led_color,
                              inserted_at = now()
                    """, (
                        parent_path,
                        ci_rn,
                        ts_val,
                        mood.get("score"),
                        mood.get("label"),
                        float(mood.get("confidence", 0.0)) if mood.get("confidence") is not None else None,
                        1,
                        telemetry.get("device") if isinstance(telemetry, dict) else None,
                        mood.get("led_color"),
                    ))
        except Exception:
            logger.exception("Failed to persist ML mood to Postgres")
    except Exception:
        logger.exception("Error during mood DB extraction/persist")

    # Attempt to set the lamp color using the computed mood LED color
    try:
        put_lamp_color(room_lbl, desk_lbl, mood.get("led_color"))
    except Exception:
        logger.exception("Lamp color PUT step failed")

    # Post to CSE like mood-service
    target = os.getenv("CSE_BASE", "http://cloud-in-cse:8080/~/id-room-mn-cse/-/moodMonitorAE/Room01/moodAnalysis").rstrip("/")
    try:
        resp = one_m2m_post_cin(target, mood)
        logger.info("Mood CIN posted (ML), status %s (target=%s)", resp.status_code, target)
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

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Invalid JSON from CSE /la")

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
