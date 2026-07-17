CREATE SCHEMA research;

CREATE TABLE research.import_runs (
    run_id text PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'success', 'failure')),
    files_seen integer NOT NULL DEFAULT 0,
    records_inserted bigint NOT NULL DEFAULT 0,
    records_existing bigint NOT NULL DEFAULT 0,
    error_type text,
    error_message text
);

CREATE TABLE research.import_checkpoints (
    source_file text PRIMARY KEY,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    line_count bigint NOT NULL CHECK (line_count >= 0),
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE research.records (
    record_id text PRIMARY KEY,
    record_type text NOT NULL,
    schema_version smallint NOT NULL,
    source_file text NOT NULL,
    source_line bigint NOT NULL CHECK (source_line > 0),
    payload jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_file, source_line),
    CHECK ((payload->>'research_only')::boolean = true),
    CHECK ((payload->>'backfill')::boolean = true),
    CHECK ((payload->>'strict_backtest_eligible')::boolean = false),
    CHECK ((payload->>'cutoff_eligible')::boolean = false)
);

CREATE INDEX research_records_type_idx ON research.records(record_type, imported_at);

CREATE TABLE research.source_assets (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    asset_id text NOT NULL,
    url text,
    asset_kind text NOT NULL,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    blob_path text NOT NULL,
    downloaded_at timestamptz,
    etag text,
    last_modified text,
    metadata_sha256 text,
    input_hash text,
    UNIQUE (asset_id, sha256)
);

CREATE TABLE research.fixtures (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    source_asset_record_id text NOT NULL REFERENCES research.source_assets(record_id),
    source_fixture_key text NOT NULL,
    competition text NOT NULL,
    match_date date NOT NULL,
    kickoff_time_raw text,
    home_team text NOT NULL,
    away_team text NOT NULL,
    home_goals integer CHECK (home_goals >= 0),
    away_goals integer CHECK (away_goals >= 0),
    result_scope text NOT NULL,
    result_eligible boolean NOT NULL,
    source_payload jsonb NOT NULL,
    UNIQUE (source_asset_record_id, source_fixture_key)
);

CREATE INDEX research_fixtures_date_idx ON research.fixtures(match_date, competition);

CREATE TABLE research.market_observations (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    fixture_record_id text NOT NULL REFERENCES research.fixtures(record_id),
    source_id text NOT NULL,
    asset_sha256 text NOT NULL,
    cohort text NOT NULL CHECK (cohort IN ('opening', 'closing')),
    market text NOT NULL CHECK (market IN ('1x2', 'total', 'asian_handicap')),
    bookmaker text NOT NULL,
    line numeric(18, 8),
    values_json jsonb NOT NULL,
    market_contract text NOT NULL
);

CREATE INDEX research_market_fixture_idx
    ON research.market_observations(fixture_record_id, cohort, market);

CREATE TABLE research.feature_rows (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    source_asset_record_id text NOT NULL REFERENCES research.source_assets(record_id),
    source_fixture_key text NOT NULL,
    competition text NOT NULL,
    match_date date NOT NULL,
    season text NOT NULL,
    cohort text NOT NULL CHECK (cohort = 'derived_closing_features'),
    feature_schema text NOT NULL,
    market_contract text NOT NULL,
    input_hash text NOT NULL,
    result_scope text NOT NULL,
    result_eligible boolean NOT NULL,
    features jsonb NOT NULL,
    UNIQUE (source_asset_record_id, source_fixture_key)
);

CREATE INDEX research_feature_date_idx ON research.feature_rows(match_date, competition);

CREATE TABLE research.quality_events (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    event_type text NOT NULL,
    status text NOT NULL,
    details jsonb NOT NULL
);

CREATE VIEW research.eligible_fixtures AS
SELECT fixture.*
FROM research.fixtures AS fixture
WHERE fixture.result_eligible = true
  AND fixture.result_scope = 'regular_time_90'
  AND NOT EXISTS (
      SELECT 1 FROM research.quality_events AS event
      WHERE event.details->>'fixture_record_id' = fixture.record_id
        AND event.status = 'failure'
  );
