-- Initialization SQL for oneM2M telemetry analytics
-- Creates dimensional schema and helper views/materialized views
-- Designed to be idempotent: uses IF NOT EXISTS

-- Enable needed extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS citext;

-- DIMENSIONS
CREATE TABLE IF NOT EXISTS dim_room (
  room_id SERIAL PRIMARY KEY,
  room_rn TEXT NOT NULL UNIQUE,
  description TEXT
);

CREATE TABLE IF NOT EXISTS dim_device (
  device_id SERIAL PRIMARY KEY,
  device_rn TEXT NOT NULL UNIQUE,
  room_id INTEGER REFERENCES dim_room(room_id),
  description TEXT
);

CREATE TABLE IF NOT EXISTS dim_metric (
  metric_id SERIAL PRIMARY KEY,
  metric_rn TEXT NOT NULL UNIQUE,
  unit TEXT
);

-- RAW oneM2M ContentInstance store
CREATE TABLE IF NOT EXISTS raw_onem2m_ci (
  parent_path TEXT NOT NULL,
  ci_rn TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  payload JSONB,
  PRIMARY KEY (parent_path, ci_rn)
);

-- FACT table for telemetry (cleaned / parsed)
CREATE TABLE IF NOT EXISTS fact_telemetry (
  id BIGSERIAL PRIMARY KEY,
  parent_path TEXT NOT NULL,
  ci_rn TEXT NOT NULL,
  ts_cse TIMESTAMPTZ NOT NULL DEFAULT now(), -- timestamp from CSE / content instance
  device_id INTEGER REFERENCES dim_device(device_id),
  room_id INTEGER REFERENCES dim_room(room_id),
  metric_id INTEGER REFERENCES dim_metric(metric_id),
  value DOUBLE PRECISION,
  value_text TEXT,
  quality JSONB,
  inserted_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (parent_path, ci_rn, metric_id)
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS fact_ts_idx
  ON fact_telemetry (ts_cse DESC NULLS LAST, device_id, metric_id);

CREATE INDEX IF NOT EXISTS fact_device_metric_idx
  ON fact_telemetry (device_id, metric_id, ts_cse DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS fact_quality_gin ON fact_telemetry USING GIN (quality);

-- Indexes to speed up room queries
CREATE INDEX IF NOT EXISTS fact_room_ts_idx
  ON fact_telemetry (room_id, ts_cse DESC NULLS LAST);

-- A view exposing metrics grouped by room and metric
CREATE OR REPLACE VIEW v_room_metrics AS
SELECT
  r.room_rn,
  d.device_rn,
  m.metric_rn,
  f.ts_cse,
  f.value,
  f.quality,
  f.parent_path,
  f.ci_rn
FROM fact_telemetry f
LEFT JOIN dim_room r ON f.room_id = r.room_id
LEFT JOIN dim_device d ON f.device_id = d.device_id
LEFT JOIN dim_metric m ON f.metric_id = m.metric_id;

-- Materialized view: latest value per device+metric over the last 5 minutes
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_latest_5m AS
WITH recent AS (
  SELECT *
  FROM fact_telemetry
  WHERE ts_cse >= now() - interval '5 minutes'
)
SELECT DISTINCT ON (device_id, metric_id)
  device_id,
  metric_id,
  ts_cse,
  value,
  quality
FROM recent
ORDER BY device_id, metric_id, ts_cse DESC;

-- Index for materialized view refresh/read
CREATE INDEX IF NOT EXISTS mv_latest_5m_idx ON mv_latest_5m (device_id, metric_id, ts_cse DESC);

-- Example view: temporary comfort score by room (simple formula)
-- Comfort formula (example): comfort = 100 - abs(22 - temp) * 2 - abs(50 - rh) * 0.5
-- This view expects metrics named 'temperature' and 'humidity' to exist in dim_metric
CREATE OR REPLACE VIEW v_room_comfort AS
WITH temps AS (
  SELECT f.room_id, f.device_id, f.ts_cse, f.value AS temperature
  FROM fact_telemetry f
  JOIN dim_metric m ON f.metric_id = m.metric_id AND m.metric_rn = 'temperature'
),
hums AS (
  SELECT f.room_id, f.device_id, f.ts_cse, f.value AS humidity
  FROM fact_telemetry f
  JOIN dim_metric m ON f.metric_id = m.metric_id AND m.metric_rn = 'humidity'
),
latest_t AS (
  SELECT DISTINCT ON (room_id) room_id, temperature, ts_cse
  FROM temps
  ORDER BY room_id, ts_cse DESC
),
latest_h AS (
  SELECT DISTINCT ON (room_id) room_id, humidity, ts_cse
  FROM hums
  ORDER BY room_id, ts_cse DESC
)
SELECT
  r.room_rn,
  COALESCE(lt.temperature, NULL) AS temperature,
  COALESCE(lh.humidity, NULL) AS humidity,
  ROUND(
    100.0
    - COALESCE(ABS(22.0 - lt.temperature) * 2.0, 0)
    - COALESCE(ABS(50.0 - lh.humidity) * 0.5, 0)
  , 2) AS comfort_score
FROM dim_room r
LEFT JOIN latest_t lt ON r.room_id = lt.room_id
LEFT JOIN latest_h lh ON r.room_id = lh.room_id;

-- Ensure materialized view can be refreshed; create a helper function
CREATE OR REPLACE FUNCTION refresh_mv_latest_5m()
RETURNS void LANGUAGE sql AS $$
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_latest_5m;
$$;

-- Safety: grant minimal access to a role that might be used by the app
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'onem2m_app') THEN
    CREATE ROLE onem2m_app LOGIN PASSWORD 'onem2m_pass';
  END IF;
EXCEPTION WHEN others THEN
  -- ignore
END$$;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO onem2m_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO onem2m_app;

-- End of init.sql
