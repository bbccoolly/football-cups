DROP VIEW football.strict_fixture_results_by_cutoff;
DROP VIEW football.current_model_eligible_snapshot_batches;
DROP VIEW football.current_verified_results;

CREATE VIEW football.current_invalid_fixtures AS
SELECT DISTINCT fixture_id
FROM football.quality_events
WHERE fixture_id IS NOT NULL
  AND event_type IN ('fixture_invalidated', 'result_cancelled')
  AND status = 'excluded';

CREATE VIEW football.current_verified_results AS
WITH score_counts AS (
    SELECT fixture_id, count(DISTINCT (home_goals, away_goals)) AS score_count
    FROM football.verified_results
    WHERE verification_status = 'accepted'
    GROUP BY fixture_id
),
accepted AS (
    SELECT result.*, score_counts.score_count
    FROM football.verified_results AS result
    JOIN score_counts USING (fixture_id)
    WHERE result.verification_status = 'accepted'
),
current_rows AS (
    SELECT DISTINCT ON (fixture_id) *
    FROM accepted
    WHERE score_count = 1
      AND NOT EXISTS (
          SELECT 1
          FROM football.verified_results AS replacement
          WHERE replacement.supersedes_record_id = accepted.record_id
            AND replacement.verification_status = 'accepted'
      )
    ORDER BY fixture_id, confirmed_at DESC, record_id DESC
)
SELECT current_rows.*
FROM current_rows
WHERE NOT EXISTS (
    SELECT 1
    FROM football.quality_events AS event
    WHERE event.fixture_id = current_rows.fixture_id
      AND event.event_type IN ('result_conflict', 'verified_result_conflict')
      AND event.status = 'failure'
)
AND NOT EXISTS (
    SELECT 1
    FROM football.current_invalid_fixtures AS invalid
    WHERE invalid.fixture_id = current_rows.fixture_id
);

CREATE VIEW football.current_model_eligible_snapshot_batches AS
SELECT DISTINCT ON (batch.fixture_id, batch.target) batch.*
FROM football.model_eligible_snapshot_batches_v2 AS batch
WHERE batch.model_strict_eligible = true
  AND NOT EXISTS (
      SELECT 1
      FROM football.current_invalid_fixtures AS invalid
      WHERE invalid.fixture_id = batch.fixture_id
  )
ORDER BY batch.fixture_id, batch.target, batch.core_observed_at DESC,
         batch.completed_at DESC, batch.record_id DESC;

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
