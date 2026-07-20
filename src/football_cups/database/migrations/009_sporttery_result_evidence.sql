ALTER TABLE football.result_candidates
    ADD COLUMN official_scope text,
    ADD COLUMN sporttery_result_observation_id text,
    ADD COLUMN sporttery_fixture_link_id text;

CREATE TABLE football.sporttery_inventory_batches (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    run_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    begin_date date NOT NULL,
    end_date date NOT NULL,
    page_size integer NOT NULL CHECK (page_size > 0),
    page_count integer NOT NULL CHECK (page_count > 0),
    row_count integer NOT NULL CHECK (row_count >= 0),
    complete boolean NOT NULL,
    raw_sha256s jsonb NOT NULL,
    source_urls jsonb NOT NULL
);

CREATE INDEX sporttery_inventory_observed_idx
    ON football.sporttery_inventory_batches (observed_at DESC, begin_date, end_date);

CREATE TABLE football.sporttery_scope_evidence (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL,
    source_url text NOT NULL,
    source_sha256 text NOT NULL,
    inventory_batch_record_id text NOT NULL,
    scope text NOT NULL,
    scope_text text NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'rejected'))
);

CREATE INDEX sporttery_scope_inventory_idx
    ON football.sporttery_scope_evidence (inventory_batch_record_id, observed_at DESC);

CREATE TABLE football.sporttery_fixture_links (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    sporttery_match_id text,
    observed_at timestamptz NOT NULL,
    inventory_batch_record_id text NOT NULL,
    match_number text,
    mapping_status text NOT NULL CHECK (mapping_status IN ('accepted', 'missing', 'rejected')),
    rejection_reason text,
    official_kickoff_at timestamptz,
    official_home_name text,
    official_away_name text
);

CREATE INDEX sporttery_fixture_links_fixture_idx
    ON football.sporttery_fixture_links (fixture_id, observed_at DESC);
CREATE INDEX sporttery_fixture_links_match_idx
    ON football.sporttery_fixture_links (sporttery_match_id, observed_at DESC);

CREATE TABLE football.sporttery_result_observations (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    sporttery_match_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    home_goals integer NOT NULL CHECK (home_goals >= 0),
    away_goals integer NOT NULL CHECK (away_goals >= 0),
    status_text text,
    result_status_text text,
    is_cancel boolean,
    scope text NOT NULL,
    inventory_batch_record_id text NOT NULL,
    scope_evidence_record_id text NOT NULL,
    fixture_link_record_id text NOT NULL,
    inventory_sha256 text NOT NULL,
    head_sha256 text NOT NULL,
    fixed_bonus_sha256 text NOT NULL,
    source_urls jsonb NOT NULL,
    raw_summary jsonb NOT NULL
);

CREATE INDEX sporttery_result_observations_fixture_idx
    ON football.sporttery_result_observations (fixture_id, observed_at DESC);
CREATE INDEX sporttery_result_observations_match_idx
    ON football.sporttery_result_observations (sporttery_match_id, observed_at DESC);

CREATE VIEW football.current_sporttery_fixture_links AS
SELECT DISTINCT ON (fixture_id) *
FROM football.sporttery_fixture_links
WHERE mapping_status = 'accepted'
ORDER BY fixture_id, observed_at DESC, record_id DESC;

CREATE VIEW football.current_sporttery_result_observations AS
SELECT DISTINCT ON (fixture_id) *
FROM football.sporttery_result_observations
WHERE scope = '90-minutes-including-stoppage'
  AND COALESCE(is_cancel, false) = false
ORDER BY fixture_id, observed_at DESC, record_id DESC;

CREATE OR REPLACE VIEW football.unsupported_records AS
SELECT *
FROM football.records
WHERE record_type NOT IN (
    'FixtureIdentity',
    'DiscoveryObservation',
    'SportteryPoolObservation',
    'SnapshotBatch',
    'MarketSnapshot',
    'BookmakerMarketRow',
    'MarketNormalization',
    'SnapshotEligibilityAssessment',
    'HandicapIndexRow',
    'SportteryScopeEvidence',
    'SportteryInventoryBatch',
    'SportteryFixtureLink',
    'SportteryResultObservation',
    'ResultCandidate',
    'VerifiedResult',
    'QualityEvent'
);
