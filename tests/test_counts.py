"""건수 기반 매물 내림 추정: 단지별 매매 매물 수 관측(engine.run_counts·listing_counts.csv) → 감소 구간 ↔ 실거래
(pipeline.link_count_drops). 실제 접속 없음."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector import pipeline as pl  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.naver import Blocked, NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from tests import fake_servers as fs  # noqa: E402

CX = {
    "11680-개포동-12-2019": {"name": "래미안블레스티지", "naverId": "100001", "umdCd": "10300"},
    "11680-개포동-1234-2023": {"name": "개포자이프레지던스", "naverId": "100002", "umdCd": "10300"},
    "11680-대치동-316-1979": {"name": "은마", "naverId": "100003", "umdCd": "10600"},
    "11680-대치동-999-2001": {"name": "미연결단지", "naverId": "", "umdCd": "10600"},
}


class CountNaver(NaverClient):
    """동(cortar) → [(naverId, dealCnt)] 를 돌려주는 가짜. block_after 번째 호출부터 차단."""

    def __init__(self, table, block_after=None):
        super().__init__(load_cfg(), state_path=None, delay=0, max_calls=50)
        self.table, self.block_after, self.asked = table, block_after, []

    def complexes(self, cortar_no):
        self.calls += 1
        if self.block_after is not None and self.calls > self.block_after:
            self.blocked_reason = "HTTP 307 요청 제한 (mobile)"
            raise Blocked(self.blocked_reason)
        self.asked.append(cortar_no)
        self.last_ok = cortar_no in self.table
        return [{"naverId": nid, "naverName": "", "dealCnt": n} for nid, n in self.table.get(cortar_no, [])]


def _engine(tmp_path, naver):
    store = Store(tmp_path / "data")
    store.write_complexes({cid: {"cid": cid, "aptSeq": "", "sgg": "11680", "umd": cid.split("-")[1],
                                 "jibun": cid.split("-")[2], "buildYear": cid.split("-")[3], **v} for cid, v in CX.items()})
    return Engine(store, None, naver, region_filter="11680", log=lambda *a: None)


@pytest.fixture
def day(monkeypatch):
    def set_day(d):
        monkeypatch.setattr(eng_mod, "today_str", lambda: d)
    return set_day


# ----------------------------------------------------------------------------- 수집

def test_run_counts_records_matched_complexes_and_skips_same_day(tmp_path, day):
    day("2026-11-01")
    nv = CountNaver({"1168010300": [("100001", 12), ("100002", 3), ("999999", 50)], "1168010600": [("100003", 20)]})
    eng = _engine(tmp_path, nv)
    res = eng.run_counts(max_dongs=10)
    assert res["status"] == "ok" and res["dongs"] == 2 and res["complexes"] == 3 and res["calls"] == 2
    rows = eng.store.read_listing_counts()
    assert {(r["cid"], r["deal"]) for r in rows} == {("11680-개포동-12-2019", 12), ("11680-개포동-1234-2023", 3),
                                                      ("11680-대치동-316-1979", 20)}       # 미연결 단지·남의 단지는 없음
    # 같은 날 다시 돌리면 조회하지 않는다
    res2 = _engine(tmp_path, CountNaver(nv.table)).run_counts(max_dongs=10)
    assert res2["dongs"] == 0 and res2["calls"] == 0 and res2["pending"] == 0
    # 다음 날: 건수가 바뀐 값이 새 행으로 쌓인다
    day("2026-11-02")
    nv3 = CountNaver({"1168010300": [("100001", 10), ("100002", 3)], "1168010600": [("100003", 20)]})
    _engine(tmp_path, nv3).run_counts(max_dongs=10)
    rows = [r for r in Store(tmp_path / "data").read_listing_counts() if r["cid"] == "11680-개포동-12-2019"]
    assert [(r["date"], r["deal"]) for r in rows] == [("2026-11-01", 12), ("2026-11-02", 10)]


def test_run_counts_oldest_first_and_failed_dong_not_recorded(tmp_path, day):
    day("2026-11-01")
    _engine(tmp_path, CountNaver({"1168010300": [("100001", 12)]})).run_counts(max_dongs=1)   # 개포동만(단지 2개라 먼저)
    day("2026-11-03")
    nv = CountNaver({"1168010300": [("100001", 11)]})          # 대치동은 응답 실패
    res = _engine(tmp_path, nv).run_counts(max_dongs=1)
    assert nv.asked == ["1168010600"]                           # 한 번도 못 본 동부터
    assert res["failedDongs"] == 1 and res["complexes"] == 0 and res["pending"] == 1
    assert all(r["cid"] != "11680-대치동-316-1979" for r in Store(tmp_path / "data").read_listing_counts())


def test_run_counts_blocked_keeps_what_it_got(tmp_path, day):
    day("2026-11-01")
    nv = CountNaver({"1168010300": [("100001", 12)], "1168010600": [("100003", 20)]}, block_after=1)
    res = _engine(tmp_path, nv).run_counts(max_dongs=10)
    assert res["status"].startswith("blocked:") and res["complexes"] == 1
    assert len(Store(tmp_path / "data").read_listing_counts()) == 1


def test_match_records_counts_from_same_response(tmp_path):
    """단지 연결(run_match) 응답에도 dealCnt 가 있으므로 추가 호출 없이 건수가 쌓인다(실제 설정의 필드 이름 확인)."""
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    try:
        fs.Handler.deal_counts = {"100001": 9}
        store = Store(tmp_path / "data")
        store.write_complexes({cid: {"cid": cid, "aptSeq": "", "sgg": "11680", "umd": cid.split("-")[1], "jibun": cid.split("-")[2],
                                     "buildYear": cid.split("-")[3], "name": v["name"], "umdCd": v["umdCd"]}
                               for cid, v in list(CX.items())[:3]})
        nv = NaverClient(fs.naver_cfg_for(base, load_cfg()), state_path=None, delay=0, max_calls=50)
        res = Engine(store, None, nv, region_filter="11680", log=lambda *a: None).run_match()
        assert res["matched"] >= 2 and res["counted"] == res["matched"]
        got = {r["cid"]: r["deal"] for r in store.read_listing_counts()}
        assert got["11680-개포동-12-2019"] == 9 and got["11680-개포동-1234-2023"] == 7
    finally:
        fs.Handler.deal_counts = {}
        srv.shutdown()


def test_upsert_counts_overwrites_same_day_and_prunes(tmp_path):
    st = Store(tmp_path / "data")
    st.upsert_listing_counts([{"date": "2025-01-01", "cid": "a", "naverId": "1", "deal": 5},
                              {"date": "2026-11-01", "cid": "a", "naverId": "1", "deal": 4}])
    n = st.upsert_listing_counts([{"date": "2026-11-01", "cid": "a", "naverId": "1", "deal": 2}], keep_since="2026-01-01")
    assert n == 1 and [(r["date"], r["deal"]) for r in st.read_listing_counts()] == [("2026-11-01", 2)]


# ----------------------------------------------------------------------------- 감소 ↔ 실거래

def _obs(cid, *pairs):
    return [{"cid": cid, "date": d, "deal": n} for d, n in pairs]


def _trade(date, price=100000, area=84.9, floor=5, direct=0, cancelled=0):
    return [date, price, area, floor, "101", direct, cancelled]


def test_drop_confirmed_by_trades_one_to_one():
    counts = _obs("c1", ("2026-09-01", 10), ("2026-09-05", 7), ("2026-09-10", 7))
    trades = {"c1": [_trade("2026-08-25"), _trade("2026-09-20"), _trade("2026-06-01"), _trade("2026-09-03", direct=1)]}
    rows = pl.link_count_drops(counts, trades, "2026-10-01")
    assert len(rows) == 1
    r = rows[0]
    assert (r["src"], r["cid"], r["date"], r["prevDate"], r["n"], r["from"], r["to"]) == ("count", "c1", "2026-09-05", "2026-09-01", 3, 10, 7)
    # 창 [8-18, 11-04] 안의 일반 거래 2건만(직거래·창 밖 제외). 남은 1건은 대기
    assert r["units"] == {"confirmed": 2, "relisted": 0, "pending": 1, "unconfirmed": 0} and r["status"] == "confirmed"
    assert r["trade"]["date"] == "2026-08-25" and r["daysToTrade"] == -11 and len(r["trades"]) == 2
    assert r["price"] is None and r["grp"] is None


def test_trade_is_used_by_only_one_drop_and_old_drop_becomes_unconfirmed():
    counts = _obs("c1", ("2026-06-01", 5), ("2026-06-03", 4), ("2026-06-10", 3))
    rows = pl.link_count_drops(counts, {"c1": [_trade("2026-06-05")]}, "2026-11-01")
    assert [r["units"]["confirmed"] for r in rows] == [1, 0]
    assert rows[1]["status"] == "unconfirmed" and rows[1]["units"]["unconfirmed"] == 1      # 75일 지남


def test_count_coming_back_is_relisted_not_sold():
    counts = _obs("c1", ("2026-10-01", 8), ("2026-10-04", 5), ("2026-10-09", 7))
    r = pl.link_count_drops(counts, {"c1": [_trade("2026-10-05")]}, "2026-10-20")[0]
    assert r["units"] == {"confirmed": 1, "relisted": 2, "pending": 0, "unconfirmed": 0}
    counts = _obs("c2", ("2026-10-01", 8), ("2026-10-04", 5), ("2026-10-09", 9))
    r = pl.link_count_drops(counts, {}, "2026-10-20")[0]
    assert r["status"] == "relisted" and r["units"]["relisted"] == 3


def test_long_gap_rise_and_future_rows_are_ignored():
    counts = (_obs("c1", ("2026-08-01", 9), ("2026-09-15", 4))          # 45일 간격 → 버림
              + _obs("c2", ("2026-10-01", 4), ("2026-10-05", 6))        # 증가
              + _obs("c3", ("2026-10-01", 4), ("2026-12-25", 1)))       # 미래 관측
    assert pl.link_count_drops(counts, {}, "2026-10-20") == []


def test_accuracy_counts_units_not_events():
    counts = _obs("c1", ("2026-06-01", 10), ("2026-06-03", 6))
    links = pl.link_count_drops(counts, {"c1": [_trade("2026-06-10")]}, "2026-11-01")
    acc = pl.accuracy_stats(links)["gone"]
    assert (acc["n"], acc["confirmed"], acc["unconfirmed"], acc["hitRate"]) == (4, 1, 3, 0.25)


def test_complex_summary_counts_block_and_timeline():
    today = dt.date(2026, 10, 20)
    counts = _obs("c1", ("2026-10-01", 8), ("2026-10-04", 5), ("2026-10-15", 5))
    trades = [_trade("2026-10-06")]
    links = pl.link_count_drops(counts, {"c1": trades}, today)
    blk = pl.complex_summary("c1", {}, [], [], trades, None, {"level": 2}, today, counts_rows=counts, count_links=links)
    c = blk["counts"]
    assert (c["now"], c["at"], c["drop30"], c["confirmed90"]) == (5, "2026-10-15", 3, 1)
    assert c["series"] == [["2026-10-01", 8], ["2026-10-04", 5], ["2026-10-15", 5]] and c["drops"][0]["n"] == 3
    assert any(e["type"] == "gone" and "3건 감소" in e["text"] and "실거래 1건 확인" in e["text"] for e in blk["timeline"])
    assert pl.complex_summary("c1", {}, [], [], trades, None, {"level": 0}, today)["counts"] is None


# ----------------------------------------------------------------------------- 환금성(거래 빈도)

def test_liquidity_stats_counts_recent_usable_trades():
    trades = [_trade("2026-10-01", area=84.9), _trade("2026-10-20", area=84.9), _trade("2026-03-05", area=59.9),
              _trade("2025-06-01", area=84.9), _trade("2024-01-10", area=59.9),
              _trade("2026-09-01", direct=1), _trade("2026-09-02", cancelled=1),     # 직거래·해제 제외
              _trade("2023-01-01"), _trade("2026-12-25")]                             # 36개월 밖·미래 제외
    st = pl.liquidity_stats(trades, "2026-11-01")
    assert (st["n12"], st["n36"], st["perYear"], st["months12"]) == (3, 5, 1.7, 2)
    assert st["last"] == "2026-10-20" and st["daysSince"] == 12
    assert st["byGrp"] == {"59": 1, "84": 2} and st["mainGrp"] == "84"
    # 모델의 월별 건수(tm)가 있으면 건수는 그 값으로(거래 목록은 240건에서 잘림). 이번 달 포함 12개 달 / 36개 달
    tm = {"202611": 4, "202610": 30, "202512": 6, "202511": 9, "202312": 5, "202311": 7, "202301": 99}
    st2 = pl.liquidity_stats(trades, "2026-11-01", monthly=tm)
    assert (st2["n12"], st2["n36"], st2["months12"], st2["perYear"]) == (40, 54, 3, 18.0)
    assert st2["last"] == "2026-10-20" and st2["byGrp"] == {"59": 1, "84": 2}
    empty = pl.liquidity_stats([], "2026-11-01")
    assert empty["n12"] == 0 and empty["last"] is None and empty["mainGrp"] is None


def test_rank_liquidity_within_sgg():
    mk = lambda n12, n36, last: {"n12": n12, "n36": n36, "last": last}  # noqa: E731
    st = {"a": mk(10, 20, "2026-10-01"), "b": mk(10, 30, "2026-09-01"), "c": mk(0, 2, "2025-01-01"),
          "d": mk(3, 3, "2026-08-01"), "x": mk(1, 1, "2026-01-01")}
    pl.rank_liquidity(st, {"a": "11680", "b": "11680", "c": "11680", "d": "11680", "x": "11650"})
    assert [st[k]["rank"] for k in "abcd"] == [2, 1, 4, 3] and st["a"]["of"] == 4      # 동률이면 36개월 건수
    assert st["b"]["level"] == "높음" and st["c"]["level"] == "거래 없음" and st["d"]["level"] == "낮음"
    assert (st["x"]["rank"], st["x"]["of"], st["x"]["pct"]) == (1, 1, 1.0)             # 다른 구는 따로
