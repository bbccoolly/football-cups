from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lxml import etree, html

from . import SCHEMA_VERSION
from .storage import stable_record_id
from .timeutil import iso_utc, parse_source_datetime


FIXTURE_PATTERN = re.compile(rb"data-fixtureid=[\"'](\d+)[\"']", re.I)
SEASON_LINK_PATTERN = re.compile(r"/zuqiu-(\d+)/")
META_CHARSET_PATTERN = re.compile(
    rb"<meta[^>]+charset\s*=\s*['\"]?\s*([a-zA-Z0-9._-]+)", re.IGNORECASE
)


@dataclass(frozen=True)
class ParsedDiscoveryPage:
    source_name: str
    source_url: str
    fixtures: list[dict[str, Any]]
    pools: list[dict[str, Any]]
    regex_fixture_ids: set[str]
    dom_fixture_ids: set[str]
    errors: list[str]

    @property
    def inventory_matches(self) -> bool:
        return self.regex_fixture_ids == self.dom_fixture_ids


def _attr(node: html.HtmlElement, name: str) -> str:
    return (node.get(name) or "").strip()


def _season_id(row: html.HtmlElement) -> str:
    for href in row.xpath(".//a[contains(@href, 'liansai.500.com/zuqiu-')]/@href"):
        match = SEASON_LINK_PATTERN.search(str(href))
        if match:
            return match.group(1)
    return ""


def _parse_time(date_text: str, time_text: str, timezone_name: str) -> str | None:
    if not date_text or not time_text:
        return None
    try:
        return iso_utc(parse_source_datetime(date_text, time_text, timezone_name))
    except ValueError:
        return None


def _canonical_encoding(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().strip("\"'").lower()
    aliases = {
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "gb-2312": "gb18030",
    }
    return aliases.get(normalized, normalized)


def _select_decoded_text(content: bytes, source_encoding: str) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    meta_match = META_CHARSET_PATTERN.search(content[:4096])
    if meta_match:
        meta_encoding = _canonical_encoding(meta_match.group(1).decode("ascii", errors="ignore"))
        if meta_encoding:
            candidates.append(("meta", meta_encoding))
    inferred = _canonical_encoding(source_encoding)
    if inferred and inferred not in {"iso-8859-1", "latin-1"}:
        candidates.append(("source", inferred))
    candidates.extend(
        [
            ("gb18030_fallback", "gb18030"),
            ("utf8_fallback", "utf-8"),
        ]
    )
    seen: set[str] = set()
    for source, encoding in candidates:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            decoded = content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        if "\ufffd" not in decoded:
            return decoded, encoding
    return content.decode(source_encoding or "utf-8", errors="replace"), source_encoding


def parse_discovery_page(
    content: bytes,
    *,
    source_name: str,
    source_url: str,
    observed_at: datetime,
    timezone_name: str,
    source_encoding: str = "utf-8",
) -> ParsedDiscoveryPage:
    regex_ids = {match.group(1).decode("ascii") for match in FIXTURE_PATTERN.finditer(content)}
    errors: list[str] = []
    fixtures: list[dict[str, Any]] = []
    pools: list[dict[str, Any]] = []
    try:
        decoded, _selected_encoding = _select_decoded_text(content, source_encoding)
        tree = html.fromstring(decoded)
    except (LookupError, etree.ParserError, ValueError) as exc:
        return ParsedDiscoveryPage(source_name, source_url, [], [], regex_ids, set(), [str(exc)])

    for row in tree.xpath("//tr[@data-fixtureid]"):
        fixture_id = _attr(row, "data-fixtureid")
        if not fixture_id.isdigit():
            continue
        row_bytes = html.tostring(row, encoding="utf-8", with_tail=False)
        match_date = _attr(row, "data-matchdate")
        match_time = _attr(row, "data-matchtime")
        buy_end_raw = _attr(row, "data-buyendtime")
        buy_end_at: str | None = None
        if buy_end_raw:
            parts = buy_end_raw.split()
            if len(parts) == 2:
                buy_end_at = _parse_time(parts[0], parts[1], timezone_name)
        kickoff_at = _parse_time(match_date, match_time, timezone_name)
        observation = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "DiscoveryObservation",
            "record_id": stable_record_id(
                "discovery", source_name, fixture_id, iso_utc(observed_at), hashlib.sha256(row_bytes).hexdigest()
            ),
            "fixture_id": fixture_id,
            "source_name": source_name,
            "source_url": source_url,
            "observed_at": iso_utc(observed_at),
            "competition_name": _attr(row, "data-simpleleague"),
            "competition_id": _attr(row, "data-matchid"),
            "season_id": _season_id(row),
            "home_team_name": _attr(row, "data-homesxname"),
            "home_team_id": _attr(row, "data-homeid"),
            "away_team_name": _attr(row, "data-awaysxname"),
            "away_team_id": _attr(row, "data-awayid"),
            "match_number": _attr(row, "data-matchnum"),
            "kickoff_at": kickoff_at,
            "kickoff_source_text": f"{match_date} {match_time}".strip(),
            "buy_end_at": buy_end_at,
            "buy_end_source_text": buy_end_raw,
            "official_handicap_raw": _attr(row, "data-rangqiu"),
            "is_show_raw": _attr(row, "data-isshow"),
            "is_active_raw": _attr(row, "data-isactive"),
            "is_end_raw": _attr(row, "data-isend"),
            "subactive_raw": _attr(row, "data-subactive"),
            "row_sha256": hashlib.sha256(row_bytes).hexdigest(),
        }
        if kickoff_at is None:
            errors.append(f"fixture {fixture_id}: invalid kickoff {match_date} {match_time}")
        if buy_end_raw and buy_end_at is None:
            errors.append(f"fixture {fixture_id}: invalid buy end {buy_end_raw}")
        critical_text = (
            observation["competition_name"],
            observation["home_team_name"],
            observation["away_team_name"],
            observation["match_number"],
        )
        if any("\ufffd" in str(value) for value in critical_text):
            errors.append(f"fixture {fixture_id}: replacement character in critical identity fields")
        fixtures.append(observation)

        for option in row.xpath(".//*[@data-type and @data-value and @data-sp]"):
            pool_type = _attr(option, "data-type")
            value = _attr(option, "data-value")
            sp = _attr(option, "data-sp")
            if not pool_type:
                continue
            pools.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "record_type": "SportteryPoolObservation",
                    "record_id": stable_record_id(
                        "sporttery_pool", source_name, fixture_id, pool_type, value, sp, iso_utc(observed_at)
                    ),
                    "fixture_id": fixture_id,
                    "source_name": source_name,
                    "source_url": source_url,
                    "observed_at": iso_utc(observed_at),
                    "pool_type": pool_type,
                    "option_value": value,
                    "sp_raw": sp,
                    "handicap_raw": observation["official_handicap_raw"],
                }
            )

    dom_ids = {item["fixture_id"] for item in fixtures}
    return ParsedDiscoveryPage(source_name, source_url, fixtures, pools, regex_ids, dom_ids, errors)


IDENTITY_FIELDS = (
    "competition_id",
    "season_id",
    "home_team_id",
    "away_team_id",
    "kickoff_at",
)


def merge_discovery_pages(
    pages: list[ParsedDiscoveryPage],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        for fixture in page.fixtures:
            grouped.setdefault(str(fixture["fixture_id"]), []).append(fixture)

    identities: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, list[dict[str, Any]]] = {}
    for fixture_id, observations in grouped.items():
        canonical = max(observations, key=lambda item: sum(bool(item.get(field)) for field in IDENTITY_FIELDS))
        identity = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "FixtureIdentity",
            "fixture_id": fixture_id,
            "competition_name": canonical.get("competition_name", ""),
            "competition_id": canonical.get("competition_id", ""),
            "season_id": canonical.get("season_id", ""),
            "home_team_name": canonical.get("home_team_name", ""),
            "home_team_id": canonical.get("home_team_id", ""),
            "away_team_name": canonical.get("away_team_name", ""),
            "away_team_id": canonical.get("away_team_id", ""),
            "kickoff_at": canonical.get("kickoff_at"),
            "buy_end_at": canonical.get("buy_end_at"),
            "match_number": canonical.get("match_number", ""),
        }
        mismatches: list[dict[str, Any]] = []
        for observation in observations:
            fields = {
                field: {str(canonical.get(field) or ""), str(observation.get(field) or "")}
                for field in IDENTITY_FIELDS
                if canonical.get(field)
                and observation.get(field)
                and str(canonical.get(field)) != str(observation.get(field))
            }
            if fields:
                mismatches.append({"source_name": observation["source_name"], "fields": fields})
        identities[fixture_id] = identity
        if mismatches:
            conflicts[fixture_id] = mismatches
    return identities, conflicts
