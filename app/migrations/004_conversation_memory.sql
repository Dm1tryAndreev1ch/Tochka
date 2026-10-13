ALTER TABLE sessions ADD COLUMN IF NOT EXISTS memory jsonb NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'website';
INSERT INTO settings(key,value) VALUES ('call_mode','{"mode":"website","revision":1}') ON CONFLICT DO NOTHING;
