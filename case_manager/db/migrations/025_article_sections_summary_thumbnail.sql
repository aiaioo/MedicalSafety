-- An article can now appear under any number of sections, on any number
-- of websites (articles.section_id -> a proper many-to-many table), plus a
-- short summary shown in public listings and an optional thumbnail chosen
-- from among the images already uploaded into the article.
--
--   psql case_manager -f db/migrations/025_article_sections_summary_thumbnail.sql

BEGIN;

ALTER TABLE articles ADD COLUMN summary TEXT NOT NULL DEFAULT '';
ALTER TABLE articles ADD COLUMN thumbnail_image_id TEXT REFERENCES article_images(id) ON DELETE SET NULL;

CREATE TABLE article_sections (
    article_id  TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    section_id  TEXT NOT NULL REFERENCES website_sections(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, section_id)
);
CREATE INDEX article_sections_section_id_idx ON article_sections (section_id);

INSERT INTO article_sections (article_id, section_id)
SELECT id, section_id FROM articles WHERE section_id IS NOT NULL;

-- section_id is replaced entirely by article_sections; dropping it also
-- drops articles_section_id_idx, which was defined solely on this column.
ALTER TABLE articles DROP COLUMN section_id;

COMMIT;
