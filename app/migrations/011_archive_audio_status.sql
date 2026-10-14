ALTER TABLE archive_entries ADD COLUMN IF NOT EXISTS audio_status text NOT NULL DEFAULT 'not_recorded';
UPDATE archive_entries SET audio_status='complete' WHERE audio_available;
