-- Queue tables for buffered ingest processing

CREATE TABLE IF NOT EXISTS ingest_queue (
  id BIGSERIAL PRIMARY KEY,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  parent_path TEXT,
  ci_rn TEXT,
  ct TEXT,
  payload JSONB NOT NULL,
  attempts INT NOT NULL DEFAULT 0,
  locked_until TIMESTAMPTZ NULL,
  status TEXT NOT NULL DEFAULT 'queued', -- queued|processing|done|failed
  processed_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS ingest_queue_status_idx
  ON ingest_queue (status, locked_until NULLS FIRST, received_at);

-- Dead letter table to retain permanently failing messages
CREATE TABLE IF NOT EXISTS ingest_dead_letter (
  id BIGSERIAL PRIMARY KEY,
  failed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  parent_path TEXT,
  ci_rn TEXT,
  ct TEXT,
  payload JSONB NOT NULL,
  attempts INT NOT NULL,
  last_error TEXT
);
