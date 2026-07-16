from __future__ import annotations

from datetime import datetime, timezone

from football_cups.collector.config import CollectorConfig
from football_cups.collector.results import (
    import_verified_results,
    make_candidate,
    parse_analysis_page,
    parse_completed_page,
)
from football_cups.collector.storage import DataStore


COMPLETED = b"""
<html><head><meta charset="utf-8"></head><body><table><tr id="a123">
 <td><span class="red">COMPLETE</span></td>
 <td><span class="mainName">Home</span></td>
 <td><div class="pk"><a class="clt1">2</a><span>-</span><a class="clt3">1</a></div></td>
 <td><span class="clientName">Away</span></td><td class="red">1 - 0</td>
</tr></table></body></html>
""".replace(b"COMPLETE", "\u5b8c".encode())

ANALYSIS = b"""
<span class="odds_hd_team"><a>Home</a></span>
<p class="odds_hd_bf"><strong>2:1</strong></p>
<span class="odds_hd_team odds_hd_team2"><a>Away</a></span>
"""


def config_for(tmp_path):
    return CollectorConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data" / "500",
        backup_dir=None,
    )


def test_two_source_candidate() -> None:
    completed = parse_completed_page(COMPLETED, {"123"})["123"]
    analysis = parse_analysis_page(ANALYSIS, "123")
    assert analysis is not None
    observed = datetime.now(timezone.utc)
    blob = {"sha256": "abc", "url": "https://example.test", "observed_at": observed.isoformat()}
    candidate = make_candidate(
        completed,
        analysis,
        observed_at=observed,
        completed_blob=blob,
        analysis_blob=blob | {"sha256": "def"},
    )
    assert candidate is not None
    assert (candidate["home_goals"], candidate["away_goals"]) == (2, 1)


def test_manual_verified_result_conflict_is_not_overwritten(tmp_path) -> None:
    store = DataStore(config_for(tmp_path))
    first = tmp_path / "first.csv"
    first.write_text(
        "fixture_id,home_goals,away_goals,source_url,confirmed_at,notes\n"
        "123,2,1,https://source.test,2026-07-15T10:00:00Z,checked\n",
        encoding="utf-8",
    )
    imported, conflicts = import_verified_results(first, store)
    assert len(imported) == 1
    assert conflicts == []

    second = tmp_path / "second.csv"
    second.write_text(
        "fixture_id,home_goals,away_goals,source_url,confirmed_at,notes\n"
        "123,3,1,https://source.test,2026-07-15T11:00:00Z,conflict\n",
        encoding="utf-8",
    )
    imported, conflicts = import_verified_results(second, store)
    assert imported == []
    assert conflicts[0]["existing"] == [2, 1]
    verified_files = list((store.config.data_dir / "results").rglob("verified/*.json"))
    assert len(verified_files) == 1
