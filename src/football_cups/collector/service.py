from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import SCHEMA_VERSION
from .backup import run_backup
from .config import CORE_MARKETS, DISCOVERY_SOURCES, MARKETS, CollectorConfig
from .discovery import merge_discovery_pages, parse_discovery_page
from .http import CollectorHttpError, ObservedResponse, RateLimitedHttpClient
from .markets import MarketCollector
from .reporting import write_daily_report
from .results import (
    import_verified_results,
    load_competition_formats,
    make_candidate,
    make_verified_result,
    parse_analysis_page,
    parse_completed_page,
)
from .state import StateStore
from .storage import DataStore, json_dumps, make_run_id, stable_record_id
from .timeutil import iso_utc, parse_http_date, parse_iso, utc_now


LOGGER = logging.getLogger(__name__)
COMPLETED_URL = "https://live.500.com/wanchang.php"


class CriticalCollectorError(RuntimeError):
    pass


class CollectorService:
    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.config.ensure_directories()
        self.data = DataStore(config)
        self.state = StateStore(config)
        self.http = RateLimitedHttpClient(config)
        self.markets = MarketCollector(config, self.http, self.data)

    def close(self) -> None:
        self.state.close()

    def __enter__(self) -> "CollectorService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def emit_quality(
        self,
        event_type: str,
        status: str,
        details: dict[str, Any],
        *,
        at: datetime | None = None,
        fixture_id: str | None = None,
        competition: str | None = None,
        market: str | None = None,
        cutoff: str | None = None,
    ) -> dict[str, Any]:
        occurred = at or utc_now()
        event_id = self.state.add_event(
            event_type,
            status,
            details,
            occurred_at=occurred,
            fixture_id=fixture_id,
            competition=competition,
            market=market,
            cutoff=cutoff,
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "QualityEvent",
            "record_id": event_id,
            "event_type": event_type,
            "status": status,
            "occurred_at": iso_utc(occurred),
            "fixture_id": fixture_id,
            "competition": competition,
            "market": market,
            "cutoff": cutoff,
            "details": details,
        }
        if self.state.claim_record(event_id, "QualityEvent", occurred):
            self.data.append_normalized("quality_events", record, occurred)
        return record

    def _observe_http(self, blob: dict[str, Any], *, context: str) -> bool:
        observed = parse_iso(blob["observed_at"])
        success = 200 <= int(blob["http_status"]) < 300
        self.emit_quality(
            "http_request",
            "success" if success else "failure",
            {
                "context": context,
                "url": blob["url"],
                "http_status": blob["http_status"],
                "sha256": blob["sha256"],
            },
            at=observed,
        )
        http_date = parse_http_date(blob.get("headers", {}).get("date"))
        if http_date is None:
            return self._clock_is_recent(observed) if context.startswith("market:") else True
        drift = abs((observed - http_date).total_seconds())
        if drift > self.config.clock_drift_limit_seconds:
            if context.startswith("discovery:"):
                self.emit_quality(
                    "clock_drift",
                    "critical",
                    {"url": blob["url"], "drift_seconds": round(drift, 3)},
                    at=observed,
                )
                return False
            self.emit_quality(
                "source_http_date_stale",
                "warning",
                {"url": blob["url"], "drift_seconds": round(drift, 3), "context": context},
                at=observed,
            )
            return self._clock_is_recent(observed)
        if context.startswith("discovery:"):
            self.state.set_meta("last_clock_check_at", iso_utc(observed))
        return True

    def _clock_is_recent(self, now: datetime) -> bool:
        checked_at = self.state.get_meta("last_clock_check_at")
        return bool(checked_at and now - parse_iso(checked_at) <= timedelta(minutes=60))

    def discover(self, *, now: datetime | None = None) -> dict[str, Any]:
        started = now or utc_now()
        run_id = make_run_id(started)
        self.state.start_run(run_id, "discover", started)
        pages = []
        sources: list[dict[str, Any]] = []
        all_clock_ok = True
        try:
            for source_name, source_url in DISCOVERY_SOURCES:
                source_entry: dict[str, Any] = {"name": source_name, "url": source_url}
                try:
                    response = self.http.request("GET", source_url)
                    blob = self.data.store_response(response, default_extension="html")
                    source_entry["raw_blob"] = blob
                    all_clock_ok = self._observe_http(blob, context=f"discovery:{source_name}") and all_clock_ok
                    if not response.ok:
                        source_entry.update(status="http_failure", error=f"HTTP {response.status_code}")
                        sources.append(source_entry)
                        continue
                    parsed = parse_discovery_page(
                        response.content,
                        source_name=source_name,
                        source_url=source_url,
                        observed_at=response.response_received_at,
                        timezone_name=self.config.timezone_name,
                        source_encoding=response.source_encoding,
                    )
                    pages.append(parsed)
                    source_entry.update(
                        status="success" if not parsed.errors else "parser_partial",
                        regex_fixture_ids=sorted(parsed.regex_fixture_ids),
                        dom_fixture_ids=sorted(parsed.dom_fixture_ids),
                        fixture_count=len(parsed.dom_fixture_ids),
                        errors=parsed.errors,
                    )
                    self.emit_quality(
                        "parser",
                        "success" if not parsed.errors else "failure",
                        {"context": f"discovery:{source_name}", "errors": parsed.errors},
                        at=response.response_received_at,
                    )
                    if not parsed.inventory_matches:
                        self.emit_quality(
                            "inventory_mismatch",
                            "failure",
                            {
                                "source_name": source_name,
                                "regex_only": sorted(parsed.regex_fixture_ids - parsed.dom_fixture_ids),
                                "dom_only": sorted(parsed.dom_fixture_ids - parsed.regex_fixture_ids),
                            },
                            at=response.response_received_at,
                        )
                    for record in [*parsed.fixtures, *parsed.pools]:
                        if self.state.claim_record(
                            record["record_id"], record["record_type"], response.response_received_at
                        ):
                            stream = (
                                "discovery_observations"
                                if record["record_type"] == "DiscoveryObservation"
                                else "sporttery_pool_observations"
                            )
                            self.data.append_normalized(stream, record, response.response_received_at)
                    sources.append(source_entry)
                except CollectorHttpError as exc:
                    source_entry.update(status="request_failure", error=str(exc))
                    sources.append(source_entry)
                    self.emit_quality(
                        "http_request",
                        "failure",
                        {"context": f"discovery:{source_name}", "url": source_url, "error": str(exc)},
                    )

            identities, conflicts = merge_discovery_pages(pages)
            latest_observed = max(
                (parse_iso(item["raw_blob"]["observed_at"]) for item in sources if item.get("raw_blob")),
                default=started,
            )
            for fixture_id, identity in identities.items():
                status = self.state.upsert_fixture(
                    identity,
                    latest_observed,
                    identity_conflict=fixture_id in conflicts,
                )
                identity_record = identity | {
                    "record_id": stable_record_id(
                        "fixture_identity", fixture_id, json_dumps(identity), iso_utc(latest_observed)
                    ),
                    "observed_at": iso_utc(latest_observed),
                    "identity_status": status,
                }
                if self.state.claim_record(
                    identity_record["record_id"], "FixtureIdentity", latest_observed
                ):
                    self.data.append_normalized("fixture_identities", identity_record, latest_observed)
                if fixture_id in conflicts:
                    self.emit_quality(
                        "identity_conflict",
                        "failure",
                        {"conflicts": conflicts[fixture_id]},
                        at=latest_observed,
                        fixture_id=fixture_id,
                        competition=identity.get("competition_name"),
                    )
                if status in {"new", "kickoff_changed"}:
                    self.state.schedule_fixture(identity, latest_observed, is_new=status == "new")

            full = (
                len(pages) == len(DISCOVERY_SOURCES)
                and all(not page.errors and page.inventory_matches for page in pages)
                and all_clock_ok
            )
            finished = utc_now()
            summary = {
                "schema_version": SCHEMA_VERSION,
                "record_type": "DiscoveryRun",
                "run_id": run_id,
                "started_at": iso_utc(started),
                "finished_at": iso_utc(finished),
                "status": "full" if full else "partial",
                "sources": sources,
                "fixture_count": len(identities),
                "fixture_ids": sorted(identities),
                "fixtures": list(identities.values()),
                "identity_conflicts": conflicts,
            }
            discovery_path = self.data.write_discovery_summary(run_id, summary, finished)
            manifest_path = self.data.write_manifest("discovery", run_id, summary, finished)
            summary["discovery_path"] = str(discovery_path)
            summary["manifest_path"] = str(manifest_path)
            self.emit_quality(
                "discovery_poll",
                "full" if full else "partial",
                {"fixture_count": len(identities), "source_count": len(pages), "run_id": run_id},
                at=finished,
            )
            if full:
                self.state.set_meta("last_full_discovery_at", iso_utc(finished))
            self.state.finish_run(run_id, "success" if full else "partial", summary, finished)
            return summary
        except Exception as exc:
            finished = utc_now()
            self.state.finish_run(
                run_id,
                "failure",
                {"error": f"{type(exc).__name__}: {exc}"},
                finished,
            )
            raise

    def _disk_status(self) -> str:
        usage = shutil.disk_usage(self.config.data_dir)
        warning = max(int(usage.total * 0.20), 50 * 1024**3)
        critical = max(int(usage.total * 0.10), 20 * 1024**3)
        if usage.free < critical:
            self.emit_quality(
                "disk_space",
                "critical",
                {"free_bytes": usage.free, "total_bytes": usage.total, "critical_bytes": critical},
            )
            return "critical"
        if usage.free < warning:
            self.emit_quality(
                "disk_space",
                "warning",
                {"free_bytes": usage.free, "total_bytes": usage.total, "warning_bytes": warning},
            )
            return "warning"
        return "ok"

    def _process_market_job(self, job: dict[str, Any]) -> str:
        now = utc_now()
        payload = job["payload"]
        fixture = payload["fixture"]
        fixture_id = str(fixture["fixture_id"])
        competition = fixture.get("competition_name") or "unknown"
        results: dict[str, Any] = payload.setdefault("market_results", {})
        captures_for_manifest: list[dict[str, Any]] = []
        retryable_failure = False
        for market in MARKETS:
            if results.get(market, {}).get("status") == "success":
                continue
            capture = self.markets.collect(fixture, market, str(job["target"]))
            clock_ok = True
            for blob in capture.raw_blobs:
                clock_ok = self._observe_http(blob, context=f"market:{market}") and clock_ok
            if capture.snapshot:
                ingested = utc_now()
                capture.snapshot["ingested_at"] = iso_utc(ingested)
                capture.snapshot["clock_ok"] = clock_ok
                if self.state.claim_record(
                    capture.snapshot["record_id"], "MarketSnapshot", ingested
                ):
                    self.data.append_normalized("market_snapshots", capture.snapshot, ingested)
                for row in capture.rows:
                    if self.state.claim_record(row["record_id"], "BookmakerMarketRow", ingested):
                        self.data.append_normalized("bookmaker_market_rows", row, ingested)
                result = {
                    "status": "success",
                    "snapshot_record_id": capture.snapshot["record_id"],
                    "observed_at": capture.snapshot["observed_at"],
                    "clock_ok": clock_ok,
                    "row_count": capture.snapshot["row_count"],
                    "bookmaker_count": capture.snapshot["bookmaker_count"],
                }
                event_status = "success"
            else:
                result = {
                    "status": "failed",
                    "error_type": capture.error_type,
                    "error": capture.error,
                }
                event_status = capture.error_type or "failure"
                retryable_failure = retryable_failure or (
                    market in CORE_MARKETS
                    and capture.error_type not in {"source_market_unavailable", "invalid_excel"}
                )
            results[market] = result
            captures_for_manifest.append(
                {
                    "market": market,
                    "status": capture.status,
                    "error_type": capture.error_type,
                    "error": capture.error,
                    "raw_blobs": capture.raw_blobs,
                    "snapshot_record_id": capture.snapshot["record_id"] if capture.snapshot else None,
                }
            )
            self.emit_quality(
                "market_capture",
                event_status,
                result,
                fixture_id=fixture_id,
                competition=competition,
                market=market,
                cutoff=str(job["target"]),
            )
            self.state.update_job_payload(job["job_id"], payload, utc_now())

        window_start = parse_iso(job["window_start"]) if job.get("window_start") else None
        window_end = parse_iso(job["window_end"]) if job.get("window_end") else None
        core_success = all(results.get(market, {}).get("status") == "success" for market in CORE_MARKETS)
        within_window = True
        if window_start and window_end:
            within_window = all(
                window_start <= parse_iso(results[market]["observed_at"]) <= window_end
                for market in CORE_MARKETS
                if results.get(market, {}).get("status") == "success"
            ) and core_success
        fixture_state = self.state.fixtures_by_ids([fixture_id]).get(fixture_id, {})
        strict_eligible = (
            str(job["target"]) != "first_seen"
            and core_success
            and within_window
            and not bool(fixture_state.get("identity_conflict"))
            and all(results[market].get("clock_ok", False) for market in CORE_MARKETS)
        )
        finished = utc_now()
        batch = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "SnapshotBatch",
            "record_id": stable_record_id("snapshot_batch", job["job_id"], json_dumps(results)),
            "job_id": job["job_id"],
            "fixture_id": fixture_id,
            "target": job["target"],
            "window_start": job.get("window_start"),
            "window_end": job.get("window_end"),
            "completed_at": iso_utc(finished),
            "market_results": results,
            "core_market_complete": core_success,
            "strict_eligible": strict_eligible,
        }
        if self.state.claim_record(batch["record_id"], "SnapshotBatch", finished):
            self.data.append_normalized("snapshot_batches", batch, finished)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "MarketCaptureManifest",
            "run_id": make_run_id(finished),
            "job": {key: job.get(key) for key in ("job_id", "fixture_id", "target", "window_start", "window_end")},
            "captures": captures_for_manifest,
            "batch": batch,
        }
        self.data.write_manifest("market", manifest["run_id"], manifest, finished)
        batch_status = "strict_eligible" if strict_eligible else ("complete" if core_success else "partial")
        self.emit_quality(
            "snapshot_batch",
            batch_status,
            {"core_market_complete": core_success, "strict_eligible": strict_eligible},
            at=finished,
            fixture_id=fixture_id,
            competition=competition,
            cutoff=str(job["target"]),
        )

        attempts = int(job.get("attempts") or 0) + 1
        may_retry = retryable_failure and attempts < 3 and (
            window_end is None or finished + timedelta(minutes=2) <= window_end
        )
        if may_retry:
            self.state.retry_job(job["job_id"], finished, "retryable market failure")
            return "retry"
        self.state.complete_job(
            job["job_id"], "done" if core_success else "partial", finished
        )
        return "done" if core_success else "partial"

    def _process_result_jobs(self, jobs: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"success": 0, "missing": 0, "failure": 0}
        if not jobs:
            return counts
        try:
            completed_response = self.http.request("GET", COMPLETED_URL)
            completed_blob = self.data.store_response(completed_response, default_extension="html")
            self._observe_http(completed_blob, context="results:completed")
        except CollectorHttpError as exc:
            for job in jobs:
                self.state.retry_job(job["job_id"], utc_now(), str(exc))
            counts["failure"] = len(jobs)
            return counts
        if not completed_response.ok:
            for job in jobs:
                self.state.retry_job(
                    job["job_id"], utc_now(), f"completed page HTTP {completed_response.status_code}"
                )
            counts["failure"] = len(jobs)
            return counts

        fixture_ids = {str(job["fixture_id"]) for job in jobs}
        completed_scores = parse_completed_page(completed_response.content, fixture_ids)
        fixture_states = self.state.fixtures_by_ids(fixture_ids)
        raw_blobs: list[dict[str, Any]] = [completed_blob]
        for job in jobs:
            fixture_id = str(job["fixture_id"])
            completed = completed_scores.get(fixture_id)
            if completed is None:
                self.state.complete_job(job["job_id"], "missing", utc_now(), "not present as completed")
                self.emit_quality(
                    "result_candidate", "missing", {"target": job["target"]}, fixture_id=fixture_id
                )
                counts["missing"] += 1
                continue
            try:
                analysis_url = f"https://odds.500.com/fenxi/shuju-{fixture_id}.shtml"
                analysis_response = self.http.request("GET", analysis_url)
                analysis_blob = self.data.store_response(analysis_response, default_extension="html")
                raw_blobs.append(analysis_blob)
                self._observe_http(analysis_blob, context="results:analysis")
                analysis = (
                    parse_analysis_page(analysis_response.content, fixture_id)
                    if analysis_response.ok
                    else None
                )
                if analysis is None:
                    raise ValueError("analysis page has no parseable score")
                observed = analysis_response.response_received_at
                candidate = make_candidate(
                    completed,
                    analysis,
                    observed_at=observed,
                    completed_blob=completed_blob,
                    analysis_blob=analysis_blob,
                )
                if candidate is None:
                    raise ValueError("completed and analysis scores conflict")
                if self.state.claim_record(candidate["record_id"], "ResultCandidate", observed):
                    self.data.write_result("candidates", candidate, observed)
                    self.data.append_normalized("result_candidates", candidate, observed)
                fixture_state = fixture_states.get(fixture_id, {})
                if fixture_state.get("competition_format") == "regular_time_only":
                    verified = make_verified_result(
                        fixture_id=fixture_id,
                        home_goals=candidate["home_goals"],
                        away_goals=candidate["away_goals"],
                        source_url=analysis_url,
                        confirmed_at=observed,
                        method="500-two-source-regular-time-competition",
                        notes="competition registry marks this fixture as regular_time_only",
                        candidate_id=candidate["record_id"],
                    )
                    if self.state.claim_record(verified["record_id"], "VerifiedResult", observed):
                        self.data.write_result("verified", verified, observed)
                        self.data.append_normalized("verified_results", verified, observed)
                else:
                    self.emit_quality(
                        "result_scope_ambiguous",
                        "warning",
                        {"competition_format": fixture_state.get("competition_format", "unknown")},
                        at=observed,
                        fixture_id=fixture_id,
                    )
                self.state.complete_job(job["job_id"], "done", observed)
                self.emit_quality(
                    "result_candidate", "success", {"candidate_id": candidate["record_id"]}, at=observed, fixture_id=fixture_id
                )
                counts["success"] += 1
            except (CollectorHttpError, ValueError) as exc:
                self.state.complete_job(job["job_id"], "failed", utc_now(), str(exc))
                self.emit_quality(
                    "result_candidate", "failure", {"error": str(exc)}, fixture_id=fixture_id
                )
                counts["failure"] += 1
        finished = utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "ResultCaptureManifest",
            "run_id": make_run_id(finished),
            "finished_at": iso_utc(finished),
            "job_ids": [job["job_id"] for job in jobs],
            "raw_blobs": raw_blobs,
            "counts": counts,
        }
        self.data.write_manifest("results", manifest["run_id"], manifest, finished)
        return counts

    def run_once(self, *, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
        started = now or utc_now()
        run_id = make_run_id(started)
        self.state.start_run(run_id, "run_once", started)
        deadline = time.monotonic() + self.config.run_time_budget_seconds
        details: dict[str, Any] = {"run_id": run_id, "discovery": None, "market_jobs": {}, "results": {}}
        exit_code = 0
        try:
            disk_status = self._disk_status()
            if self.state.discovery_due(started):
                details["discovery"] = self.discover()
                if details["discovery"]["status"] != "full":
                    exit_code = 1
            if disk_status == "critical":
                raise CriticalCollectorError("critical disk threshold reached; market downloads stopped")

            while time.monotonic() < deadline:
                jobs = self.state.due_jobs(utc_now(), job_type="market", limit=1)
                if not jobs:
                    break
                status = self._process_market_job(jobs[0])
                details["market_jobs"][jobs[0]["job_id"]] = status
                if status in {"retry", "partial"}:
                    exit_code = 1

            if time.monotonic() < deadline:
                result_jobs = self.state.due_jobs(utc_now(), job_type="result", limit=20)
                details["results"] = self._process_result_jobs(result_jobs)
                if details["results"].get("failure"):
                    exit_code = 1

            heartbeat = utc_now()
            self.state.set_meta("last_heartbeat_at", iso_utc(heartbeat))
            local_now = heartbeat.astimezone(ZoneInfo(self.config.timezone_name))
            previous_day = local_now.date() - timedelta(days=1)
            if self.state.get_meta("last_daily_report_date") != previous_day.isoformat():
                write_daily_report(self.config, self.state, self.data, previous_day)
                self.state.set_meta("last_daily_report_date", previous_day.isoformat())
            status = "success" if exit_code == 0 else "partial"
            self.emit_quality("runner", status, details, at=heartbeat)
            self.state.finish_run(run_id, status, details, heartbeat)
            return exit_code, details
        except CriticalCollectorError as exc:
            finished = utc_now()
            details["error"] = str(exc)
            self.state.finish_run(run_id, "critical", details, finished)
            return 3, details
        except Exception as exc:
            finished = utc_now()
            details["error"] = f"{type(exc).__name__}: {exc}"
            self.state.finish_run(run_id, "failure", details, finished)
            LOGGER.exception("collector run failed")
            return 1, details

    def verify_results(self, input_path: Path) -> tuple[int, dict[str, Any]]:
        imported, conflicts = import_verified_results(input_path, self.data)
        now = utc_now()
        for record in imported:
            if self.state.claim_record(record["record_id"], "VerifiedResult", now):
                self.data.append_normalized("verified_results", record, now)
        for conflict in conflicts:
            self.emit_quality(
                "verified_result_conflict",
                "failure",
                conflict,
                fixture_id=conflict["fixture_id"],
            )
        return (2 if conflicts else 0), {"imported": len(imported), "conflicts": conflicts}

    def smoke_live(self, *, active_fixture_id: str, completed_fixture_id: str) -> tuple[int, dict[str, Any]]:
        fixture = {"fixture_id": active_fixture_id, "kickoff_at": None}
        market_results: dict[str, Any] = {}
        exit_code = 0
        for market in MARKETS:
            capture = self.markets.collect(fixture, market, "smoke_live")
            for blob in capture.raw_blobs:
                self._observe_http(blob, context=f"smoke:{market}")
            market_results[market] = {
                "status": capture.status,
                "error_type": capture.error_type,
                "error": capture.error,
                "raw_blob_count": len(capture.raw_blobs),
                "row_count": capture.snapshot["row_count"] if capture.snapshot else 0,
            }
            if market in CORE_MARKETS and capture.status != "success":
                exit_code = 1

        result_checks: dict[str, Any] = {"completed_page": None, "analysis_page": None}
        try:
            completed_response = self.http.request("GET", COMPLETED_URL)
            completed_blob = self.data.store_response(completed_response, default_extension="html")
            self._observe_http(completed_blob, context="smoke:results:completed")
            completed_scores = (
                parse_completed_page(completed_response.content, {completed_fixture_id})
                if completed_response.ok
                else {}
            )
            result_checks["completed_page"] = {
                "status": "success" if completed_fixture_id in completed_scores else "missing",
                "http_status": completed_response.status_code,
                "raw_blob": completed_blob,
            }
            if completed_fixture_id not in completed_scores:
                exit_code = 1

            analysis_url = f"https://odds.500.com/fenxi/shuju-{completed_fixture_id}.shtml"
            analysis_response = self.http.request("GET", analysis_url)
            analysis_blob = self.data.store_response(analysis_response, default_extension="html")
            self._observe_http(analysis_blob, context="smoke:results:analysis")
            analysis = parse_analysis_page(analysis_response.content, completed_fixture_id) if analysis_response.ok else None
            result_checks["analysis_page"] = {
                "status": "success" if analysis else "missing",
                "http_status": analysis_response.status_code,
                "raw_blob": analysis_blob,
            }
            if analysis is None:
                exit_code = 1
        except CollectorHttpError as exc:
            result_checks["error"] = str(exc)
            exit_code = 1

        return exit_code, {
            "status": "success" if exit_code == 0 else "partial",
            "active_fixture_id": active_fixture_id,
            "completed_fixture_id": completed_fixture_id,
            "markets": market_results,
            "results": result_checks,
        }


def rebuild_state(config: CollectorConfig) -> dict[str, Any]:
    state_path = config.state_path
    backup_path: Path | None = None
    if state_path.exists():
        backup_path = state_path.with_name(f"collector.sqlite3.pre-rebuild-{make_run_id()}.bak")
        state_path.replace(backup_path)
    for suffix in ("-wal", "-shm"):
        Path(str(state_path) + suffix).unlink(missing_ok=True)
    rebuilt = StateStore(config)
    processed = 0
    fixtures = 0
    try:
        for path in sorted((config.data_dir / "discovery").glob("*/*/*/*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                observed = parse_iso(payload["finished_at"])
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            for identity in payload.get("fixtures", []):
                status = rebuilt.upsert_fixture(
                    identity,
                    observed,
                    identity_conflict=str(identity.get("fixture_id")) in payload.get("identity_conflicts", {}),
                )
                rebuilt.schedule_fixture(identity, observed, is_new=status == "new")
                fixtures += status == "new"
            processed += 1
        rebuilt.set_meta("state_rebuilt_at", iso_utc(utc_now()))
    finally:
        rebuilt.close()
    return {
        "manifests_processed": processed,
        "fixtures_rebuilt": fixtures,
        "previous_state_backup": str(backup_path) if backup_path else None,
    }
