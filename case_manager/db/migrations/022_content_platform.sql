-- Admin / content-creator roles, plus a small public content platform:
-- websites (currently medicalsafety.in and caseplan.in -- an admin can add
-- more later), each with an ordered list of sections, and articles
-- ("webpages") a content creator writes and an admin or the author assigns
-- to a section to publish. An article's doc is the same kind of
-- Tiptap/ProseMirror JSON a report's is (see reports.doc above), just never
-- paginated or exported -- so it gets the same JSONB-wholesale treatment.
--
--   psql case_manager -f db/migrations/022_content_platform.sql

BEGIN;

ALTER TABLE users ADD COLUMN is_admin           BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN is_content_creator BOOLEAN NOT NULL DEFAULT FALSE;

-- The seed account (see db/migrations/001_users_and_access.sql) is the
-- first administrator; they can designate others from the admin page.
UPDATE users SET is_admin = TRUE WHERE email = 'cohan.sujay@gmail.com';

CREATE TABLE websites (
    id          TEXT PRIMARY KEY,               -- e.g. "medicalsafety.in"
    domain      TEXT NOT NULL UNIQUE,            -- the host it's served on (see app.py's inject_site_name)
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE website_sections (
    id          TEXT PRIMARY KEY,
    website_id  TEXT NOT NULL REFERENCES websites(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0,      -- display order within the website
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX website_sections_website_id_idx ON website_sections (website_id);

INSERT INTO websites (id, domain, name) VALUES
    ('medicalsafety.in', 'medicalsafety.in', 'Medical Safety'),
    ('caseplan.in', 'caseplan.in', 'Case Plan');

INSERT INTO website_sections (id, website_id, title, position) VALUES
    ('medicalsafety-articles', 'medicalsafety.in', 'Articles', 0),
    ('medicalsafety-guides', 'medicalsafety.in', 'Guides', 1),
    ('caseplan-articles', 'caseplan.in', 'Articles', 0),
    ('caseplan-guides', 'caseplan.in', 'Guides', 1);

-- A NULL section_id means the article is an unpublished draft. `published`
-- is tracked separately (not just "has a section") so a content creator can
-- pick a section ahead of time and still hold the article back, or
-- temporarily unpublish it without losing the placement.
CREATE TABLE articles (
    id          TEXT PRIMARY KEY,                -- "<slugified-title>-<hex6>"
    title       TEXT NOT NULL DEFAULT '',
    doc         JSONB,
    section_id  TEXT REFERENCES website_sections(id) ON DELETE SET NULL,
    published   BOOLEAN NOT NULL DEFAULT FALSE,
    position    INTEGER NOT NULL DEFAULT 0,       -- display order within its section
    author_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX articles_author_id_idx ON articles (author_id);
CREATE INDEX articles_section_id_idx ON articles (section_id);

-- Images a content creator inserts into an article, stored the same way a
-- user's account photo is (BYTEA on its own row) -- simpler than the
-- storage-backend key scheme in db/storage_backend.py, and fine for the
-- modest number/size of images a web article embeds.
CREATE TABLE article_images (
    id            TEXT PRIMARY KEY,               -- short hex id, embedded in its serving URL
    article_id    TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    content_type  TEXT NOT NULL,
    data          BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX article_images_article_id_idx ON article_images (article_id);

COMMIT;
