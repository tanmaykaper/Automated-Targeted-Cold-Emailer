"""Resilient HTTP: timeouts, exponential backoff + jitter, Retry-After, circuit breakers.

Every external call goes through `Http.request(provider=...)`. Failures raise a typed
`ProviderError` with a category, which stage code catches per-record. A provider that is
clearly broken for this run (bad key, plan lacks access, quota exhausted, repeated 5xx)
trips its breaker, so later calls fail fast instead of burning the whole tick on timeouts.
"""
from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class ProviderError(Exception):
    """category ∈ network | timeout | rate_limit | quota | auth | client_error | server_error | parse | circuit_open | budget"""

    def __init__(self, provider: str, category: str, message: str, status: int | None = None):
        super().__init__(f"[{provider}:{category}] {message}")
        self.provider = provider
        self.category = category
        self.status = status


@dataclass
class Breaker:
    failures: int = 0
    open_reason: str = ""
    threshold: int = 4

    @property
    def is_open(self) -> bool:
        return bool(self.open_reason)


@dataclass
class Metrics:
    calls: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)          # (provider, category)
    breaker_reasons: dict = field(default_factory=dict)

    def error_summary(self) -> dict[str, int]:
        out: Counter = Counter()
        for (_, cat), n in self.errors.items():
            out[cat] += n
        return dict(out)


# Providers whose 429 means "monthly quota exhausted", not "slow down"
QUOTA_429 = {"hunter"}
# Providers whose 403 means rate-limited (Hunter docs) rather than forbidden
RATE_403 = {"hunter"}


class Http:
    def __init__(self, max_retries: int = 3, backoff_base: float = 1.0, backoff_cap: float = 30.0,
                 connect_timeout: float = 6.0, read_timeout: float = 25.0, sleep=time.sleep):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.timeout = (connect_timeout, read_timeout)
        self.breakers: dict[str, Breaker] = defaultdict(Breaker)
        self.metrics = Metrics()
        self._sleep = sleep

    def available(self, provider: str) -> bool:
        return not self.breakers[provider].is_open

    def trip(self, provider: str, reason: str) -> None:
        b = self.breakers[provider]
        if not b.is_open:
            b.open_reason = reason
            self.metrics.breaker_reasons[provider] = reason
            log.warning("Circuit OPEN for %s for the rest of this run: %s", provider, reason)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(self.backoff_cap, self.backoff_base * 2 ** attempt) * random.uniform(0.6, 1.4)

    def request(self, method: str, url: str, *, provider: str, timeout=None, expect_json: bool = True,
                ok_statuses: tuple[int, ...] = (), **kwargs):
        """Returns parsed JSON (or the Response when expect_json=False). Raises ProviderError."""
        breaker = self.breakers[provider]
        if breaker.is_open:
            raise ProviderError(provider, "circuit_open", breaker.open_reason)
        last: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            self.metrics.calls[provider] += 1
            try:
                resp = self.session.request(method, url, timeout=timeout or self.timeout, **kwargs)
            except requests.Timeout as e:
                last = ProviderError(provider, "timeout", str(e)[:200])
            except requests.RequestException as e:
                last = ProviderError(provider, "network", str(e)[:200])
            else:
                status = resp.status_code
                if status < 400 or status in ok_statuses:
                    breaker.failures = 0
                    if not expect_json:
                        return resp
                    try:
                        return resp.json()
                    except ValueError:
                        last = ProviderError(provider, "parse", f"non-JSON response (HTTP {status})", status)
                        break
                body = resp.text[:300].replace("\n", " ")
                if status == 401 or (status == 403 and provider not in RATE_403):
                    is_site = provider.startswith("fetch:")
                    cat = "blocked" if is_site else "auth"
                    err = ProviderError(provider, cat, f"HTTP {status}: {body}", status)
                    self.metrics.errors[(provider, cat)] += 1
                    self.trip(provider, f"HTTP {status} (site refuses automated fetches)" if is_site
                              else f"HTTP {status} (key invalid or plan lacks access)")
                    raise err
                if status == 402 or (status == 429 and provider in QUOTA_429):
                    err = ProviderError(provider, "quota", f"HTTP {status}: {body}", status)
                    self.metrics.errors[(provider, "quota")] += 1
                    self.trip(provider, f"HTTP {status} (quota/credits exhausted)")
                    raise err
                if status in (429, 403):
                    last = ProviderError(provider, "rate_limit", f"HTTP {status}: {body[:120]}", status)
                    if attempt < self.max_retries:
                        # Retry-After header, or Google-style RetryInfo {"retryDelay": "34s"} in the body
                        hint = resp.headers.get("Retry-After")
                        if not hint and (m := re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text)):
                            hint = m.group(1)
                        try:
                            long_wait = float(hint or 0) > 120 or "PerDay" in resp.text
                        except ValueError:
                            long_wait = False
                        if long_wait:  # daily quota exhausted: waiting won't help this run
                            err = ProviderError(provider, "quota", f"HTTP {status} daily quota exhausted", status)
                            self.metrics.errors[(provider, "quota")] += 1
                            self.trip(provider, "daily quota exhausted")
                            raise err
                        self._sleep(self._backoff(attempt, hint))
                        continue
                    break
                if status >= 500:
                    last = ProviderError(provider, "server_error", f"HTTP {status}: {body}", status)
                else:
                    # Other 4xx: our request is wrong for this record — don't retry, don't trip.
                    err = ProviderError(provider, "client_error", f"HTTP {status}: {body}", status)
                    self.metrics.errors[(provider, "client_error")] += 1
                    raise err
            if attempt < self.max_retries:
                self._sleep(self._backoff(attempt, None))
        assert last is not None
        self.metrics.errors[(provider, last.category)] += 1
        breaker.failures += 1
        if breaker.failures >= breaker.threshold:
            self.trip(provider, f"{breaker.failures} consecutive failures ({last.category})")
        raise last
