-- A tagline shown under a website's name on the public page (replacing the
-- hardcoded "Articles and guides from <name>." subtitle -- see app.py's
-- public_view and templates/public.html), and a description per section.
--
--   psql case_manager -f db/migrations/024_website_tagline_section_description.sql

BEGIN;

ALTER TABLE websites ADD COLUMN tagline TEXT NOT NULL DEFAULT '';
ALTER TABLE website_sections ADD COLUMN description TEXT NOT NULL DEFAULT '';

-- Seed with exactly what the hardcoded subtitle used to read, so nothing
-- visibly changes until someone edits it from the admin/websites page.
UPDATE websites SET tagline = 'Articles and guides from ' || name || '.';

COMMIT;
