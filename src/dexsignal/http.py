"""HTTP session with timeouts, retries and per-host rate limiting."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "dexsignal-research/1.0 (+private research tool)"


class RateLimiter:
    """Spaces requests to each host so no host sees more than N per minute."""

    def __init__(
        self,
        max_per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 60.0 / max_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = self._clock()
            allowed = self._next_allowed.get(host, now)
            delay = max(0.0, allowed - now)
            self._next_allowed[host] = max(now, allowed) + self._interval
        if delay > 0:
            self._sleep(delay)


class HttpClient:
    """Thin wrapper around ``requests.Session`` enforcing NFR-1 and NFR-2."""

    def __init__(
        self,
        timeout_seconds: float,
        max_requests_per_minute: int,
        session: requests.Session | None = None,
        limiter: RateLimiter | None = None,
        retries: int = 3,
    ) -> None:
        self._timeout = (min(5.0, timeout_seconds), timeout_seconds)
        self._limiter = limiter or RateLimiter(max_requests_per_minute)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        retry = Retry(
            total=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_json(
        self, url: str, params: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        self._limiter.wait(urlsplit(url).netloc)
        response = self.session.get(url, params=params, headers=headers, timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        self._limiter.wait(urlsplit(url).netloc)
        response = self.session.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()
        return response.json()
