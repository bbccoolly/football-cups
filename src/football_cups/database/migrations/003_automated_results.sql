ALTER TABLE football.result_candidates
    ADD COLUMN kickoff_at timestamptz,
    ADD COLUMN status_code text,
    ADD COLUMN live_page_sha256 text,
    ADD COLUMN analysis_consistency text;

CREATE INDEX result_candidates_fixture_score_idx
    ON football.result_candidates (fixture_id, home_goals, away_goals, observed_at DESC);

ALTER TABLE football.verified_results
    ADD COLUMN verification_status text NOT NULL DEFAULT 'accepted'
        CHECK (verification_status IN ('accepted', 'superseded', 'disputed')),
    ADD COLUMN supersedes_record_id text,
    ADD COLUMN correction_reason text;

CREATE INDEX verified_results_fixture_score_idx
    ON football.verified_results (fixture_id, home_goals, away_goals, confirmed_at DESC);

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
);

CREATE VIEW football.strict_fixture_results_by_cutoff AS
SELECT DISTINCT
    batch.fixture_id,
    batch.target,
    result.record_id AS verified_result_id,
    result.home_goals,
    result.away_goals,
    result.confirmed_at
FROM football.snapshot_batches AS batch
JOIN football.current_verified_results AS result
  ON result.fixture_id = batch.fixture_id
WHERE batch.strict_eligible = true;
