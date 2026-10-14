CREATE TABLE IF NOT EXISTS company (
  id int PRIMARY KEY CHECK(id=1), name text NOT NULL DEFAULT 'Точка', revision int NOT NULL DEFAULT 1,
  settings jsonb NOT NULL DEFAULT '{"record_audio":false,"text_days":30,"audio_days":30,"policy_version":1}'
);
INSERT INTO company(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS staff_users (
  id uuid PRIMARY KEY, username text NOT NULL UNIQUE, display_name text NOT NULL,
  password_hash text NOT NULL, role text NOT NULL, permissions jsonb NOT NULL,
  active bool NOT NULL DEFAULT true, revision int NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES staff_users;
ALTER TABLE audit ADD COLUMN IF NOT EXISTS user_id uuid;
ALTER TABLE audit ADD COLUMN IF NOT EXISTS request_id text;
ALTER TABLE audit ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'allowed';
ALTER TABLE audit ADD COLUMN IF NOT EXISTS purpose text;
ALTER TABLE audit ADD COLUMN IF NOT EXISTS previous_hash text;
ALTER TABLE audit ADD COLUMN IF NOT EXISTS entry_hash text;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz;
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS synced_at timestamptz;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS audio_consent bool NOT NULL DEFAULT false;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS policy jsonb NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS agent_config jsonb NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_at timestamptz;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_method text;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS connected_at timestamptz;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS started_at timestamptz;
CREATE TABLE IF NOT EXISTS session_usage (
  session_id uuid PRIMARY KEY REFERENCES sessions ON DELETE CASCADE,
  llm_requests int NOT NULL DEFAULT 0, prompt_tokens bigint NOT NULL DEFAULT 0,
  completion_tokens bigint NOT NULL DEFAULT 0, unreported_requests int NOT NULL DEFAULT 0,
  llm_seconds double precision NOT NULL DEFAULT 0,
  stt_audio_seconds double precision NOT NULL DEFAULT 0, stt_seconds double precision NOT NULL DEFAULT 0,
  tts_audio_seconds double precision NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS archive_entries (
  id uuid PRIMARY KEY, session_id uuid UNIQUE NOT NULL REFERENCES sessions ON DELETE CASCADE,
  voice_profile text NOT NULL, source text NOT NULL, started_at timestamptz,
  ended_at timestamptz NOT NULL, transcript_expires_at timestamptz NOT NULL,
  audio_expires_at timestamptz, audio_available bool NOT NULL DEFAULT false,
  deleted_at timestamptz, policy jsonb NOT NULL, transcript_hash text NOT NULL,
  audio_hash text, audio_seconds double precision NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agents (
  profile text PRIMARY KEY CHECK(profile IN ('male','female')), name text NOT NULL,
  instructions text NOT NULL DEFAULT '', knowledge_ids jsonb NOT NULL DEFAULT '[]',
  allowed_actions jsonb NOT NULL DEFAULT '["booking","callback","handoff"]',
  revision int NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO agents(profile,name) VALUES('male','Геннадий'),('female','Мария') ON CONFLICT DO NOTHING;
CREATE INDEX IF NOT EXISTS archive_ended ON archive_entries(ended_at DESC);
