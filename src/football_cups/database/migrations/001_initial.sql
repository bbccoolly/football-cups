CREATE TABLE football.import_runs (
    run_id text PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'success', 'failure')),
    files_seen integer NOT NULL DEFAULT 0,
    lines_seen bigint NOT NULL DEFAULT 0,
    records_inserted bigint NOT NULL DEFAULT 0,
    records_existing bigint NOT NULL DEFAULT 0,
    inserted_by_type jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_type text,
    error_message text
);

CREATE TABLE football.import_checkpoints (
    source_file text PRIMARY KEY,
    byte_offset bigint NOT NULL CHECK (byte_offset >= 0),
    line_number bigint NOT NULL CHECK (line_number >= 0),
    file_size bigint NOT NULL CHECK (file_size >= 0),
    file_mtime_ns bigint NOT NULL,
    last_record_id text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE football.records (
    record_id text PRIMARY KEY,
    record_type text NOT NULL,
    schema_version smallint NOT NULL,
    fixture_id text,
    event_at timestamptz,
    payload jsonb NOT NULL,
    source_file text NOT NULL,
    source_line bigint NOT NULL CHECK (source_line > 0),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_file, source_line)
);

CREATE INDEX records_type_event_idx ON football.records (record_type, event_at);
CREATE INDEX records_fixture_event_idx ON football.records (fixture_id, event_at);

CREATE TABLE football.fixture_identities (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    kickoff_at timestamptz,
    buy_end_at timestamptz,
    competition_id text,
    competition_name text,
    season_id text,
    match_number text,
    home_team_id text,
    home_team_name text,
    away_team_id text,
    away_team_name text,
    identity_status text
);

CREATE INDEX fixture_identities_fixture_observed_idx
    ON football.fixture_identities (fixture_id, observed_at DESC);

CREATE TABLE football.discovery_observations (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    kickoff_at timestamptz,
    buy_end_at timestamptz,
    source_name text NOT NULL,
    source_url text NOT NULL,
    competition_id text,
    competition_name text,
    season_id text,
    match_number text,
    home_team_id text,
    home_team_name text,
    away_team_id text,
    away_team_name text,
    official_handicap_raw text,
    is_show_raw text,
    is_active_raw text,
    is_end_raw text,
    row_sha256 text
);

CREATE INDEX discovery_fixture_observed_idx
    ON football.discovery_observations (fixture_id, observed_at DESC);
CREATE INDEX discovery_source_observed_idx
    ON football.discovery_observations (source_name, observed_at DESC);

CREATE TABLE football.sporttery_pool_observations (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    source_name text NOT NULL,
    source_url text NOT NULL,
    pool_type text NOT NULL,
    option_value text NOT NULL,
    handicap_raw text,
    sp_raw text,
    sp_decimal numeric(18, 8)
);

CREATE INDEX sporttery_fixture_observed_idx
    ON football.sporttery_pool_observations (fixture_id, observed_at DESC);

CREATE TABLE football.snapshot_batches (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    target text NOT NULL,
    job_id text,
    window_start timestamptz,
    window_end timestamptz,
    completed_at timestamptz,
    core_market_complete boolean NOT NULL,
    strict_eligible boolean NOT NULL,
    market_results jsonb NOT NULL
);

CREATE INDEX snapshot_batches_fixture_target_idx
    ON football.snapshot_batches (fixture_id, target, completed_at DESC);
CREATE INDEX snapshot_batches_strict_idx
    ON football.snapshot_batches (strict_eligible, target, window_end);

CREATE TABLE football.market_snapshots (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    market text NOT NULL,
    target text NOT NULL,
    observed_at timestamptz NOT NULL,
    ingested_at timestamptz,
    corrected_at timestamptz,
    source_event_time timestamptz,
    source_url text,
    raw_sha256 text,
    parser_version text,
    parse_status text,
    source_market_available boolean,
    clock_ok boolean,
    bookmaker_count integer,
    row_count integer
);

CREATE INDEX market_snapshots_asof_idx
    ON football.market_snapshots (fixture_id, observed_at DESC, market, target);
CREATE INDEX market_snapshots_sha_idx ON football.market_snapshots (raw_sha256);

CREATE TABLE football.bookmaker_market_rows (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    market text NOT NULL,
    target text NOT NULL,
    observed_at timestamptz NOT NULL,
    corrected_at timestamptz,
    source_event_time timestamptz,
    opening_source_event_time timestamptz,
    source_bookmaker_id text,
    source_bookmaker_name text,
    row_role text NOT NULL CHECK (row_role IN ('bookmaker', 'official', 'summary', 'unknown')),
    opening jsonb,
    current jsonb,
    opening_home numeric(18, 8),
    opening_draw numeric(18, 8),
    opening_away numeric(18, 8),
    opening_line numeric(18, 8),
    opening_over numeric(18, 8),
    opening_under numeric(18, 8),
    current_home numeric(18, 8),
    current_draw numeric(18, 8),
    current_away numeric(18, 8),
    current_line numeric(18, 8),
    current_over numeric(18, 8),
    current_under numeric(18, 8)
);

CREATE INDEX bookmaker_rows_asof_idx
    ON football.bookmaker_market_rows (fixture_id, observed_at DESC, market, target);
CREATE INDEX bookmaker_rows_company_idx
    ON football.bookmaker_market_rows (source_bookmaker_name, market, observed_at DESC);

CREATE TABLE football.result_candidates (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    home_goals integer NOT NULL CHECK (home_goals >= 0),
    away_goals integer NOT NULL CHECK (away_goals >= 0),
    half_time_score_raw text,
    status_raw text,
    scope text NOT NULL,
    completed_page_sha256 text,
    analysis_page_sha256 text,
    source_urls jsonb NOT NULL
);

CREATE INDEX result_candidates_fixture_observed_idx
    ON football.result_candidates (fixture_id, observed_at DESC);

CREATE TABLE football.verified_results (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    confirmed_at timestamptz NOT NULL,
    home_goals integer NOT NULL CHECK (home_goals >= 0),
    away_goals integer NOT NULL CHECK (away_goals >= 0),
    scope text NOT NULL,
    source_url text NOT NULL,
    verification_method text NOT NULL,
    notes text,
    candidate_id text
);

CREATE INDEX verified_results_fixture_confirmed_idx
    ON football.verified_results (fixture_id, confirmed_at DESC);

CREATE TABLE football.quality_events (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    occurred_at timestamptz NOT NULL,
    event_type text NOT NULL,
    status text NOT NULL,
    fixture_id text,
    competition text,
    market text,
    cutoff text,
    details jsonb NOT NULL
);

CREATE INDEX quality_events_time_idx
    ON football.quality_events (occurred_at DESC, event_type, status);
CREATE INDEX quality_events_fixture_idx
    ON football.quality_events (fixture_id, occurred_at DESC);

CREATE VIEW football.latest_fixture_identities AS
SELECT DISTINCT ON (fixture_id)
    record_id,
    fixture_id,
    observed_at,
    kickoff_at,
    buy_end_at,
    competition_id,
    competition_name,
    season_id,
    match_number,
    home_team_id,
    home_team_name,
    away_team_id,
    away_team_name,
    identity_status
FROM football.fixture_identities
ORDER BY fixture_id, observed_at DESC, record_id DESC;

CREATE VIEW football.unsupported_records AS
SELECT *
FROM football.records
WHERE record_type NOT IN (
    'FixtureIdentity',
    'DiscoveryObservation',
    'SportteryPoolObservation',
    'SnapshotBatch',
    'MarketSnapshot',
    'BookmakerMarketRow',
    'ResultCandidate',
    'VerifiedResult',
    'QualityEvent'
);

CREATE FUNCTION football.market_rows_as_of(
    p_fixture_id text,
    p_prediction_cutoff timestamptz
)
RETURNS TABLE (
    record_id text,
    fixture_id text,
    market text,
    target text,
    observed_at timestamptz,
    corrected_at timestamptz,
    source_event_time timestamptz,
    source_bookmaker_id text,
    source_bookmaker_name text,
    row_role text,
    opening jsonb,
    current jsonb
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        b.record_id,
        b.fixture_id,
        b.market,
        b.target,
        b.observed_at,
        b.corrected_at,
        b.source_event_time,
        b.source_bookmaker_id,
        b.source_bookmaker_name,
        b.row_role,
        b.opening,
        b.current
    FROM football.bookmaker_market_rows AS b
    WHERE b.fixture_id = p_fixture_id
      AND b.observed_at <= p_prediction_cutoff
      AND (b.corrected_at IS NULL OR b.corrected_at <= p_prediction_cutoff)
    ORDER BY b.observed_at, b.market, b.source_bookmaker_name, b.record_id
$$;
CREATE FUNCTION football.market_snapshots_as_of(
    p_fixture_id text,
    p_prediction_cutoff timestamptz
)
RETURNS SETOF football.market_snapshots
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT s.*
    FROM football.market_snapshots AS s
    WHERE s.fixture_id = p_fixture_id
      AND s.observed_at <= p_prediction_cutoff
      AND (s.corrected_at IS NULL OR s.corrected_at <= p_prediction_cutoff)
    ORDER BY s.observed_at, s.market, s.target, s.record_id
$$;
