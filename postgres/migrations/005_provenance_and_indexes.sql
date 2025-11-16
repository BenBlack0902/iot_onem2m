-- Add provenance and indexes for faster queries
BEGIN;

-- 1) Add raw_id to fact_telemetry and set FK to raw_onem2m_ci(id)
ALTER TABLE IF EXISTS fact_telemetry
  ADD COLUMN IF NOT EXISTS raw_id BIGINT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.table_constraints tc
    WHERE tc.table_name = 'fact_telemetry'
      AND tc.constraint_type = 'FOREIGN KEY'
      AND tc.constraint_name = 'fact_telemetry_raw_id_fkey'
  ) THEN
    ALTER TABLE fact_telemetry
      ADD CONSTRAINT fact_telemetry_raw_id_fkey
      FOREIGN KEY (raw_id) REFERENCES raw_onem2m_ci(id) ON DELETE SET NULL;
  END IF;
END$$;

-- 2) Indexes to support common queries
CREATE INDEX IF NOT EXISTS fact_parent_path_idx ON fact_telemetry (parent_path);
CREATE INDEX IF NOT EXISTS fact_metric_ts_idx ON fact_telemetry (metric_id, ts_cse DESC NULLS LAST);

COMMIT;
