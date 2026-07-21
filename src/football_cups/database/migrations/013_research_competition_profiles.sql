ALTER TABLE research.shadow_predictions
    ADD COLUMN competition_id text,
    ADD COLUMN competition_name text,
    ADD COLUMN competition_type text,
    ADD COLUMN market_evidence_tier text,
    ADD COLUMN evaluation_group text,
    ADD COLUMN classification_status text,
    ADD COLUMN registry_version text,
    ADD COLUMN policy_version text,
    ADD COLUMN registry_file_sha256 text,
    ADD COLUMN registry_canonical_sha256 text,
    ADD COLUMN direction_strength double precision,
    ADD COLUMN bookmaker_dispersion double precision,
    ADD COLUMN raw_confidence_label text,
    ADD COLUMN competition_confidence_cap text,
    ADD COLUMN confidence_label text,
    ADD COLUMN confidence_reasons jsonb,
    ADD COLUMN risk_flags jsonb,
    ADD COLUMN identity_record_id text,
    ADD COLUMN identity_observed_at timestamptz,
    ADD COLUMN automatic_verified_fixture_count integer,
    ADD COLUMN evaluation_span_days double precision,
    ADD COLUMN review_eligible boolean;

ALTER TABLE research.shadow_predictions
    ADD CONSTRAINT research_shadow_competition_type_check CHECK (
        competition_type IS NULL OR competition_type IN (
            'domestic_league',
            'lower_evidence_league',
            'continental_competition',
            'international_competition',
            'unknown'
        )
    ),
    ADD CONSTRAINT research_shadow_tier_check CHECK (
        market_evidence_tier IS NULL OR market_evidence_tier IN ('A', 'B', 'C', 'D')
    ),
    ADD CONSTRAINT research_shadow_classification_check CHECK (
        classification_status IS NULL OR classification_status IN ('provisional', 'reviewed')
    ),
    ADD CONSTRAINT research_shadow_raw_confidence_check CHECK (
        raw_confidence_label IS NULL OR raw_confidence_label IN (
            'observation_only', 'low', 'medium', 'high'
        )
    ),
    ADD CONSTRAINT research_shadow_cap_check CHECK (
        competition_confidence_cap IS NULL OR competition_confidence_cap IN (
            'observation_only', 'low', 'medium', 'high'
        )
    ),
    ADD CONSTRAINT research_shadow_confidence_check CHECK (
        confidence_label IS NULL OR confidence_label IN (
            'observation_only', 'low', 'medium', 'high'
        )
    ),
    ADD CONSTRAINT research_shadow_direction_strength_check CHECK (
        direction_strength IS NULL OR direction_strength BETWEEN 0 AND 1
    ),
    ADD CONSTRAINT research_shadow_dispersion_check CHECK (
        bookmaker_dispersion IS NULL OR bookmaker_dispersion BETWEEN 0 AND 1
    ),
    ADD CONSTRAINT research_shadow_evaluation_count_check CHECK (
        automatic_verified_fixture_count IS NULL OR automatic_verified_fixture_count >= 0
    ),
    ADD CONSTRAINT research_shadow_evaluation_span_check CHECK (
        evaluation_span_days IS NULL OR evaluation_span_days >= 0
    );

CREATE INDEX research_shadow_predictions_competition_idx
    ON research.shadow_predictions(competition_id, target, prediction_cutoff);
