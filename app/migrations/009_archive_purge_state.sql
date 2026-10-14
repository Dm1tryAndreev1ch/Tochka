-- Keep completed deletion tombstones without selecting them on every sweep.
ALTER TABLE archive_entries ADD COLUMN IF NOT EXISTS purged_at timestamptz;
