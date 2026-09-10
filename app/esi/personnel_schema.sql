-- Additive M1 schema. Not executed by server startup until the M2 opt-in migration.
CREATE TABLE IF NOT EXISTS personnel_profiles (
    character_id BIGINT PRIMARY KEY CHECK (character_id > 0),
    name TEXT NOT NULL,
    name_checked_at DOUBLE PRECISION NOT NULL,
    affiliation_json TEXT,
    affiliation_fetched_at DOUBLE PRECISION,
    first_seen_at DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_personnel_last_seen ON personnel_profiles (last_seen_at, character_id);
CREATE TABLE IF NOT EXISTS personnel_names (
    name_key TEXT PRIMARY KEY,
    character_id BIGINT NOT NULL REFERENCES personnel_profiles(character_id),
    verified_at DOUBLE PRECISION NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_personnel_names_character ON personnel_names (character_id, active);
CREATE TABLE IF NOT EXISTS personnel_organizations (
    kind TEXT NOT NULL CHECK (kind IN ('corporation', 'alliance')),
    entity_id BIGINT NOT NULL CHECK (entity_id > 0),
    name TEXT NOT NULL,
    fetched_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (kind, entity_id)
);
CREATE TABLE IF NOT EXISTS personnel_refresh_jobs (
    kind TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    context_key TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 5),
    next_due_at DOUBLE PRECISION NOT NULL,
    revision BIGINT NOT NULL DEFAULT 1,
    leased_revision BIGINT NOT NULL DEFAULT 0,
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, entity_key, context_key)
);
CREATE INDEX IF NOT EXISTS idx_personnel_refresh_due ON personnel_refresh_jobs
    (next_due_at, priority, lease_until);
CREATE INDEX IF NOT EXISTS idx_personnel_refresh_lane ON personnel_refresh_jobs
    (kind, priority, next_due_at, lease_until);
CREATE TABLE IF NOT EXISTS personnel_backfill_progress (
    source TEXT PRIMARY KEY,
    cursor_value TEXT NOT NULL DEFAULT '',
    imported BIGINT NOT NULL DEFAULT 0,
    rejected BIGINT NOT NULL DEFAULT 0
);
