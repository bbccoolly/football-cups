CREATE TABLE research.europe_guardrail_assessments (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    channel text NOT NULL,
    fixture_id text NOT NULL,
    competition_id text NOT NULL CHECK (competition_id IN ('63', '101')),
    target text NOT NULL CHECK (target IN ('T-24h', 'T-6h', 'T-60m', 'T-10m')),
    prediction_cutoff timestamptz NOT NULL,
    assessed_at timestamptz NOT NULL,
    policy_version text NOT NULL,
    policy_revision integer NOT NULL CHECK (policy_revision > 0),
    policy_status text NOT NULL CHECK (policy_status = 'shadow'),
    policy_snapshot jsonb NOT NULL,
    policy_file_sha256 text NOT NULL CHECK (policy_file_sha256 ~ '^[0-9a-f]{64}$'),
    policy_canonical_sha256 text NOT NULL CHECK (policy_canonical_sha256 ~ '^[0-9a-f]{64}$'),
    git_commit text,
    relevant_source_tree_sha256 text NOT NULL CHECK (relevant_source_tree_sha256 ~ '^[0-9a-f]{64}$'),
    relevant_dirty_paths jsonb NOT NULL,
    identity_record_id text,
    selected_batch_record_id text,
    snapshot_record_ids jsonb NOT NULL,
    source_row_record_ids jsonb NOT NULL,
    source_hashes jsonb NOT NULL,
    base_probabilities jsonb NOT NULL,
    base_direction text CHECK (base_direction IN ('home', 'draw', 'away')),
    institution_details jsonb NOT NULL,
    trajectory jsonb NOT NULL,
    raw_features jsonb NOT NULL,
    rule_evaluations jsonb NOT NULL,
    rule_flags jsonb NOT NULL,
    proposed_action text NOT NULL CHECK (proposed_action IN ('keep', 'caution', 'downgrade', 'abstain')),
    proposed_confidence_cap text CHECK (
        proposed_confidence_cap IS NULL OR proposed_confidence_cap IN ('observation_only', 'low', 'medium', 'high')
    ),
    reasons jsonb NOT NULL,
    audit_status text NOT NULL CHECK (audit_status IN ('eligible', 'unavailable')),
    payload jsonb NOT NULL,
    UNIQUE (channel, fixture_id, target, prediction_cutoff, policy_version)
);

CREATE INDEX research_europe_guardrail_fixture_idx
    ON research.europe_guardrail_assessments(fixture_id, target, prediction_cutoff);

CREATE VIEW research.current_europe_guardrail_assessments AS
SELECT DISTINCT ON (channel, fixture_id, target, prediction_cutoff)
    assessment.*
FROM research.europe_guardrail_assessments AS assessment
ORDER BY channel, fixture_id, target, prediction_cutoff,
         policy_revision DESC, assessed_at DESC, record_id DESC;
