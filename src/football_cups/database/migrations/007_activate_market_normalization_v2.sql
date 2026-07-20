CREATE VIEW football.current_bookmaker_market_rows AS
SELECT * FROM football.current_bookmaker_market_rows_v2;

CREATE VIEW football.current_model_eligible_snapshot_batches AS
SELECT DISTINCT ON (fixture_id, target) *
FROM football.model_eligible_snapshot_batches_v2
WHERE model_strict_eligible = true
ORDER BY fixture_id, target, core_observed_at DESC, completed_at DESC, record_id DESC;

DROP FUNCTION football.market_rows_as_of(text, timestamptz);

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
    current jsonb,
    parser_version text,
    normalization_version smallint,
    normalization_record_id text
)
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT
        row.record_id,
        row.fixture_id,
        row.market,
        row.target,
        row.observed_at,
        row.corrected_at,
        row.source_event_time,
        row.source_bookmaker_id,
        row.source_bookmaker_name,
        row.row_role,
        row.opening,
        row.current,
        row.parser_version,
        row.normalization_version,
        row.normalization_record_id
    FROM football.current_bookmaker_market_rows AS row
    WHERE row.fixture_id = p_fixture_id
      AND row.observed_at <= p_prediction_cutoff
      AND (row.corrected_at IS NULL OR row.corrected_at <= p_prediction_cutoff)
    ORDER BY row.observed_at, row.market, row.source_bookmaker_name, row.record_id
$$;

DROP VIEW football.strict_fixture_results_by_cutoff;

CREATE VIEW football.strict_fixture_results_by_cutoff AS
SELECT DISTINCT
    batch.fixture_id,
    batch.target,
    result.record_id AS verified_result_id,
    result.home_goals,
    result.away_goals,
    result.confirmed_at
FROM football.current_model_eligible_snapshot_batches AS batch
JOIN football.current_verified_results AS result
  ON result.fixture_id = batch.fixture_id;
