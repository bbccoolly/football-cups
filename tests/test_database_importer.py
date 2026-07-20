from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone

import pytest

from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import apply_migrations, connect
from football_cups.database.importer import (
    AppendOnlyViolation,
    ImportAlreadyRunning,
    ImportContractError,
    _last_record_id,
    bookmaker_role,
    import_jsonl_tree,
    import_lock,
    import_manifests,
    insert_record,
    parse_decimal,
    parse_time,
    validate_record,
)
from football_cups.database.queries import as_of_audit, market_rows_as_of


class FakeCursor:
    rowcount = 1


class RecordingConnection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, sql, params=None):
        values = tuple(params or ())
        assert sql.count("%s") == len(values)
        self.calls.append((sql, values))
        return FakeCursor()


def base_record(record_type: str, **values):
    return {
        "record_id": f"id-{record_type}",
        "record_type": record_type,
        "schema_version": 1,
        "fixture_id": "123",
        **values,
    }


@pytest.mark.parametrize(
    "record",
    [
        base_record("FixtureIdentity", observed_at="2026-07-16T01:00:00Z"),
        base_record(
            "DiscoveryObservation",
            observed_at="2026-07-16T01:00:00Z",
            source_name="default",
            source_url="https://example.test/discovery",
        ),
        base_record(
            "SportteryPoolObservation",
            observed_at="2026-07-16T01:00:00Z",
            source_name="default",
            source_url="https://example.test/discovery",
            pool_type="nspf",
            option_value="3",
            sp_raw="1.50",
        ),
        base_record(
            "SnapshotBatch",
            target="T-60m",
            core_market_complete=True,
            strict_eligible=True,
            market_results={},
        ),
        base_record(
            "MarketSnapshot",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T01:00:00Z",
        ),
        base_record(
            "BookmakerMarketRow",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T01:00:00Z",
            source_bookmaker_name="Bookmaker",
            opening={"home": {"decimal": "2.0"}},
            current={"home": {"decimal": "1.9"}},
        ),
        base_record(
            "MarketNormalization",
            snapshot_record_id="snapshot-id",
            market="ouzhi",
            target="T-60m",
            normalization_version=2,
            parser_version="500-market-v2",
            normalized_at="2026-07-16T01:00:00Z",
            status="accepted",
            valid_bookmaker_rows=3,
            line_parse_failure_count=0,
            snapshot_observed_at="2026-07-16T01:00:00Z",
            quality_reasons=[],
            decoding={},
            reprocessed=False,
            event_origin="live",
        ),
        base_record(
            "SnapshotEligibilityAssessment",
            snapshot_batch_record_id="batch-id",
            target="T-60m",
            assessment_version=2,
            assessed_at="2026-07-16T01:00:00Z",
            collection_eligible=True,
            data_complete=True,
            model_strict_eligible=True,
            market_stats={},
            ineligibility_reasons=[],
            event_origin="live",
        ),
        base_record(
            "HandicapIndexRow",
            target="T-60m",
            observed_at="2026-07-16T01:00:00Z",
            handicap_line="-1",
            home_index="2.0",
            draw_index="3.0",
            away_index="4.0",
            raw_cells=[],
            parser_version="500-market-v2",
            normalization_version=2,
            normalized_at="2026-07-16T01:00:00Z",
            source_snapshot_record_id="snapshot-id",
            normalization_record_id="normalization-id",
            snapshot_observed_at="2026-07-16T01:00:00Z",
            source_row_index=0,
            reprocessed=False,
            event_origin="live",
        ),
        base_record(
            "ResultCandidate",
            observed_at="2026-07-16T01:00:00Z",
            kickoff_at="2026-07-15T22:00:00Z",
            home_goals=1,
            away_goals=0,
            scope="candidate",
            status_code="4",
            live_page_sha256="live",
            analysis_consistency="passed",
            source_urls=[],
        ),
        base_record(
            "VerifiedResult",
            confirmed_at="2026-07-16T01:00:00Z",
            home_goals=1,
            away_goals=0,
            scope="90-minutes-including-stoppage",
            source_url="https://example.test/result",
            verification_method="manual",
            verification_status="accepted",
        ),
        base_record(
            "QualityEvent",
            occurred_at="2026-07-16T01:00:00Z",
            event_type="test",
            status="success",
            details={},
        ),
    ],
)
def test_typed_insert_parameter_counts(record) -> None:
    connection = RecordingConnection()
    assert insert_record(connection, record, source_file="normalized/test.jsonl", source_line=1)
    assert len(connection.calls) == 2


def test_import_contract_helpers() -> None:
    assert parse_decimal("1.25") == parse_decimal({"decimal": "1.25"})
    assert bookmaker_role("最高值") == "summary"
    assert bookmaker_role("竞彩官方") == "official"
    assert bookmaker_role("Bookmaker") == "bookmaker"
    assert parse_time("2026-07-16T01:00:00Z") == datetime(
        2026, 7, 16, 1, tzinfo=timezone.utc
    )
    with pytest.raises(ImportContractError):
        parse_time("2026-07-16T01:00:00")
    with pytest.raises(ImportContractError):
        validate_record({}, source_file="test.jsonl", source_line=1)


def test_checkpoint_tail_validation() -> None:
    first = {"record_id": "one"}
    second = {"record_id": "two"}
    content = (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode()
    assert _last_record_id(io.BytesIO(content), len(content)) == "two"
    with pytest.raises(AppendOnlyViolation):
        _last_record_id(io.BytesIO(content[:-1]), len(content) - 1)


def test_postgres_replay_and_as_of_integration(tmp_path) -> None:
    database_url = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE_URL")
    local_test = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE") == "1"
    if not database_url and not local_test:
        pytest.skip("PostgreSQL integration test is not configured")
    config = DatabaseConfig(tmp_path, tmp_path / "data" / "500", database_url)
    normalized = config.normalized_dir / "2026" / "07" / "16"
    normalized.mkdir(parents=True)
    manifest_dir = config.data_dir / "manifests" / "2026" / "07" / "16"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "test-discovery.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "DiscoveryRun",
                "run_id": "test-run",
                "status": "full",
                "started_at": "2026-07-16T09:59:00Z",
                "finished_at": "2026-07-16T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    rows = [
        base_record(
            "MarketSnapshot",
            record_id="snapshot-before",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T10:00:00Z",
            parser_version="500-market-v2",
        ),
        base_record(
            "MarketNormalization",
            record_id="normalization-before",
            snapshot_record_id="snapshot-before",
            market="ouzhi",
            target="T-60m",
            normalization_version=2,
            parser_version="500-market-v2",
            normalized_at="2026-07-16T15:00:00Z",
            status="accepted",
            valid_bookmaker_rows=3,
            line_parse_failure_count=0,
            snapshot_observed_at="2026-07-16T10:00:00Z",
            quality_reasons=[],
            decoding={},
            reprocessed=True,
            event_origin="reprocess",
        ),
        base_record(
            "BookmakerMarketRow",
            record_id="row-before",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T10:00:00Z",
            source_bookmaker_name="Before",
            opening={"home": {"decimal": "2.0"}},
            current={"home": {"decimal": "1.9"}},
            parser_version="500-market-v2",
            normalization_version=2,
            normalized_at="2026-07-16T15:00:00Z",
            source_snapshot_record_id="snapshot-before",
            normalization_record_id="normalization-before",
            snapshot_observed_at="2026-07-16T10:00:00Z",
            source_row_index=0,
            reprocessed=True,
            event_origin="reprocess",
        ),
        base_record(
            "MarketSnapshot",
            record_id="snapshot-after",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T12:00:00Z",
            parser_version="500-market-v2",
        ),
        base_record(
            "MarketNormalization",
            record_id="normalization-after",
            snapshot_record_id="snapshot-after",
            market="ouzhi",
            target="T-60m",
            normalization_version=2,
            parser_version="500-market-v2",
            normalized_at="2026-07-16T15:00:00Z",
            status="accepted",
            valid_bookmaker_rows=3,
            line_parse_failure_count=0,
            snapshot_observed_at="2026-07-16T12:00:00Z",
            quality_reasons=[],
            decoding={},
            reprocessed=True,
            event_origin="reprocess",
        ),
        base_record(
            "BookmakerMarketRow",
            record_id="row-after",
            market="ouzhi",
            target="T-60m",
            observed_at="2026-07-16T12:00:00Z",
            source_bookmaker_name="After",
            opening={"home": {"decimal": "2.0"}},
            current={"home": {"decimal": "1.8"}},
            parser_version="500-market-v2",
            normalization_version=2,
            normalized_at="2026-07-16T15:00:00Z",
            source_snapshot_record_id="snapshot-after",
            normalization_record_id="normalization-after",
            snapshot_observed_at="2026-07-16T12:00:00Z",
            source_row_index=0,
            reprocessed=True,
            event_origin="reprocess",
        ),
        base_record(
            "ResultCandidate",
            record_id="result-candidate",
            observed_at="2026-07-16T13:00:00Z",
            kickoff_at="2026-07-16T10:00:00Z",
            home_goals=2,
            away_goals=1,
            scope="candidate-full-time-scope-not-yet-confirmed",
            status_code="4",
            live_page_sha256="live-page",
            analysis_consistency="passed",
            source_urls=["https://example.test/result"],
        ),
        base_record(
            "VerifiedResult",
            record_id="verified-result",
            confirmed_at="2026-07-16T14:00:00Z",
            home_goals=2,
            away_goals=1,
            scope="90-minutes-including-stoppage",
            source_url="https://example.test/result",
            verification_method="manual-import",
            verification_status="accepted",
        ),
    ]
    path = normalized / "bookmaker_market_rows.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    with connect(config, autocommit=True) as raw:
        if not raw.info.dbname.endswith("_test"):
            pytest.fail("integration test database name must end with _test")
        raw.execute("DROP SCHEMA IF EXISTS research CASCADE")
        raw.execute("DROP SCHEMA IF EXISTS football CASCADE")

    with connect(config) as connection:
        assert apply_migrations(connection, target_version="006") == [
            "001", "002", "003", "004", "005", "006"
        ]
        with connect(config) as competing:
            with import_lock(connection):
                with pytest.raises(ImportAlreadyRunning):
                    with import_lock(competing):
                        pass
        manifests = import_manifests(connection, config.data_dir)
        assert manifests.manifests_inserted == 1
        manifests_again = import_manifests(connection, config.data_dir)
        assert manifests_again.manifests_existing == 1
        original_manifest = manifest_path.read_text(encoding="utf-8")
        changed_manifest = json.loads(original_manifest)
        changed_manifest["status"] = "changed"
        manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
        with pytest.raises(AppendOnlyViolation):
            import_manifests(connection, config.data_dir)
        manifest_path.write_text(original_manifest, encoding="utf-8")
        first = import_jsonl_tree(connection, config.normalized_dir)
        assert first.records_inserted == 8
        assert apply_migrations(connection, target_version="007") == ["007"]
        second = import_jsonl_tree(connection, config.normalized_dir)
        assert second.records_inserted == 0
        assert second.lines_seen == 0

        connection.execute("DELETE FROM football.import_checkpoints")
        connection.commit()
        replay = import_jsonl_tree(connection, config.normalized_dir)
        assert replay.records_existing == 8
        typed_results = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM football.result_candidates) AS candidates,
                (SELECT count(*) FROM football.verified_results) AS verified
            """
        ).fetchone()
        assert typed_results == {"candidates": 1, "verified": 1}
        current_verified = connection.execute(
            "SELECT count(*) AS count FROM football.current_verified_results"
        ).fetchone()
        assert current_verified["count"] == 1
        assert insert_record(
            connection,
            base_record(
                "QualityEvent",
                record_id="result-conflict",
                occurred_at="2026-07-16T15:00:00Z",
                event_type="result_conflict",
                status="failure",
                details={"reason": "source disagreement"},
            ),
            source_file="normalized/test-conflict.jsonl",
            source_line=1,
        )
        connection.commit()
        current_after_conflict = connection.execute(
            "SELECT count(*) AS count FROM football.current_verified_results"
        ).fetchone()
        assert current_after_conflict["count"] == 0

        checkpoint_before = connection.execute(
            "SELECT byte_offset, line_number FROM football.import_checkpoints"
        ).fetchone()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    base_record(
                        "QualityEvent",
                        record_id="rolled-back-record",
                        occurred_at="2026-07-16T12:30:00Z",
                        event_type="rollback-test",
                        status="success",
                        details={},
                    )
                )
                + "\n"
            )
            handle.write(json.dumps({"schema_version": 999}) + "\n")
        with pytest.raises(ImportContractError):
            import_jsonl_tree(connection, config.normalized_dir)
        checkpoint_after = connection.execute(
            "SELECT byte_offset, line_number FROM football.import_checkpoints"
        ).fetchone()
        assert checkpoint_after == checkpoint_before
        rolled_back = connection.execute(
            "SELECT count(*) AS count FROM football.records WHERE record_id='rolled-back-record'"
        ).fetchone()
        assert rolled_back["count"] == 0

        with path.open("r+b") as handle:
            handle.truncate(checkpoint_before["byte_offset"])
            handle.seek(0, 2)
            handle.write(b'{"record_id":"partial')
        partial = import_jsonl_tree(connection, config.normalized_dir)
        assert partial.records_inserted == 0
        partial_checkpoint = connection.execute(
            "SELECT byte_offset, line_number FROM football.import_checkpoints"
        ).fetchone()
        assert partial_checkpoint == checkpoint_before

        cutoff = parse_time("2026-07-16T11:00:00Z")
        result = market_rows_as_of(
            connection,
            fixture_id="123",
            prediction_cutoff=cutoff,
            limit=100,
        )
        assert [row["record_id"] for row in result] == ["row-before"]
        audit = as_of_audit(
            connection,
            fixture_id="123",
            prediction_cutoff=cutoff,
        )
        assert audit["observed_after_cutoff"] == 0
        assert audit["corrected_after_cutoff"] == 0
