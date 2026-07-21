from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook

from football_cups.database.config import DatabaseConfig
from football_cups.database.connection import apply_migrations, connect
from football_cups.research import RESEARCH_FLAGS
from football_cups.research.config import ResearchConfig
from football_cups.research.database import import_research_files
from football_cups.research.http import AccessPolicyError, BudgetExceeded, ResearchHttpClient
from football_cups.research.modeling import (
    build_closing_1x2_dataset,
    train_devig_consensus_model,
    write_model_dataset,
)
from football_cups.research.normalize import (
    ResearchIntegrityError,
    import_k1_dataset,
    normalize_football_data_csv,
    normalize_world_cup_xlsx,
)
from football_cups.research.registry import ASSETS, ResearchAsset
from football_cups.research.reporting import _competition_label, _metric_rows, load_records
from football_cups.research.state import ResearchState
from football_cups.research.storage import ResearchStore


def config(tmp_path: Path) -> ResearchConfig:
    return ResearchConfig(tmp_path, tmp_path / "data" / "research")


def source_asset(asset: ResearchAsset, digest: str = "a" * 64) -> dict:
    return {
        "schema_version": 1,
        "record_type": "ResearchSourceAsset",
        "record_id": f"asset-{digest}",
        **RESEARCH_FLAGS,
        "source_id": asset.source_id,
        "asset_id": asset.asset_id,
        "url": asset.url,
        "asset_kind": asset.kind,
        "sha256": digest,
        "size_bytes": 1,
        "blob_path": f"raw/blobs/aa/{digest}.csv",
        "downloaded_at": "2026-07-17T00:00:00Z",
    }


def test_registry_contains_only_public_static_assets() -> None:
    assert len(ASSETS) == 19
    assert all(asset.url.startswith("https://www.football-data.co.uk/") for asset in ASSETS)
    assert not any("500.com" in asset.url for asset in ASSETS)


def test_config_refuses_weaker_access_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be lower"):
        ResearchConfig(tmp_path, tmp_path / "research", min_interval_seconds=9.9)
    with pytest.raises(ValueError, match="between 1 and 60"):
        ResearchConfig(tmp_path, tmp_path / "research", requests_per_24h=61)


def test_persistent_request_budget(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    state = ResearchState(cfg.state_path)
    store = ResearchStore(cfg)
    now = datetime(2026, 7, 17, tzinfo=UTC)
    for _ in range(cfg.requests_per_24h):
        state.record_request("www.football-data.co.uk", now, 1)
    client = ResearchHttpClient(cfg, state, store, sleep=lambda _: None, now=lambda: now)
    with pytest.raises(BudgetExceeded):
        client._check_host("www.football-data.co.uk", now)
    state.close()


class FakeResponse:
    status_code = 403
    encoding = "utf-8"
    headers = {"content-type": "text/html", "content-length": "7"}

    def iter_content(self, chunk_size: int):
        yield b"blocked"

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers = {}

    def get(self, *args, **kwargs):
        return FakeResponse()


class RedirectResponse(FakeResponse):
    status_code = 302
    headers = {"location": "https://outside.example/file.csv", "content-length": "0"}

    def iter_content(self, chunk_size: int):
        return iter(())


class RedirectSession(FakeSession):
    def get(self, *args, **kwargs):
        return RedirectResponse()


class NotModifiedResponse(FakeResponse):
    status_code = 304
    headers = {"content-length": "0"}

    def iter_content(self, chunk_size: int):
        return iter(())


def test_blocked_source_opens_seven_day_circuit(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    state = ResearchState(cfg.state_path)
    store = ResearchStore(cfg)
    now = datetime(2026, 7, 17, tzinfo=UTC)
    state.save_robots(
        "www.football-data.co.uk",
        body="User-agent: *\nDisallow:\n",
        sha256="a" * 64,
        checked_at=now,
    )
    client = ResearchHttpClient(
        cfg,
        state,
        store,
        session=FakeSession(),  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: now,
    )
    with pytest.raises(AccessPolicyError, match="blocked response"):
        client.fetch(ASSETS[0])
    snapshot = state.host_snapshot("www.football-data.co.uk", now)
    assert str(snapshot["circuit_until"]).startswith("2026-07-24")
    state.close()


def test_cross_host_redirect_is_rejected(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    state = ResearchState(cfg.state_path)
    store = ResearchStore(cfg)
    now = datetime(2026, 7, 17, tzinfo=UTC)
    client = ResearchHttpClient(
        cfg,
        state,
        store,
        session=RedirectSession(),  # type: ignore[arg-type]
        sleep=lambda _: None,
        now=lambda: now,
    )
    with pytest.raises(AccessPolicyError, match="cross-host redirect"):
        client._request_once(ASSETS[0].url)
    state.close()


def test_not_modified_is_not_treated_as_redirect(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    state = ResearchState(cfg.state_path)
    store = ResearchStore(cfg)
    now = datetime(2026, 7, 17, tzinfo=UTC)
    session = FakeSession()
    session.get = lambda *args, **kwargs: NotModifiedResponse()  # type: ignore[method-assign]
    client = ResearchHttpClient(
        cfg, state, store, session=session, sleep=lambda _: None, now=lambda: now  # type: ignore[arg-type]
    )
    response, body, _ = client._request_once(ASSETS[0].url)
    assert response.status_code == 304
    assert body == b""
    state.close()


def test_football_data_normalization_separates_opening_and_closing() -> None:
    asset = next(asset for asset in ASSETS if asset.asset_id == "football-data-2526-e0")
    content = (
        "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A,"
        "B365CH,B365CD,B365CA,B365>2.5,B365<2.5,B365C>2.5,B365C<2.5,"
        "AHh,B365AHH,B365AHA,AHCh,B365CAHH,B365CAHA\n"
        "E0,15/08/2025,20:00,Home,Away,2,1,2.0,3.5,4.0,1.9,3.6,4.2,"
        "1.8,2.0,1.9,1.9,-0.5,1.9,1.9,-0.75,2.0,1.8\n"
    ).encode()
    records, summary = normalize_football_data_csv(
        asset, content, source_asset(asset), since=date(2025, 1, 1)
    )
    fixtures = [record for record in records if record["record_type"] == "ResearchFixture"]
    markets = [record for record in records if record["record_type"] == "ResearchMarketObservation"]
    assert len(fixtures) == 1
    assert fixtures[0]["result_scope"] == "regular_time_90"
    assert {(record["cohort"], record["market"]) for record in markets} == {
        ("opening", "1x2"),
        ("closing", "1x2"),
        ("opening", "total"),
        ("closing", "total"),
        ("opening", "asian_handicap"),
        ("closing", "asian_handicap"),
    }
    assert summary["fixtures"] == 1
    assert fixtures[0]["competition"] == "Premier League"


def test_extra_league_schema_is_closing_1x2_only() -> None:
    asset = next(asset for asset in ASSETS if asset.asset_id == "football-data-extra-bra")
    content = (
        "Country,League,Season,Date,Time,Home,Away,HG,AG,Res,AvgCH,AvgCD,AvgCA\n"
        "Brazil,Serie A,2025,01/01/2025,20:00,Home,Away,1,0,H,2.0,3.5,4.0\n"
    ).encode()
    records, summary = normalize_football_data_csv(
        asset, content, source_asset(asset), since=date(2025, 1, 1)
    )
    fixture = next(record for record in records if record["record_type"] == "ResearchFixture")
    market = next(record for record in records if record["record_type"] == "ResearchMarketObservation")
    assert fixture["competition"] == "Serie A"
    assert market["cohort"] == "closing"
    assert market["market_contract"] == "1x2_only"
    assert _competition_label(fixture) == "Brazil / Serie A"
    assert summary["fixtures"] == 1


def test_world_cup_extra_time_is_not_result_eligible(tmp_path: Path) -> None:
    workbook = Workbook()
    main = workbook.active
    main.title = "WorldCup2026"
    main.append(
        [
            "Competition", "Home", "Away", "Date", "Time", "HGFT", "AGFT", "Finished",
            "bet365-H", "bet365-D", "bet365-A", "H-Max", "D-Max", "A-Max",
            "H-Avg", "D-Avg", "A-Avg",
        ]
    )
    main.append(["World Cup 2026", "A", "B", datetime(2026, 7, 1), "20:00", 1, 1, "Penalties", 2, 3, 4, 2.1, 3.1, 4.1, 2, 3, 4])
    qualifiers = workbook.create_sheet("WorldCup2026Qualifiers")
    qualifiers.append(["Date", "Home", "Away", "HG", "AG", "H_Max", "D_Max", "A_Max", "H_Avg", "D_Avg", "A_Avg"])
    qualifiers.append([datetime(2026, 6, 1), "C", "D", 2, 0, 2, 3, 4, 1.9, 2.9, 3.9])
    path = tmp_path / "world-cup.xlsx"
    workbook.save(path)
    asset = next(asset for asset in ASSETS if asset.kind == "world_cup_xlsx")
    records, summary = normalize_world_cup_xlsx(
        config(tmp_path), asset, path, source_asset(asset), since=date(2025, 1, 1)
    )
    fixtures = [record for record in records if record["record_type"] == "ResearchFixture"]
    quality = [record for record in records if record["record_type"] == "ResearchQualityEvent"]
    assert len(fixtures) == 2
    assert len(quality) == 2
    assert all(record["result_eligible"] is False for record in fixtures)
    assert summary["result_scope_ambiguous"] == 2


def test_k1_rejects_unaccepted_artifact(tmp_path: Path) -> None:
    csv_path = tmp_path / "k1.csv"
    metadata_path = tmp_path / "k1.json"
    csv_path.write_text("fixture_id,season\n1,2025\n", encoding="utf-8")
    metadata_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ResearchIntegrityError, match="SHA-256"):
        import_k1_dataset(config(tmp_path), csv_path, metadata_path)


def test_metrics_are_deterministic_and_mark_small_calibration_sample() -> None:
    points = [
        {"actual": 0, "market": (0.5, 0.3, 0.2)},
        {"actual": 2, "market": (0.2, 0.3, 0.5)},
    ]
    first = _metric_rows(points, "market")
    second = _metric_rows(points, "market")
    assert first == second
    assert first["sample_size"] == 2
    assert first["calibration_conclusion"] == "insufficient_sample"


def research_record(record_type: str, record_id: str, **values) -> dict:
    return {
        "schema_version": 1,
        "record_type": record_type,
        "record_id": record_id,
        **RESEARCH_FLAGS,
        **values,
    }


def _market_fixture_records() -> list[dict]:
    asset = next(asset for asset in ASSETS if asset.asset_id == "football-data-2526-e0")
    asset_record = source_asset(asset)
    fixture = research_record(
        "ResearchFixture",
        "fixture-with-consensus",
        source_id="football-data",
        source_asset_record_id=asset_record["record_id"],
        source_fixture_key="fixture-1",
        competition="Premier League",
        match_date="2025-08-15",
        home_team="Home",
        away_team="Away",
        home_goals=2,
        away_goals=1,
        result_scope="regular_time_90",
        result_eligible=True,
        source_payload={},
    )
    markets = [
        research_record(
            "ResearchMarketObservation",
            f"market-{bookmaker}",
            fixture_record_id=fixture["record_id"],
            source_id="football-data",
            asset_sha256=asset_record["sha256"],
            cohort="closing",
            market="1x2",
            bookmaker=bookmaker,
            line=None,
            values={"home": home, "draw": draw, "away": away},
            market_contract="core3_available",
        )
        for bookmaker, home, draw, away in (
            ("B365", 2.0, 3.5, 4.0),
            ("BW", 2.1, 3.4, 3.9),
            ("IW", 1.95, 3.6, 4.2),
            ("Avg", 2.0, 3.5, 4.0),
        )
    ]
    return [asset_record, fixture, *markets]


def test_model_dataset_uses_three_real_closing_bookmakers(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = ResearchStore(cfg)
    store.write_records("football-data", "run", "records", _market_fixture_records())
    points, _ = build_closing_1x2_dataset(cfg)
    assert len(points) == 1
    assert points[0].bookmaker_count == 3
    assert points[0].bookmakers == ("B365", "BW", "IW")
    result = write_model_dataset(
        cfg,
        training_before_date=date(2026, 1, 1),
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert result["status"] == "created"
    model = train_devig_consensus_model(
        cfg,
        training_before_date=date(2026, 1, 1),
        activate=True,
        channel="research-shadow-v1",
        now=datetime(2026, 7, 20, 1, tzinfo=UTC),
    )
    assert model["model_version"].startswith("devig-consensus-v1-")
    records = load_records(cfg)
    activation = next(
        record for record in records.values() if record["record_type"] == "ResearchModelActivation"
    )
    assert activation["research_kind"] == "model_artifact"
    assert activation["backfill"] is True


def test_research_postgres_import_is_isolated(tmp_path: Path) -> None:
    database_url = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE_URL")
    local_test = os.environ.get("FOOTBALL_CUPS_TEST_DATABASE") == "1"
    if not database_url and not local_test:
        pytest.skip("PostgreSQL integration test is not configured")
    database_config = DatabaseConfig(tmp_path, tmp_path / "data" / "500", database_url)
    normalized = tmp_path / "data" / "research" / "normalized" / "test" / "run"
    normalized.mkdir(parents=True)
    asset_id = "research-asset"
    fixture_id = "research-fixture"
    records = [
        research_record(
            "ResearchSourceAsset",
            asset_id,
            source_id="test",
            asset_id="asset",
            url=None,
            asset_kind="csv",
            sha256="a" * 64,
            size_bytes=1,
            blob_path="raw/blobs/aa/test.csv",
        ),
        research_record(
            "ResearchFixture",
            fixture_id,
            source_id="test",
            source_asset_record_id=asset_id,
            source_fixture_key="fixture",
            competition="Test",
            match_date="2025-01-01",
            home_team="Home",
            away_team="Away",
            home_goals=1,
            away_goals=0,
            result_scope="regular_time_90",
            result_eligible=True,
            source_payload={},
        ),
        research_record(
            "ResearchShadowPrediction",
            "research-shadow",
            research_kind="shadow_event",
            backfill=False,
            channel="research-shadow-v1",
            fixture_id="live-fixture",
            target="T-6h",
            prediction_cutoff="2026-07-21T06:00:00Z",
            published_at="2026-07-21T06:01:00Z",
            status="published",
            model_key="devig-consensus-v1",
            model_version="test-version",
            activation_record_id=None,
            selected_batch_record_id="batch",
            source_snapshot_record_id="snapshot",
            market_observed_at="2026-07-21T05:59:00Z",
            bookmaker_count=8,
            probabilities={"home": 0.6, "draw": 0.25, "away": 0.15, "sum": 1.0},
            features={},
            abstention_reason=None,
            competition_id="5",
            competition_name="芬兰超级联赛",
            competition_type="lower_evidence_league",
            market_evidence_tier="C",
            evaluation_group="finland-top-flight",
            classification_status="provisional",
            registry_version="competition-profile-v1",
            policy_version="shadow-confidence-v1",
            registry_file_sha256="a" * 64,
            registry_canonical_sha256="b" * 64,
            direction_strength=0.35,
            bookmaker_dispersion=0.01,
            raw_confidence_label="high",
            competition_confidence_cap="low",
            confidence_label="low",
            confidence_reasons=["competition_confidence_cap"],
            risk_flags=["low_market_evidence_tier"],
            identity_record_id="identity-as-of",
            identity_observed_at="2026-07-20T12:00:00Z",
            automatic_verified_fixture_count=0,
            evaluation_span_days=0.0,
            review_eligible=False,
        ),
    ]
    path = normalized / "records.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with connect(database_config, autocommit=True) as raw:
        if not raw.info.dbname.endswith("_test"):
            pytest.fail("integration test database name must end with _test")
        raw.execute("DROP SCHEMA IF EXISTS research CASCADE")
        raw.execute("DROP SCHEMA IF EXISTS football CASCADE")
    with connect(database_config) as connection:
        apply_migrations(connection)
        before = connection.execute("SELECT count(*) AS count FROM football.records").fetchone()["count"]
        first = import_research_files(connection, tmp_path / "data" / "research" / "normalized")
        second = import_research_files(connection, tmp_path / "data" / "research" / "normalized")
        after = connection.execute("SELECT count(*) AS count FROM football.records").fetchone()["count"]
        shadow = connection.execute(
            "SELECT market_evidence_tier, confidence_label "
            "FROM research.shadow_predictions WHERE record_id='research-shadow'"
        ).fetchone()
        assert first["records_inserted"] == 3
        assert second["records_inserted"] == 0
        assert before == after == 0
        assert dict(shadow) == {"market_evidence_tier": "C", "confidence_label": "low"}
