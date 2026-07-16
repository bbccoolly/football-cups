from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from .config import CollectorConfig
from .timeutil import utc_now


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-encoding",
    "content-length",
    "content-type",
    "date",
    "etag",
    "expires",
    "last-modified",
    "retry-after",
}


class CollectorHttpError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObservedResponse:
    method: str
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    request_started_at: datetime
    response_received_at: datetime
    source_encoding: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class RateLimitedHttpClient:
    def __init__(self, config: CollectorConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self._last_request_monotonic = 0.0

    def request(self, method: str, url: str, **kwargs: Any) -> ObservedResponse:
        kwargs.setdefault("timeout", self.config.request_timeout_seconds)
        last_error: Exception | None = None
        for attempt in range(len(self.config.retry_delays_seconds) + 1):
            remaining = self.config.request_min_interval_seconds - (
                time.monotonic() - self._last_request_monotonic
            )
            if remaining > 0:
                time.sleep(remaining)
            started = utc_now()
            self._last_request_monotonic = time.monotonic()
            try:
                response = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= len(self.config.retry_delays_seconds):
                    break
                time.sleep(self.config.retry_delays_seconds[attempt])
                continue
            received = utc_now()
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < len(
                self.config.retry_delays_seconds
            ):
                response.close()
                time.sleep(self.config.retry_delays_seconds[attempt])
                continue
            encoding = response.encoding or response.apparent_encoding or "unknown"
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in SAFE_RESPONSE_HEADERS
            }
            return ObservedResponse(
                method=method.upper(),
                url=response.url or url,
                status_code=response.status_code,
                headers=headers,
                content=response.content,
                request_started_at=started,
                response_received_at=received,
                source_encoding=encoding,
            )
        raise CollectorHttpError(f"{method.upper()} {url} failed: {last_error}")

