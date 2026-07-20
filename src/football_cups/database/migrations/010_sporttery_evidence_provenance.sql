ALTER TABLE football.sporttery_inventory_batches
    ADD COLUMN failure_reason text;

ALTER TABLE football.sporttery_fixture_links
    ADD COLUMN source_fixture_identity_record_id text
        REFERENCES football.records(record_id);

CREATE INDEX sporttery_fixture_links_identity_idx
    ON football.sporttery_fixture_links (source_fixture_identity_record_id);
