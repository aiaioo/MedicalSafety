-- Each user's own drag-and-drop order of the cards on the Cases page. Cases without a row (new ones) list first.
--
--   psql case_manager -f db/migrations/054_user_case_order.sql

BEGIN;

CREATE TABLE user_case_order (
    user_id   BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    case_id   TEXT    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    PRIMARY KEY (user_id, case_id)
);

COMMIT;
