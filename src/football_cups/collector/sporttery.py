from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import SCHEMA_VERSION
from .storage import stable_record_id
from .timeutil import iso_utc, parse_iso


SPORTTERY_RESULT_PAGE_URL = "https://www.lottery.gov.cn/jc/zqsgkj/"
SPORTTERY_API_BASE = "https://webapi.sporttery.cn/gateway/uniform/football"
SPORTTERY_INVENTORY_URL = f"{SPORTTERY_API_BASE}/getUniformMatchResultV1.qry"
SPORTTERY_HEAD_URL = f"{SPORTTERY_API_BASE}/getMatchHeadV1.qry"
SPORTTERY_FIXED_BONUS_URL = f"{SPORTTERY_API_BASE}/getFixedBonusV1.qry"
OFFICIAL_RESULT_SCOPE = "90-minutes-including-stoppage"
OFFICIAL_SCOPE_TEXT = "全场比分（90分钟）包含伤停补时阶段"


class SportteryEvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SportteryInventoryRow:
    sporttery_match_id: str
    match_number: str
    kickoff_at: datetime | None
    home_name: str
    away_name: str
    score: tuple[int, int] | None
    status_text: str
    result_status_text: str
    is_cancel: bool | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class SportteryDetail:
    sporttery_match_id: str
    match_number: str
    kickoff_at: datetime | None
    home_name: str
    away_name: str
    head_score: tuple[int, int] | None
    fixed_score: tuple[int, int] | None
    is_cancel: bool | None
    raw_head: dict[str, Any]
    raw_fixed: dict[str, Any]


@dataclass(frozen=True)
class SportteryMapping:
    status: str
    reason: str | None
    row: SportteryInventoryRow | None


def sporttery_inventory_url(begin_date: str, end_date: str, *, page_no: int, page_size: int) -> str:
    return f"{SPORTTERY_INVENTORY_URL}?{urlencode({'matchBeginDate': begin_date, 'matchEndDate': end_date, 'pageSize': page_size, 'pageNo': page_no, 'clientCode': '3001'})}"


def sporttery_head_url(sporttery_match_id: str) -> str:
    return f"{SPORTTERY_HEAD_URL}?{urlencode({'source': 'web', 'sportteryMatchId': sporttery_match_id})}"


def sporttery_fixed_bonus_url(sporttery_match_id: str) -> str:
    return f"{SPORTTERY_FIXED_BONUS_URL}?{urlencode({'clientCode': '3001', 'matchId': sporttery_match_id})}"


def is_sporttery_blocked(content: bytes) -> bool:
    lowered = content.lower()
    markers = (
        b"tencent cloud edgeone",
        b"restricted access",
        b'id="statuscode">567',
        b"captcha",
        "安全策略拦截".encode("utf-8"),
        "验证码".encode("utf-8"),
    )
    return any(marker in lowered for marker in markers)


def official_scope_present(content: bytes) -> bool:
    if is_sporttery_blocked(content):
        return False
    for encoding in ("utf-8", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        compact = re.sub(r"\s+", "", text)
        return "全场比分（90分钟）" in compact and "伤停补时" in compact
    return False


def _payload(content: bytes) -> dict[str, Any]:
    if is_sporttery_blocked(content):
        raise SportteryEvidenceError("blocked", "official Sporttery response is blocked")
    try:
        decoded = content.decode("utf-8-sig")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SportteryEvidenceError("invalid_json", "official Sporttery response is not JSON") from exc
    if not isinstance(payload, dict):
        raise SportteryEvidenceError("invalid_json", "official Sporttery response root is not an object")
    error_code = str(payload.get("errorCode", "0"))
    if error_code not in {"0", "0000"}:
        raise SportteryEvidenceError("api_error", f"official Sporttery API returned {error_code}")
    return payload


def _score(value: Any) -> tuple[int, int] | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text in {"-1:-1", "VS", "vs"}:
        return None
    match = re.fullmatch(r"(\d+)\s*[:\-]\s*(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _beijing_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        except ValueError:
            pass
    try:
        return parse_iso(text).astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _truthy_cancel(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _rows_from_value(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in (
        "matchResult",
        "matchResultList",
        "list",
        "rows",
        "data",
        "records",
        "resultList",
    ):
        rows = _rows_from_value(value.get(key))
        if rows:
            return rows
    return []


def inventory_total_pages(payload: dict[str, Any], *, default: int) -> int:
    value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
    assert isinstance(value, dict)
    for key in ("totalPage", "totalPages", "pages", "pageCount"):
        raw = value.get(key)
        if raw not in (None, ""):
            try:
                return max(int(raw), 1)
            except (TypeError, ValueError):
                pass
    return default


def parse_inventory(content: bytes) -> list[SportteryInventoryRow]:
    payload = _payload(content)
    rows = _rows_from_value(payload.get("value"))
    parsed: list[SportteryInventoryRow] = []
    for row in rows:
        sporttery_id = _text(row, "sportteryMatchId", "matchId", "id")
        if not sporttery_id.isdigit():
            continue
        score = _score(_text(row, "fullCourtGoal", "sectionsNo999", "score", "matchResult"))
        parsed.append(
            SportteryInventoryRow(
                sporttery_match_id=sporttery_id,
                match_number=_text(row, "matchNum", "matchNumber", "matchNo"),
                kickoff_at=_beijing_time(_text(row, "matchDateTime", "matchTime", "matchDate")),
                home_name=_text(row, "homeTeamShortName", "homeTeamName", "homeTeamAllName"),
                away_name=_text(row, "awayTeamShortName", "awayTeamName", "awayTeamAllName"),
                score=score,
                status_text=_text(row, "matchStatus", "matchStatusCn", "status", "statusDesc"),
                result_status_text=_text(row, "matchResultStatus", "matchResultStatusCn", "resultStatus"),
                is_cancel=_truthy_cancel(row.get("isCancel")),
                raw=row,
            )
        )
    return parsed


def parse_detail(head_content: bytes, fixed_content: bytes) -> SportteryDetail:
    head_payload = _payload(head_content)
    fixed_payload = _payload(fixed_content)
    head = head_payload.get("value")
    fixed = fixed_payload.get("value")
    if not isinstance(head, dict) or not isinstance(fixed, dict):
        raise SportteryEvidenceError("invalid_detail", "official detail value is not an object")
    sporttery_id = _text(head, "sportteryMatchId") or _text(fixed, "matchId", "sportteryMatchId")
    if not sporttery_id.isdigit():
        raise SportteryEvidenceError("invalid_detail", "official detail has no sportteryMatchId")
    return SportteryDetail(
        sporttery_match_id=sporttery_id,
        match_number=_text(head, "matchNum", "matchNumber"),
        kickoff_at=_beijing_time(_text(head, "matchDateTime")),
        home_name=_text(head, "homeTeamShortName", "homeTeamName", "homeTeamAllName"),
        away_name=_text(head, "awayTeamShortName", "awayTeamName", "awayTeamAllName"),
        head_score=_score(_text(head, "fullCourtGoal")),
        fixed_score=_score(_text(fixed, "sectionsNo999", "fullCourtGoal")),
        is_cancel=_truthy_cancel(fixed.get("isCancel")),
        raw_head=head,
        raw_fixed=fixed,
    )


def _norm_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"\s+", "", normalized)


def _alias(value: Any, aliases: dict[str, str]) -> str:
    normalized = _norm_name(value)
    return aliases.get(normalized, normalized)


def accepted_mapping(
    fixture: dict[str, Any],
    rows: list[SportteryInventoryRow],
    *,
    aliases: dict[str, str] | None = None,
    kickoff_tolerance_minutes: int = 5,
) -> SportteryMapping:
    aliases = aliases or {}
    match_number = str(fixture.get("match_number") or "").strip()
    kickoff_text = fixture.get("kickoff_at")
    home_name = fixture.get("home_team_name")
    away_name = fixture.get("away_team_name")
    if not match_number or not kickoff_text or not home_name or not away_name:
        return SportteryMapping("rejected", "fixture_identity_incomplete", None)
    try:
        fixture_kickoff = parse_iso(str(kickoff_text)).astimezone(ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return SportteryMapping("rejected", "fixture_kickoff_invalid", None)
    candidates = [row for row in rows if row.match_number == match_number]
    if not candidates:
        return SportteryMapping("missing", "match_number_not_in_complete_inventory", None)
    if len(candidates) > 1:
        return SportteryMapping("rejected", "match_number_duplicate", None)
    row = candidates[0]
    if row.kickoff_at is None:
        return SportteryMapping("rejected", "official_kickoff_missing", row)
    if abs((row.kickoff_at - fixture_kickoff).total_seconds()) > kickoff_tolerance_minutes * 60:
        return SportteryMapping("rejected", "kickoff_mismatch", row)
    if _alias(row.home_name, aliases) != _alias(home_name, aliases):
        return SportteryMapping("rejected", "home_team_mismatch", row)
    if _alias(row.away_name, aliases) != _alias(away_name, aliases):
        return SportteryMapping("rejected", "away_team_mismatch", row)
    return SportteryMapping("accepted", None, row)


def make_scope_evidence(*, observed_at: datetime, page_blob: dict[str, Any], batch_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "SportteryScopeEvidence",
        "record_id": stable_record_id("sporttery_scope", page_blob["sha256"], batch_id),
        "observed_at": iso_utc(observed_at),
        "source_url": page_blob["url"],
        "source_sha256": page_blob["sha256"],
        "inventory_batch_record_id": batch_id,
        "scope": OFFICIAL_RESULT_SCOPE,
        "scope_text": OFFICIAL_SCOPE_TEXT,
        "status": "accepted",
    }


def make_inventory_batch(
    *,
    run_id: str,
    observed_at: datetime,
    begin_date: str,
    end_date: str,
    page_size: int,
    page_count: int,
    rows: list[SportteryInventoryRow],
    raw_blobs: list[dict[str, Any]],
    complete: bool,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "SportteryInventoryBatch",
        "record_id": stable_record_id("sporttery_inventory", begin_date, end_date, run_id),
        "run_id": run_id,
        "observed_at": iso_utc(observed_at),
        "begin_date": begin_date,
        "end_date": end_date,
        "page_size": page_size,
        "page_count": page_count,
        "row_count": len(rows),
        "complete": complete,
        "failure_reason": failure_reason,
        "raw_sha256s": [blob["sha256"] for blob in raw_blobs],
        "source_urls": [blob["url"] for blob in raw_blobs],
    }


def make_fixture_link(
    *,
    fixture_id: str,
    observed_at: datetime,
    inventory_batch_record_id: str,
    mapping: SportteryMapping,
    source_fixture_identity_record_id: str | None = None,
) -> dict[str, Any]:
    row = mapping.row
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "SportteryFixtureLink",
        "record_id": stable_record_id(
            "sporttery_link",
            fixture_id,
            row.sporttery_match_id if row else "",
            inventory_batch_record_id,
            mapping.status,
            mapping.reason or "",
        ),
        "fixture_id": fixture_id,
        "sporttery_match_id": row.sporttery_match_id if row else None,
        "observed_at": iso_utc(observed_at),
        "inventory_batch_record_id": inventory_batch_record_id,
        "source_fixture_identity_record_id": source_fixture_identity_record_id,
        "match_number": row.match_number if row else None,
        "mapping_status": mapping.status,
        "rejection_reason": mapping.reason,
        "official_kickoff_at": iso_utc(row.kickoff_at) if row and row.kickoff_at else None,
        "official_home_name": row.home_name if row else None,
        "official_away_name": row.away_name if row else None,
    }


def make_result_observation(
    *,
    fixture_id: str,
    observed_at: datetime,
    link_record_id: str,
    inventory_batch_record_id: str,
    scope_record_id: str,
    row: SportteryInventoryRow,
    detail: SportteryDetail,
    inventory_blob: dict[str, Any],
    head_blob: dict[str, Any],
    fixed_blob: dict[str, Any],
) -> dict[str, Any]:
    if row.score is None:
        raise SportteryEvidenceError("score_missing", "official inventory has no result score")
    if detail.head_score != row.score or detail.fixed_score != row.score:
        raise SportteryEvidenceError("detail_score_conflict", "official detail score does not match inventory")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "SportteryResultObservation",
        "record_id": stable_record_id(
            "sporttery_result",
            fixture_id,
            row.sporttery_match_id,
            row.score[0],
            row.score[1],
            inventory_blob["sha256"],
            head_blob["sha256"],
            fixed_blob["sha256"],
        ),
        "fixture_id": fixture_id,
        "sporttery_match_id": row.sporttery_match_id,
        "observed_at": iso_utc(observed_at),
        "home_goals": row.score[0],
        "away_goals": row.score[1],
        "status_text": row.status_text,
        "result_status_text": row.result_status_text,
        "is_cancel": detail.is_cancel if detail.is_cancel is not None else row.is_cancel,
        "scope": OFFICIAL_RESULT_SCOPE,
        "inventory_batch_record_id": inventory_batch_record_id,
        "scope_evidence_record_id": scope_record_id,
        "fixture_link_record_id": link_record_id,
        "inventory_sha256": inventory_blob["sha256"],
        "head_sha256": head_blob["sha256"],
        "fixed_bonus_sha256": fixed_blob["sha256"],
        "source_urls": [inventory_blob["url"], head_blob["url"], fixed_blob["url"]],
        "raw_summary": {
            "match_number": row.match_number,
            "home_name": row.home_name,
            "away_name": row.away_name,
            "head_home_name": detail.home_name,
            "head_away_name": detail.away_name,
        },
    }


def make_official_candidate(
    *,
    fixture_id: str,
    kickoff_at: str | None,
    observed_at: datetime,
    observation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "ResultCandidate",
        "record_id": stable_record_id(
            "result_candidate_sporttery",
            fixture_id,
            observation["home_goals"],
            observation["away_goals"],
            observation["record_id"],
        ),
        "fixture_id": fixture_id,
        "kickoff_at": kickoff_at,
        "home_goals": observation["home_goals"],
        "away_goals": observation["away_goals"],
        "half_time_score_raw": None,
        "status_code": observation.get("result_status_text"),
        "status_raw": observation.get("status_text"),
        "observed_at": iso_utc(observed_at),
        "scope": "official-90-minute-candidate",
        "official_scope": OFFICIAL_RESULT_SCOPE,
        "sporttery_result_observation_id": observation["record_id"],
        "sporttery_fixture_link_id": observation["fixture_link_record_id"],
        "live_page_sha256": observation["inventory_sha256"],
        "completed_page_sha256": observation["inventory_sha256"],
        "analysis_page_sha256": observation["head_sha256"],
        "analysis_consistency": "sporttery-head-fixed-passed",
        "source_urls": observation["source_urls"],
    }


def detail_consistent_with_mapping(row: SportteryInventoryRow, detail: SportteryDetail) -> bool:
    if row.sporttery_match_id != detail.sporttery_match_id:
        return False
    if row.match_number and detail.match_number and row.match_number != detail.match_number:
        return False
    if row.kickoff_at and detail.kickoff_at and abs((row.kickoff_at - detail.kickoff_at).total_seconds()) > 300:
        return False
    return _norm_name(row.home_name) == _norm_name(detail.home_name) and _norm_name(row.away_name) == _norm_name(detail.away_name)


def mapping_identity_valid(record: dict[str, Any]) -> bool:
    values = (
        record.get("match_number"),
        record.get("kickoff_at"),
        record.get("home_team_name"),
        record.get("away_team_name"),
    )
    mojibake_markers = ("\ufffd", "锟斤拷", "Ã", "Â")
    return all(value not in (None, "") for value in values) and not any(
        marker in str(value) for value in values for marker in mojibake_markers
    )


def load_mapping_identities(data_dir: Path, fixture_ids: set[str]) -> dict[str, dict[str, Any]]:
    selected: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in sorted((data_dir / "normalized").rglob("fixture_identities.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    fixture_id = str(record.get("fixture_id") or "")
                    if fixture_id not in fixture_ids or not mapping_identity_valid(record):
                        continue
                    try:
                        observed_at = parse_iso(str(record.get("observed_at") or ""))
                    except ValueError:
                        continue
                    current = selected.get(fixture_id)
                    if current is None or observed_at > current[0]:
                        selected[fixture_id] = (observed_at, record)
        except OSError:
            continue
    return {fixture_id: record for fixture_id, (_, record) in selected.items()}


def audit_sporttery_evidence(data_dir: Path, *, fixture_id: str | None = None) -> dict[str, Any]:
    relevant_types = {
        "SportteryScopeEvidence",
        "SportteryInventoryBatch",
        "SportteryFixtureLink",
        "SportteryResultObservation",
    }
    records: dict[str, dict[str, dict[str, Any]]] = {
        record_type: {} for record_type in relevant_types
    }
    official_candidates: dict[str, dict[str, Any]] = {}
    official_verified: dict[str, dict[str, Any]] = {}
    identity_records: dict[str, dict[str, Any]] = {}
    malformed: list[str] = []
    files_scanned = 0

    normalized_dir = data_dir / "normalized"
    for path in sorted(normalized_dir.rglob("*.jsonl")) if normalized_dir.is_dir() else ():
        files_scanned += 1
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        malformed.append(f"{path}:{line_number}:invalid_json")
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_type = str(record.get("record_type") or "")
                    record_id = str(record.get("record_id") or "")
                    if not record_id:
                        continue
                    if record_type in relevant_types:
                        records[record_type][record_id] = record
                    elif record_type == "FixtureIdentity":
                        identity_records[record_id] = record
                    elif record_type == "ResultCandidate" and record.get("official_scope") == OFFICIAL_RESULT_SCOPE:
                        official_candidates[record_id] = record
                    elif (
                        record_type == "VerifiedResult"
                        and record.get("verification_method") == "sporttery-official-90-minute"
                    ):
                        official_verified[record_id] = record
        except OSError as exc:
            malformed.append(f"{path}:read_error:{type(exc).__name__}")

    scopes = records["SportteryScopeEvidence"]
    batches = records["SportteryInventoryBatch"]
    links = records["SportteryFixtureLink"]
    observations = records["SportteryResultObservation"]
    errors = list(malformed)

    def selected(record: dict[str, Any]) -> bool:
        return fixture_id is None or str(record.get("fixture_id") or "") == fixture_id

    selected_links = {key: value for key, value in links.items() if selected(value)}
    selected_observations = {key: value for key, value in observations.items() if selected(value)}
    selected_candidates = {key: value for key, value in official_candidates.items() if selected(value)}
    selected_verified = {key: value for key, value in official_verified.items() if selected(value)}

    for record_id, link in selected_links.items():
        if link.get("mapping_status") != "accepted":
            continue
        identity = identity_records.get(
            str(link.get("source_fixture_identity_record_id") or "")
        )
        if not identity:
            errors.append(f"{record_id}:missing_source_fixture_identity")
        elif str(identity.get("fixture_id")) != str(link.get("fixture_id")):
            errors.append(f"{record_id}:source_fixture_identity_mismatch")

    for record_id, observation in selected_observations.items():
        scope = scopes.get(str(observation.get("scope_evidence_record_id") or ""))
        batch = batches.get(str(observation.get("inventory_batch_record_id") or ""))
        link = links.get(str(observation.get("fixture_link_record_id") or ""))
        if not scope or scope.get("status") != "accepted" or scope.get("scope") != OFFICIAL_RESULT_SCOPE:
            errors.append(f"{record_id}:missing_accepted_scope")
        if not batch or batch.get("complete") is not True:
            errors.append(f"{record_id}:missing_complete_inventory")
        if not link or link.get("mapping_status") != "accepted":
            errors.append(f"{record_id}:missing_accepted_fixture_link")
        elif str(link.get("fixture_id")) != str(observation.get("fixture_id")):
            errors.append(f"{record_id}:fixture_link_mismatch")

    for record_id, candidate in selected_candidates.items():
        observation = observations.get(str(candidate.get("sporttery_result_observation_id") or ""))
        link = links.get(str(candidate.get("sporttery_fixture_link_id") or ""))
        if not observation:
            errors.append(f"{record_id}:missing_result_observation")
        elif (
            observation.get("home_goals"),
            observation.get("away_goals"),
            str(observation.get("fixture_id")),
        ) != (
            candidate.get("home_goals"),
            candidate.get("away_goals"),
            str(candidate.get("fixture_id")),
        ):
            errors.append(f"{record_id}:observation_score_or_fixture_mismatch")
        if not link or link.get("mapping_status") != "accepted":
            errors.append(f"{record_id}:missing_candidate_fixture_link")

    for record_id, verified in selected_verified.items():
        candidate = official_candidates.get(str(verified.get("candidate_id") or ""))
        if not candidate:
            errors.append(f"{record_id}:missing_official_candidate")
        elif (
            candidate.get("home_goals"),
            candidate.get("away_goals"),
            str(candidate.get("fixture_id")),
        ) != (
            verified.get("home_goals"),
            verified.get("away_goals"),
            str(verified.get("fixture_id")),
        ):
            errors.append(f"{record_id}:candidate_score_or_fixture_mismatch")

    fixture_ids = sorted(
        {
            str(record.get("fixture_id"))
            for group in (selected_links, selected_observations, selected_candidates, selected_verified)
            for record in group.values()
            if record.get("fixture_id") is not None
        }
    )
    incomplete_batches = sum(1 for record in batches.values() if record.get("complete") is not True)
    nonaccepted_links = sum(
        1 for record in selected_links.values() if record.get("mapping_status") != "accepted"
    )
    warnings = []
    if incomplete_batches:
        warnings.append(f"incomplete_inventory_batches:{incomplete_batches}")
    if nonaccepted_links:
        warnings.append(f"nonaccepted_fixture_links:{nonaccepted_links}")
    if not selected_verified:
        warnings.append("no_official_verified_results")
    status = "failed" if errors else ("ok" if selected_verified else "warning")
    return {
        "status": status,
        "fixture_id": fixture_id,
        "files_scanned": files_scanned,
        "counts": {
            "scope_evidence": len(scopes),
            "inventory_batches": len(batches),
            "fixture_links": len(selected_links),
            "result_observations": len(selected_observations),
            "official_candidates": len(selected_candidates),
            "official_verified_results": len(selected_verified),
            "incomplete_inventory_batches": incomplete_batches,
            "nonaccepted_fixture_links": nonaccepted_links,
            "referenced_fixture_identities": len(
                {
                    str(record.get("source_fixture_identity_record_id"))
                    for record in selected_links.values()
                    if record.get("source_fixture_identity_record_id")
                }
            ),
        },
        "fixture_ids": fixture_ids,
        "errors": errors,
        "warnings": warnings,
    }
