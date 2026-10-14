ALTER TABLE agents ADD COLUMN IF NOT EXISTS category_schema jsonb NOT NULL DEFAULT '{}';
