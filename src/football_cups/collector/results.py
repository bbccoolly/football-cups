from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from lxml import etree, html

from . import SCHEMA_VERSION
from .storage import DataStore, stable_record_id
from .timeutil import iso_utc, parse_iso, utc_now


@dataclass(frozen=True)
class CompletedScore:
    fixture_id: str
    home_goals: int
    away_goals: int
    half_time_score_raw: str | None
    status_raw: str
    home_name: str
    away_name: str


@dataclass(frozen=True)
class AnalysisScore:
    fixture_id: str
    home_goals: int
    away_goals: int
    home_name: str
    away_name: str


def _score(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\s*[:\-]\s*(\d+)\s*", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_completed_page(content: bytes, known_fixture_ids: set[str]) -> dict[str, CompletedScore]:
    try:
        tree = html.fromstring(content)
    except (etree.ParserError, ValueError):
        return {}
    results: dict[str, CompletedScore] = {}
    for row in tree.xpath("//tr[starts-with(@id, 'a')]"):
        raw_id = str(row.get("id") or "")
        fixture_id = raw_id[1:] if raw_id.startswith("a") else ""
        if fixture_id not in known_fixture_ids:
            continue
        status_raw = " ".join(row.xpath(".//span[contains(@class, 'red')]/text()"))
        if "完" not in status_raw:
            continue
        home_score = row.xpath(".//div[contains(@class, 'pk')]//a[contains(@class, 'clt1')]/text()")
        away_score = row.xpath(".//div[contains(@class, 'pk')]//a[contains(@class, 'clt3')]/text()")
        if not home_score or not away_score or not home_score[0].strip().isdigit() or not away_score[0].strip().isdigit():
            continue
        home_name = " ".join(row.xpath(".//*[contains(@class, 'mainName')]/text()") or [""]).strip()
        away_name = " ".join(row.xpath(".//*[contains(@class, 'clientName')]/text()") or [""]).strip()
        half_cells = row.xpath(".//td[contains(@class, 'red')]/text()")
        half_raw = next((value.strip() for value in half_cells if _score(value.strip())), None)
        results[fixture_id] = CompletedScore(
            fixture_id=fixture_id,
            home_goals=int(home_score[0].strip()),
            away_goals=int(away_score[0].strip()),
            half_time_score_raw=half_raw,
            status_raw=status_raw.strip(),
            home_name=home_name,
            away_name=away_name,
        )
    return results


def parse_analysis_page(content: bytes, fixture_id: str) -> AnalysisScore | None:
    try:
        tree = html.fromstring(content)
    except (etree.ParserError, ValueError):
        return None
    score_text = " ".join(tree.xpath("//p[contains(@class, 'odds_hd_bf')]//strong/text()"))
    parsed = _score(score_text)
    if parsed is None:
        return None
    team_nodes = tree.xpath("//span[contains(@class, 'odds_hd_team')]//a/text()")
    home_name = team_nodes[0].strip() if team_nodes else ""
    away_name = team_nodes[-1].strip() if len(team_nodes) > 1 else ""
    return AnalysisScore(fixture_id, parsed[0], parsed[1], home_name, away_name)


def load_competition_formats(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    competitions = payload.get("competitions", {})
    return {str(key): str(value) for key, value in competitions.items()}


def make_candidate(
    completed: CompletedScore,
    analysis: AnalysisScore,
    *,
    observed_at: datetime,
    completed_blob: dict[str, Any],
    analysis_blob: dict[str, Any],
) -> dict[str, Any] | None:
    if (completed.home_goals, completed.away_goals) != (analysis.home_goals, analysis.away_goals):
        return None
    record_id = stable_record_id(
        "result_candidate",
        completed.fixture_id,
        completed.home_goals,
        completed.away_goals,
        completed_blob["sha256"],
        analysis_blob["sha256"],
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "ResultCandidate",
        "record_id": record_id,
        "fixture_id": completed.fixture_id,
        "home_goals": completed.home_goals,
        "away_goals": completed.away_goals,
        "half_time_score_raw": completed.half_time_score_raw,
        "status_raw": completed.status_raw,
        "observed_at": iso_utc(observed_at),
        "scope": "candidate-full-time-scope-not-yet-confirmed",
        "completed_page_sha256": completed_blob["sha256"],
        "analysis_page_sha256": analysis_blob["sha256"],
        "source_urls": [completed_blob["url"], analysis_blob["url"]],
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
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "VerifiedResult",
        "record_id": stable_record_id(
            "verified_result", fixture_id, home_goals, away_goals, source_url, iso_utc(confirmed_at)
        ),
        "fixture_id": fixture_id,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "scope": "90-minutes-including-stoppage",
        "source_url": source_url,
        "confirmed_at": iso_utc(confirmed_at),
        "verification_method": method,
        "notes": notes,
        "candidate_id": candidate_id,
    }


def existing_verified_results(data_dir: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in data_dir.glob("results/*/*/verified/*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
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
            )
            data_store.write_result("verified", record, confirmed_at)
            existing[fixture_id] = record
            imported.append(record)
    return imported, conflicts

