-- 002_create_fact_mood.sql
-- Create table to persist computed mood scores (idempotent)

CREATE TABLE IF NOT EXISTS fact_mood (
  mood_id BIGSERIAL PRIMARY KEY,
  parent_path TEXT,
  ci_rn TEXT,
  ts_cse TIMESTAMPTZ,
  score INTEGER,
  label TEXT,
  confidence DOUBLE PRECISION,
  room_id INTEGER REFERENCES dim_room(room_id),
  device TEXT,
  inserted_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (parent_path, ci_rn)
);

CREATE INDEX IF NOT EXISTS fact_mood_ts_idx ON fact_mood (ts_cse DESC, inserted_at DESC);
