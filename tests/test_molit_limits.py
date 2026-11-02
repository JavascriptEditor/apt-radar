"""HTTP 429 실운영 실패와 중간 적재 재개 회귀 테스트. 외부 API 호출 없음."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from collector import engine as engine_module
from collector.engine import Engine
from collector.molit import ApiError, BudgetExhausted, MolitClient, RateLimited
from collector.store import Store
from tests.fake_servers import gen_trades


def response(status, retry_after=None):
    r = requests.Response()
    r.status_code = status
    r._content = b"<response><header><resultCode>00</resultCode></header></response>"
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    return r


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)


@pytest.fixture
def waits(monkeypatch):
    delays = []
    monkeypatch.setattr("collector.molit.time.sleep", delays.append)
    return delays


def test_persistent_429_stops_without_further_requests(waits):
    session = Session([response(429)] * 5)
    client = MolitClient("key", session=session, sleep=0)
    with pytest.raises(RateLimited):
        client.fetch_month("11680", "202501")
    assert session.calls == client.calls == 5
    assert waits == [2, 4, 6, 8]
    with pytest.raises(RateLimited):
        client.fetch_month("11680", "202412")
    assert session.calls == 5


def test_transient_429_honors_retry_after_and_recovers(waits):
    session = Session([response(429, "12"), response(200)])
    client = MolitClient("key", session=session, sleep=0)
    assert client.fetch_month("11680", "202501") == []
    assert waits == [12]
    assert not client.limited


def test_long_retry_after_defers_to_next_run(waits):
    session = Session([response(429, "3600")])
    with pytest.raises(RateLimited):
        MolitClient("key", session=session).fetch_month("11680", "202501")
    assert session.calls == 1
    assert waits == []


def test_retry_after_http_date(waits):
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    client = MolitClient("key", session=Session([response(429, future), response(200)]), sleep=0)
    client.fetch_month("11680", "202501")
    assert 28 <= waits[0] <= 30


def test_retries_do_not_exceed_call_budget(waits):
    session = Session([response(503)] * 5)
    client = MolitClient("key", call_budget=2, session=session, sleep=0)
    with pytest.raises(BudgetExhausted):
        client.fetch_month("11680", "202501")
    assert session.calls == client.calls == 2


@pytest.mark.parametrize("error", [RateLimited("HTTP 429"), BudgetExhausted("budget"), ApiError("HTTP 500")])
def test_backfill_keeps_fetched_months_and_resumes_failed_month(tmp_path, monkeypatch, error):
    monkeypatch.setattr(engine_module, "this_ym", lambda: "202511")
    client = MolitClient("key", sleep=0)
    failed = "202503"

    def fetch(code, ym):
        client.calls += 1
        if ym == failed:
            raise error
        return gen_trades(code, ym)

    monkeypatch.setattr(client, "fetch_month", fetch)
    store = Store(tmp_path)
    eng = Engine(store, client, region_filter="11680", log=lambda *a: None)
    if isinstance(error, ApiError):
        with pytest.raises(ApiError):
            eng.run_trades(reserve=0)
    else:
        result = eng.run_trades(reserve=0)
        assert result["stopped"].startswith("rate_limit:" if isinstance(error, RateLimited) else "budget:")
    saved = store.read_json("progress.json", {})
    assert saved["backfill"]["11680"]["cursor"] == failed
    assert saved["callsToday"] == 9
    rows = store.read_trades_year("11680", "2025")
    assert {r["ym"] for r in rows} >= {"202504", "202505"}
    eng2 = Engine(store, MolitClient("key"), region_filter="11680", log=lambda *a: None)
    assert eng2.progress["backfill"]["11680"]["cursor"] == failed


def test_interrupted_scan_is_not_marked_complete(tmp_path, monkeypatch):
    client = MolitClient("key")
    monkeypatch.setattr(client, "count_month", lambda *a: (_ for _ in ()).throw(RateLimited("HTTP 429")))
    eng = Engine(Store(tmp_path), client, region_filter="11680", log=lambda *a: None)
    eng.run_scan(force=True)
    assert "scanAt" not in eng.progress
