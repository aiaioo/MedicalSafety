-- A counter bumped each time a page of a document is rotated, so page images
-- the browser has cached (for months) are fetched afresh. Apply after
-- 056_annexure_page_numbers_default_top_center.sql:
--
--   psql case_manager -f db/migrations/057_document_rotation_rev.sql

ALTER TABLE documents ADD COLUMN rotation_rev INTEGER NOT NULL DEFAULT 0;
