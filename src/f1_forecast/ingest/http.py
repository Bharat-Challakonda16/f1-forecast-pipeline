"""Polite, cached HTTP client shared by all API downloaders.

Three rules, applied to every request:

1. **Never download twice.** Each response is stored under a key derived from the URL and
   query parameters. If the key exists, the stored copy is returned and no request is made.
2. **Respect rate limits.** A minimum gap between calls plus any number of
   "at most N calls per T seconds" windows. The client waits; it never exceeds a limit.
3. **Retry transient failures** (429 / 5xx / connection errors) with exponential backoff,
   honouring ``Retry-After`` when the server sends it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], None]


# --------------------------------------------------------------------------- rate limiting


@dataclass
class RateLimiter:
    """Blocks until a call is allowed under every configured limit.

    ``windows`` is a list of ``(max_calls, period_s)`` pairs.
    """

    min_interval_s: float = 0.0
    windows: list[tuple[int, float]] = field(default_factory=list)
    clock: Clock = time.monotonic
    sleep: Sleep = time.sleep

    def __post_init__(self) -> None:
        longest = max((calls for calls, _ in self.windows), default=1)
        self._history: deque[float] = deque(maxlen=longest)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None, **kwargs: Any) -> RateLimiter:
        cfg = cfg or {}
        windows = [(int(w["calls"]), float(w["period_s"])) for w in cfg.get("windows", [])]
        return cls(min_interval_s=float(cfg.get("min_interval_s", 0.0)), windows=windows, **kwargs)

    def _wait_needed(self, now: float) -> float:
        wait = 0.0
        if self._history and self.min_interval_s:
            wait = max(wait, self._history[-1] + self.min_interval_s - now)
        for calls, period in self.windows:
            if len(self._history) >= calls:
                oldest_in_window = self._history[-calls]
                wait = max(wait, oldest_in_window + period - now)
        return wait

    def acquire(self) -> None:
        while (wait := self._wait_needed(self.clock())) > 0:
            log.debug("rate limit: sleeping %.2fs", wait)
            self.sleep(wait)
        self._history.append(self.clock())


# --------------------------------------------------------------------------- response store


class Store(Protocol):
    """Where cached responses live (local disk now; a Databricks Volume in CI later)."""

    def exists(self, key: str) -> bool: ...
    def read(self, key: str) -> bytes: ...
    def write(self, key: str, data: bytes) -> None: ...


@dataclass
class LocalStore:
    root: Path

    def _path(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic: an interrupted run never leaves a half-written file


def cache_key(source: str, url: str, params: Mapping[str, Any] | None = None) -> str:
    """Stable, filesystem-safe key: ``<source>/<readable prefix>-<hash>.json``."""
    query = urlencode(sorted((params or {}).items()), doseq=True)
    full = f"{url}?{query}" if query else url
    digest = hashlib.sha256(full.encode()).hexdigest()[:16]
    tail = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json")
    readable = "".join(c if c.isalnum() or c in "-_" else "_" for c in tail)[:40] or "root"
    return f"{source}/{readable}-{digest}.json"


# --------------------------------------------------------------------------- client


class DownloadError(RuntimeError):
    pass


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    backoff_initial_s: float = 2.0
    backoff_max_s: float = 120.0
    retry_on_status: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> RetryPolicy:
        cfg = cfg or {}
        return cls(
            max_attempts=int(cfg.get("max_attempts", 5)),
            backoff_initial_s=float(cfg.get("backoff_initial_s", 2.0)),
            backoff_max_s=float(cfg.get("backoff_max_s", 120.0)),
            retry_on_status=frozenset(cfg.get("retry_on_status", [429, 500, 502, 503, 504])),
        )

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_max_s)
            except ValueError:
                pass  # HTTP-date form: fall back to exponential backoff
        return min(self.backoff_initial_s * 2 ** (attempt - 1), self.backoff_max_s)


@dataclass
class CachedClient:
    """GET JSON with caching, rate limiting and retries. One instance per source."""

    source: str
    store: Store
    limiter: RateLimiter
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    user_agent: str = "f1-forecast-pipeline"
    timeout_s: float = 60.0
    session: requests.Session = field(default_factory=requests.Session)
    sleep: Sleep = time.sleep

    def __post_init__(self) -> None:
        self.session.headers["User-Agent"] = self.user_agent
        self.stats = {"cache_hits": 0, "downloads": 0, "retries": 0}

    def get_bytes(self, url: str, params: Mapping[str, Any] | None = None) -> bytes:
        key = cache_key(self.source, url, params)
        if self.store.exists(key):
            self.stats["cache_hits"] += 1
            return self.store.read(key)
        data = self._fetch(url, params)
        self.store.write(key, data)
        self.stats["downloads"] += 1
        return data

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        return json.loads(self.get_bytes(url, params))

    def _fetch(self, url: str, params: Mapping[str, Any] | None) -> bytes:
        last_error = ""
        for attempt in range(1, self.retry.max_attempts + 1):
            self.limiter.acquire()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error, retry_after = f"{type(exc).__name__}: {exc}", None
            else:
                if resp.status_code == 200:
                    return resp.content
                if resp.status_code not in self.retry.retry_on_status:
                    raise DownloadError(f"{self.source}: HTTP {resp.status_code} for {resp.url}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            if attempt == self.retry.max_attempts:
                break
            wait = self.retry.delay(attempt, retry_after)
            self.stats["retries"] += 1
            log.warning(
                "%s: %s, retry %d in %.0fs: %s", self.source, last_error, attempt, wait, url
            )
            self.sleep(wait)
        raise DownloadError(
            f"{self.source}: gave up after {self.retry.max_attempts} attempts ({last_error}): {url}"
        )


def client_for(
    source: str,
    store: Store,
    sources_cfg: Mapping[str, Any],
    defaults: Mapping[str, Any],
    **kwargs: Any,
) -> CachedClient:
    """Build a client for ``source`` from ``configs/sources.yaml``."""
    cfg = sources_cfg[source]
    return CachedClient(
        source=source,
        store=store,
        limiter=RateLimiter.from_config(cfg.get("rate_limit")),
        retry=RetryPolicy.from_config(defaults.get("retries")),
        user_agent=defaults.get("user_agent", "f1-forecast-pipeline"),
        timeout_s=float(defaults.get("timeout_s", 60)),
        **kwargs,
    )
