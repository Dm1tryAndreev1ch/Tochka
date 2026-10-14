-- Bookkeeping is administered only by the separate migration principal.
ALTER TABLE public.schema_migrations
  ADD COLUMN IF NOT EXISTS source_sha256 text,
  ADD COLUMN IF NOT EXISTS checksum_origin text,
  ADD COLUMN IF NOT EXISTS checksum_recorded_at timestamptz;
ALTER TABLE public.schema_migrations
  ADD CONSTRAINT schema_migrations_checksum_format
    CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
  ADD CONSTRAINT schema_migrations_checksum_origin
    CHECK (checksum_origin IS NULL OR checksum_origin IN ('applied','legacy_baseline'));
