from __future__ import annotations

import json
import logging
import shutil
import time
from collections import defaultdict
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
    AnalysisScore,
    ResultParseError,
    existing_result_records,
    import_verified_results,
    is_blocked_result_page,
    load_competition_formats,
    make_candidate,
    make_verified_result,
    parse_analysis_page,
    parse_live_result,
    parse_live_result_feed,
    result_feed_url,
    result_page_url,
)
from .state import StateStore
from .storage import DataStore, json_dumps, make_run_id, stable_record_id
from .timeutil import iso_utc, parse_http_date, parse_iso, utc_now


LOGGER = logging.getLogger(__name__)
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
        self.competition_formats = load_competition_formats(
            self.config.workspace / "config" / "competition-formats.json"
        )
        format_version = stable_record_id(
            "competition_formats", json_dumps(self.competition_formats)
        )
        if self.state.get_meta("competition_formats_version") != format_version:
            self.state.sync_competition_formats(self.competition_formats)
            self.state.set_meta("competition_formats_version", format_version)
        if self.state.get_meta("result_reconciliation_schedule_version") != "1":
            self.state.ensure_result_reconciliation_jobs(utc_now())
            self.state.set_meta("result_reconciliation_schedule_version", "1")

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

    def _observe_http(
        self, blob: dict[str, Any], *, context: str, content_valid: bool = True
    ) -> bool:
        observed = parse_iso(blob["observed_at"])
        success = 200 <= int(blob["http_status"]) < 300 and content_valid
        self.emit_quality(
            "http_request",
            "success" if success else "failure",
            {
                "context": context,
                "url": blob["url"],
                "http_status": blob["http_status"],
                "sha256": blob["sha256"],
                "content_valid": content_valid,
            },
            at=observed,
        )
        http_date = parse_http_date(blob.get("headers", {}).get("date"))
        if http_date is None:
            return self._clock_is_recent(observed) if context.startswith("market:") else True
        drift = abs((observed - http_date).total_seconds())
        if drift > self.config.clock_drift_limit_seconds:
            if context.startswith("discovery:"):
                self.state.set_meta("last_clock_drift_at", iso_utc(observed))
                self.state.set_meta("last_clock_drift_seconds", str(round(drift, 3)))
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

            self.state.sync_competition_formats(self.competition_formats)

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
        warning, critical = self.config.disk_thresholds(usage.total)
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
        counts = {
            "candidate": 0,
            "verified": 0,
            "isolated": 0,
            "missing": 0,
            "failure": 0,
            "conflict": 0,
            "cancelled": 0,
        }
        if not jobs:
            return counts

        fixture_ids = {str(job["fixture_id"]) for job in jobs}
        fixture_states = self.state.fixtures_by_ids(fixture_ids)
        candidate_scores: dict[str, set[tuple[int, int]]] = defaultdict(set)
        for record in existing_result_records(self.config.data_dir, "candidates"):
            try:
                score = (int(record["home_goals"]), int(record["away_goals"]))
            except (KeyError, TypeError, ValueError):
                continue
            candidate_scores[str(record.get("fixture_id"))].add(score)
        verified_records: dict[str, dict[str, Any]] = {}
        verified_scores: dict[str, set[tuple[int, int]]] = defaultdict(set)
        for record in existing_result_records(self.config.data_dir, "verified"):
            try:
                score = (int(record["home_goals"]), int(record["away_goals"]))
            except (KeyError, TypeError, ValueError):
                continue
            fixture_id = str(record.get("fixture_id"))
            verified_records[fixture_id] = record
            verified_scores[fixture_id].add(score)

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        feed_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in jobs:
            kickoff_at = str(job.get("payload", {}).get("kickoff_at") or "")
            try:
                url = result_page_url(kickoff_at, self.config.timezone_name)
                feed_url = result_feed_url(kickoff_at)
            except ValueError:
                url = ""
                feed_url = ""
            grouped[url].append(job)
            feed_grouped[feed_url].append(job)

        page_results: dict[str, dict[str, Any]] = {}
        raw_blobs: list[dict[str, Any]] = []
        page_attempts: list[dict[str, Any]] = []
        captures: list[dict[str, Any]] = []
        feed_results: dict[str, dict[str, Any]] = {}

        def fetch_sources(
            urls: list[str], target: dict[str, dict[str, Any]], *, context: str, extension: str
        ) -> None:
            for url in urls:
                if not url:
                    target[url] = {"error": "fixture kickoff is missing or invalid"}
                    page_attempts.append({"url": url, "status": "invalid_kickoff", "context": context})
                    continue
                try:
                    response = self.http.request("GET", url)
                    blob = self.data.store_response(response, default_extension=extension)
                    raw_blobs.append(blob)
                    blocked = is_blocked_result_page(response.content)
                    self._observe_http(blob, context=context, content_valid=not blocked)
                    if not response.ok:
                        error = f"source HTTP {response.status_code}"
                    elif blocked:
                        error = "source was blocked"
                    else:
                        error = None
                    target[url] = {"response": response, "blob": blob, "error": error}
                    page_attempts.append(
                        {
                            "url": url,
                            "context": context,
                            "status": "success" if error is None else "failure",
                            "raw_blob": blob,
                            "error": error,
                        }
                    )
                except CollectorHttpError as exc:
                    target[url] = {"error": str(exc)}
                    page_attempts.append(
                        {"url": url, "context": context, "status": "request_failure", "error": str(exc)}
                    )

        fetch_sources(list(grouped), page_results, context="results:live", extension="html")
        fetch_sources(
            list(feed_grouped), feed_results, context="results:live-feed", extension="txt"
        )

        for job in jobs:
            fixture_id = str(job["fixture_id"])
            target = str(job.get("target") or "unknown")
            kickoff_at = str(job.get("payload", {}).get("kickoff_at") or "")
            try:
                live_url = result_page_url(kickoff_at, self.config.timezone_name)
                feed_url = result_feed_url(kickoff_at)
            except ValueError:
                live_url = ""
                feed_url = ""
            page = page_results.get(live_url, {"error": "live result page was not requested"})
            feed = feed_results.get(feed_url, {"error": "live result feed was not requested"})
            final_reconciliation = target == "R+7d"
            live = None
            live_response = None
            live_blob = None
            parse_errors: list[ResultParseError] = []
            source_errors = [str(source["error"]) for source in (page, feed) if source.get("error")]
            if not page.get("error"):
                try:
                    live = parse_live_result(page["response"].content, fixture_id)
                    live_response = page["response"]
                    live_blob = page["blob"]
                except ResultParseError as exc:
                    parse_errors.append(exc)
            if live is None and not feed.get("error"):
                try:
                    live = parse_live_result_feed(feed["response"].content, fixture_id)
                    live_response = feed["response"]
                    live_blob = feed["blob"]
                except ResultParseError as exc:
                    parse_errors.append(exc)

            if live is None and not parse_errors:
                now = utc_now()
                attempts = int(job.get("attempts") or 0) + 1
                error = "; ".join(source_errors) or "result sources were unavailable"
                if attempts < 3 and not final_reconciliation:
                    self.state.retry_job(job["job_id"], now, error)
                else:
                    self.state.complete_job(job["job_id"], "failed", now, error)
                    if final_reconciliation:
                        self.emit_quality(
                            "result_unresolved",
                            "failure",
                            {"reason": error, "target": target},
                            at=now,
                            fixture_id=fixture_id,
                            cutoff=target,
                        )
                self.emit_quality(
                    "result_candidate",
                    "failure",
                    {"error": error, "target": target},
                    at=now,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
                counts["failure"] += 1
                continue

            if live is None:
                exc = parse_errors[-1]
                now = max(
                    source["response"].response_received_at
                    for source in (page, feed)
                    if source.get("response") is not None
                )
                if any(error.code == "cancelled" for error in parse_errors):
                    self.state.complete_job(job["job_id"], "cancelled", now, str(exc))
                    self.state.cancel_future_result_jobs(
                        fixture_id, now, except_job_id=job["job_id"]
                    )
                    self.emit_quality(
                        "result_cancelled",
                        "excluded",
                        {"target": target, "source_errors": [error.code for error in parse_errors]},
                        at=now,
                        fixture_id=fixture_id,
                        cutoff=target,
                    )
                    counts["cancelled"] += 1
                    continue
                normal_missing = exc.code in {"fixture_missing", "not_finished"}
                status = "missing" if normal_missing else "failure"
                self.state.complete_job(job["job_id"], status, now, f"{exc.code}: {exc}")
                self.emit_quality(
                    "result_candidate",
                    status,
                    {"error_code": exc.code, "error": str(exc), "target": target},
                    at=now,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
                if final_reconciliation:
                    self.emit_quality(
                        "result_unresolved",
                        "failure",
                        {"reason": exc.code, "target": target},
                        at=now,
                        fixture_id=fixture_id,
                        cutoff=target,
                    )
                counts[status] += 1
                continue

            assert live_response is not None and live_blob is not None
            live_source_url = str(live_blob["url"])
            if any(error.code == "cancelled" for error in parse_errors):
                now = live_response.response_received_at
                self.state.complete_job(
                    job["job_id"], "conflict", now, "result sources disagree on cancellation status"
                )
                self.emit_quality(
                    "result_conflict",
                    "failure",
                    {
                        "reason": "result sources disagree on cancellation status",
                        "target": target,
                    },
                    at=now,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
                counts["conflict"] += 1
                continue

            analysis_url = f"https://odds.500.com/fenxi/shuju-{fixture_id}.shtml"
            analysis_blob: dict[str, Any] | None = None
            analysis: AnalysisScore | None = None
            analysis_error: str | None = None
            observed = live_response.response_received_at
            try:
                analysis_response = self.http.request("GET", analysis_url)
                analysis_blob = self.data.store_response(analysis_response, default_extension="html")
                raw_blobs.append(analysis_blob)
                analysis_blocked = is_blocked_result_page(analysis_response.content)
                self._observe_http(
                    analysis_blob,
                    context="results:analysis",
                    content_valid=not analysis_blocked,
                )
                observed = analysis_response.response_received_at
                if not analysis_response.ok:
                    analysis_error = f"HTTP {analysis_response.status_code}"
                elif analysis_blocked:
                    analysis_error = "blocked page"
                else:
                    analysis = parse_analysis_page(analysis_response.content, fixture_id)
                    if analysis is None:
                        analysis_error = "analysis page has no matching parseable score"
            except CollectorHttpError as exc:
                analysis_error = str(exc)

            if analysis is None:
                consistency = "unavailable"
            elif (analysis.home_goals, analysis.away_goals) == (live.home_goals, live.away_goals):
                consistency = "passed"
            else:
                consistency = "conflict"

            candidate = make_candidate(
                live,
                kickoff_at=kickoff_at or None,
                observed_at=observed,
                live_blob=live_blob,
                analysis_blob=analysis_blob,
                analysis_consistency=consistency,
            )
            score = (candidate["home_goals"], candidate["away_goals"])
            prior_scores = candidate_scores[fixture_id]
            evidence_conflict = len(prior_scores | {score}) > 1
            if evidence_conflict or consistency == "conflict":
                self.emit_quality(
                    "result_conflict",
                    "failure",
                    {
                        "existing_scores": sorted([list(value) for value in prior_scores]),
                        "live_score": list(score),
                        "analysis_score": (
                            [analysis.home_goals, analysis.away_goals] if analysis else None
                        ),
                        "target": target,
                    },
                    at=observed,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
                counts["conflict"] += 1
            candidate_scores[fixture_id].add(score)
            if self.state.claim_record(candidate["record_id"], "ResultCandidate", observed):
                self.data.write_result("candidates", candidate, observed)
                self.data.append_normalized("result_candidates", candidate, observed)
            self.emit_quality(
                "result_candidate",
                "success",
                {
                    "candidate_id": candidate["record_id"],
                    "analysis_consistency": consistency,
                    "analysis_error": analysis_error,
                    "target": target,
                },
                at=observed,
                fixture_id=fixture_id,
                cutoff=target,
            )
            counts["candidate"] += 1

            fixture_state = fixture_states.get(fixture_id, {})
            competition_format = fixture_state.get("competition_format", "unknown")
            identity_conflict = bool(fixture_state.get("identity_conflict"))
            terminal = False
            job_status = "candidate_only"
            if consistency == "passed" and not evidence_conflict and not identity_conflict:
                if competition_format == "regular_time_only":
                    prior_verified = verified_records.get(fixture_id)
                    verified_conflict = len(verified_scores[fixture_id] | {score}) > 1
                    if verified_conflict:
                        self.emit_quality(
                            "result_conflict",
                            "failure",
                            {
                                "verified_score": [
                                    prior_verified["home_goals"],
                                    prior_verified["away_goals"],
                                ],
                                "incoming_score": list(score),
                                "target": target,
                            },
                            at=observed,
                            fixture_id=fixture_id,
                            cutoff=target,
                        )
                        counts["conflict"] += 1
                        job_status = "conflict"
                    else:
                        verified = make_verified_result(
                            fixture_id=fixture_id,
                            home_goals=candidate["home_goals"],
                            away_goals=candidate["away_goals"],
                            source_url=live_source_url,
                            confirmed_at=observed,
                            method="500-two-page-regular-time-competition",
                            notes="live and analysis scores agree; competition registry is regular_time_only",
                            candidate_id=candidate["record_id"],
                        )
                        if self.state.claim_record(verified["record_id"], "VerifiedResult", observed):
                            self.data.write_result("verified", verified, observed)
                            self.data.append_normalized("verified_results", verified, observed)
                            self.emit_quality(
                                "verified_result",
                                "accepted",
                                {"record_id": verified["record_id"], "target": target},
                                at=observed,
                                fixture_id=fixture_id,
                                competition=fixture_state.get("competition"),
                                cutoff=target,
                            )
                            counts["verified"] += 1
                        verified_records[fixture_id] = verified
                        verified_scores[fixture_id].add(score)
                        terminal = True
                        job_status = "done"
                else:
                    self.emit_quality(
                        "result_scope_ambiguous",
                        "isolated",
                        {"competition_format": competition_format, "target": target},
                        at=observed,
                        fixture_id=fixture_id,
                        competition=fixture_state.get("competition"),
                        cutoff=target,
                    )
                    counts["isolated"] += 1
                    terminal = True
                    job_status = "isolated"
            elif identity_conflict:
                self.emit_quality(
                    "result_identity_conflict",
                    "isolated",
                    {"target": target},
                    at=observed,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
                counts["isolated"] += 1
                terminal = True
                job_status = "isolated"
            elif consistency == "conflict" or evidence_conflict:
                job_status = "conflict"

            self.state.complete_job(job["job_id"], job_status, observed, analysis_error)
            if terminal:
                self.state.cancel_future_result_jobs(
                    fixture_id, observed, except_job_id=job["job_id"]
                )
            elif final_reconciliation:
                self.emit_quality(
                    "result_unresolved",
                    "failure",
                    {"reason": job_status, "target": target},
                    at=observed,
                    fixture_id=fixture_id,
                    cutoff=target,
                )
            captures.append(
                {
                    "job_id": job["job_id"],
                    "fixture_id": fixture_id,
                    "target": target,
                    "candidate_id": candidate["record_id"],
                    "analysis_consistency": consistency,
                    "live_source_url": live_source_url,
                    "job_status": job_status,
                }
            )

        finished = utc_now()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "ResultCaptureManifest",
            "run_id": make_run_id(finished),
            "finished_at": iso_utc(finished),
            "job_ids": [job["job_id"] for job in jobs],
            "page_attempts": page_attempts,
            "raw_blobs": raw_blobs,
            "captures": captures,
            "counts": counts,
        }
        self.data.write_manifest("results", manifest["run_id"], manifest, finished)
        return counts

    def reconcile_results(self, start: datetime, end: datetime) -> dict[str, Any]:
        if end <= start:
            raise ValueError("until must be after since")
        candidates = existing_result_records(self.config.data_dir, "candidates")
        verified = existing_result_records(self.config.data_dir, "verified")
        passed_candidates = {
            str(record.get("fixture_id"))
            for record in candidates
            if record.get("analysis_consistency") == "passed"
        }
        verified_ids = {str(record.get("fixture_id")) for record in verified}
        jobs: list[dict[str, Any]] = []
        requested_at = utc_now()
        for fixture in self.state.all_fixtures():
            kickoff_text = fixture.get("kickoff_at")
            if not kickoff_text:
                continue
            kickoff = parse_iso(kickoff_text)
            if not (start <= kickoff < end) or kickoff > requested_at:
                continue
            fixture_id = str(fixture["fixture_id"])
            if fixture_id in verified_ids:
                continue
            if fixture_id in passed_candidates and fixture.get("competition_format") != "regular_time_only":
                continue
            jobs.append(
                {
                    "job_id": f"reconcile:{fixture_id}:{make_run_id(requested_at)}",
                    "job_type": "result",
                    "fixture_id": fixture_id,
                    "target": "reconcile",
                    "attempts": 0,
                    "payload": {"fixture": fixture["identity"], "kickoff_at": kickoff_text},
                }
            )
        totals: dict[str, int] = defaultdict(int)
        for index in range(0, len(jobs), 20):
            for key, value in self._process_result_jobs(jobs[index : index + 20]).items():
                totals[key] += value
        return {
            "status": "completed",
            "start": iso_utc(start),
            "end": iso_utc(end),
            "fixtures_queued": len(jobs),
            "counts": dict(totals),
        }

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

    def smoke_live(
        self,
        *,
        active_fixture_id: str,
        completed_fixture_id: str,
        completed_kickoff_at: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
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

        result_checks: dict[str, Any] = {"live_page": None, "analysis_page": None}
        try:
            fixture_state = self.state.fixtures_by_ids({completed_fixture_id}).get(
                completed_fixture_id, {}
            )
            kickoff_at = completed_kickoff_at or fixture_state.get("kickoff_at")
            if not kickoff_at:
                kickoff_at = iso_utc(utc_now())
            live_url = result_page_url(str(kickoff_at), self.config.timezone_name)
            live_response = self.http.request("GET", live_url)
            live_blob = self.data.store_response(live_response, default_extension="html")
            blocked = is_blocked_result_page(live_response.content)
            self._observe_http(
                live_blob, context="smoke:results:live", content_valid=not blocked
            )
            live = (
                parse_live_result(live_response.content, completed_fixture_id)
                if live_response.ok and not blocked
                else None
            )
            result_checks["live_page"] = {
                "status": "success" if live else "missing",
                "http_status": live_response.status_code,
                "source_url": live_url,
                "raw_blob": live_blob,
            }
            if live is None:
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
            if analysis is None or live is None or (
                analysis.home_goals,
                analysis.away_goals,
            ) != (live.home_goals, live.away_goals):
                exit_code = 1
        except (CollectorHttpError, ResultParseError, ValueError) as exc:
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
        formats = load_competition_formats(config.workspace / "config" / "competition-formats.json")
        rebuilt.sync_competition_formats(formats)
        rebuilt.set_meta(
            "competition_formats_version",
            stable_record_id("competition_formats", json_dumps(formats)),
        )
        rebuilt.ensure_result_reconciliation_jobs(utc_now())
        rebuilt.set_meta("result_reconciliation_schedule_version", "1")
        for record in existing_result_records(config.data_dir, "candidates"):
            try:
                observed = parse_iso(str(record["observed_at"]))
                fixture_id = str(record["fixture_id"])
            except (KeyError, ValueError):
                continue
            rebuilt.claim_record(str(record["record_id"]), "ResultCandidate", observed)
            rebuilt.add_event(
                "result_candidate",
                "success",
                {"candidate_id": record["record_id"], "target": "rebuilt"},
                occurred_at=observed,
                fixture_id=fixture_id,
                cutoff="rebuilt",
            )
        for record in existing_result_records(config.data_dir, "verified"):
            try:
                confirmed = parse_iso(str(record["confirmed_at"]))
                fixture_id = str(record["fixture_id"])
            except (KeyError, ValueError):
                continue
            rebuilt.claim_record(str(record["record_id"]), "VerifiedResult", confirmed)
            rebuilt.add_event(
                "verified_result",
                "accepted",
                {"record_id": record["record_id"], "target": "rebuilt"},
                occurred_at=confirmed,
                fixture_id=fixture_id,
                cutoff="rebuilt",
            )
        rebuilt.set_meta("state_rebuilt_at", iso_utc(utc_now()))
    finally:
        rebuilt.close()
    return {
        "manifests_processed": processed,
        "fixtures_rebuilt": fixtures,
        "previous_state_backup": str(backup_path) if backup_path else None,
    }
