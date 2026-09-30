-- A special, well-known section per website: "Legal tools", rendered in its
-- own panel in the top-right of the public page (see app.py's public_view
-- and templates/public.html) with a sign-in box above its articles, rather
-- than in the main grid of ordinary sections.
--
--   psql case_manager -f db/migrations/023_legal_tools_section.sql

BEGIN;

ALTER TABLE website_sections ADD COLUMN kind TEXT NOT NULL DEFAULT 'section' CHECK (kind IN ('section', 'legal_tools'));

-- At most one per website -- the public page has exactly one such panel.
CREATE UNIQUE INDEX website_sections_one_legal_tools_idx ON website_sections (website_id) WHERE kind = 'legal_tools';

INSERT INTO website_sections (id, website_id, title, position, kind)
SELECT w.id || '-legal-tools', w.id, 'Legal tools', 99, 'legal_tools' FROM websites w;

COMMIT;
