-- Text expiration does not shorten a separately approved audio retention period.
ALTER TABLE archive_entries ADD COLUMN IF NOT EXISTS transcript_purged_at timestamptz;
