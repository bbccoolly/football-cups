ALTER TABLE football.bookmaker_market_rows
    ADD COLUMN parser_version text,
    ADD COLUMN normalization_version smallint,
    ADD COLUMN normalized_at timestamptz,
    ADD COLUMN source_snapshot_record_id text REFERENCES football.market_snapshots(record_id),
    ADD COLUMN normalization_record_id text,
    ADD COLUMN source_page_sha256 text,
    ADD COLUMN source_workbook_sha256 text,
    ADD COLUMN source_page_observed_at timestamptz,
    ADD COLUMN snapshot_observed_at timestamptz,
    ADD COLUMN source_row_index integer,
    ADD COLUMN line_movement jsonb,
    ADD COLUMN reprocessed boolean NOT NULL DEFAULT false,
    ADD COLUMN event_origin text NOT NULL DEFAULT 'live'
        CHECK (event_origin IN ('live', 'reprocess'));

CREATE TABLE football.market_normalizations (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    snapshot_record_id text NOT NULL REFERENCES football.market_snapshots(record_id),
    market text NOT NULL,
    target text NOT NULL,
    normalization_version smallint NOT NULL CHECK (normalization_version > 0),
    parser_version text NOT NULL,
    normalized_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'rejected')),
    valid_bookmaker_rows integer NOT NULL CHECK (valid_bookmaker_rows >= 0),
    line_parse_failure_count integer NOT NULL CHECK (line_parse_failure_count >= 0),
    source_page_sha256 text,
    source_workbook_sha256 text,
    source_page_observed_at timestamptz,
    snapshot_observed_at timestamptz NOT NULL,
    quality_reasons jsonb NOT NULL,
    decoding jsonb NOT NULL,
    reprocessed boolean NOT NULL,
    event_origin text NOT NULL CHECK (event_origin IN ('live', 'reprocess')),
    UNIQUE (snapshot_record_id, normalization_version)
);

CREATE INDEX market_normalizations_current_idx
    ON football.market_normalizations (snapshot_record_id, normalization_version DESC, normalized_at DESC);

ALTER TABLE football.bookmaker_market_rows
    ADD CONSTRAINT bookmaker_market_rows_normalization_fk
    FOREIGN KEY (normalization_record_id)
    REFERENCES football.market_normalizations(record_id);

CREATE INDEX bookmaker_rows_normalization_idx
    ON football.bookmaker_market_rows (normalization_record_id, source_row_index);

CREATE TABLE football.snapshot_eligibility_assessments (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    snapshot_batch_record_id text NOT NULL REFERENCES football.snapshot_batches(record_id),
    target text NOT NULL,
    assessment_version smallint NOT NULL CHECK (assessment_version > 0),
    assessed_at timestamptz NOT NULL,
    collection_eligible boolean NOT NULL,
    data_complete boolean NOT NULL,
    model_strict_eligible boolean NOT NULL,
    market_stats jsonb NOT NULL,
    ineligibility_reasons jsonb NOT NULL,
    event_origin text NOT NULL CHECK (event_origin IN ('live', 'reprocess')),
    UNIQUE (snapshot_batch_record_id, assessment_version)
);

CREATE INDEX snapshot_assessments_current_idx
    ON football.snapshot_eligibility_assessments
    (snapshot_batch_record_id, assessment_version DESC, assessed_at DESC);

CREATE TABLE football.handicap_index_rows (
    record_id text PRIMARY KEY REFERENCES football.records(record_id) ON DELETE CASCADE,
    fixture_id text NOT NULL,
    target text NOT NULL,
    observed_at timestamptz NOT NULL,
    source_bookmaker_name text,
    handicap_line numeric(18, 8),
    home_index numeric(18, 8),
    draw_index numeric(18, 8),
    away_index numeric(18, 8),
    home_probability numeric(18, 8),
    draw_probability numeric(18, 8),
    away_probability numeric(18, 8),
    return_rate numeric(18, 8),
    home_kelly numeric(18, 8),
    draw_kelly numeric(18, 8),
    away_kelly numeric(18, 8),
    raw_cells jsonb NOT NULL,
    parser_version text NOT NULL,
    normalization_version smallint NOT NULL,
    normalized_at timestamptz NOT NULL,
    source_snapshot_record_id text NOT NULL REFERENCES football.market_snapshots(record_id),
    normalization_record_id text NOT NULL REFERENCES football.market_normalizations(record_id),
    source_page_sha256 text,
    source_page_observed_at timestamptz,
    snapshot_observed_at timestamptz NOT NULL,
    source_row_index integer NOT NULL,
    reprocessed boolean NOT NULL,
    event_origin text NOT NULL CHECK (event_origin IN ('live', 'reprocess'))
);

CREATE INDEX handicap_index_snapshot_idx
    ON football.handicap_index_rows (source_snapshot_record_id, normalization_version, source_row_index);

CREATE VIEW football.current_bookmaker_market_rows_v2 AS
WITH current_normalization AS (
    SELECT DISTINCT ON (snapshot_record_id)
        record_id,
        snapshot_record_id,
        normalization_version,
        parser_version
    FROM football.market_normalizations
    WHERE status = 'accepted'
    ORDER BY snapshot_record_id, normalization_version DESC, normalized_at DESC, record_id DESC
)
SELECT row.*
FROM football.bookmaker_market_rows AS row
JOIN current_normalization AS normalization
  ON normalization.record_id = row.normalization_record_id;

CREATE VIEW football.model_eligible_snapshot_batches_v2 AS
WITH current_assessment AS (
    SELECT DISTINCT ON (snapshot_batch_record_id) *
    FROM football.snapshot_eligibility_assessments
    ORDER BY snapshot_batch_record_id, assessment_version DESC, assessed_at DESC, record_id DESC
)
SELECT
    batch.*,
    assessment.assessment_version,
    assessment.assessed_at,
    assessment.data_complete,
    assessment.model_strict_eligible,
    assessment.market_stats,
    assessment.ineligibility_reasons,
    GREATEST(
        NULLIF(batch.market_results->'ouzhi'->>'observed_at', '')::timestamptz,
        NULLIF(batch.market_results->'yazhi'->>'observed_at', '')::timestamptz,
        NULLIF(batch.market_results->'daxiao'->>'observed_at', '')::timestamptz
    ) AS core_observed_at
FROM football.snapshot_batches AS batch
JOIN current_assessment AS assessment
  ON assessment.snapshot_batch_record_id = batch.record_id;

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
    'ResultCandidate',
    'VerifiedResult',
    'QualityEvent'
);
