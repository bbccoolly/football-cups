from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .config import CORE_MARKETS, CollectorConfig
from .http import ObservedResponse
from .markets import (
    NORMALIZATION_VERSION,
    PARSER_VERSION,
    parse_market_html_v2,
    parse_market_workbook,
    parse_market_workbook_v2,
)
from .state import StateStore
from .storage import json_dumps, stable_record_id
from .timeutil import iso_utc, parse_iso, utc_now


REPARSE_ALGORITHM_VERSION = 2


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return payload


def _blob_content(config: CollectorConfig, blob: dict[str, Any]) -> bytes:
    relative = blob.get("path")
    if not isinstance(relative, str):
        raise ValueError("raw blob has no path")
    path = config.data_dir / relative
    content = path.read_bytes()
    if len(content) != int(blob.get("size_bytes", -1)):
        raise ValueError(f"raw blob size mismatch: {relative}")
    if _sha256(content) != blob.get("sha256"):
        raise ValueError(f"raw blob hash mismatch: {relative}")
    return content


def _observed_response(blob: dict[str, Any], content: bytes) -> ObservedResponse:
    return ObservedResponse(
        method=str(blob.get("method") or "GET"),
        url=str(blob.get("url") or ""),
        status_code=int(blob.get("http_status") or 0),
        headers=dict(blob.get("headers") or {}),
        content=content,
        request_started_at=parse_iso(str(blob["request_started_at"])),
        response_received_at=parse_iso(str(blob["observed_at"])),
        source_encoding=str(blob.get("source_encoding") or "unknown"),
    )


def _row_numeric_signature(row: dict[str, Any], market: str) -> tuple[Any, ...]:
    labels = ("home", "away") if market == "yazhi" else ("over", "under")
    values: list[Any] = []
    for section in ("current", "opening"):
        container = row.get(section) or {}
        for label in labels:
            value = (container.get(label) or {}).get("decimal")
            values.append(Decimal(str(value)) if value is not None else None)
    return tuple(values)


def _parity_matches(legacy_rows: list[dict[str, Any]], v2_rows: list[dict[str, Any]], market: str) -> bool:
    legacy = Counter(_row_numeric_signature(row, market) for row in legacy_rows)
    current = Counter(_row_numeric_signature(row, market) for row in v2_rows)
    return all(current[signature] >= count for signature, count in legacy.items())


def _capture_blob(capture: dict[str, Any], predicate) -> dict[str, Any] | None:
    for blob in capture.get("raw_blobs") or []:
        if isinstance(blob, dict) and predicate(str(blob.get("url") or "")):
            return blob
    return None


def _assessment(
    batch: dict[str, Any],
    normalizations: dict[str, dict[str, Any]],
    assessed_at: datetime,
) -> dict[str, Any]:
    fixture_id = str(batch["fixture_id"])
    market_stats: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for market in sorted(CORE_MARKETS):
        normalization = normalizations.get(market)
        stats = {
            "status": normalization.get("status") if normalization else "missing",
            "normalization_record_id": normalization.get("record_id") if normalization else None,
            "valid_bookmaker_rows": int(
                normalization.get("valid_bookmaker_rows", 0) if normalization else 0
            ),
        }
        market_stats[market] = stats
        if stats["status"] != "accepted":
            reasons.append(f"{market}:normalization_not_accepted")
        if stats["valid_bookmaker_rows"] < 3:
            reasons.append(f"{market}:insufficient_complete_bookmakers")
    collection_eligible = bool(batch.get("strict_eligible"))
    data_complete = not reasons
    if not collection_eligible:
        reasons.insert(0, "collection_not_strict_eligible")
    record_id = stable_record_id(
        "snapshot_eligibility_assessment",
        batch["record_id"],
        NORMALIZATION_VERSION,
        json_dumps(market_stats),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "SnapshotEligibilityAssessment",
        "record_id": record_id,
        "fixture_id": fixture_id,
        "snapshot_batch_record_id": batch["record_id"],
        "target": batch.get("target"),
        "assessment_version": NORMALIZATION_VERSION,
        "assessed_at": iso_utc(assessed_at),
        "collection_eligible": collection_eligible,
        "data_complete": data_complete,
        "model_strict_eligible": collection_eligible and data_complete,
        "market_stats": market_stats,
        "ineligibility_reasons": reasons,
        "event_origin": "reprocess",
    }


def _selected_manifests(
    config: CollectorConfig, start: datetime, end: datetime
) -> list[tuple[Path, bytes, dict[str, Any]]]:
    selected: list[tuple[Path, bytes, dict[str, Any]]] = []
    for path in sorted(config.data_dir.joinpath("manifests").rglob("*-market.json")):
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("record_type") != "MarketCaptureManifest":
            continue
        if (payload.get("eligibility_assessment") or {}).get("assessment_version") == 2:
            continue
        batch = payload.get("batch") or {}
        completed = batch.get("completed_at")
        if completed and start <= parse_iso(str(completed)) < end:
            selected.append((path, raw, payload))
    return selected


def collect_market_reparse(
    config: CollectorConfig,
    *,
    start: datetime,
    end: datetime,
    normalized_at: datetime | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], str]:
    if end <= start:
        raise ValueError("until must be after since")
    selected = _selected_manifests(config, start, end)
    input_digest = hashlib.sha256()
    input_digest.update(f"reparse-algorithm-v{REPARSE_ALGORITHM_VERSION}".encode("ascii"))
    input_digest.update(PARSER_VERSION.encode("ascii"))
    input_digest.update(iso_utc(start).encode("ascii"))
    input_digest.update(iso_utc(end).encode("ascii"))
    for path, raw, _ in selected:
        input_digest.update(path.relative_to(config.data_dir).as_posix().encode("utf-8"))
        input_digest.update(hashlib.sha256(raw).digest())
    repair_id = f"market-v2-{input_digest.hexdigest()[:24]}"
    assessed = normalized_at or utc_now()
    records: dict[str, list[dict[str, Any]]] = {
        "market_normalizations": [],
        "bookmaker_market_rows": [],
        "handicap_index_rows": [],
        "snapshot_eligibility_assessments": [],
    }
    counters: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    with StateStore(config) as state:
        fixture_lookup = {str(item["fixture_id"]): item for item in state.all_fixtures()}
    seen_records: set[str] = set()
    batch_contexts: dict[str, dict[str, Any]] = {}
    for _, _, manifest in selected:
        batch = manifest.get("batch") or {}
        fixture_id = str(batch.get("fixture_id") or (manifest.get("job") or {}).get("fixture_id"))
        if not fixture_id.isdigit() or not batch.get("record_id"):
            counters["invalid_manifests"] += 1
            continue
        batch_record_id = str(batch["record_id"])
        context = batch_contexts.get(batch_record_id)
        if context is None:
            context = {
                "batch": batch,
                "fixture_id": fixture_id,
                "normalizations": {},
            }
            batch_contexts[batch_record_id] = context
        else:
            prior_batch = context["batch"]
            if context["fixture_id"] != fixture_id or prior_batch.get("target") != batch.get(
                "target"
            ):
                raise ValueError(f"conflicting manifests for snapshot batch {batch_record_id}")
            if parse_iso(str(batch["completed_at"])) >= parse_iso(
                str(prior_batch["completed_at"])
            ):
                context["batch"] = batch
        fixture = fixture_lookup.get(fixture_id, {})
        kickoff_at = fixture.get("kickoff_at")
        normalizations: dict[str, dict[str, Any]] = context["normalizations"]
        for capture in manifest.get("captures") or []:
            if not isinstance(capture, dict) or capture.get("status") != "success":
                continue
            market = str(capture.get("market") or "")
            result = (batch.get("market_results") or {}).get(market) or {}
            snapshot_record_id = result.get("snapshot_record_id")
            observed_text = result.get("observed_at")
            if market not in {"ouzhi", "yazhi", "daxiao", "rangqiu"} or not snapshot_record_id or not observed_text:
                continue
            observed = parse_iso(str(observed_text))
            try:
                if market == "ouzhi":
                    workbook_blob = _capture_blob(capture, lambda url: "europe_xls.php" in url)
                    if workbook_blob is None:
                        raise ValueError("missing European odds workbook blob")
                    workbook = _blob_content(config, workbook_blob)
                    _, rows, normalization = parse_market_workbook_v2(
                        workbook,
                        fixture_id=fixture_id,
                        market=market,
                        target=str(batch.get("target")),
                        observed_at=observed,
                        kickoff_at=kickoff_at,
                        timezone_name=config.timezone_name,
                        raw_sha256=str(workbook_blob["sha256"]),
                        normalized_at=assessed,
                        source_snapshot_record_id=str(snapshot_record_id),
                        reprocessed=True,
                    )
                else:
                    page_blob = _capture_blob(capture, lambda url: url.endswith(".shtml"))
                    if page_blob is None:
                        raise ValueError("missing market HTML blob")
                    page_content = _blob_content(config, page_blob)
                    page_response = _observed_response(page_blob, page_content)
                    _, rows, normalization = parse_market_html_v2(
                        page_response,
                        fixture_id=fixture_id,
                        market=market,
                        target=str(batch.get("target")),
                        kickoff_at=kickoff_at,
                        timezone_name=config.timezone_name,
                        raw_sha256=str(page_blob["sha256"]),
                        normalized_at=assessed,
                        source_snapshot_record_id=str(snapshot_record_id),
                        snapshot_observed_at=observed,
                        reprocessed=True,
                    )
                    workbook_blob = _capture_blob(
                        capture,
                        lambda url: "xls.php" in url or "rangqiu_xls.php" in url,
                    )
                    if market in {"yazhi", "daxiao"} and workbook_blob is not None:
                        workbook = _blob_content(config, workbook_blob)
                        _, legacy_rows = parse_market_workbook(
                            workbook,
                            fixture_id=fixture_id,
                            market=market,
                            target=str(batch.get("target")),
                            observed_at=observed,
                            kickoff_at=kickoff_at,
                            timezone_name=config.timezone_name,
                            raw_sha256=str(workbook_blob["sha256"]),
                        )
                        normalization["source_workbook_sha256"] = workbook_blob["sha256"]
                        for row in rows:
                            row["source_workbook_sha256"] = workbook_blob["sha256"]
                        if not _parity_matches(legacy_rows, rows, market):
                            normalization["status"] = "rejected"
                            normalization["quality_reasons"].append("html_excel_numeric_mismatch")
                            rows = []
                            counters["parity_failures"] += 1
                prior_normalization = normalizations.get(market)
                if prior_normalization is None or parse_iso(
                    str(normalization["snapshot_observed_at"])
                ) >= parse_iso(str(prior_normalization["snapshot_observed_at"])):
                    normalizations[market] = normalization
                for record in [normalization, *rows]:
                    if record["record_id"] in seen_records:
                        continue
                    seen_records.add(record["record_id"])
                    stream = {
                        "MarketNormalization": "market_normalizations",
                        "BookmakerMarketRow": "bookmaker_market_rows",
                        "HandicapIndexRow": "handicap_index_rows",
                    }[record["record_type"]]
                    records[stream].append(record)
                counters[f"{market}_normalizations"] += 1
                counters[f"{market}_rows"] += len(rows)
                counters[f"{market}_valid_bookmaker_rows"] += int(
                    normalization.get("valid_bookmaker_rows") or 0
                )
                counters[f"{market}_line_parse_failures"] += int(
                    normalization.get("line_parse_failure_count") or 0
                )
                counters[f"{market}_source_event_time_rows"] += int(
                    normalization.get("source_event_time_rows") or 0
                )
                if normalization["status"] != "accepted":
                    counters["rejected_normalizations"] += 1
                for reason in normalization.get("quality_reasons") or []:
                    reasons[str(reason)] += 1
            except (OSError, ValueError, RuntimeError) as exc:
                counters["capture_failures"] += 1
                reasons[f"{market}:{type(exc).__name__}:{exc}"] += 1
    for batch_record_id in sorted(batch_contexts):
        context = batch_contexts[batch_record_id]
        assessment = _assessment(context["batch"], context["normalizations"], assessed)
        if assessment["record_id"] not in seen_records:
            seen_records.add(assessment["record_id"])
            records["snapshot_eligibility_assessments"].append(assessment)
        counters["assessments"] += 1
        if assessment["model_strict_eligible"]:
            counters["model_strict_eligible"] += 1
        for reason in assessment["ineligibility_reasons"]:
            reasons[str(reason)] += 1
    summary = {
        "status": "ready" if not counters["capture_failures"] else "partial",
        "repair_id": repair_id,
        "reparse_algorithm_version": REPARSE_ALGORITHM_VERSION,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "since": iso_utc(start),
        "until": iso_utc(end),
        "manifests_seen": len(selected),
        "counts": dict(sorted(counters.items())),
        "reasons": dict(sorted(reasons.items())),
        "records": {name: len(items) for name, items in records.items()},
        "metrics": {
            "market_data_complete_rate": (
                counters["model_strict_eligible"] / counters["assessments"]
                if counters["assessments"]
                else None
            ),
            "mojibake_detected_count": sum(
                count for reason, count in reasons.items() if "mojibake" in reason
            ),
        },
        "network_requests": 0,
    }
    return records, summary, repair_id


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    content = "".join(json_dumps(record) + "\n" for record in records).encode("utf-8")
    path.write_bytes(content)
    return {"name": path.name, "size_bytes": len(content), "sha256": _sha256(content)}


def _validate_published_repair(run_dir: Path) -> None:
    manifest_path = run_dir / "manifest.json"
    complete_path = run_dir / "complete.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = _read_json(manifest_path)
    complete = _read_json(complete_path)
    if complete.get("status") != "completed" or complete.get("manifest_sha256") != _sha256(
        manifest_raw
    ):
        raise ValueError(f"repair completion evidence is invalid: {run_dir}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError(f"repair file inventory is invalid: {run_dir}")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError(f"repair file inventory entry is invalid: {run_dir}")
        name = str(item["name"])
        if Path(name).name != name:
            raise ValueError(f"unsafe repair file name: {name}")
        raw = (run_dir / name).read_bytes()
        if len(raw) != item.get("size_bytes") or _sha256(raw) != item.get("sha256"):
            raise ValueError(f"repair file integrity check failed: {name}")


def _repair_markdown(summary: dict[str, Any]) -> str:
    counts = summary.get("counts") or {}
    records = summary.get("records") or {}
    lines = [
        "# 盘口标准化 V2 修复报告",
        "",
        f"- 修复批次：`{summary['repair_id']}`",
        f"- 输入窗口：`{summary['since']}` 至 `{summary['until']}`",
        f"- 网络请求：{summary.get('network_requests', 0)}",
        f"- 市场 manifest：{summary.get('manifests_seen', 0)}",
        f"- 模型严格合格批次：{counts.get('model_strict_eligible', 0)}",
        f"- 乱码检测：{(summary.get('metrics') or {}).get('mojibake_detected_count', 0)}",
        "",
        "## 记录数量",
        "",
    ]
    lines.extend(f"- `{name}`：{count}" for name, count in sorted(records.items()))
    lines.extend(["", "## 隔离原因", ""])
    reasons = summary.get("reasons") or {}
    lines.extend(f"- `{name}`：{count}" for name, count in sorted(reasons.items()))
    if not reasons:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def publish_market_reparse(
    config: CollectorConfig,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    normalized_at = utc_now()
    records, summary, repair_id = collect_market_reparse(
        config, start=start, end=end, normalized_at=normalized_at
    )
    final_dir = config.data_dir / "normalized" / "repairs" / repair_id
    if final_dir.exists():
        _validate_published_repair(final_dir)
        report_dir = config.data_dir / "reports" / "repairs"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{repair_id}.json"
        report_markdown = report_dir / f"{repair_id}.md"
        if not report_path.is_file():
            report_path.write_text(
                json_dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        if not report_markdown.is_file():
            report_markdown.write_text(
                _repair_markdown(summary), encoding="utf-8", newline="\n"
            )
        return summary | {
            "status": "unchanged",
            "path": str(final_dir),
            "report": str(report_path),
            "report_markdown": str(report_markdown),
        }
    stage_dir = config.data_dir / "state" / "reparse-staging" / repair_id
    if stage_dir.exists():
        raise ValueError(f"stale repair staging directory exists: {stage_dir}")
    stage_dir.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    file_names = {
        "market_normalizations": "market_normalizations.jsonl",
        "bookmaker_market_rows": "bookmaker_market_rows.jsonl",
        "handicap_index_rows": "handicap_index_rows.jsonl",
        "snapshot_eligibility_assessments": "snapshot_eligibility_assessments.jsonl",
    }
    for stream, name in file_names.items():
        files.append(_write_jsonl(stage_dir / name, records[stream]))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "MarketReparseRun",
        "run_id": repair_id,
        "parser_version": PARSER_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "started_at": iso_utc(normalized_at),
        "finished_at": iso_utc(utc_now()),
        "status": "completed",
        "summary": summary,
        "files": files,
    }
    manifest_raw = (json_dumps(manifest, indent=2) + "\n").encode("utf-8")
    (stage_dir / "manifest.json").write_bytes(manifest_raw)
    complete = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "MarketReparseComplete",
        "run_id": repair_id,
        "status": "completed",
        "manifest_sha256": _sha256(manifest_raw),
        "completed_at": iso_utc(utc_now()),
    }
    (stage_dir / "complete.json").write_text(
        json_dumps(complete, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _validate_published_repair(stage_dir)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage_dir, final_dir)
    report_dir = config.data_dir / "reports" / "repairs"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{repair_id}.json"
    report_path.write_text(json_dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    report_markdown = report_dir / f"{repair_id}.md"
    report_markdown.write_text(_repair_markdown(summary), encoding="utf-8", newline="\n")
    return summary | {
        "status": "completed",
        "path": str(final_dir),
        "report": str(report_path),
        "report_markdown": str(report_markdown),
    }


def audit_market_data(config: CollectorConfig) -> dict[str, Any]:
    start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    records, summary, _ = collect_market_reparse(config, start=start, end=utc_now())
    del records
    return summary | {"status": "audited"}
