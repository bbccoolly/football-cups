from __future__ import annotations

import hashlib
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from football_cups.collector.storage import SingleInstanceLock, make_run_id

from . import RESEARCH_FLAGS
from .config import ResearchConfig
from .registry import REGISTERED_HOSTS, ROBOTS_URLS, ResearchAsset
from .state import ResearchState
from .storage import ResearchStore, stable_id


USER_AGENT = "football-cups-research/0.1"
BLOCK_MARKERS = (
    b"id=statusCode>567<",
    b"Access Restricted",
    b"captcha",
    "请求已被站点的安全策略拦截".encode("utf-8"),
)


class ResearchHttpError(RuntimeError):
    pass


class AccessPolicyError(ResearchHttpError):
    pass


class BudgetExceeded(ResearchHttpError):
    pass


class IntegrityError(ResearchHttpError):
    pass


@dataclass(frozen=True)
class FetchResult:
    asset_id: str
    status: str
    http_status: int
    sha256: str | None
    blob_path: str | None
    observed_at: str
    size_bytes: int


def utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


class ResearchHttpClient:
    def __init__(
        self,
        config: ResearchConfig,
        state: ResearchState,
        store: ResearchStore,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = utc_now,
        jitter: Callable[[float, float], float] = random.uniform,
        retry_delays: tuple[float, ...] = (60.0, 300.0),
    ) -> None:
        self.config = config
        self.state = state
        self.store = store
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/plain;q=0.9,*/*;q=0.1",
            }
        )
        self.sleep = sleep
        self.now = now
        self.jitter = jitter
        self.retry_delays = retry_delays

    def _host_lock(self, host: str) -> SingleInstanceLock:
        return SingleInstanceLock(
            self.config.research_dir / "state" / f"{host}.lock",
            stale_after=timedelta(minutes=30),
        )

    def _check_host(self, host: str, now: datetime) -> dict[str, object]:
        if host not in REGISTERED_HOSTS:
            raise AccessPolicyError(f"unregistered research host: {host}")
        snapshot = self.state.host_snapshot(host, now)
        circuit_until = _parse_time(snapshot.get("circuit_until"))  # type: ignore[arg-type]
        if circuit_until and circuit_until > now:
            raise AccessPolicyError(
                f"source circuit is open until {circuit_until.isoformat()}: "
                f"{snapshot.get('circuit_reason') or 'unknown'}"
            )
        if int(snapshot["requests"]) >= self.config.requests_per_24h:
            raise BudgetExceeded("24-hour request budget exhausted")
        if int(snapshot["bytes"]) >= self.config.bytes_per_24h:
            raise BudgetExceeded("24-hour byte budget exhausted")
        last_request = _parse_time(snapshot.get("last_request_at"))  # type: ignore[arg-type]
        if last_request:
            minimum = self.config.min_interval_seconds * self.jitter(1.0, 1.2)
            remaining = minimum - (now - last_request).total_seconds()
            if remaining > 0:
                self.sleep(remaining)
        return snapshot

    def _request_once(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        redirect_count: int = 0,
    ) -> tuple[requests.Response, bytes, datetime]:
        host = urlparse(url).hostname or ""
        now = self.now()
        snapshot = self._check_host(host, now)
        remaining_byte_budget = self.config.bytes_per_24h - int(snapshot["bytes"])
        response = self.session.get(
            url,
            headers=headers,
            timeout=self.config.request_timeout_seconds,
            allow_redirects=False,
            stream=True,
        )
        if 300 <= response.status_code < 400 and response.status_code != 304:
            location = response.headers.get("location")
            response.close()
            self.state.record_request(host, self.now(), 0)
            if not location:
                raise AccessPolicyError("redirect response has no Location header")
            target = urljoin(url, location)
            target_host = urlparse(target).hostname or ""
            if target_host != host or target_host not in REGISTERED_HOSTS:
                raise AccessPolicyError(f"cross-host redirect is not allowed: {target_host}")
            if redirect_count >= 3:
                raise AccessPolicyError("same-host redirect limit exceeded")
            return self._request_once(
                target,
                headers=headers,
                redirect_count=redirect_count + 1,
            )
        declared = response.headers.get("content-length")
        if declared and int(declared) > min(self.config.max_file_bytes, remaining_byte_budget):
            response.close()
            self.state.record_request(host, self.now(), 0)
            raise IntegrityError("declared file size exceeds configured limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > min(self.config.max_file_bytes, remaining_byte_budget):
                response.close()
                self.state.record_request(host, self.now(), total)
                raise IntegrityError("download exceeded configured file limit")
            chunks.append(chunk)
        observed_at = self.now()
        body = b"".join(chunks)
        self.state.record_request(host, observed_at, len(body))
        return response, body, observed_at

    def ensure_robots(self, host: str) -> None:
        now = self.now()
        snapshot = self.state.host_snapshot(host, now)
        checked = _parse_time(snapshot.get("robots_checked_at"))  # type: ignore[arg-type]
        if checked and now - checked < timedelta(hours=24) and snapshot.get("robots_body"):
            return
        robots_url = ROBOTS_URLS.get(host)
        if not robots_url:
            raise AccessPolicyError(f"no robots URL registered for {host}")
        response, body, observed_at = self._request_once(robots_url)
        if response.status_code != 200:
            raise AccessPolicyError(f"robots request failed with HTTP {response.status_code}")
        text = body.decode(response.encoding or "utf-8", errors="replace")
        self.state.save_robots(
            host,
            body=text,
            sha256=hashlib.sha256(body).hexdigest(),
            checked_at=observed_at,
        )

    def _robots_allows(self, asset: ResearchAsset) -> bool:
        host = urlparse(asset.url).hostname or ""
        snapshot = self.state.host_snapshot(host, self.now())
        body = str(snapshot.get("robots_body") or "")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(ROBOTS_URLS[host])
        parser.parse(body.splitlines())
        return parser.can_fetch(USER_AGENT, asset.url)

    @staticmethod
    def _retry_after(response: requests.Response, now: datetime) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        if value.strip().isdigit():
            return float(value.strip())
        try:
            parsed = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, (parsed - now).total_seconds())

    def fetch(self, asset: ResearchAsset) -> FetchResult:
        host = urlparse(asset.url).hostname or ""
        lock = self._host_lock(host)
        if not lock.acquire(wait_seconds=0):
            raise AccessPolicyError(f"another research process is accessing {host}")
        try:
            self.ensure_robots(host)
            if not self._robots_allows(asset):
                raise AccessPolicyError(f"robots policy disallows asset: {asset.asset_id}")
            cached = self.state.asset_cache(asset.asset_id) or {}
            headers: dict[str, str] = {}
            if cached.get("etag"):
                headers["If-None-Match"] = str(cached["etag"])
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = str(cached["last_modified"])

            last_error: Exception | None = None
            for attempt in range(len(self.retry_delays) + 1):
                try:
                    response, body, observed_at = self._request_once(asset.url, headers=headers)
                except requests.RequestException as exc:
                    last_error = exc
                    failures = self.state.record_failure(host, now=self.now())
                    if failures >= 3:
                        self.state.open_circuit(host, self.now() + timedelta(hours=24), str(exc))
                    if attempt >= len(self.retry_delays):
                        break
                    self.sleep(self.retry_delays[attempt])
                    continue

                if response.status_code == 304:
                    self.state.record_success(host)
                    return FetchResult(
                        asset.asset_id,
                        "unchanged",
                        304,
                        str(cached.get("sha256") or "") or None,
                        str(cached.get("blob_path") or "") or None,
                        observed_at.isoformat().replace("+00:00", "Z"),
                        0,
                    )
                blocked = response.status_code in {401, 403, 567} or any(
                    marker.lower() in body[:200_000].lower() for marker in BLOCK_MARKERS
                )
                if blocked:
                    reason = f"blocked response HTTP {response.status_code}"
                    self.state.open_circuit(host, self.now() + timedelta(days=7), reason)
                    raise AccessPolicyError(reason)
                if response.status_code == 429:
                    self.state.record_failure(host, now=self.now())
                    if attempt >= len(self.retry_delays):
                        self.state.open_circuit(
                            host,
                            self.now() + timedelta(hours=24),
                            "HTTP 429 retry budget exhausted",
                        )
                        last_error = ResearchHttpError("HTTP 429 retry budget exhausted")
                        break
                    delay = self._retry_after(response, self.now())
                    self.sleep(delay if delay is not None else self.retry_delays[attempt])
                    continue
                if 500 <= response.status_code < 600:
                    failures = self.state.record_failure(host, now=self.now())
                    if failures >= 3:
                        self.state.open_circuit(
                            host, self.now() + timedelta(hours=24), f"HTTP {response.status_code}"
                        )
                    if attempt >= len(self.retry_delays):
                        last_error = ResearchHttpError(f"HTTP {response.status_code}")
                        break
                    self.sleep(self.retry_delays[attempt])
                    continue
                if response.status_code != 200:
                    raise ResearchHttpError(f"unexpected HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if asset.extension == "csv" and (
                    "html" in content_type
                    or body.lstrip().lower().startswith((b"<html", b"<!doctype html"))
                ):
                    raise IntegrityError("CSV asset returned HTML")
                if asset.extension == "xlsx" and not body.startswith(b"PK\x03\x04"):
                    raise IntegrityError("XLSX asset has an invalid ZIP signature")
                digest, path = self.store.store_blob(body, asset.extension)
                relative = path.relative_to(self.config.research_dir).as_posix()
                self.state.save_asset(
                    asset.asset_id,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    sha256=digest,
                    blob_path=relative,
                    observed_at=observed_at,
                )
                self.state.record_success(host)
                return FetchResult(
                    asset.asset_id,
                    "downloaded",
                    200,
                    digest,
                    relative,
                    observed_at.isoformat().replace("+00:00", "Z"),
                    len(body),
                )
            raise ResearchHttpError(f"failed to fetch {asset.asset_id}: {last_error}")
        finally:
            lock.release()


def fetch_assets(
    config: ResearchConfig, assets: list[ResearchAsset]
) -> tuple[str, list[FetchResult], list[dict[str, str]]]:
    state = ResearchState(config.state_path)
    store = ResearchStore(config)
    client = ResearchHttpClient(config, state, store)
    run_id = make_run_id()
    results: list[FetchResult] = []
    errors: list[dict[str, str]] = []
    try:
        for asset in assets:
            try:
                results.append(client.fetch(asset))
            except (ResearchHttpError, OSError, ValueError) as exc:
                errors.append(
                    {"asset_id": asset.asset_id, "error_type": type(exc).__name__, "error": str(exc)}
                )
                if isinstance(exc, (AccessPolicyError, BudgetExceeded, IntegrityError)):
                    break
        store.write_manifest(
            run_id,
            "fetch",
            {
                "schema_version": 1,
                "run_id": run_id,
                "source": "football-data",
                "status": "completed" if not errors else "partial",
                "results": [result.__dict__ for result in results],
                "errors": errors,
            },
        )
        if errors:
            store.write_records(
                "access-events",
                run_id,
                "source-access-failures",
                (
                    {
                        "schema_version": 1,
                        "record_type": "ResearchQualityEvent",
                        "record_id": stable_id(
                            "research_source_access_failure",
                            error["asset_id"],
                            error["error_type"],
                            error["error"],
                            run_id,
                        ),
                        **RESEARCH_FLAGS,
                        "source_id": "football-data",
                        "event_type": "source_access_failure",
                        "status": "failure",
                        "details": error,
                    }
                    for error in errors
                ),
            )
        return run_id, results, errors
    finally:
        state.close()
