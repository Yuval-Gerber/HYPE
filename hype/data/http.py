"""Shared synchronous HTTP client with rate limiting + retry/backoff.

The free Birdeye/Helius tiers rate-limit aggressively (observed HTTP 429), and
some endpoints sit behind Cloudflare that blocks the default urllib User-Agent.
This client centralizes:
  - a per-instance minimum interval between requests (token-bucket style),
  - exponential backoff on 429 / 5xx,
  - a real User-Agent header.

Thresholds are parameters, not magic numbers, so callers set them from config.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import requests

DEFAULT_USER_AGENT = "Hype/0.2 (+local copy-trader)"


class RateLimiter:
    """Enforces a minimum interval between calls across threads."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class HttpClient:
    def __init__(
        self,
        *,
        min_interval: float = 1.2,
        user_agent: str = DEFAULT_USER_AGENT,
        max_retries: int = 4,
        timeout: float = 30.0,
    ) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.limiter = RateLimiter(min_interval)
        self.max_retries = max_retries
        self.timeout = timeout

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout)
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                # Honor Retry-After if present, else exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after and retry_after.isdigit()) else min(2 ** attempt, 8)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return resp

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)
