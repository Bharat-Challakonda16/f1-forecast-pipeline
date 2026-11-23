from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import requests

from f1_forecast.config import load_source_defaults, load_sources
from f1_forecast.ingest.http import (
    CachedClient,
    DownloadError,
    LocalStore,
    RateLimiter,
    RetryPolicy,
    cache_key,
    client_for,
)


class FakeClock:
    """Deterministic time: sleeping just advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b"{}"
    headers: dict[str, str] = field(default_factory=dict)
    url: str = "https://example.test"


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(tmp_path, responses, **limiter_kw):
    clock = FakeClock()
    session = FakeSession(responses)
    client = CachedClient(
        source="test",
        store=LocalStore(tmp_path),
        limiter=RateLimiter(clock=clock, sleep=clock.sleep, **limiter_kw),
        retry=RetryPolicy(max_attempts=3, backoff_initial_s=1, backoff_max_s=10),
        session=session,
        sleep=clock.sleep,
    )
    return client, session, clock


# ----------------------------------------------------------------------------- rate limiter


def test_min_interval_spaces_calls():
    clock = FakeClock()
    lim = RateLimiter(min_interval_s=0.25, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        lim.acquire()
    assert clock.now == pytest.approx(0.75)


def test_window_limit_blocks_until_oldest_call_expires():
    clock = FakeClock()
    lim = RateLimiter(windows=[(3, 60)], clock=clock, sleep=clock.sleep)
    for _ in range(3):
        lim.acquire()
    assert clock.now == 0
    lim.acquire()  # 4th call must wait for the window to roll
    assert clock.now == pytest.approx(60)


def test_multiple_windows_all_respected():
    clock = FakeClock()
    lim = RateLimiter(windows=[(2, 1), (3, 100)], clock=clock, sleep=clock.sleep)
    for _ in range(4):
        lim.acquire()
    # calls at t=0, 0, 1 (per-second cap), then 100 (per-100s cap)
    assert clock.now == pytest.approx(100)


def test_limiter_from_config():
    lim = RateLimiter.from_config(
        {"min_interval_s": 0.3, "windows": [{"calls": 450, "period_s": 3600}]}
    )
    assert lim.min_interval_s == 0.3
    assert lim.windows == [(450, 3600.0)]


# ----------------------------------------------------------------------------- cache key


def test_cache_key_ignores_param_order_and_is_readable():
    a = cache_key("jolpica", "https://x/f1/2026/1/laps.json", {"limit": 100, "offset": 0})
    b = cache_key("jolpica", "https://x/f1/2026/1/laps.json", {"offset": 0, "limit": 100})
    assert a == b
    assert a.startswith("jolpica/laps-")


def test_cache_key_differs_by_params():
    url = "https://x/laps.json"
    assert cache_key("s", url, {"offset": 0}) != cache_key("s", url, {"offset": 100})


# ----------------------------------------------------------------------------- client


def test_second_request_served_from_cache(tmp_path):
    client, session, _ = make_client(tmp_path, [FakeResponse(200, b'{"a": 1}')])
    assert client.get_json("https://x/a.json") == {"a": 1}
    assert client.get_json("https://x/a.json") == {"a": 1}
    assert len(session.calls) == 1
    assert client.stats == {"cache_hits": 1, "downloads": 1, "retries": 0}


def test_cache_survives_new_client(tmp_path):
    client, _, _ = make_client(tmp_path, [FakeResponse(200, b"[1]")])
    client.get_json("https://x/a.json")
    client2, session2, _ = make_client(tmp_path, [])
    assert client2.get_json("https://x/a.json") == [1]
    assert session2.calls == []


def test_retries_on_429_honouring_retry_after(tmp_path):
    client, session, clock = make_client(
        tmp_path, [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, b"[]")]
    )
    assert client.get_json("https://x/a.json") == []
    assert clock.slept == [7.0]
    assert client.stats["retries"] == 1


def test_retries_connection_errors_with_backoff(tmp_path):
    client, _, clock = make_client(
        tmp_path,
        [requests.ConnectionError("boom"), FakeResponse(503), FakeResponse(200, b"{}")],
    )
    client.get_json("https://x/a.json")
    assert clock.slept == [1, 2]  # exponential: 1s, 2s


def test_gives_up_after_max_attempts_and_caches_nothing(tmp_path):
    client, _, _ = make_client(tmp_path, [FakeResponse(500)] * 3)
    with pytest.raises(DownloadError, match="gave up after 3"):
        client.get_json("https://x/a.json")
    assert not any(tmp_path.rglob("*.json"))


def test_non_retryable_status_fails_immediately(tmp_path):
    client, session, _ = make_client(tmp_path, [FakeResponse(404)])
    with pytest.raises(DownloadError, match="HTTP 404"):
        client.get_json("https://x/missing.json")
    assert len(session.calls) == 1


def test_client_for_reads_repo_config(tmp_path):
    client = client_for("openf1", LocalStore(tmp_path), load_sources(), load_source_defaults())
    assert client.limiter.windows == [(28, 60.0)]
    assert "non-commercial" in client.user_agent
