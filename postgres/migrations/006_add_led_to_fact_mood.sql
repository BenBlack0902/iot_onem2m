-- Add LED color field to fact_mood for visual indicator mapping
BEGIN;
ALTER TABLE IF EXISTS fact_mood
  ADD COLUMN IF NOT EXISTS led_color TEXT;

-- Optional index if you plan to filter by color categories (not strictly needed)
-- CREATE INDEX IF NOT EXISTS fact_mood_led_color_idx ON fact_mood (led_color);
COMMIT;
