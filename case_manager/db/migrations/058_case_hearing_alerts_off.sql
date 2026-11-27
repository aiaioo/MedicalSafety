-- Per-case hearing alerts: a row means this user does not want hearing emails about this case (a switch on the
-- case's card on the Cases page). No row means they do. users.hearing_alerts (052) still turns all of them off.
--
--   psql case_manager -f db/migrations/058_case_hearing_alerts_off.sql

BEGIN;

CREATE TABLE user_case_alerts_off (
    user_id   BIGINT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    case_id   TEXT    NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, case_id)
);

COMMIT;
