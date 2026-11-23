from __future__ import annotations

import json
from datetime import UTC, datetime

from f1_forecast.ingest.http import CachedClient, LocalStore, RateLimiter
from f1_forecast.ingest.jolpica import (
    RoundId,
    completed_rounds,
    fetch_paged,
    ingest_round,
    rounds_with_race_results,
)

BASE = "https://api.test/ergast/f1/"


def write_csv(store: LocalStore, key: str, header: str, *rows: str) -> None:
    store.write(key, ("\n".join([header, *rows]) + "\n").encode())


def test_rounds_with_race_results_only_counts_classified_race_entries(tmp_path):
    store, f = LocalStore(tmp_path), "jolpica/dump/2026-11-01"
    write_csv(store, f"{f}/formula_one_season.csv", "id,year", "s1,2026")
    write_csv(
        store,
        f"{f}/formula_one_round.csv",
        "id,season_id,number,name",
        "r18,s1,18,USA",
        "r19,s1,19,Mexico",
        "r20,s1,20,Brazil",
    )
    write_csv(
        store,
        f"{f}/formula_one_session.csv",
        "id,round_id,type",
        "a,r18,R",
        "b,r19,Q1",  # qualifying only: race not run yet when the dump was made
        "c,r19,R",
        "d,r20,FP1",
    )
    write_csv(
        store,
        f"{f}/formula_one_sessionentry.csv",
        "id,session_id,position",
        "1,a,1",
        "2,b,1",
        "3,c,",  # entry list exists but no classified position yet
    )
    assert rounds_with_race_results(store, f) == {RoundId(2026, 18)}


def race(rnd: int, day: str, time: str = "14:00:00Z") -> dict:
    return {"season": "2026", "round": str(rnd), "date": day, "time": time}


def test_completed_rounds_waits_for_publish_grace():
    races = [race(20, "2026-11-08"), race(21, "2026-11-22", "06:00:00Z"), race(22, "2026-11-29")]
    now = datetime(2026, 11, 22, 8, 0, tzinfo=UTC)  # 2h after round 21 started
    assert completed_rounds(races, now) == [RoundId(2026, 20)]
    later = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)
    assert completed_rounds(races, later) == [RoundId(2026, 20), RoundId(2026, 21)]


class PagedFakeClient(CachedClient):
    """Serves MRData pages for a given total, without HTTP or rate limiting."""

    def __init__(self, store, totals: dict[str, int]):
        super().__init__(source="jolpica", store=store, limiter=RateLimiter())
        self.totals = totals
        self.fetched: list[tuple[str, int]] = []

    def fetch(self, url, params=None):
        endpoint = url.rsplit("/", 1)[-1].removesuffix(".json")
        self.fetched.append((endpoint, params["offset"]))
        return json.dumps({"MRData": {"total": str(self.totals.get(endpoint, 0))}}).encode()


def test_fetch_paged_walks_all_offsets_and_lands_readable_keys(tmp_path):
    store = LocalStore(tmp_path)
    client = PagedFakeClient(store, {"laps": 1234})
    pages = list(fetch_paged(client, BASE, RoundId(2026, 21), "laps", required=True))
    assert len(pages) == 13
    assert (tmp_path / "jolpica/api/2026/21/laps/offset=01200.json").is_file()
    # second run: everything comes from the store
    client2 = PagedFakeClient(store, {"laps": 1234})
    assert len(list(fetch_paged(client2, BASE, RoundId(2026, 21), "laps", True))) == 13
    assert client2.fetched == []


def test_unpublished_required_endpoint_is_not_stored(tmp_path):
    client = PagedFakeClient(LocalStore(tmp_path), {"laps": 0})
    assert list(fetch_paged(client, BASE, RoundId(2026, 22), "laps", required=True)) == []
    assert not any(tmp_path.rglob("*.json"))


def test_optional_empty_endpoint_is_stored_once(tmp_path):
    client = PagedFakeClient(LocalStore(tmp_path), {"sprint": 0})
    assert len(list(fetch_paged(client, BASE, RoundId(2026, 21), "sprint", required=False))) == 1
    assert (tmp_path / "jolpica/api/2026/21/sprint/offset=00000.json").is_file()


def test_ingest_round_stops_when_results_not_out(tmp_path):
    client = PagedFakeClient(LocalStore(tmp_path), {})
    assert ingest_round(client, BASE, RoundId(2026, 22)) is False
    assert [e for e, _ in client.fetched] == ["results"]


def test_ingest_round_lands_all_endpoints(tmp_path):
    totals = {
        "results": 22,
        "qualifying": 22,
        "laps": 150,
        "sprint": 0,
        "pitstops": 40,
        "driverstandings": 22,
        "constructorstandings": 11,
    }
    client = PagedFakeClient(LocalStore(tmp_path), totals)
    assert ingest_round(client, BASE, RoundId(2026, 21)) is True
    landed = {p.parent.name for p in tmp_path.rglob("offset=*.json")}
    assert landed == set(totals)
