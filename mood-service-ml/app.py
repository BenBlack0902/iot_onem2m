import os
import time
import json
import logging
from typing import Any, Dict, Optional

import numpy as np
import joblib
from fastapi import FastAPI, Request, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mood-service-ml")

app = FastAPI(title="Mood Service ML (safe, calculation-only)")

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


def extract_con_from_notification(payload: Dict[str, Any]) -> Optional[Any]:
    """
    Try to locate the telemetry content value in common oneM2M notification shapes.

    Expected useful path:
      payload["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"]

    But notifications vary, so search recursively for a dict with key 'm2m:cin'
    or a leaf key 'con'.
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
            return None
    return None


def compute_mood_score(sample: Dict[str, Any]) -> Dict[str, Any]:
    """
    ML-powered mood estimator using a saved model (if present).
    Robust against missing values; falls back to a heuristic if model unavailable.
    Returns a dict {score, label, ts, confidence?}
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

    score = None
    confidence = None
    if _model is not None:
        try:
            # If model supports predict_proba or has a 'predict' that returns a value
            pred = _model.predict(X)
            score = float(pred[0])
            # attempt to get confidence if classifier/regressor provides it
            if hasattr(_model, "predict_proba"):
                try:
                    probs = _model.predict_proba(X)
                    # If multi-class, take the max class probability
                    confidence = float(np.max(probs[0]))
                except Exception:
                    confidence = None
        except Exception as e:
            logger.error("Model prediction failed: %s", e)
            score = None

    # --- Fallback if model missing or failed ---
    if score is None or np.isnan(score):
        co2, noise, lux, temp, rh, occ = features
        score = 100 * (
            (1200 - co2) / 800 * 0.25
            + (80 - noise) / 50 * 0.20
            + (lux - 100) / 700 * 0.20
            + 1.0 * 0.15
            + 1.0 * 0.10
            + min(1.0, occ / 5.0) * 0.10
        )

    # --- Normalize + label ---
    score = max(0, min(int(round(float(score))), 100))
    if score >= 75:
        label = "focus"
    elif score >= 50:
        label = "neutral"
    else:
        label = "tired"

    result = {"score": score, "label": label, "ts": int(time.time())}
    if confidence is not None:
        result["confidence"] = float(confidence)
    return result


@app.post("/notify")
async def notify(request: Request):
    """
    Receive notification (oneM2M or otherwise), extract telemetry and return
    the computed mood. This service intentionally does NOT persist results or
    post them to the CSE — it is calculation-only for testing.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    logger.info("Received notification payload (ML calc): %s", payload)
    con_field = extract_con_from_notification(payload)
    telemetry = parse_con(con_field)
    if telemetry is None:
        logger.warning("Could not find telemetry 'con' in notification")
        raise HTTPException(status_code=400, detail="No telemetry 'con' found in notification")

    # Normalize common synonyms
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

    mood = compute_mood_score(telemetry)

    # Return the mood result only. No DB writes, no CSE posts.
    return {"result": "ok", "mood": mood}
