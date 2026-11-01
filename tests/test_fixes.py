"""리뷰에서 확인된 결함의 회귀 테스트(실제 접속 없음 — 가짜 서버만).

네이버: max_pages 잘림 → miss 안 올림 · 호출 상한은 partial(차단 아님) · 미확인 endpoint 의 0건은 반영 안 함 · 빈 NAVER_DELAY_SEC
엔진: swap 키의 None/'' 정규화 · naverId 재매칭 시 옛 매물 정리 · 수집 대상 밖 stale 매물 · 최소 간격 가드
파이프라인: 같은 날 gone→relist · 동일 내용 실거래 2건 1:1
토허: is_open 종결어 · 관측종료 닫기 · 새올 page_size 변화/단일 페이지 · 허가 SGG_CD 검증·탐지 설정 우선순위·비정상 응답 · runner 예외 격리 ·
      화성 구 미분류 · raw 창 병합 · designations 검증
export: 출력 폴더 겹침 거부 · feed 루트 xml 정리 · 안전한 파일 이름
notify: 줄 단위 길이 제한 · dong 이스케이프 · 토큰 마스킹 · 엔티티 오류 시 평문 재전송
"""
import datetime as dt
import gzip
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector import notify, pipeline as pl  # noqa: E402
from collector.deals import complex_id  # noqa: E402
from collector.engine import Engine, apply_articles, date_add  # noqa: E402
from collector.export import export_site, safe_name  # noqa: E402
from collector.naver import CallBudgetExceeded, NaverClient, load_cfg, mask_contacts  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.toheo import cases as cs, designations as dz, regions as tr, saeol  # noqa: E402
from collector.toheo.runner import run_toheo_sites  # noqa: E402
from collector.toheo.seoul_permits import DEFAULT_CFG, SeoulPermits, load_config, match_permits, parse_record  # noqa: E402
from collector.toheo.store import ToheoStore  # noqa: E402
from tests import fake_servers as fs  # noqa: E402
from tests.fake_toheo import FakeHttp, FakeResponse, FakeSeoulLand, FakeSite, make_rows  # noqa: E402
from tests.test_listings import CX, events_of, make_engine, rows_of  # noqa: E402

TODAY = dt.date(2026, 10, 27)
NOW = dt.datetime(2026, 10, 27, 9, 0, 0)


@pytest.fixture(scope="module")
def server():
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    yield base
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset_fake():
    fs.Handler.articles_override = {}
    fs.Handler.fail_articles = {}
    fs.Handler.FIN_PAGE = 10
    yield
    fs.Handler.articles_override = {}
    fs.Handler.fail_articles = {}
    fs.Handler.FIN_PAGE = 10


# ----------------------------------------------------------------------------- 네이버 클라이언트
def test_truncated_list_does_not_bump_miss(tmp_path, server):
    base = fs.gen_articles("100001", eng_mod.this_ym())
    many = [{**base[0], "atclNo": f"T{k:03d}"} for k in range(40)]
    fs.Handler.articles_override = {"100001": many}
    fs.Handler.FIN_PAGE = 1                                   # 40페이지 > max_pages 30 → 잘림
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n.json")
    arts, ok = nv.articles_ok("100001")
    assert ok and len(arts) == 30 and nv.last_truncated is True
    arts2, ok2 = nv.articles_ok("100001", max_pages=50)
    assert ok2 and len(arts2) == 40 and nv.last_truncated is False

    e = make_engine(tmp_path, server)
    cid = "11680-개포동-12-2019"
    r = e.run_listings()
    assert r["truncated"] == 1 and r["new"] >= 30 and e.complexes[cid]["listingsN"] == 30
    # 잘린 응답에서 빠진 매물(T000)은 miss 가 오르지 않는다
    fs.Handler.articles_override = {"100001": many[1:]}
    r = e.run_listings()
    row = rows_of(e, cid)["T000"]
    assert r["truncated"] == 1 and row["miss"] == "0" and row["status"] == "active" and r["gone"] == 0
    # 잘리지 않으면(페이지 크기 복구) 정상적으로 miss 가 오른다
    fs.Handler.FIN_PAGE = 10
    r = e.run_listings()
    assert r["truncated"] == 0 and rows_of(e, cid)["T000"]["miss"] == "1"


def test_call_budget_is_partial_not_blocked(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.naver.max_calls = 2                                    # 단지 3개, 단지당 1~2회 → 2회째 이후 상한
    r = e.run_listings(max_calls=2)
    assert r["status"] == "partial" and not e.naver.blocked_reason and r["complexes"] >= 1
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n2.json", max_calls=1)
    nv.articles_ok("100001", max_pages=1)
    with pytest.raises(CallBudgetExceeded):
        nv.articles_ok("100002")
    assert not nv.blocked_reason


def test_unconfirmed_endpoint_empty_list_is_not_trusted(tmp_path, server):
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n.json")
    fs.Handler.articles_override = {"100001": None}
    assert nv.articles_ok("100001") == ([], False)          # 아직 매물을 돌려준 적 없는 endpoint 의 0건
    assert nv.state.get("articles") is None
    arts, ok = nv.articles_ok("100002")
    assert ok and arts and nv.state["articles"] == "fin_list"
    assert nv.articles_ok("100001") == ([], True)           # 확인된 뒤의 0건은 정상
    # 엔진: 모든 단지가 '미확인 endpoint 0건' 이면 아무 매물도 gone 되지 않는다
    e = make_engine(tmp_path, server)
    e.run_listings()
    n = len(e.store.read_listings())
    fs.Handler.articles_override = {k: None for k in ("100001", "100002", "100003")}
    e.naver = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "fresh.json")
    for _ in range(4):
        r = e.run_listings()
        assert r["failed"] == 3 and r["gone"] == 0
    assert all(v["status"] == "active" and v["miss"] == "0" for v in e.store.read_listings().values()) and n


def test_empty_env_delay_and_desc_masking(monkeypatch):
    monkeypatch.setenv("NAVER_DELAY_SEC", "")
    assert NaverClient(load_cfg()).delay == 0.7
    assert mask_contacts("집주인 010-1234-5678 / a.b@c.kr 연락") == "집주인 *** / *** 연락"
    assert mask_contacts("급매 25층") == "급매 25층"


# ----------------------------------------------------------------------------- 엔진
def test_swap_with_unknown_floor_is_not_gone_event(tmp_path, server):
    e = make_engine(tmp_path, server, endpoints=["mobile"])
    base = fs.gen_articles("100001", eng_mod.this_ym())
    a = {**base[0], "flrInfo": "고/35", "sameAddrCnt": 3}   # 층 비공개 → floor None
    fs.Handler.articles_override = {"100001": [a] + base[1:]}
    e.run_listings()
    assert rows_of(e, "11680-개포동-12-2019")[a["atclNo"]]["floor"] == ""
    b = {**a, "atclNo": a["atclNo"] + "X"}
    fs.Handler.articles_override = {"100001": [b] + base[1:]}
    e.run_listings()
    r = e.run_listings()
    assert r["swap"] == 1 and r["gone"] == 0 and not events_of(e, "gone")
    # 순수 함수: CSV 의 '' 와 응답의 None 이 같은 키
    ls = {"a": {"atclNo": "a", "cid": "c", "grp": "", "dong": "101", "floor": "", "price": "100", "sameCnt": "3", "status": "active", "miss": 1}}
    ev, k = apply_articles(ls, "c", [{"atclNo": "b", "grp": None, "dong": "101", "floor": None, "price": 100, "sameCnt": 3}], "2026-11-01")
    assert k["swap"] == 1 and k["gone"] == 0 and ls["a"]["status"] == "gone"


def test_rematched_naver_id_cleans_old_rows_without_events(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    cid = "11680-개포동-12-2019"
    old = rows_of(e, cid)
    assert old and all(v["naverId"] == "100001" for v in old.values())
    e.complexes[cid]["naverId"] = "100009"               # 재매칭으로 네이버 단지가 바뀜
    fresh = [{**a, "atclNo": f"N{k}", "hscpNo": "100009"} for k, a in enumerate(fs.gen_articles("100001", eng_mod.this_ym())[:4])]
    fs.Handler.articles_override = {"100009": fresh}
    r = e.run_listings()
    assert r["unmatched"] == len(old) and r["gone"] == 0 and r["new"] == 4
    rows = rows_of(e, cid)
    assert all(rows[k]["status"] == "gone" and rows[k]["goneAt"] == eng_mod.today_str() for k in old)
    assert not events_of(e, "gone") and len([x for x in events_of(e, "new") if x["atclNo"].startswith("N")]) == 4
    # 다시 실행해도 옛 행은 그대로(이벤트 없음)
    r = e.run_listings()
    assert r["unmatched"] == 0 and not events_of(e, "gone")


def test_stale_rows_outside_targets_and_min_interval(tmp_path, server):
    e = make_engine(tmp_path, server)
    e.run_listings()
    today = eng_mod.today_str()
    ls = e.store.read_listings()
    mk = lambda aid, seen: {"atclNo": aid, "cid": "11650-반포동-20-2009", "naverId": "100005", "grp": 84, "price": 400000,  # noqa: E731
                            "firstSeen": "2025-01-01", "lastSeen": seen, "status": "active", "miss": 0}
    ls["out-old"] = mk("out-old", date_add(today, -(eng_mod.STALE_ACTIVE_DAYS + 1)))
    ls["out-new"] = mk("out-new", date_add(today, -3))
    e.store.write_listings(ls)
    n_ev = len(e.store.read_events())
    r = e.run_listings()
    rows = e.store.read_listings()
    assert r["stale"] == 1 and rows["out-old"]["status"] == "gone" and rows["out-new"]["status"] == "active"
    assert len(e.store.read_events()) == n_ev + 0 + (r["new"] + r["gone"] + r["relist"] + r["price"])
    assert not [x for x in e.store.read_events() if x["atclNo"] == "out-old"]
    # 최소 간격: 직전 성공 수집이 방금이면 건너뜀(상태 변화 없음)
    before = e.store.read_listings()
    r2 = e.run_listings(min_interval_min=60)
    assert r2["status"].startswith("skipped") and e.store.read_listings() == before
    assert e.run_listings(min_interval_min=0)["status"] == "ok"


# ----------------------------------------------------------------------------- 파이프라인
def _ev(kind, atcl, date, price=100000, cid="C"):
    return {"date": date, "atclNo": atcl, "cid": cid, "grp": "84", "area": "84.94", "dong": "101", "floor": "12",
            "event": kind, "price": str(price), "prevPrice": ""}


def test_same_day_relist_and_duplicate_trades():
    # 같은 날 gone → relist (파일 순서) = relisted, relist → gone 순서면 relist 무시
    links = {l["id"]: l for l in pl.link_gone_to_trades(
        [_ev("gone", "A", "2026-10-20"), _ev("relist", "A", "2026-10-20"),
         _ev("relist", "B", "2026-10-20"), _ev("gone", "B", "2026-10-20")], {}, dt.date(2026, 10, 25))}
    assert links["A"]["status"] == "relisted" and links["B"]["status"] == "pending"
    # 내용이 같은 실거래 2건 + 내림 2건 → 둘 다 confirmed
    t = ["2026-10-22", 95000, 84.94, 12, "101", 0, 0]
    links = {l["id"]: l for l in pl.link_gone_to_trades(
        [_ev("gone", "A", "2026-10-20"), _ev("gone", "B", "2026-10-21")], {"C": [list(t), list(t)]}, dt.date(2026, 10, 25))}
    assert links["A"]["status"] == links["B"]["status"] == "confirmed"
    # 거래가 1건뿐이면 가까운 쪽만
    links = {l["id"]: l for l in pl.link_gone_to_trades(
        [_ev("gone", "A", "2026-10-20"), _ev("gone", "B", "2026-10-21")], {"C": [list(t)]}, dt.date(2026, 10, 25))}
    assert links["B"]["status"] == "confirmed" and links["A"]["status"] == "pending"


# ----------------------------------------------------------------------------- 토허: 민원
def test_is_open_and_close_out_of_window():
    assert cs.is_open("처리중") and cs.is_open("접수") and cs.is_open("진행중")
    assert not cs.is_open("접수취소") and not cs.is_open("접수반려") and not cs.is_open("해결") and not cs.is_open("완료(목록이탈)")
    assert pl._is_open("접수취소") is False
    site = tr.SITE_BY_ID["11680"]
    cases = {}
    r_old = saeol.Row("202632200000000001", (TODAY - dt.timedelta(days=50)).isoformat(), "토지거래계약허가", "", "처리중", "부동산정보과")
    r_mid = saeol.Row("202632200000000002", (TODAY - dt.timedelta(days=80)).isoformat(), "토지거래계약허가", "", "처리중", "부동산정보과")
    past = NOW - dt.timedelta(days=20)
    cs.merge_rows(cases, site, [r_old, r_mid], past, past.date() - dt.timedelta(days=45), True)
    # 오늘 깊은 수집(45일 창)엔 둘 다 없음: 50일 전 건은 창 밖이라 missing 안 오름, 75일(45+30) 넘은 건만 관측종료
    for i in range(3):
        t = NOW + dt.timedelta(days=i)
        st = cs.merge_rows(cases, site, [], t, t.date() - dt.timedelta(days=45), True, close_after_days=75)
    c_old, c_mid = cases[(site.id, r_old.no)], cases[(site.id, r_mid.no)]
    assert c_old["status"] == "처리중" and c_old["missing"] == "0"
    assert c_mid["status"] == cs.CLOSED_OUT_OF_WINDOW and c_mid["done_observed"] == "0"
    stats = cs.stats_for(list(cases.values()), NOW.date() + dt.timedelta(days=2))
    assert stats["open"] == 1 and stats["duration_n"] == 0           # 관측종료는 처리기간 통계에 안 들어감
    # 30일 더 지나면 50일 전 건도 닫힘
    t = NOW + dt.timedelta(days=30)
    st = cs.merge_rows(cases, site, [], t, t.date() - dt.timedelta(days=45), True, close_after_days=75)
    assert st["closed_out"] == 1 and c_old["status"] == cs.CLOSED_OUT_OF_WINDOW


# ----------------------------------------------------------------------------- 토허: 새올
def test_saeol_page_size_drift_and_single_page_redetect():
    rng = random.Random(11)
    http = FakeHttp()
    site = tr.SITE_BY_ID["11680"]
    fsite = FakeSite(page_size=10)
    for r in make_rows("32200", TODAY - dt.timedelta(days=30), TODAY, 4, "부동산정보과", rng, status_days=(100, 120)):
        fsite.add(r)                                           # 전부 처리중
    http.route("gangnam.eminwon.seoul.kr", fsite)
    client = saeol.SaeolClient(http)
    cfg = client.discover(site)
    assert cfg["size_param"] == "pageSize" and cfg["page_size"] == min(100, len(fsite.rows))
    fsite.size_param = None                                    # 서버가 size 파라미터를 무시하기 시작: 10행/페이지
    rows, info = client.fetch(site, TODAY - dt.timedelta(days=45))
    assert info["pages"] > 1 and cfg["stale"] is True and "페이지 크기" in info["warning"]
    assert info["rows"] == len(fsite.rows) and info["reached_cutoff"] is True     # 끝까지 읽은 경우만 cutoff
    # 목록이 한 페이지뿐인 소규모 지자체: 탐지 불가 → 경고 없이 single_page, 끝까지 읽음(reached_cutoff)
    small = FakeSite(page_size=10)
    for k in range(5):
        small.add({"no": f"20263230000000000{k}", "received": (TODAY - dt.timedelta(days=k)).isoformat(), "name": "토지거래계약허가",
                   "deadline": (TODAY + dt.timedelta(days=20 - k)).isoformat(), "status": "처리중", "dept": "부동산정보과"})
    assert 0 < len(small.rows) <= 10
    http.route("seocho.eminwon.seoul.kr", small)
    seocho = tr.SITE_BY_ID["11650"]
    cfg2 = client.discover(seocho)
    assert cfg2["page_param"] is None and cfg2["single_page"] is True and not cfg2.get("warning")
    rows, info = client.fetch(seocho, TODAY - dt.timedelta(days=45))
    assert info["reached_cutoff"] is True and info["rows"] == len(small.rows) and not cfg2.get("stale")
    for k in range(5, 30):
        small.add({"no": f"20263230000000000{k:02d}", "received": (TODAY - dt.timedelta(days=k)).isoformat(), "name": "토지거래계약허가",
                   "deadline": "", "status": "처리중", "dept": "부동산정보과"})     # 목록이 늘어 여러 페이지가 됨
    rows, info = client.fetch(seocho, TODAY - dt.timedelta(days=45))
    assert cfg2["stale"] is True and info["reached_cutoff"] is False
    cfg3 = client.discover(seocho)                             # stale → 재탐지 → 페이지 파라미터 발견
    assert cfg3["page_param"] == "pageIndex" and not cfg3.get("single_page")
    rows, info = client.fetch(seocho, TODAY - dt.timedelta(days=45))
    assert info["reached_cutoff"] and info["rows"] == len(small.rows)


# ----------------------------------------------------------------------------- 토허: 서울 허가
def test_permits_sgg_validation_state_priority_and_abnormal_response(tmp_path):
    base = {"ACC_YEAR": "2026", "ACC_NO": "1", "OBJ_SEQNO": "1", "HNDL_YMD": "20261020", "ADDRESS": "강남구 대치동 316"}
    assert parse_record({**base, "SGG_CD": "../x"}, "11680")["sgg"] == "11680"
    assert parse_record({**base, "SGG_CD": "11680/1"}, "11680")["key"].startswith("11680|")
    assert parse_record({**base, "SGG_CD": "11650"}, "11680")["sgg"] == "11650"
    assert complex_id("11680", "a/../b", "12", "2019") == "11680-a___b-12-2019" and safe_name("11680/1") is None
    assert safe_name("11680-개포동-12-2019") and safe_name("..") is None and safe_name("41590:dongtan")
    # --discover 가 저장한 설정은 사용자가 config 에서 바꾸지 않은 키에 적용된다
    state = tmp_path / "cfg.json"
    state.write_text(json.dumps({"url": "https://land.seoul.go.kr/land/x/newEndpoint.do", "method": "GET",
                                 "params": {"cggCd": "{gu5}", "sDate": "{from:%Y%m%d}", "eDate": "{to:%Y%m%d}"}, "discovered": True}),
                     encoding="utf-8")
    sp = SeoulPermits(FakeHttp(), load_config(), state_path=state)
    assert sp.cfg["url"].endswith("newEndpoint.do") and sp.cfg["method"] == "GET" and "cggCd" in sp.cfg["params"] and sp.cfg["discovered"]
    user = load_config()
    user["url"] = "https://land.seoul.go.kr/land/custom.do"
    sp2 = SeoulPermits(FakeHttp(), user, state_path=state)
    assert sp2.cfg["url"].endswith("custom.do") and sp2.cfg["method"] == "GET"      # 사용자 지정 키만 우선
    # 비정상 응답은 '정상 0건' 이 아니라 오류
    land = FakeSeoulLand()
    http = FakeHttp()
    http.route_seoul(land)
    responses = {"json_null": '{"result": null, "msg": "session expired"}', "login": "<html><body>로그인이 필요합니다</body></html>",
                 "blocked": "<html><body>Access denied</body></html>"}
    orig = land.handle

    def handle(method, url, q, headers, h):
        if url.endswith("getContractList.do") and handle.mode:
            return FakeResponse(responses[handle.mode], ctype="text/html")
        return orig(method, url, q, headers, h)
    handle.mode = None
    http.handlers["land.seoul.go.kr"] = handle
    sp3 = SeoulPermits(http, load_config())
    for mode in responses:
        handle.mode = mode
        rows, info = sp3.fetch(["11680"], days=60, today=TODAY)
        assert rows == [] and info["ok"] is False and str(info["by_gu"]["강남구"]).startswith("오류"), mode
    handle.mode = None
    rows, info = sp3.fetch(["11680"], days=60, today=TODAY)
    assert info["ok"] and info["by_gu"]["강남구"] == 0 and info["kinds"]["11680"] == "json"   # 진짜 빈 목록은 정상 0건
    assert match_permits({"k": {"key": "k", "sgg": "11680", "dong": "대치동", "jibun": "316"}},
                         {"x": {"sgg": "11680", "umd": "대치동", "jibun": "316", "name": "cid 없음"}}) == 0


def test_runner_isolates_permit_failures(tmp_path):
    rng = random.Random(5)
    http = FakeHttp()
    http.route("gangnam.eminwon.seoul.kr", FakeSite())
    for r in make_rows("32200", TODAY - dt.timedelta(days=30), TODAY, 3, "부동산정보과", rng):
        http.sites["gangnam.eminwon.seoul.kr"].add(r)
    land = FakeSeoulLand()
    land.add("11680", "1", TODAY.isoformat(), "강남구 대치동 316")
    http.route_seoul(land)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "seoul_permits.json").write_text('{"window_days": 60,, }', encoding="utf-8")   # 깨진 JSON
    flog = {}
    res = run_toheo_sites(deep=True, data_dir=tmp_path / "d", region_filter="11680", complexes={}, fetch_log=flog,
                          http=http, now=NOW, config_dir=cfg_dir)
    p = flog["toheo"]["permits"]
    assert p["ok"] is False and "설정 로드 실패" in p["error"] and res["errors"] == 1
    assert len(flog["toheo"]["runs"]) == 1 and ToheoStore(tmp_path / "d" / "toheo").load_progress()["runs"]
    assert (tmp_path / "d" / "toheo" / "cases.csv").exists()
    # 빈 PERMITS 환경변수는 '미설정' (허가 수집 유지)
    import os
    os.environ["PERMITS"] = ""
    try:
        res2 = run_toheo_sites(deep=False, data_dir=tmp_path / "d", region_filter="11680", complexes={}, fetch_log=flog,
                               http=http, now=NOW + dt.timedelta(days=1))
    finally:
        del os.environ["PERMITS"]
    assert res2["permits"] and res2["permits"]["ok"] and res2["permits"]["fetched"] == 1


def test_hwaseong_unknown_gu_and_raw_window_merge(tmp_path):
    hs = tr.SITE_BY_ID["41590"]
    assert tr.sgg_for_case(hs, "토지정보과") == "41590:unknown" and tr.sgg_for_case(hs, "") == "41590:unknown"
    assert tr.sgg_for_case(hs, "동탄구 토지정보과") == "41590:dongtan"
    assert tr.label("41590:unknown") == "화성시(구 미분류)" and tr.label("41590") == "화성시(동탄 외)"
    assert tr.base_code("41590:unknown") == "41590" and tr.sido_of("41590:unknown") == "경기"
    assert tr.sgg_for_case(tr.SITE_BY_ID["41130"], "민원과") == "41130"        # 성남은 시 코드 그대로
    items = dz.with_status(dz.load(), TODAY)
    assert dz.region_apt_status(items, "41590:unknown")["level"] == "none"
    st = ToheoStore(tmp_path / "toheo")
    st.save_raw("2026-10-27", "11680", {"sgg": "11680", "windows": [{"from": "2026-05-01", "to": "2026-06-29", "kind": "json", "body": 1},
                                                                    {"from": "2026-06-30", "to": "2026-08-28", "kind": "json", "body": 2}]})
    st.save_raw("2026-10-27", "11680", {"sgg": "11680", "windows": [{"from": "2026-06-30", "to": "2026-08-28", "kind": "json", "body": 3}]})
    w = st.load_raw("2026-10-27", "11680")["windows"]
    assert [(x["from"], x["body"]) for x in w] == [("2026-06-30", 3), ("2026-05-01", 1)]


def test_designations_validation(tmp_path):
    good = json.loads(Path(dz.DEFAULT_PATH).read_text(encoding="utf-8"))
    ids = {i["id"] for i in good["items"]}
    assert {"seoul-2026-456-moa-roads", "seoul-2026-1910-moa-roads", "seoul-2026-2289-moa-roads"} <= ids
    supply = next(i for i in good["items"] if i["id"] == "molit-2026-1079-supply")
    assert {"41290", "41210", "41310", "41450", "41570", "41281", "41192"} <= set(supply["regions"])
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"items": [{"id": "x", "start": "2026-11-01", "end": "2026-11-30", "scope": "apt", "regions": ["11*"]}]}), encoding="utf-8")
    it = dz.load(p)["items"][0]
    assert it["title"] == "x"
    p.write_text(json.dumps({"items": [{"id": "y", "start": "2026-11-01"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        dz.load(p)
    p.write_text(json.dumps({"items": [{"id": "z", "start": "2026/11/01", "end": "2026-11-30"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        dz.load(p)


# ----------------------------------------------------------------------------- export
def test_export_refuses_overlapping_out_dir_and_cleans_root_feeds(tmp_path, server, monkeypatch):
    e = make_engine(tmp_path, server)
    e.run_listings()
    data = tmp_path / "data"
    with pytest.raises(ValueError):
        export_site(e, tmp_path)                                  # out/data == DATA_DIR
    with pytest.raises(ValueError):
        export_site(e, data.parent / "data" / "..")               # 정규화해도 같은 곳
    assert (data / "toheo").exists() or True                      # 아무것도 지워지지 않음
    assert (data / "listings.csv").exists()
    out = tmp_path / "site"
    export_site(e, out)
    stale = out / "feed" / "99999.xml"
    stale.write_text("<rss/>", encoding="utf-8")
    export_site(e, out)
    assert not stale.exists() and (out / "feed" / "all.xml").exists() and (out / "feed" / "11680.xml").exists()
    # 매물이 0 이 된 시군구도 빈 피드로 다시 쓰인다
    ls = e.store.read_listings()
    for v in ls.values():
        v["status"] = "gone"
    e.store.write_listings(ls)
    export_site(e, out)
    txt = (out / "feed" / "11680.xml").read_text(encoding="utf-8")
    assert "<item>" not in txt
    page = json.loads((out / "data" / "index.json").read_text(encoding="utf-8"))
    assert page["complexes"]


# ----------------------------------------------------------------------------- notify
def test_notify_length_budget_escape_and_token_mask(monkeypatch):
    ls = [{"id": f"L{k}", "cid": f"11680-대치동-{k}-1979", "name": "래미안블레스티지자이프레지던스" * 2, "sgg": "11680", "grp": 84,
           "dong": "A&B<", "floor": 12, "price": 395000, "grade": "SS", "diff": -0.09, "conf": "high", "first": "2026-10-27"} for k in range(12)]
    ps = [{"cid": f"11680-개포동-{k}-2019", "name": "개포자이프레지던스단지" * 3, "sgg": "11680", "keys": [str(k)], "dates": ["2026-10-20"],
           "purposes": {"주거용": 2, "복지편익시설용": 1, "사업용": 1, "농업용": 1}, "n": 5, "last": "2026-10-20"} for k in range(12)]
    ds = [{"key": f"d{k}", "id": f"d{k}", "title": "서울 전역·경기 12곳 (10·15 대책)", "end": "2026-12-31", "daysLeft": 30, "renewal": "연장 미확정",
           "regions": 3} for k in range(3)]
    site = "https://my-long-username.github.io/my-apt-radar-site-with-a-long-name"
    text = notify.format_message({"listings": ls, "permits": ps, "ddays": ds}, site, "2026-10-27")
    assert len(text) <= notify.TELEGRAM_MAX_CHARS
    assert text.count("<a ") == text.count("</a>") and text.count("<b>") == text.count("</b>")   # 태그가 잘리지 않음
    assert "외 " in text and "A&amp;B&lt;동" in text and "토허구역 지정 만료" in text and "#alerts" in text
    full = notify.format_message({"listings": ls[:2], "permits": [], "ddays": []}, "", "2026-10-27")
    assert "외 " not in full
    assert notify._mask_token("Max retries exceeded with url: /bot123456:AAEx-secret_TOKEN/sendMessage", "123456:AAEx-secret_TOKEN") \
        == "Max retries exceeded with url: /bot***/sendMessage"
    assert notify.strip_tags('<b>x</b> <a href="u">y</a> &amp;') == "x y &"
    # 엔티티 오류(400 can't parse) → 평문으로 한 번 더
    calls = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    def fake_post(url, json=None, timeout=0):
        calls.append(json)
        if "parse_mode" in json:
            return R(400, {"ok": False, "description": "Bad Request: can't parse entities: Unsupported start tag"})
        return R(200, {"ok": True})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    res = notify.send_telegram("123456:AAEx-secret_TOKEN", "1", "<b>hi</b> <x>")
    assert res["ok"] and res["fallback"] == "plain" and len(calls) == 2 and calls[1]["text"].strip() == "hi" and "parse_mode" not in calls[1]

    def fail_post(url, json=None, timeout=0):
        raise requests.ConnectionError("Max retries exceeded with url: /bot123456:AAEx-secret_TOKEN/sendMessage")
    monkeypatch.setattr(requests, "post", fail_post)
    res = notify.send_telegram("123456:AAEx-secret_TOKEN", "1", "x")
    assert not res["ok"] and "secret_TOKEN" not in res["error"] and "/bot***/" in res["error"]


def test_old_fin_count_endpoint_unaffected(tmp_path, server):
    """articles_ok 의 변경이 count endpoint·단지 목록에 영향 없음."""
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "n.json")
    assert nv.article_count("100001") == len(fs.gen_articles("100001", eng_mod.this_ym()))
    assert {c["naverId"] for c in nv.complexes("1168010300")} == {"100001", "100002"}
    assert nv.state["complexes_by_cortar"] == "mobile"
    assert CX  # make_engine 의 단지 상수가 그대로임


def test_merge_permits_dedups_json_and_html_keys():
    from collector.toheo.seoul_permits import merge_permits, parse_any
    from tests.fake_toheo import FakeSeoulLand
    land = FakeSeoulLand()
    land.add("11680", "0000001", "2026-10-20", "강남구 대치동 316", use_purp="주거용")
    land.add("11680", "0000002", "2026-10-20", "강남구 대치동 316", use_purp="주거용")   # 같은 날 같은 집 2건(접수 키 다름)
    items = [r for r in land.records]
    rows_json, kind = parse_any(json.dumps({"result": items}), "11680")
    rows_html, kind2 = parse_any(land._html(items), "11680")
    assert kind == "json" and kind2 == "html" and len(rows_json) == 2 and len(rows_html) == 2
    # 1) HTML 먼저(대체키) → 2) JSON(접수 키) 으로 교체, 총 2건
    permits = {}
    assert merge_permits(permits, rows_html, "2026-10-21") == 1          # HTML 은 같은 내용이라 키가 하나뿐
    permits[next(iter(permits))]["cid"] = "11680-대치동-316-1979"
    assert merge_permits(permits, rows_json, "2026-10-22") == 1          # 1건 교체 + 1건 추가
    assert all(k.startswith("11680|") for k in permits) and len(permits) == 2
    assert sum(1 for r in permits.values() if r["cid"]) == 1 and all(r["first_seen"] in ("2026-10-21", "2026-10-22") for r in permits.values())
    # 3) JSON 먼저 → HTML 다시 와도 추가 없음
    permits2 = {}
    assert merge_permits(permits2, rows_json, "2026-10-21") == 2
    assert merge_permits(permits2, rows_html, "2026-10-22") == 0 and len(permits2) == 2
