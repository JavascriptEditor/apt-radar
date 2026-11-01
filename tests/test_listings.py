"""매물 상태 기계(engine.run_listings) + 네이버 fin.land 클라이언트 테스트. 실제 접속 없음(가짜 서버만).

DESIGN.md 2절: miss 2회→gone, 실패 응답 무변화, 의심 응답 가드, relist, price 이벤트, 120일 정리, swap, 구 파일 호환.
"""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector.engine import Engine, apply_articles, date_add  # noqa: E402
from collector.naver import Blocked, NaverClient, load_cfg  # noqa: E402
from collector.store import EVENT_FIELDS, LISTING_FIELDS, Store  # noqa: E402
from tests import fake_servers as fs  # noqa: E402

CX = {  # 가짜 세계의 단지 중 강남구 3개 (네이버 id 100001~100003)
    "11680-개포동-12-2019": {"name": "래미안블레스티지", "naverId": "100001", "lastYm": "202610"},
    "11680-개포동-1234-2023": {"name": "개포자이프레지던스", "naverId": "100002", "lastYm": "202609"},
    "11680-대치동-316-1979": {"name": "은마", "naverId": "100003", "lastYm": "202610"},
}


@pytest.fixture(scope="module")
def server():
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    yield base
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_fake():
    fs.Handler.articles_override = {}
    fs.Handler.fail_articles = {}
    yield
    fs.Handler.articles_override = {}
    fs.Handler.fail_articles = {}


def make_engine(tmp, base, endpoints=None) -> Engine:
    store = Store(tmp / "data")
    if not (tmp / "data" / "complexes.csv").exists():
        cx = {cid: {"cid": cid, "sgg": "11680", "umdCd": "10300", "umd": cid.split("-")[1], "jibun": cid.split("-")[2],
                    "buildYear": cid.split("-")[3], "firstYm": "201901", "nTrades": 100, **v} for cid, v in CX.items()}
        store.write_complexes(cx)
    nv = NaverClient(fs.naver_cfg_for(base, load_cfg(), endpoints), state_path=tmp / "data" / "naver_cfg.json")
    return Engine(store, None, nv, region_filter="11680", log=lambda *a: None)


def rows_of(e: Engine, cid: str) -> dict[str, dict]:
    return {k: v for k, v in e.store.read_listings().items() if v["cid"] == cid}


def events_of(e: Engine, kind: str | None = None, cid: str | None = None) -> list[dict]:
    return [r for r in e.store.read_events() if (kind is None or r["event"] == kind) and (cid is None or r["cid"] == cid)]


# ------------------------------------------------------------------ 네이버 클라이언트
def test_fin_land_endpoint_and_cursor_pages(tmp_path, server):
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n.json")
    arts, ok = nv.articles_ok("100001")
    assert ok and arts
    assert nv.state["articles"] == "fin_list"
    expected = fs.gen_articles("100001", eng_mod.this_ym())
    assert len(arts) == len(expected)                       # lastInfo 커서로 모든 페이지를 받음
    assert nv.calls == (len(expected) + fs.Handler.FIN_PAGE - 1) // fs.Handler.FIN_PAGE
    by_id = {a["atclNo"]: a for a in arts}
    for m in expected:
        a = by_id[m["atclNo"]]
        fl, tp = m["flrInfo"].split("/")
        assert a["floor"] == int(fl) and a["top"] == int(tp)
        assert a["dong"] == m["bildNm"].replace("동", "") and a["price"] == fs.parse_listing_price(m["prcInfo"])
        assert abs(a["area"] - float(m["spc2"])) < 1e-6 and a["grp"] == int(float(m["spc2"]))
        assert a["sameCnt"] == m["sameAddrCnt"] and a["confirmYmd"] == "20251028" and a["direct"] == 0
        assert a["naverId"] == "100001"
    # 모바일(m.land) 경로와 같은 매물·같은 호가
    nv2 = NaverClient(fs.naver_cfg_for(server, load_cfg(), ["mobile"]), state_path=tmp_path / "n2.json")
    arts2, ok2 = nv2.articles_ok("100001")
    assert ok2 and nv2.state["articles"] == "mobile"
    assert {(a["atclNo"], a["price"], a["floor"]) for a in arts2} == {(a["atclNo"], a["price"], a["floor"]) for a in arts}
    # count endpoint
    assert nv.article_count("100001") == len(expected)
    # articles() 는 목록만
    assert len(nv.articles("100001")) == len(expected)


def test_fin_fail_falls_back_to_mobile(tmp_path, server):
    cfg = fs.naver_cfg_for(server, load_cfg())
    cfg["articles"][0]["url"] = server + "/nope/article/list"   # fin 404 → mobile 로
    nv = NaverClient(cfg, state_path=tmp_path / "n.json")
    arts, ok = nv.articles_ok("100001")
    assert ok and arts and nv.state["articles"] == "mobile"


def test_articles_ok_false_on_failures(tmp_path, server):
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n.json")
    for mode in ("500", "html", "json_error"):
        fs.Handler.fail_articles = {"100002": mode}
        arts, ok = nv.articles_ok("100002")
        assert ok is False and arts == [], mode
        assert not nv.blocked_reason
    fs.Handler.fail_articles = {}
    assert nv.articles_ok("100002")[1] is True
    # 매물 0건(정상 빈 응답)은 ok=True
    fs.Handler.articles_override = {"100002": None}
    assert nv.articles_ok("100002") == ([], True)


def test_blocked_403_and_429(tmp_path, server):
    for path, code in (("/blocked", "403"), ("/blocked429", "429")):
        cfg = fs.naver_cfg_for(server, load_cfg())
        for ep in cfg["articles"]:
            ep["url"] = server + path + "?x={complexNo}&page={page}"
        nv = NaverClient(cfg, state_path=tmp_path / f"n{code}.json")
        with pytest.raises(Blocked):
            nv.articles_ok("100001")
        assert code in nv.blocked_reason
        with pytest.raises(Blocked):   # 이후 호출은 즉시 Blocked
            nv.articles_ok("100002")


# ------------------------------------------------------------------ 상태 기계
def test_new_then_miss_twice_gone_then_relist(tmp_path, server):
    e = make_engine(tmp_path, server)
    today = eng_mod.today_str()
    r1 = e.run_listings()
    assert r1["status"] == "ok" and r1["complexes"] == 3 and r1["endpoint"] == "fin_list"
    assert r1["new"] == r1["n"] > 0 and r1["gone"] == r1["suspect"] == r1["failed"] == 0
    rows = rows_of(e, "11680-개포동-12-2019")
    assert all(v["status"] == "active" and v["miss"] == "0" and v["firstSeen"] == today for v in rows.values())
    assert all(v["priceFirst"] == v["price"] and v["priceChanges"] == "0" for v in rows.values())
    assert len(events_of(e, "new")) == r1["new"]
    assert e.complexes["11680-개포동-12-2019"]["listingsN"] == len(rows)

    # 매물 X 하나가 응답에서 사라짐 → 1회: miss=1, active 유지
    base = fs.gen_articles("100001", eng_mod.this_ym())
    x = base[0]["atclNo"]
    fs.Handler.articles_override = {"100001": base[1:]}
    r2 = e.run_listings()
    row = rows_of(e, "11680-개포동-12-2019")[x]
    assert row["status"] == "active" and row["miss"] == "1" and row["goneAt"] == ""
    assert r2["gone"] == 0 and not events_of(e, "gone")
    # 2회: gone
    r3 = e.run_listings()
    row = rows_of(e, "11680-개포동-12-2019")[x]
    assert row["status"] == "gone" and row["miss"] == "2" and row["goneAt"] == today
    assert r3["gone"] == 1 and r3["nGone"] == 1 and r3["n"] == r1["n"] - 1
    g = events_of(e, "gone")
    assert len(g) == 1 and g[0]["atclNo"] == x and g[0]["price"] == row["price"] and g[0]["prevPrice"] == ""
    assert g[0]["grp"] == row["grp"] and g[0]["dong"] == row["dong"] and g[0]["floor"] == row["floor"]
    # 다른 매물은 그대로
    assert all(v["miss"] == "0" for k, v in rows_of(e, "11680-개포동-12-2019").items() if k != x)

    # 더 사라져 있어도 gone 행의 miss 는 더 늘지 않고 이벤트도 다시 나지 않음
    e.run_listings()
    row = rows_of(e, "11680-개포동-12-2019")[x]
    assert row["status"] == "gone" and row["miss"] == "2" and len(events_of(e, "gone")) == 1

    # 다시 나타남 → relist (호가 그대로면 price 이벤트 없음)
    fs.Handler.articles_override = {}
    r5 = e.run_listings()
    row = rows_of(e, "11680-개포동-12-2019")[x]
    assert row["status"] == "active" and row["miss"] == "0" and row["goneAt"] == ""
    assert row["firstSeen"] == today and r5["relist"] == 1 and r5["new"] == 0
    rl = events_of(e, "relist")
    assert len(rl) == 1 and rl[0]["atclNo"] == x and rl[0]["price"] == row["price"] and rl[0]["prevPrice"] == row["price"]
    assert not events_of(e, "price")


def test_failed_response_changes_nothing(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    before = e.store.read_listings()
    n_events = len(e.store.read_events())
    at = e.complexes["11680-개포동-12-2019"]["listingsAt"]
    for mode in ("500", "html", "json_error"):
        fs.Handler.fail_articles = {"100001": mode}
        r = e.run_listings()
        assert r["status"] == "ok" and r["failed"] == 1 and r["gone"] == r["new"] == 0, mode
        assert e.store.read_listings() == before, mode
        assert len(e.store.read_events()) == n_events
    # 실패해도 순환은 이어짐(listingsAt 갱신), listingsN 은 유지
    assert e.complexes["11680-개포동-12-2019"]["listingsAt"] >= at
    assert e.complexes["11680-개포동-12-2019"]["listingsN"] == len(rows_of(e, "11680-개포동-12-2019"))
    # 실패 중에도 다른 단지의 매물은 정상 반영(한 매물 제거 → miss 1)
    base = fs.gen_articles("100003", eng_mod.this_ym())
    fs.Handler.articles_override = {"100003": base[1:]}
    e.run_listings()
    assert rows_of(e, "11680-대치동-316-1979")[base[0]["atclNo"]]["miss"] == "1"


def test_suspect_guard(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    cid = "11680-개포동-12-2019"
    assert int(e.complexes[cid]["listingsN"]) >= eng_mod.SUSPECT_MIN_PREV
    before = rows_of(e, cid)
    fs.Handler.articles_override = {"100001": None}   # 0건(정상 JSON)
    for k in range(1, eng_mod.SUSPECT_ACCEPT_AFTER):
        r = e.run_listings()
        assert r["suspect"] == 1 and r["failed"] == 0 and r["gone"] == 0, k
        assert rows_of(e, cid) == before
        assert e.complexes[cid]["listingsN"] == len(before)
        assert e.progress["listingsSuspect"]["100001"] == k
    # 연속 SUSPECT_ACCEPT_AFTER 회면 실제 0건으로 받아들임 → miss 1
    r = e.run_listings()
    assert r["suspect"] == 0 and all(v["miss"] == "1" for v in rows_of(e, cid).values())
    assert e.complexes[cid]["listingsN"] == 0 and "100001" not in e.progress["listingsSuspect"]
    # 직전 5건 미만이면 0건도 그냥 반영
    cid2 = "11680-대치동-316-1979"
    fs.Handler.articles_override = {"100003": fs.gen_articles("100003", eng_mod.this_ym())[:3]}
    e.run_listings()
    e.run_listings()
    assert e.complexes[cid2]["listingsN"] == 3
    fs.Handler.articles_override = {"100003": None}
    r = e.run_listings()
    assert r["suspect"] == 0
    assert sum(1 for v in rows_of(e, cid2).values() if v["miss"] == "1") == 3


def test_price_event(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    base = fs.gen_articles("100001", eng_mod.this_ym())
    x = dict(base[0])
    old_price = fs.parse_listing_price(x["prcInfo"])
    eok, man = divmod(old_price - 10000, 10000)
    x["prcInfo"] = f"{eok}억 {man:,}" if man else f"{eok}억"
    new_price = fs.parse_listing_price(x["prcInfo"])
    assert new_price == old_price - 10000
    fs.Handler.articles_override = {"100001": [x] + base[1:]}
    r = e.run_listings()
    assert r["price"] == 1
    row = rows_of(e, "11680-개포동-12-2019")[x["atclNo"]]
    assert row["price"] == str(new_price) and row["priceFirst"] == str(old_price) and row["priceChanges"] == "1"
    ev = events_of(e, "price")
    assert len(ev) == 1 and ev[0]["price"] == str(new_price) and ev[0]["prevPrice"] == str(old_price)
    # 같은 호가면 더 이상 이벤트 없음
    e.run_listings()
    assert len(events_of(e, "price")) == 1 and rows_of(e, "11680-개포동-12-2019")[x["atclNo"]]["priceChanges"] == "1"


def test_gone_cleanup_120d_and_event_prune_400d(tmp_path, server):
    e = make_engine(tmp_path, server)
    today = eng_mod.today_str()
    cid = "11680-개포동-12-2019"
    mk = lambda aid, status, gone_at: {"atclNo": aid, "cid": cid, "naverId": "100001", "grp": 84, "price": 300000,  # noqa: E731
                                       "firstSeen": "2025-01-01", "lastSeen": "2025-06-01", "status": status,
                                       "goneAt": gone_at, "miss": 2 if status == "gone" else 0}
    e.store.write_listings({
        "old-gone": mk("old-gone", "gone", date_add(today, -(eng_mod.GONE_KEEP_DAYS + 1))),
        "edge-gone": mk("edge-gone", "gone", date_add(today, -eng_mod.GONE_KEEP_DAYS)),
        "recent-gone": mk("recent-gone", "gone", date_add(today, -30)),
    })
    e.store.append_events([
        {"date": date_add(today, -(eng_mod.EVENT_KEEP_DAYS + 1)), "atclNo": "old-gone", "cid": cid, "event": "gone", "price": 300000},
        {"date": date_add(today, -eng_mod.EVENT_KEEP_DAYS), "atclNo": "edge-gone", "cid": cid, "event": "gone", "price": 300000},
        {"date": date_add(today, -30), "atclNo": "recent-gone", "cid": cid, "event": "gone", "price": 300000},
    ])
    r = e.run_listings()
    rows = e.store.read_listings()
    assert "old-gone" not in rows and "edge-gone" in rows and "recent-gone" in rows
    assert r["pruned"] == 1 and r["prunedEvents"] == 1
    ev = e.store.read_events()
    assert [x["atclNo"] for x in ev if x["event"] == "gone"] == ["edge-gone", "recent-gone"]   # 이벤트는 남음(400일 이내)
    assert [x["date"] for x in ev] == sorted(x["date"] for x in ev)                             # 날짜 오름차순
    # gone 행은 응답에 없어도 miss 가 늘지 않는다
    assert rows["recent-gone"]["miss"] == "2" and rows["recent-gone"]["status"] == "gone"
    # since 필터
    assert all(x["date"] >= date_add(today, -31) for x in e.store.read_events(since=date_add(today, -31)))


def test_swap_duplicate_listing_no_event(tmp_path, server):
    e = make_engine(tmp_path, server)
    base = fs.gen_articles("100001", eng_mod.this_ym())
    a = dict(base[0])
    a["sameAddrCnt"] = 3
    fs.Handler.articles_override = {"100001": [a] + base[1:]}
    e.run_listings()
    # 같은 집(동·층·면적·호가)의 새 대표 매물 b 가 a 를 대체
    b = dict(a)
    b["atclNo"] = a["atclNo"] + "X"
    fs.Handler.articles_override = {"100001": [b] + base[1:]}
    e.run_listings()
    r = e.run_listings()
    rows = rows_of(e, "11680-개포동-12-2019")
    assert rows[a["atclNo"]]["status"] == "gone" and rows[b["atclNo"]]["status"] == "active"
    assert r["swap"] == 1 and r["gone"] == 0
    assert not events_of(e, "gone")
    assert [x["atclNo"] for x in events_of(e, "new") if x["atclNo"] == b["atclNo"]]
    # sameCnt == 1 이면 같은 집이 남아 있어도 gone 이벤트
    fs.Handler.articles_override = {"100001": [{**b, "sameAddrCnt": 1}] + base[1:]}
    e.run_listings()
    c = {**b, "atclNo": b["atclNo"] + "Y", "sameAddrCnt": 1}
    fs.Handler.articles_override = {"100001": [c] + base[1:]}
    e.run_listings()
    r = e.run_listings()
    assert r["gone"] == 1 and r["swap"] == 0 and len(events_of(e, "gone")) == 1


def test_blocked_mid_run_keeps_progress(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    before = e.store.read_listings()
    cfg = fs.naver_cfg_for(server, load_cfg())
    for ep in cfg["articles"]:
        ep["url"] = server + "/blocked429?x={complexNo}&page={page}"
    e.naver = NaverClient(cfg, state_path=tmp_path / "nb.json")
    r = e.run_listings()
    assert r["status"].startswith("blocked") and "429" in r["status"]
    assert e.store.read_listings() == before


def test_events_file_schema_and_old_csv_compat(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    with open(tmp_path / "data" / "listing_events.csv", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        rows = list(rd)
    assert header == EVENT_FIELDS and rows and all(len(r) == len(EVENT_FIELDS) for r in rows)
    assert {r[EVENT_FIELDS.index("event")] for r in rows} == {"new"}
    with open(tmp_path / "data" / "listings.csv", encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == LISTING_FIELDS
    assert "fair" not in LISTING_FIELDS and "grade" not in LISTING_FIELDS

    # 구 형식(판정 컬럼 포함, status 없음) 파일 → 기본값으로 채워 읽힘
    old_fields = LISTING_FIELDS[:LISTING_FIELDS.index("status")] + ["fair", "diffPct", "grade", "reasons", "conf"]
    p = tmp_path / "old" / "listings.csv"
    p.parent.mkdir()
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=old_fields)
        w.writeheader()
        w.writerow({"atclNo": "1", "cid": "c", "price": "123", "firstSeen": "2025-01-01", "lastSeen": "2025-01-02",
                    "fair": "130", "grade": "S"})
    st = Store(tmp_path / "old")
    ls = st.read_listings()
    assert ls["1"]["status"] == "active" and ls["1"]["miss"] == "0" and ls["1"]["goneAt"] == ""
    assert ls["1"]["priceFirst"] == "123" and ls["1"]["priceChanges"] == "0"
    st.write_listings(ls)   # 죽은 컬럼(fair/grade…)은 버리고 새 컬럼으로 저장
    back = st.read_listings()
    assert back["1"]["status"] == "active" and set(back["1"]) == set(LISTING_FIELDS)
    assert {k: ls["1"][k] for k in LISTING_FIELDS} == back["1"]
    assert st.read_listings(active_only=True) == back
    ls["1"]["status"] = "gone"
    st.write_listings(ls)
    assert st.read_listings(active_only=True) == {}


def test_apply_articles_pure():
    """엔진 없이 상태 기계만."""
    ls: dict[str, dict] = {}
    arts = [{"atclNo": "a", "price": 100, "grp": 84, "dong": "101", "floor": 3, "area": 84.9, "sameCnt": 1},
            {"atclNo": "b", "price": 200, "grp": 59, "dong": "102", "floor": 5, "area": 59.9, "sameCnt": 1}]
    ev, k = apply_articles(ls, "c1", [dict(a) for a in arts], "2026-11-01")
    assert k["new"] == 2 and [x["event"] for x in ev] == ["new", "new"] and ev[0]["prevPrice"] == ""
    ev, k = apply_articles(ls, "c1", [{**arts[0], "price": 90}], "2026-11-02")
    assert k == {"new": 0, "relist": 0, "price": 1, "gone": 0, "swap": 0, "unmatched": 0}
    assert ls["a"]["priceChanges"] == 1 and ls["a"]["priceFirst"] == 100 and ls["b"]["miss"] == 1
    assert ev[0]["event"] == "price" and ev[0]["price"] == 90 and ev[0]["prevPrice"] == 100
    ev, k = apply_articles(ls, "c1", [{**arts[0], "price": 90}], "2026-11-03")
    assert k["gone"] == 1 and ls["b"]["status"] == "gone" and ls["b"]["goneAt"] == "2026-11-03" and ls["b"]["miss"] == 2
    # 다른 단지 호출은 이 단지 행을 건드리지 않음
    ev, k = apply_articles(ls, "c2", [], "2026-11-04")
    assert not ev and ls["a"]["miss"] == 0
    ev, k = apply_articles(ls, "c1", [dict(a) for a in arts], "2026-11-05")
    assert k["relist"] == 1 and k["price"] == 1 and ls["b"]["status"] == "active" and ls["b"]["miss"] == 0
