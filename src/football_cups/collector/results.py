from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lxml import etree, html

from . import SCHEMA_VERSION
from .storage import DataStore, stable_record_id
from .timeutil import iso_utc, parse_iso


RESULT_SCOPE_CANDIDATE = "candidate-full-time-scope-not-yet-confirmed"
RESULT_SCOPE_VERIFIED = "90-minutes-including-stoppage"
COMPETITION_FORMATS = frozenset({"regular_time_only", "may_have_extra_time", "unknown"})
LIVE_RESULTS_URL = "https://live.500.com/?e={date_key}"
LIVE_RESULT_FEED_URL = (
    "https://live.500.com/static/info/bifen/xml/livedata/jczq/{date_key}Full.txt"
)


class ResultParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LiveScore:
    fixture_id: str
    home_goals: int
    away_goals: int
    status_code: str
    half_time_score_raw: str | None
    home_name: str
    away_name: str


@dataclass(frozen=True)
class AnalysisScore:
    fixture_id: str
    home_goals: int
    away_goals: int
    home_name: str
    away_name: str


def result_page_url(kickoff_at: str, timezone_name: str) -> str:
    kickoff = parse_iso(kickoff_at).astimezone(ZoneInfo(timezone_name))
    return LIVE_RESULTS_URL.format(date_key=kickoff.strftime("%Y%m%d"))


def result_feed_url(kickoff_at: str) -> str:
    kickoff = parse_iso(kickoff_at)
    # The 500 jczq feed is partitioned by its slate date, which matches the UTC
    # kickoff date for overnight Beijing fixtures in the discovery contract.
    return LIVE_RESULT_FEED_URL.format(date_key=kickoff.strftime("%Y%m%d"))


def is_blocked_result_page(content: bytes) -> bool:
    lowered = content.lower()
    markers = (
        b"tencent cloud edgeone",
        b"restricted access",
        b'id="statuscode">567',
        "安全策略拦截".encode("utf-8"),
    )
    return any(marker in lowered for marker in markers)


def _class_nodes(node: Any, class_name: str) -> list[Any]:
    return node.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), $class_name)]",
        class_name=f" {class_name} ",
    )


def _score_node(nodes: list[Any], *, fixture_id: str, side: str) -> int:
    if len(nodes) != 1:
        raise ResultParseError(
            "score_node_count",
            f"fixture {fixture_id} must expose exactly one {side} score node; found {len(nodes)}",
        )
    value = "".join(nodes[0].itertext()).strip()
    if not value or any(character < "0" or character > "9" for character in value):
        raise ResultParseError(
            "score_not_integer", f"fixture {fixture_id} {side} score is not a non-negative integer"
        )
    return int(value)


def _score(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\s*[:\-]\s*(\d+)\s*", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_live_result(content: bytes, fixture_id: str) -> LiveScore:
    if is_blocked_result_page(content):
        raise ResultParseError("blocked_page", "live result page is an access block page")
    try:
        tree = html.fromstring(content)
    except (etree.ParserError, ValueError) as exc:
        raise ResultParseError("invalid_html", "live result page is not parseable HTML") from exc
    rows = tree.xpath(
        "//tr[@fid=$fixture_id or @id=$row_id]",
        fixture_id=str(fixture_id),
        row_id=f"a{fixture_id}",
    )
    if len(rows) != 1:
        code = "fixture_missing" if not rows else "fixture_duplicate"
        raise ResultParseError(
            code,
            f"fixture {fixture_id} must have exactly one live result row; found {len(rows)}",
        )
    row = rows[0]
    status_code = str(row.get("status") or "").strip()
    if status_code in {"5", "6"}:
        raise ResultParseError(
            "cancelled", f"fixture {fixture_id} is cancelled, abandoned, or otherwise not settled"
        )
    if status_code != "4":
        raise ResultParseError(
            "not_finished", f"fixture {fixture_id} is not marked finished (status=4)"
        )
    score_boxes = _class_nodes(row, "pk")
    if len(score_boxes) != 1:
        raise ResultParseError(
            "score_container_count",
            f"fixture {fixture_id} must expose exactly one score container; found {len(score_boxes)}",
        )
    score_box = score_boxes[0]
    home_goals = _score_node(_class_nodes(score_box, "clt1"), fixture_id=fixture_id, side="home")
    away_goals = _score_node(_class_nodes(score_box, "clt3"), fixture_id=fixture_id, side="away")
    half_cells = row.xpath(".//td[contains(concat(' ', normalize-space(@class), ' '), ' red ')]/text()")
    half_raw = next((value.strip() for value in half_cells if _score(value.strip())), None)
    home_names = _class_nodes(row, "mainName")
    away_names = _class_nodes(row, "clientName")
    return LiveScore(
        fixture_id=str(fixture_id),
        home_goals=home_goals,
        away_goals=away_goals,
        status_code=status_code,
        half_time_score_raw=half_raw,
        home_name=" ".join(home_names[0].itertext()).strip() if home_names else "",
        away_name=" ".join(away_names[0].itertext()).strip() if away_names else "",
    )


def parse_live_result_feed(content: bytes, fixture_id: str) -> LiveScore:
    if is_blocked_result_page(content):
        raise ResultParseError("blocked_page", "live result feed is an access block page")
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultParseError("invalid_feed", "live result feed is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ResultParseError("invalid_feed", "live result feed root must be an array")
    rows = [
        row
        for row in payload
        if isinstance(row, list) and row and str(row[0]) == str(fixture_id)
    ]
    if len(rows) != 1:
        code = "fixture_missing" if not rows else "fixture_duplicate"
        raise ResultParseError(
            code,
            f"fixture {fixture_id} must have exactly one live feed row; found {len(rows)}",
        )
    row = rows[0]
    if len(row) < 4:
        raise ResultParseError("invalid_feed", f"fixture {fixture_id} live feed row is incomplete")
    status_code = str(row[1]).strip()
    if status_code in {"5", "6"}:
        raise ResultParseError(
            "cancelled", f"fixture {fixture_id} is cancelled, abandoned, or otherwise not settled"
        )
    if status_code != "4":
        raise ResultParseError(
            "not_finished", f"fixture {fixture_id} is not marked finished (status=4)"
        )

    def first_score(value: Any, side: str) -> int:
        raw = str(value).split(",", 1)[0].strip()
        if not raw or any(character < "0" or character > "9" for character in raw):
            raise ResultParseError(
                "score_not_integer",
                f"fixture {fixture_id} {side} feed score is not a non-negative integer",
            )
        return int(raw)

    return LiveScore(
        fixture_id=str(fixture_id),
        home_goals=first_score(row[2], "home"),
        away_goals=first_score(row[3], "away"),
        status_code=status_code,
        half_time_score_raw=None,
        home_name="",
        away_name="",
    )


def parse_analysis_page(content: bytes, fixture_id: str) -> AnalysisScore | None:
    try:
        tree = html.fromstring(content)
    except (etree.ParserError, ValueError):
        return None
    page_ids = [str(value).strip() for value in tree.xpath("//input[@id='id']/@value") if str(value).strip()]
    if page_ids and (len(set(page_ids)) != 1 or page_ids[0] != str(fixture_id)):
        return None
    score_text = " ".join(tree.xpath("//p[contains(@class, 'odds_hd_bf')]//strong/text()"))
    parsed = _score(score_text)
    if parsed is None:
        return None
    team_nodes = tree.xpath("//span[contains(@class, 'odds_hd_team')]//a/text()")
    home_name = team_nodes[0].strip() if team_nodes else ""
    away_name = team_nodes[-1].strip() if len(team_nodes) > 1 else ""
    return AnalysisScore(str(fixture_id), parsed[0], parsed[1], home_name, away_name)


def load_competition_formats(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("competitions"), dict):
        raise ValueError("competition formats must use schema_version=1 and a competitions object")
    competitions: dict[str, str] = {}
    for raw_name, raw_format in payload["competitions"].items():
        name = str(raw_name).strip()
        competition_format = str(raw_format).strip()
        if not name or competition_format not in COMPETITION_FORMATS:
            raise ValueError(f"invalid competition format: {raw_name!r}={raw_format!r}")
        competitions[name] = competition_format
    raw_ids = payload.get("competition_ids", {})
    if raw_ids is None:
        raw_ids = {}
    if not isinstance(raw_ids, dict):
        raise ValueError("competition_ids must be an object when provided")
    for raw_id, raw_format in raw_ids.items():
        competition_id = str(raw_id).strip()
        competition_format = str(raw_format).strip()
        if not competition_id.isdigit() or competition_format not in COMPETITION_FORMATS:
            raise ValueError(f"invalid competition id format: {raw_id!r}={raw_format!r}")
        competitions[f"id:{competition_id}"] = competition_format
    return competitions


def make_candidate(
    live: LiveScore,
    *,
    kickoff_at: str | None,
    observed_at: datetime,
    live_blob: dict[str, Any],
    analysis_blob: dict[str, Any] | None,
    analysis_consistency: str,
) -> dict[str, Any]:
    analysis_sha256 = analysis_blob.get("sha256") if analysis_blob else None
    source_urls = [live_blob["url"]]
    if analysis_blob:
        source_urls.append(analysis_blob["url"])
    record_id = stable_record_id(
        "result_candidate",
        live.fixture_id,
        live.home_goals,
        live.away_goals,
        live_blob["sha256"],
        analysis_sha256 or "",
        analysis_consistency,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "ResultCandidate",
        "record_id": record_id,
        "fixture_id": live.fixture_id,
        "kickoff_at": kickoff_at,
        "home_goals": live.home_goals,
        "away_goals": live.away_goals,
        "half_time_score_raw": live.half_time_score_raw,
        "status_code": live.status_code,
        "status_raw": live.status_code,
        "observed_at": iso_utc(observed_at),
        "scope": RESULT_SCOPE_CANDIDATE,
        "live_page_sha256": live_blob["sha256"],
        "completed_page_sha256": live_blob["sha256"],
        "analysis_page_sha256": analysis_sha256,
        "analysis_consistency": analysis_consistency,
        "source_urls": source_urls,
    }


def make_verified_result(
    *,
    fixture_id: str,
    home_goals: int,
    away_goals: int,
    source_url: str,
    confirmed_at: datetime,
    method: str,
    notes: str,
    candidate_id: str | None = None,
    supersedes_record_id: str | None = None,
    correction_reason: str | None = None,
    evidence_level: str | None = None,
    attestor_id: str | None = None,
    attestation_note: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "VerifiedResult",
        "record_id": stable_record_id(
            "verified_result", fixture_id, home_goals, away_goals, method, source_url
        ),
        "fixture_id": fixture_id,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "scope": RESULT_SCOPE_VERIFIED,
        "source_url": source_url,
        "confirmed_at": iso_utc(confirmed_at),
        "verification_method": method,
        "verification_status": "accepted",
        "notes": notes,
        "candidate_id": candidate_id,
        "supersedes_record_id": supersedes_record_id,
        "correction_reason": correction_reason,
        "evidence_level": evidence_level,
        "attestor_id": attestor_id,
        "attestation_note": attestation_note,
    }


def existing_result_records(data_dir: Path, kind: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in data_dir.glob(f"results/*/*/{kind}/*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def existing_verified_results(data_dir: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for record in existing_result_records(data_dir, "verified"):
        found[str(record.get("fixture_id"))] = record
    return found


def import_verified_results(
    input_path: Path,
    data_store: DataStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing = existing_verified_results(data_store.config.data_dir)
    imported: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"fixture_id", "home_goals", "away_goals", "source_url", "confirmed_at", "notes"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or []))
            raise ValueError(f"verified result CSV missing fields: {', '.join(missing)}")
        for row_number, row in enumerate(reader, start=2):
            fixture_id = (row.get("fixture_id") or "").strip()
            if not fixture_id.isdigit():
                raise ValueError(f"row {row_number}: invalid fixture_id")
            try:
                home_goals = int(row["home_goals"])
                away_goals = int(row["away_goals"])
                confirmed_at = parse_iso(row["confirmed_at"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"row {row_number}: invalid score or confirmed_at") from exc
            if home_goals < 0 or away_goals < 0:
                raise ValueError(f"row {row_number}: goals must be non-negative")
            prior = existing.get(fixture_id)
            if prior and (prior["home_goals"], prior["away_goals"]) != (home_goals, away_goals):
                conflicts.append(
                    {
                        "fixture_id": fixture_id,
                        "existing": [prior["home_goals"], prior["away_goals"]],
                        "incoming": [home_goals, away_goals],
                        "row": row_number,
                    }
                )
                continue
            record = make_verified_result(
                fixture_id=fixture_id,
                home_goals=home_goals,
                away_goals=away_goals,
                source_url=(row.get("source_url") or "").strip(),
                confirmed_at=confirmed_at,
                method="manual-import",
                notes=(row.get("notes") or "").strip(),
                candidate_id=(row.get("candidate_id") or "").strip() or None,
            )
            data_store.write_result("verified", record, confirmed_at)
            existing[fixture_id] = record
            imported.append(record)
    return imported, conflicts
