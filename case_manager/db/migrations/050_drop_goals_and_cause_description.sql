-- Causes are now just a title, managed from a dropdown on the Cases page: the goals (and their links to cases) and
-- the cause description are removed. This deletes any goals and cause descriptions that were saved.
--
--   psql case_manager -f db/migrations/050_drop_goals_and_cause_description.sql

DROP TABLE IF EXISTS goal_cases;
DROP TABLE IF EXISTS goals;
ALTER TABLE causes DROP COLUMN IF EXISTS description;
