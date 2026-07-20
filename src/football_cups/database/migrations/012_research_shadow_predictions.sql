DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint AS con
        JOIN pg_class AS cls ON cls.oid = con.conrelid
        JOIN pg_namespace AS nsp ON nsp.oid = cls.relnamespace
        WHERE nsp.nspname = 'research'
          AND cls.relname = 'records'
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%payload%'
    LOOP
        EXECUTE format('ALTER TABLE research.records DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

UPDATE research.records
SET payload = jsonb_set(payload, '{research_kind}', '"historical"', true)
WHERE payload ? 'research_kind' = false;

ALTER TABLE research.records
    ADD CONSTRAINT research_records_flags_v2 CHECK (
        (payload->>'research_only')::boolean = true
        AND (payload->>'strict_backtest_eligible')::boolean = false
        AND (payload->>'cutoff_eligible')::boolean = false
        AND (
            (
                coalesce(payload->>'research_kind', 'historical') IN ('historical', 'model_artifact')
                AND (payload->>'backfill')::boolean = true
            )
            OR (
                payload->>'research_kind' = 'shadow_event'
                AND (payload->>'backfill')::boolean = false
            )
        )
    );

CREATE TABLE research.model_datasets (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    model_key text NOT NULL,
    dataset_hash text NOT NULL,
    training_before_date date NOT NULL,
    created_at timestamptz NOT NULL,
    source_record_count bigint NOT NULL CHECK (source_record_count >= 0),
    fixture_count bigint NOT NULL CHECK (fixture_count >= 0),
    feature_schema text NOT NULL,
    training_fixture_ids jsonb NOT NULL,
    evaluation_fixture_ids jsonb NOT NULL,
    payload jsonb NOT NULL,
    UNIQUE (model_key, dataset_hash)
);

CREATE TABLE research.model_versions (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    model_key text NOT NULL,
    model_version text NOT NULL,
    dataset_record_id text NOT NULL REFERENCES research.model_datasets(record_id),
    trained_at timestamptz NOT NULL,
    algorithm text NOT NULL,
    artifact_json jsonb NOT NULL,
    metrics jsonb NOT NULL,
    UNIQUE (model_key, model_version)
);

CREATE TABLE research.model_activations (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    channel text NOT NULL,
    model_key text NOT NULL,
    model_version text NOT NULL,
    model_record_id text NOT NULL REFERENCES research.model_versions(record_id),
    activated_at timestamptz NOT NULL,
    active_from timestamptz NOT NULL,
    active_until timestamptz,
    status text NOT NULL CHECK (status IN ('active', 'inactive')),
    notes text
);

CREATE INDEX research_model_activations_current_idx
    ON research.model_activations(channel, status, active_from DESC, activated_at DESC);

CREATE VIEW research.current_model_activations AS
SELECT DISTINCT ON (channel) activation.*
FROM research.model_activations AS activation
WHERE activation.status = 'active'
  AND activation.active_from <= clock_timestamp()
  AND (activation.active_until IS NULL OR activation.active_until > clock_timestamp())
ORDER BY channel, active_from DESC, activated_at DESC, record_id DESC;

CREATE TABLE research.shadow_predictions (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    channel text NOT NULL,
    fixture_id text NOT NULL,
    target text NOT NULL,
    prediction_cutoff timestamptz NOT NULL,
    published_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('published', 'abstained')),
    model_key text,
    model_version text,
    activation_record_id text REFERENCES research.model_activations(record_id),
    selected_batch_record_id text,
    source_snapshot_record_id text,
    market_observed_at timestamptz,
    bookmaker_count integer CHECK (bookmaker_count IS NULL OR bookmaker_count >= 0),
    probabilities jsonb NOT NULL,
    features jsonb NOT NULL,
    abstention_reason text,
    UNIQUE (channel, fixture_id, target, prediction_cutoff)
);

CREATE INDEX research_shadow_predictions_fixture_idx
    ON research.shadow_predictions(fixture_id, target, prediction_cutoff);

CREATE TABLE research.retrospective_evaluations (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    model_key text,
    model_version text,
    evaluated_at timestamptz NOT NULL,
    evaluation_kind text NOT NULL,
    dataset_hash text,
    metrics jsonb NOT NULL,
    payload jsonb NOT NULL
);

CREATE TABLE research.shadow_evaluations (
    record_id text PRIMARY KEY REFERENCES research.records(record_id) ON DELETE CASCADE,
    model_key text,
    model_version text,
    evaluated_at timestamptz NOT NULL,
    evaluation_kind text NOT NULL,
    dataset_hash text,
    metrics jsonb NOT NULL,
    payload jsonb NOT NULL
);
