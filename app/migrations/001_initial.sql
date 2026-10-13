CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS services (
  id text PRIMARY KEY, name text NOT NULL, description text NOT NULL, price int NOT NULL CHECK(price>=0),
  duration int NOT NULL CHECK(duration>0), active bool NOT NULL DEFAULT true, revision int NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  id uuid PRIMARY KEY, token_hash text NOT NULL UNIQUE, channel text NOT NULL, state text NOT NULL DEFAULT 'bot',
  context jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
  ended_at timestamptz, consent bool NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id bigserial PRIMARY KEY, session_id uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
  role text NOT NULL, text text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  message_key uuid, reply jsonb, UNIQUE(session_id,message_key)
);
CREATE TABLE IF NOT EXISTS drafts (
  id uuid PRIMARY KEY, session_id uuid NOT NULL REFERENCES sessions ON DELETE CASCADE,
  kind text NOT NULL, payload jsonb NOT NULL, revision int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'awaiting_confirmation', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS actions (
  id uuid PRIMARY KEY, session_id uuid REFERENCES sessions ON DELETE SET NULL, draft_id uuid UNIQUE,
  idempotency_key uuid NOT NULL UNIQUE, kind text NOT NULL, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS bookings (
  id uuid PRIMARY KEY, action_id uuid NOT NULL UNIQUE REFERENCES actions, service_id text NOT NULL REFERENCES services,
  service_name text NOT NULL, price int NOT NULL, duration int NOT NULL,
  resource_id text NOT NULL DEFAULT 'main', starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
  name text NOT NULL, phone text NOT NULL, status text NOT NULL DEFAULT 'confirmed',
  token_hash text NOT NULL, token_expires_at timestamptz NOT NULL, revision int NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(), CHECK(ends_at>starts_at),
  EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at,ends_at,'[)') WITH &&) WHERE (status='confirmed')
);
CREATE TABLE IF NOT EXISTS requests (
  id uuid PRIMARY KEY, action_id uuid NOT NULL UNIQUE REFERENCES actions, session_id uuid REFERENCES sessions ON DELETE SET NULL,
  kind text NOT NULL, name text NOT NULL, phone text NOT NULL, summary text NOT NULL, status text NOT NULL DEFAULT 'new',
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge (
  id uuid PRIMARY KEY, title text NOT NULL, content text NOT NULL, source text NOT NULL,
  revision int NOT NULL DEFAULT 1, published_revision int, published_content text, published_title text,
  archived bool NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS outbox (
  id uuid PRIMARY KEY, action_id uuid NOT NULL REFERENCES actions, entity text NOT NULL,
  payload jsonb NOT NULL, status text NOT NULL DEFAULT 'pending', attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(), external_id text, error text,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(action_id,entity)
);
CREATE TABLE IF NOT EXISTS audit (
  id bigserial PRIMARY KEY, actor text NOT NULL, event text NOT NULL, resource_id text,
  details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash text PRIMARY KEY, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id,id);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(next_attempt_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at DESC);
INSERT INTO settings(key,value) VALUES ('schedule','{"opens":9,"closes":18,"weekdays":[0,1,2,3,4,5],"closed_dates":[]}'),('operator','{"available":false}') ON CONFLICT DO NOTHING;
INSERT INTO services(id,name,description,price,duration) VALUES
 ('consultation','Консультация','Разберём задачу и предложим решение. Тестовая услуга.',30,30),
 ('diagnostics','Диагностика','Проверка устройства и заключение специалиста. Тестовая услуга.',45,60),
 ('setup','Настройка','Установка и настройка программного обеспечения. Тестовая услуга.',60,60)
ON CONFLICT DO NOTHING;
