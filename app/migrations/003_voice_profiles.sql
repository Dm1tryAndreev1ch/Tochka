ALTER TABLE sessions ADD COLUMN IF NOT EXISTS voice_profile text NOT NULL DEFAULT 'male' CHECK(voice_profile IN ('male','female'));
