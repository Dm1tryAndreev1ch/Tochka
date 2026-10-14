ALTER TABLE sessions ADD COLUMN IF NOT EXISTS data_erased bool NOT NULL DEFAULT false;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS assigned_user_id uuid REFERENCES staff_users;
