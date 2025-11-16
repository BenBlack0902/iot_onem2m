-- Allow multiple raw_onem2m_ci rows per (parent_path, ci_rn)
-- and multiple fact_telemetry rows per (parent_path, ci_rn, metric_id)

BEGIN;

-- Drop composite primary key on raw table and add surrogate id PK
ALTER TABLE IF EXISTS raw_onem2m_ci
  DROP CONSTRAINT IF EXISTS raw_onem2m_ci_pkey;

ALTER TABLE IF EXISTS raw_onem2m_ci
  ADD COLUMN IF NOT EXISTS id BIGSERIAL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name = 'raw_onem2m_ci' AND constraint_type = 'PRIMARY KEY'
  ) THEN
    EXECUTE 'ALTER TABLE raw_onem2m_ci ADD PRIMARY KEY (id)';
  END IF;
END$$;

-- Drop uniqueness on fact_telemetry so every notification can insert its own fact rows
ALTER TABLE IF EXISTS fact_telemetry
  DROP CONSTRAINT IF EXISTS fact_telemetry_parent_path_ci_rn_metric_id_key;

COMMIT;
