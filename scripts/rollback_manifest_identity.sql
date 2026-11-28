-- Restore the package_manifests uniqueness the previous release writes against.
--
-- Releases before the manifest version-identity change upsert with
--   ON CONFLICT (owner, repo, name, kind, file_path)
-- and fail on every manifest write once that constraint is gone. The current
-- release drops it on startup, because one manifest file may now record several
-- versions of a package. Run this BEFORE starting the previous image:
--
--   1. Stop every current-release worker (they would drop the constraint again).
--   2. psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/rollback_manifest_identity.sql
--   3. Start the previous image.
--
-- Rows beyond one per key are extra versions only the current release reads.
-- The kept row prefers a production (is_dev = 0) entry, then the newest write.
-- Rolling forward again needs nothing: the current release re-runs its migration.

BEGIN;

LOCK TABLE package_manifests IN ACCESS EXCLUSIVE MODE;

DELETE FROM package_manifests
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY owner, repo, name, kind, file_path
                   ORDER BY is_dev ASC, updated_at DESC, id DESC
               ) AS rn
        FROM package_manifests
    ) AS ranked
    WHERE rn > 1
);

ALTER TABLE package_manifests
    ADD CONSTRAINT package_manifests_owner_repo_name_kind_file_path_key
    UNIQUE (owner, repo, name, kind, file_path);

COMMIT;
