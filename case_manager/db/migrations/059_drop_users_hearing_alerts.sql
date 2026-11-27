-- The account-wide "Email me about the hearings of my tracked cases" setting is gone: hearing emails are chosen case
-- by case (058). Anyone who had switched it off gets those choices carried over to the cases they can reach now.
--
--   psql case_manager -f db/migrations/059_drop_users_hearing_alerts.sql

BEGIN;

INSERT INTO user_case_alerts_off (user_id, case_id)
SELECT e.user_id, e.case_id FROM eff_user_cases e JOIN users u ON u.id = e.user_id WHERE NOT u.hearing_alerts
ON CONFLICT DO NOTHING;

ALTER TABLE users DROP COLUMN hearing_alerts;

COMMIT;
