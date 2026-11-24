-- New reports' annexures include only blackouts by default (was 'none', i.e.
-- omit annotations). Existing reports keep their current setting. Apply after
-- 054_user_case_order.sql:
--
--   psql case_manager -f db/migrations/055_annexure_annotations_default_blackouts.sql

ALTER TABLE reports ALTER COLUMN annexure_annotations SET DEFAULT 'blackouts';
