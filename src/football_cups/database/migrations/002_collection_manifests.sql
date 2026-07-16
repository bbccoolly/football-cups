CREATE TABLE football.collection_manifests (
    source_file text PRIMARY KEY,
    sha256 text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    schema_version smallint NOT NULL,
    record_type text NOT NULL,
    run_id text,
    status text,
    fixture_id text,
    started_at timestamptz,
    finished_at timestamptz,
    payload jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX collection_manifests_type_time_idx
    ON football.collection_manifests (record_type, finished_at DESC);
CREATE INDEX collection_manifests_fixture_idx
    ON football.collection_manifests (fixture_id, finished_at DESC);
