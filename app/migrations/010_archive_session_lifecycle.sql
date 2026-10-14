-- An expired session must not remove the only index of its encrypted files.
ALTER TABLE archive_entries DROP CONSTRAINT IF EXISTS archive_entries_session_id_fkey;
ALTER TABLE archive_entries ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE archive_entries ADD CONSTRAINT archive_entries_session_id_fkey
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL;
