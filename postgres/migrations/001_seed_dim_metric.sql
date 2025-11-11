-- Seed canonical metrics (idempotent)
INSERT INTO dim_metric(metric_rn, unit)
VALUES
  ('temperature', 'C'),
  ('humidity', '%'),
  ('co2', 'ppm'),
  ('lux', 'lux'),
  ('noise', 'dB'),
  ('occupancy', 'count')
ON CONFLICT (metric_rn) DO NOTHING;
