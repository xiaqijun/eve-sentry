CREATE TABLE IF NOT EXISTS esi_id_cache (
    endpoint TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    stale_until TIMESTAMPTZ NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    PRIMARY KEY (endpoint, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_esi_id_cache_refresh
    ON esi_id_cache (next_retry_at, expires_at);
