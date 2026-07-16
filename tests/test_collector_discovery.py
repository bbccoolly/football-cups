from __future__ import annotations

from datetime import datetime, timezone

from football_cups.collector.discovery import merge_discovery_pages, parse_discovery_page


PAGE = b"""
<html><body><table>
<tr data-fixtureid="123" data-homesxname="Home" data-awaysxname="Away"
 data-matchdate="2026-07-16" data-matchtime="03:00" data-rangqiu="-1"
 data-simpleleague="League" data-homeid="10" data-awayid="20" data-matchid="30"
 data-matchnum="W101" data-buyendtime="2026-07-15 22:00:00"
 data-isshow="1" data-isactive="1" data-isend="0">
 <td><a href="https://liansai.500.com/zuqiu-900/">League</a></td>
 <td><p data-type="nspf" data-value="3" data-sp="2.50">2.50</p></td>
</tr>
<tr style="display:none" data-fixtureid="456" data-homesxname="Hidden Home"
 data-awaysxname="Hidden Away" data-matchdate="2026-07-17" data-matchtime="08:30"
 data-simpleleague="Other" data-homeid="11" data-awayid="21" data-matchid="31"
 data-isshow="0" data-isactive="0" data-isend="1"></tr>
</table></body></html>
"""


def test_discovery_keeps_hidden_and_pool_observations() -> None:
    observed = datetime(2026, 7, 15, 6, tzinfo=timezone.utc)
    parsed = parse_discovery_page(
        PAGE,
        source_name="spf",
        source_url="https://example.test",
        observed_at=observed,
        timezone_name="Asia/Shanghai",
    )

    assert parsed.inventory_matches
    assert parsed.dom_fixture_ids == {"123", "456"}
    assert parsed.fixtures[0]["kickoff_at"] == "2026-07-15T19:00:00Z"
    assert parsed.fixtures[0]["season_id"] == "900"
    assert parsed.fixtures[1]["is_show_raw"] == "0"
    assert parsed.pools[0]["pool_type"] == "nspf"
    assert parsed.pools[0]["sp_raw"] == "2.50"


def test_inventory_detects_regex_only_fixture() -> None:
    page = PAGE + b'<script data-fixtureid="999"></script>'
    parsed = parse_discovery_page(
        page,
        source_name="default",
        source_url="https://example.test",
        observed_at=datetime.now(timezone.utc),
        timezone_name="Asia/Shanghai",
    )
    assert not parsed.inventory_matches
    assert parsed.regex_fixture_ids - parsed.dom_fixture_ids == {"999"}


def test_merge_reports_identity_conflicts() -> None:
    observed = datetime.now(timezone.utc)
    first = parse_discovery_page(
        PAGE,
        source_name="default",
        source_url="https://one.test",
        observed_at=observed,
        timezone_name="Asia/Shanghai",
    )
    second_page = PAGE.replace(b'data-homeid="10"', b'data-homeid="99"')
    second = parse_discovery_page(
        second_page,
        source_name="mixed",
        source_url="https://two.test",
        observed_at=observed,
        timezone_name="Asia/Shanghai",
    )
    identities, conflicts = merge_discovery_pages([first, second])
    assert set(identities) == {"123", "456"}
    assert "123" in conflicts


def test_gbk_page_and_second_precision_buy_end_are_decoded() -> None:
    page = PAGE.replace(b"Home", "\u82f1\u683c\u5170".encode("gbk"), 1)
    parsed = parse_discovery_page(
        page,
        source_name="default",
        source_url="https://example.test",
        observed_at=datetime.now(timezone.utc),
        timezone_name="Asia/Shanghai",
        source_encoding="gbk",
    )
    assert parsed.errors == []
    assert parsed.fixtures[0]["home_team_name"] == "\u82f1\u683c\u5170"
    assert parsed.fixtures[0]["buy_end_at"] == "2026-07-15T14:00:00Z"
