DROP VIEW football.strict_fixture_results_by_cutoff;
DROP VIEW football.current_verified_results;

ALTER TABLE football.verified_results
    ADD COLUMN evidence_level text,
    ADD COLUMN attestor_id text,
    ADD COLUMN attestation_note text;

CREATE VIEW football.current_verified_results AS
WITH eligible AS (
    SELECT result.*
    FROM football.verified_results AS result
    LEFT JOIN football.result_candidates AS candidate
      ON candidate.record_id = result.candidate_id
    WHERE result.verification_status = 'accepted'
      AND (
          result.verification_method NOT IN (
              'manual',
              'manual-import',
              'project-owner-manual-declaration'
          )
          OR (
              result.verification_method = 'project-owner-manual-declaration'
              AND result.evidence_level = 'self_attestation'
              AND result.attestor_id = 'project-owner'
              AND result.candidate_id IS NOT NULL
              AND candidate.fixture_id = result.fixture_id
              AND candidate.home_goals = result.home_goals
              AND candidate.away_goals = result.away_goals
              AND candidate.analysis_consistency = 'passed'
          )
      )
),
score_counts AS (
    SELECT fixture_id, count(DISTINCT (home_goals, away_goals)) AS score_count
    FROM eligible
    GROUP BY fixture_id
),
accepted AS (
    SELECT result.*, score_counts.score_count
    FROM eligible AS result
    JOIN score_counts USING (fixture_id)
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
