"""HTTP client with declared User-Agent, rate limiting, retry/backoff and explicit failures."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests


class ProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status = status
        self.retryable = retryable


@dataclass
class HttpClient:
    provider: str
    user_agent: str
    min_interval_s: float = 0.2          # SEC fair access allows <= 10 req/s; we stay well below
    max_retries: int = 3
    timeout_s: float = 30.0
    backoff_base_s: float = 1.0
    session: requests.Session = field(default_factory=requests.Session)
    _last: float = 0.0
    sleep = staticmethod(time.sleep)

    def get(self, url: str, *, headers: dict | None = None, params: dict | None = None) -> requests.Response:
        h = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        if headers:
            h.update(headers)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, headers=h, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_exc = ProviderError(self.provider, f"network error: {exc}", retryable=True)
            else:
                if resp.status_code == 200:
                    return resp
                retryable = resp.status_code in (429, 500, 502, 503, 504)
                last_exc = ProviderError(self.provider, f"HTTP {resp.status_code} for {url}",
                                         status=resp.status_code, retryable=retryable)
                if not retryable:
                    raise last_exc
                ra = resp.headers.get("Retry-After")
                if ra and ra.isdigit():
                    self.sleep(min(int(ra), 60))
            if attempt < self.max_retries:
                self.sleep(self.backoff_base_s * (2 ** attempt))
        assert last_exc is not None
        raise last_exc
