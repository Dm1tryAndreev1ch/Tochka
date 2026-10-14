ALTER TABLE turns ADD COLUMN IF NOT EXISTS generated_text text;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS in_reply_to uuid;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS sent_chars int NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN IF NOT EXISTS delivery text NOT NULL DEFAULT 'delivered';
UPDATE turns SET generated_text=text WHERE role='assistant' AND generated_text IS NULL;
UPDATE turns t SET text='',delivery='legacy_unconfirmed'
FROM sessions s WHERE t.session_id=s.id AND s.channel='voice' AND t.role='assistant';
CREATE UNIQUE INDEX IF NOT EXISTS turns_reply_key ON turns(session_id,in_reply_to) WHERE in_reply_to IS NOT NULL;
