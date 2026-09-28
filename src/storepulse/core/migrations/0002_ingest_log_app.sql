-- 0002: ingest_log.app_id (docs/SPEC.md, Storage). Never edit; add a new migration instead.
--
-- Distinguishes one app's collection attempts from another's for a per-app source
-- (play_installs, play_vitals log one row per app under the same report_date); NULL for
-- an account-wide source (apple_sales, play_sales, play_earnings), where report_date
-- alone already identifies one attempt.

ALTER TABLE ingest_log ADD COLUMN app_id INTEGER REFERENCES apps(id);
