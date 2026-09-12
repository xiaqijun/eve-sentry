CREATE TABLE IF NOT EXISTS personnel_relation_snapshots (
    context_key TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('corporation', 'alliance')),
    source_id BIGINT NOT NULL CHECK (source_id > 0),
    revision BIGINT NOT NULL DEFAULT 0,
    successful_at DOUBLE PRECISION,
    expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    retry_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    authorized INTEGER NOT NULL DEFAULT 1 CHECK (authorized IN (0, 1)),
    PRIMARY KEY (context_key, source_kind, source_id)
);
CREATE TABLE IF NOT EXISTS personnel_relation_entries (
    context_key TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id BIGINT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('corporation', 'alliance')),
    target_id BIGINT NOT NULL CHECK (target_id > 0),
    standing DOUBLE PRECISION NOT NULL CHECK (standing BETWEEN -10 AND 10),
    PRIMARY KEY (context_key, source_kind, source_id, target_kind, target_id),
    FOREIGN KEY (context_key, source_kind, source_id)
        REFERENCES personnel_relation_snapshots(context_key, source_kind, source_id)
);
