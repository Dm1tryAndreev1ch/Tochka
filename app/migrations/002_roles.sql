ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'admin';
