-- RAW INGESTED DATA (one row per source fetch)
CREATE TABLE raw_source_data (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT NOT NULL,          -- 'github' | 'stackexchange' | 'devto' | 'hackernews'
    source_handle   TEXT NOT NULL,          -- the handle used to fetch
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,         -- full raw API response, re-derivable
    fetch_meta      JSONB,                  -- rate limit headers, latency ms, http status
    payload_version INT NOT NULL DEFAULT 1  -- for future reprocessing without re-fetching
);

CREATE INDEX idx_raw_source_data_source ON raw_source_data(source, source_handle);

-- CANONICAL PERSONS
CREATE TABLE persons (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name        TEXT,
    canonical_email     TEXT,
    location            TEXT,
    bio                 TEXT,
    avatar_url          TEXT,
    llm_summary         TEXT,               -- open-router-generated paragraph
    resolution_status   TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | RESOLVED | AMBIGUOUS | FAILED
    enrichment_status   TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | LLM_RUNNING | READY | FAILED
    first_query         JSONB,              -- immutable original search context
    latest_query        JSONB,              -- updated on every re-resolve
    last_resolved_at    TIMESTAMPTZ,
    last_attempted_at   TIMESTAMPTZ,
    last_error          TEXT,
    last_error_at       TIMESTAMPTZ,
    retry_count         INT NOT NULL DEFAULT 0,
    provider_statuses   JSONB,              -- {"github":"SUCCESS","stackoverflow":"FAILED",...}
    conflicts           JSONB,              -- conflicting field values across sources
    completeness_score  FLOAT,
    last_resolution_ms  INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- UNIFIED ATTRIBUTES (key/value, source-attributed)
-- Survives a 5th data source with zero schema changes
CREATE TABLE person_attributes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id   UUID NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    attr_key    TEXT NOT NULL,    -- e.g. 'languages', 'top_tags', 'article_count'
    attr_value  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_person_attributes_upsert
ON person_attributes(person_id, source, attr_key);

CREATE INDEX idx_person_attributes_person ON person_attributes(person_id, source);

-- LINKAGES — raw record ↔ canonical person
CREATE TABLE source_links (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id        UUID NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    raw_source_id    UUID NOT NULL REFERENCES raw_source_data(id) ON DELETE CASCADE,
    source           TEXT NOT NULL,
    source_handle    TEXT NOT NULL,
    confidence       NUMERIC(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    confidence_notes JSONB,               -- which signals fired and their weights
    linked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(person_id, raw_source_id)
);

CREATE INDEX idx_source_links_person ON source_links(person_id);

-- One external account cannot link to two different canonical persons
CREATE UNIQUE INDEX idx_source_links_unique_handle
ON source_links(source, source_handle)
WHERE source_handle IS NOT NULL;

-- OBSERVABILITY
CREATE TABLE api_call_log (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source       TEXT NOT NULL,
    endpoint     TEXT,
    status_code  INT,
    latency_ms   INT,
    called_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE llm_usage_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id       UUID REFERENCES persons(id),
    prompt_tokens   INT,
    output_tokens   INT,
    model           TEXT,
    called_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- auto-update persons.updated_at
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

CREATE TRIGGER persons_updated_at
    BEFORE UPDATE ON persons
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
