-- 007_create_fact_mood_ml.sql
-- Create table to persist ML-computed mood scores (idempotent)
-- Mirrors fact_mood but stores ML-derived results to a separate table

CREATE TABLE IF NOT EXISTS fact_mood_ml (
  mood_id BIGSERIAL PRIMARY KEY,
  parent_path TEXT,
  ci_rn TEXT,
  ts_cse TIMESTAMPTZ,
  score INTEGER,
  label TEXT,
  confidence DOUBLE PRECISION,
  room_id INTEGER REFERENCES dim_room(room_id),
  device TEXT,
  led_color TEXT,
  inserted_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (parent_path, ci_rn)
);

CREATE INDEX IF NOT EXISTS fact_mood_ml_ts_idx ON fact_mood_ml (ts_cse DESC, inserted_at DESC);
