"""토허 서브패키지 테스트: 새올 파싱·탐지·merge, 허가 파싱·매칭·중복키, designations, runner 흐름."""
import datetime as dt
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.toheo import cases as cs, designations as dz, saeol  # noqa: E402
from collector.toheo import regions as tr  # noqa: E402
from collector.toheo.runner import run_toheo, run_toheo_sites  # noqa: E402
from collector.toheo.seoul_permits import (SeoulPermits, load_config, match_permits, merge_permits,  # noqa: E402
                                           parse_any, parse_html, parse_record, split_address)
from collector.toheo.store import CASE_FIELDS, PERMIT_FIELDS, ToheoStore  # noqa: E402
from tests.fake_toheo import FakeHttp, FakeSeoulLand, FakeSite, make_permits, make_rows  # noqa: E402

TODAY = dt.date(2026, 10, 27)
NOW = dt.datetime(2026, 10, 27, 9, 0, 0)


def _site_with_rows(rng, code="32200", dept="도시환경국 부동산정보과", **kw):
    fs = FakeSite(**kw)
    for r in make_rows(code, TODAY - dt.timedelta(days=70), TODAY, 6, dept, rng):
        fs.add(r)
    return fs


# ----------------------------------------------------------------------------- regions

def test_regions_sites_and_sgg_mapping():
    assert len([s for s in tr.SITES if s.sido == "서울"]) == 25
    assert tr.SITE_BY_SGG["11680"].name == "강남구"
    assert tr.SITE_BY_SGG["41135"].id == "41130" and tr.SITE_BY_SGG["41130"].name == "성남시"
    assert tr.SITE_BY_SGG["41590:dongtan"].id == "41590"
    assert "gwanakgu.eminwon.seoul.kr" in tr.SITE_BY_ID["11620"].hosts
    assert tr.SITE_BY_ID["11350"].list_urls()[-1].startswith("http://")
    assert tr.sgg_for_case(tr.SITE_BY_ID["41460"], "기흥구 민원지적과") == "41463"
    assert tr.sgg_for_case(tr.SITE_BY_ID["41460"], "민원과") == "41460"   # 구 미분류 → 시 코드
    assert tr.sgg_for_case(tr.SITE_BY_ID["11680"], "아무부서") == "11680"
    assert tr.sgg_for_case(tr.SITE_BY_ID["41590"], "동탄구 토지정보과") == "41590:dongtan"
    assert tr.label("11680") == "강남구" and tr.label("41135") == "성남 분당구"
    assert tr.label("41590:dongtan") == "화성 동탄구" and tr.label("41130") == "성남시(구 미분류)"
    assert tr.toheo_sgg_for("41590", "반송동") == "41590:dongtan" and tr.toheo_sgg_for("41590", "병점동") == "41590"


def test_toheo_sggs_filter():
    assert tr.toheo_sggs("서울") == [s for s in tr.ALL_SGGS if s.startswith("11")]
    assert tr.toheo_sggs("11680,41135") == ["11680", "41135"]
    assert "41590:dongtan" in tr.toheo_sggs("화성") and "41590" in tr.toheo_sggs("41590")
    assert tr.toheo_sggs("인천") == []
    assert set(tr.toheo_sggs(None)) == set(tr.ALL_SGGS)
    assert [s.id for s in tr.sites_for(["41135", "11680"])] == ["11680", "41130"]


# ----------------------------------------------------------------------------- 새올

def test_parse_list_rows_and_total():
    fs = _site_with_rows(random.Random(1))
    html = fs.render({})
    rows, total = saeol.parse_list(html)
    assert total == len(fs.rows)
    assert len(rows) == 10
    assert rows[0].no.startswith("2026")
    assert rows[0].received <= TODAY.isoformat()
    assert all(r.status in ("처리중", "해결", "취하", "반려") for r in rows)


def test_discover_and_fetch_with_pagination():
    rng = random.Random(2)
    http = FakeHttp()
    gangnam = tr.SITE_BY_ID["11680"]
    fs = _site_with_rows(rng)
    http.route("gangnam.eminwon.seoul.kr", fs)
    client = saeol.SaeolClient(http)
    cfg = client.discover(gangnam)
    assert cfg["host"] == "gangnam.eminwon.seoul.kr" and cfg["scheme"] == "https"
    assert cfg["page_param"] == "pageIndex"
    assert cfg["size_param"] == "pageSize" and cfg["page_size"] == 100
    rows, info = client.fetch(gangnam, TODAY - dt.timedelta(days=30))
    assert info["reached_cutoff"]
    assert all("토지거래" in r.name for r in rows)
    expect = [r for r in fs.rows if "토지거래" in r["name"] and r["received"] >= (TODAY - dt.timedelta(days=30)).isoformat()]
    assert {r.no for r in rows} >= {r["no"] for r in expect}


def test_discover_without_size_param_uses_second_host_and_http_fallback():
    rng = random.Random(3)
    http = FakeHttp()
    sd = tr.SITE_BY_ID["11200"]
    fs = _site_with_rows(rng, code="30300", page_param="pageNo", size_param=None)
    http.route("eminwon.sd.go.kr", fs)  # 1순위 호스트(sd.eminwon.seoul.kr)는 없음
    client = saeol.SaeolClient(http)
    cfg = client.discover(sd)
    assert cfg["host"] == "eminwon.sd.go.kr"
    assert cfg["page_param"] == "pageNo" and cfg["size_param"] is None and cfg["page_size"] == 10
    rows, info = client.fetch(sd, TODAY - dt.timedelta(days=20))
    assert info["pages"] > 1 and info["reached_cutoff"]
    # 노원구: https 후보가 모두 죽고 http 폴백만 살아 있는 경우
    nowon = tr.SITE_BY_ID["11350"]
    http.route("nowon.eminwon.seoul.kr", _site_with_rows(rng, code="35000"))
    orig = http.request

    def req(method, url, **kw):
        if url.startswith("https://nowon."):
            raise ConnectionError("tls down")
        return orig(method, url, **kw)
    http.request = req
    cfg = client.discover(nowon)
    assert cfg["host"] == "nowon.eminwon.seoul.kr" and cfg["scheme"] == "http"
    rows, info = client.fetch(nowon, TODAY - dt.timedelta(days=5))
    assert rows and not info["error"]


def test_merge_tracks_completion_observation_and_sgg():
    site = tr.SITE_BY_ID["41130"]
    cases = {}
    now = dt.datetime(2026, 10, 1, 9)
    r1 = saeol.Row("202638100000059630", "2026-09-29", "토지거래계약허가", "2026-10-20", "처리중", "분당구 시민봉사과")
    r2 = saeol.Row("202638100000059631", "2026-09-20", "토지거래계약허가", "2026-10-11", "해결", "수정구 민원과")
    r4 = saeol.Row("202638100000059632", "2026-09-21", "토지거래계약허가", "2026-10-12", "해결", "토지정보과")
    st = cs.merge_rows(cases, site, [r1, r2, r4], now, now.date() - dt.timedelta(days=45), True)
    assert st["new"] == 3
    assert cases[(site.id, r1.no)]["sgg"] == "41135"
    assert cases[(site.id, r2.no)]["sgg"] == "41131"
    assert cases[(site.id, r4.no)]["sgg"] == "41130"          # 구 미분류 → 시 코드
    assert cases[(site.id, r2.no)]["done_observed"] == "0"    # 처음부터 완료 → 완료일 미상
    later = dt.datetime(2026, 10, 14, 9)
    r1b = saeol.Row(r1.no, r1.received, r1.name, r1.deadline, "해결", r1.dept)
    st = cs.merge_rows(cases, site, [r1b, r2, r4], later, later.date() - dt.timedelta(days=45), True)
    c = cases[(site.id, r1.no)]
    assert st["completed_observed"] == 1 and c["done_date"] == "2026-10-14" and c["done_observed"] == "1"
    # 목록에서 3회 사라지면 목록이탈 완료
    r3 = saeol.Row("202638100000059700", "2026-10-10", "토지거래계약허가", "2026-10-30", "처리중", "분당구 시민봉사과")
    cs.merge_rows(cases, site, [r3], later, later.date() - dt.timedelta(days=45), True)
    for i in range(3):
        t = later + dt.timedelta(days=i + 1)
        cs.merge_rows(cases, site, [], t, t.date() - dt.timedelta(days=45), True)
    assert cases[(site.id, r3.no)]["status"] == "완료(목록이탈)"
    # cutoff 를 못 읽은 수집은 '사라짐'을 세지 않음
    r5 = saeol.Row("202638100000059800", "2026-10-12", "토지거래계약허가", "2026-11-01", "처리중", "분당구 시민봉사과")
    cs.merge_rows(cases, site, [r5], later, later.date() - dt.timedelta(days=45), True)
    cs.merge_rows(cases, site, [], later + dt.timedelta(days=1), None, False)
    assert cases[(site.id, r5.no)]["missing"] == "0"


def test_region_stats_and_estimate():
    cases = []
    for i in range(20):
        rec = TODAY - dt.timedelta(days=30 + i)
        done = rec + dt.timedelta(days=12 + (i % 5))
        cases.append({"sgg": "11680", "received": rec.isoformat(), "deadline": (rec + dt.timedelta(days=20)).isoformat(),
                      "status": "해결", "done_date": done.isoformat(), "done_observed": "1", "no": str(i), "dept": ""})
    for i in range(5):
        rec = TODAY - dt.timedelta(days=i)
        cases.append({"sgg": "11680", "received": rec.isoformat(), "deadline": (rec + dt.timedelta(days=20)).isoformat(),
                      "status": "처리중", "done_date": "", "done_observed": "0", "no": f"o{i}", "dept": ""})
    cases.append({"sgg": "11650", "received": TODAY.isoformat(), "deadline": "", "status": "처리중",
                  "done_date": "", "done_observed": "0", "no": "x", "dept": ""})
    stats = cs.region_stats(cases, TODAY)
    st = stats["11680"]
    assert st["open"] == 5 and st["received_7d"] == 5
    assert 12 <= st["median_days"] <= 16
    assert st["deadline_days"] == 20
    assert len(st["series"]["dates"]) == 180 and len(st["spark"]) == 28
    assert stats["11650"]["open"] == 1
    dur = cs.durations_by_sgg(stats)
    est = cs.estimate(dur, "11680")
    assert est and est["n"] == st["duration_n"] and est["p25"] <= est["p50"] <= est["p90"]
    assert cs.estimate(dur, "11650") is None
    assert cs.estimate(dur, "all")["n"] == est["n"]


# ----------------------------------------------------------------------------- 서울 허가

def test_parse_record_alias_address_and_key():
    rec = {"SGG_CD": "11170", "ACC_YEAR": "2026", "ACC_NO": "0001320", "OBJ_SEQNO": "1", "HNDL_YMD": "20261030",
           "ADDRESS": "용산구 후암동 409-105 ", "BOBN": "0409", "BUBN": "0105", "JIMOK": "대", "USE_PURP": "주거용",
           "JOB_GBN_NM": "허가", "DEAL_END_YMD": "20281030", "EXEC_YMD": "20261030"}
    row = parse_record(rec, "11170", load_config()["fields"])
    assert row["key"] == "11170|2026|0001320|1"
    assert row["gu"] == "용산구" and row["dong"] == "후암동" and row["jibun"] == "409-105"
    assert row["hndl_ymd"] == "2026-10-30" and row["duty_end"] == "2028-10-30" and row["job_gbn"] == "허가"
    assert set(row) == set(PERMIT_FIELDS)
    # alias 키·주소 없이 본번/부번만 있는 경우, 키 전부 비면 대체키
    row2 = parse_record({"PROCESS_DE": "2026.10.01", "ADDR": "강남구 대치동", "BOBN": "0316", "BUBN": "0000",
                         "PRCUSE_NM": "주거용", "PROCESS_RSLT_NM": "허가"}, "11680")
    assert row2["jibun"] == "316" and row2["key"] == "강남구 대치동|2026-10-01|주거용" and row2["sgg"] == "11680"
    assert parse_record({"HNDL_YMD": "", "ADDRESS": ""}, "11680") is None
    assert split_address("서울특별시 강남구 대치동 산 12") == ("강남구", "대치동", "산12")
    assert split_address("중구 명동1가 1-1") == ("중구", "명동1가", "1-1")


def test_parse_any_json_then_html_fallback():
    rows, kind = parse_any(json.dumps({"result": [{"SGG_CD": "11680", "ACC_YEAR": "2026", "ACC_NO": "1", "OBJ_SEQNO": "1",
                                                   "HNDL_YMD": "20261020", "ADDRESS": "강남구 대치동 316", "USE_PURP": "주거용",
                                                   "JOB_GBN_NM": "허가"}]}), "11680")
    assert kind == "json" and len(rows) == 1 and rows[0]["dong"] == "대치동"
    html = """<table><tr><th>연번</th><th>허가일자</th><th>주소</th><th>지목</th><th>허가년월일</th><th>이용목적</th><th>신고,고발 등</th></tr>
    <tr><td>1</td><td>2026-10-20</td><td>강남구 대치동</td><td>대</td><td>2026-10-20</td><td>주거용</td><td></td></tr>
    <tr><td>316</td><td>(대)</td><td>2028-11-05</td></tr>
    <tr><td>2</td><td>2026-10-21</td><td>강남구 개포동 1234-5</td><td>대</td><td>2026-10-21</td><td>주거용(실거주)</td><td></td></tr>
    <tr><td>1234-5</td><td>(대)</td><td>2028-11-06</td></tr></table>"""
    pms = parse_html(html, "강남구")
    assert len(pms) == 2 and pms[0].dong == "대치동" and pms[0].jibun == "316" and pms[0].duty_end == "2028-11-05"
    rows, kind = parse_any(html, "11680")
    assert kind == "html" and len(rows) == 2 and rows[1]["jibun"] == "1234-5" and rows[1]["job_gbn"] == "허가"
    assert rows[0]["key"] == "강남구 대치동 316|2026-10-20|주거용"
    # JSON 이지만 목록이 없으면 HTML 폴백(빈 결과)
    rows, kind = parse_any('{"status":"ok"}', "11680")
    assert kind == "html" and rows == []


def test_merge_permits_dedup_and_match():
    today = TODAY.isoformat()
    a = parse_record({"SGG_CD": "11680", "ACC_YEAR": "2026", "ACC_NO": "10", "OBJ_SEQNO": "1", "HNDL_YMD": "20261020",
                      "ADDRESS": "강남구 대치동 316", "USE_PURP": "주거용", "JOB_GBN_NM": "미결"}, "11680")
    b = parse_record({"SGG_CD": "11680", "ACC_YEAR": "2026", "ACC_NO": "11", "OBJ_SEQNO": "1", "HNDL_YMD": "20261021",
                      "ADDRESS": "강남구 개포동 1234-5", "USE_PURP": "주거용", "JOB_GBN_NM": "허가"}, "11680")
    c = parse_record({"SGG_CD": "11680", "ACC_YEAR": "2026", "ACC_NO": "12", "OBJ_SEQNO": "1", "HNDL_YMD": "20261022",
                      "ADDRESS": "강남구 역삼동 1", "USE_PURP": "주거용", "JOB_GBN_NM": "허가"}, "11680")
    permits = {}
    assert merge_permits(permits, [a, b, c], today) == 3
    assert permits[a["key"]]["first_seen"] == today
    a2 = dict(a, job_gbn="허가", duty_end="2028-10-20")
    assert merge_permits(permits, [a2, b], "2026-10-28") == 0          # 같은 키 → 추가 없음
    assert permits[a["key"]]["job_gbn"] == "허가" and permits[a["key"]]["duty_end"] == "2028-10-20"
    assert permits[a["key"]]["first_seen"] == today
    cxs = {"11680-대치동-316-1979": {"cid": "11680-대치동-316-1979", "sgg": "11680", "umd": "대치동", "jibun": "316",
                                   "name": "은마", "nTrades": "50"},
           "11680-개포동-1234-2024": {"cid": "11680-개포동-1234-2024", "sgg": "11680", "umd": "개포동", "jibun": "1234",
                                    "name": "디에이치", "nTrades": "10"},
           "11680-개포동-1234-1990": {"cid": "11680-개포동-1234-1990", "sgg": "11680", "umd": "개포동", "jibun": "1234",
                                    "name": "옛단지", "nTrades": "3"}}
    n = match_permits(permits, cxs)
    assert n == 2
    assert permits[a["key"]]["cname"] == "은마"
    assert permits[b["key"]]["cname"] == "디에이치"     # 본번 일치 후보 중 nTrades 최다
    assert permits[c["key"]]["cid"] == ""
    # 단지 인덱스가 바뀌어 사라진 cid 는 다시 매칭
    del cxs["11680-개포동-1234-2024"]
    assert match_permits(permits, cxs) == 2 and permits[b["key"]]["cname"] == "옛단지"


def test_seoul_permits_client_session_and_windows(tmp_path):
    http = FakeHttp()
    land = FakeSeoulLand()
    http.route_seoul(land)
    rng = random.Random(4)
    make_permits(land, "11680", TODAY, rng, [("대치동", "316"), ("개포동", "12")], n=20, days=50)
    land.add("11680", "9999999", (TODAY - dt.timedelta(days=100)).isoformat(), "강남구 대치동 316")   # 창 밖
    sp = SeoulPermits(http, load_config(), state_path=tmp_path / "cfg.json")
    rows, info = sp.fetch(["11680"], days=60, today=TODAY)
    assert info["ok"] and land.hits["page"] == 1 and land.hits["nosession"] == 0
    assert info["by_gu"]["강남구"] == len(rows) == 20 and info["kinds"]["11680"] == "json"
    assert len(info["raw"]["11680"]) == 1 and info["raw"]["11680"][0]["body"]["resultCnt"] == 20
    # backfill: 60일 창을 여러 번
    rows2, info2 = sp.fetch(["11680"], days=60, backfill_from=TODAY - dt.timedelta(days=150), today=TODAY)
    assert len(rows2) == 21 and len(info2["raw"]["11680"]) == 3
    assert land.hits["page"] == 1          # 세션은 한 번만
    # 세션 쿠키 없이 바로 POST 하면 빈 결과(가짜 서버 규칙 확인)
    http2 = FakeHttp()
    http2.route_seoul(land)
    r = http2.post("https://land.seoul.go.kr/land/wsklis/getContractList.do", data={"sggCd": "11680"},
                   headers={"X-Requested-With": "XMLHttpRequest"})
    assert json.loads(r.text)["result"] == []
    # HTML 응답이면 폴백 파서
    land.mode = "html"
    sp2 = SeoulPermits(FakeHttp(), load_config())
    sp2.http.route_seoul(land)
    rows3, info3 = sp2.fetch(["11680"], days=60, today=TODAY)
    assert info3["kinds"]["11680"] == "html" and len(rows3) == 20 and all(r["dong"] in ("대치동", "개포동") for r in rows3)
    # 서버 오류는 by_gu 에 오류 문자열, 전체 실패 시 ok=False
    land.mode = "error"
    rows4, info4 = sp2.fetch(["11680"], days=60, today=TODAY)
    assert rows4 == [] and not info4["ok"] and str(info4["by_gu"]["강남구"]).startswith("오류")


# ----------------------------------------------------------------------------- designations

def test_designations_status_and_region_lookup():
    data = dz.load()
    items = dz.with_status(data, TODAY)
    ids = {i["id"]: i for i in items}
    for it in items:
        assert set(dz.FIELDS) <= set(it), it["id"]
        assert it["target_person"] in ("전체", "내국인", "외국인등") and isinstance(it["apt_relevant"], bool)
    m = ids["molit-2025-1219"]
    assert m["status"] == "active" and m["days_left"] == 65 and m["renewal_status"] == "연장 미확정"
    assert "11680" in m["region_ids"] and "41135" in m["region_ids"] and "41463" not in m["region_ids"]
    assert "41463" in ids["gg-2026-0630"]["region_ids"] and "41590:dongtan" in ids["gg-2026-0630"]["region_ids"]
    assert "41590" not in ids["gg-2026-0630"]["region_ids"]
    assert dz.region_apt_status(items, "11680")["level"] == "full"
    assert dz.region_apt_status(items, "11680")["partial_ids"]           # 재건축 14개 단지 등 부분 지정
    assert dz.region_apt_status(items, "41461")["level"] == "none"        # 처인구: 60㎡ 토지 지정만(apt_relevant=false)
    assert dz.region_apt_status(items, "41590:dongtan")["until"] == "2027-12-31"
    assert dz.region_apt_status(items, "41590")["level"] == "none"
    assert dz.region_apt_status(items, "41463")["level"] == "full"
    # 10·15 만료 뒤: 강남은 재건축 단지 부분 지정만, 외국인등 지정은 level 에 영향 없음
    later = dz.with_status(data, dt.date(2027, 1, 15))
    st = dz.region_apt_status(later, "11680")
    assert st["level"] == "partial" and st["designation"] == "seoul-2026-1510-redev"
    assert dz.region_apt_status(later, "11110")["level"] == "none"
    assert dz.region_apt_status(later, "41135")["level"] == "none"
    assert any(i["target_person"] == "외국인등" and "11110" in i["region_ids"] for i in later)
    assert ids["molit-2026-1079-supply"]["apt_relevant"] is False and ids["molit-2026-1079-supply"]["threshold_m2"] == 60
    for key in ("molit-2026-106-seongnam", "molit-2026-107-gwacheon", "molit-2026-312-yongin"):
        assert ids[key]["apt_relevant"] is False and ids[key]["status"] == "active"
    assert dz.matches(["11*"], "11740") and not dz.matches(["11*"], "41135")
    assert dz.matches(["41590"], "41590:dongtan") and not dz.matches(["41590:dongtan"], "41590")


# ----------------------------------------------------------------------------- runner

def _complexes():
    return {
        "11680-대치동-316-1979": {"cid": "11680-대치동-316-1979", "sgg": "11680", "umd": "대치동", "jibun": "316",
                                "name": "은마", "nTrades": "50"},
        "11680-개포동-12-2019": {"cid": "11680-개포동-12-2019", "sgg": "11680", "umd": "개포동", "jibun": "12",
                               "name": "래미안블레스티지", "nTrades": "30"},
    }


def _world(rng):
    http = FakeHttp()
    http.route("gangnam.eminwon.seoul.kr", _site_with_rows(rng))
    http.route("eminwon.seongnam.go.kr", _site_with_rows(rng, code="38100", dept="분당구 시민봉사과", page_param="pageNo"))
    land = FakeSeoulLand()
    make_permits(land, "11680", TODAY, rng, [("대치동", "316"), ("개포동", "12"), ("역삼동", "700")], n=25)
    http.route_seoul(land)
    return http, land


def test_runner_flow_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("TOHEO_REGION_FILTER", raising=False)
    monkeypatch.delenv("REGION_FILTER", raising=False)
    rng = random.Random(5)
    http, land = _world(rng)
    data = tmp_path / "data"
    flog = {}
    cxs = _complexes()
    res = run_toheo_sites(deep=False, data_dir=data, region_filter="11680,41135,11650", complexes=cxs, fetch_log=flog,
                          http=http, now=NOW)
    assert res["deep"] is True                      # 첫 실행은 항상 deep
    assert res["sites"] == 3 and res["new"] > 0 and res["errors"] == 1   # 서초구(11650)는 호스트 없음 → 오류지만 계속
    t = flog["toheo"]
    assert set(t) >= {"sites", "permits", "runs", "at"}
    assert t["sites"]["11680"]["ok"] and t["sites"]["41130"]["ok"] and not t["sites"]["11650"]["ok"]
    for k in ("at", "ok", "host", "pages", "rows", "toheo_rows", "reached_cutoff", "error", "warning", "deep",
              "new", "updated", "completed_observed", "dropped"):
        assert k in t["sites"]["11680"]
    p = t["permits"]
    assert p["ok"] and p["fetched"] == 25 and p["added"] == 25 and p["matched"] > 0
    assert set(p["by_gu"]) == {"강남구", "서초구"} and p["by_gu"]["서초구"] == 0
    assert len(t["runs"]) == 1 and t["runs"][0]["http_calls"] == http.calls

    store = ToheoStore(data / "toheo")
    cases = store.load_cases()
    assert cases and all(set(CASE_FIELDS) <= set(c) for c in cases.values())
    assert {c["sgg"] for c in cases.values()} == {"11680", "41135"}
    permits = store.load_permits()
    assert len(permits) == 25 and all(set(PERMIT_FIELDS) <= set(r) for r in permits.values())
    assert sum(1 for r in permits.values() if r["cid"]) == p["matched"]
    assert all(r["cname"] in ("은마", "래미안블레스티지") for r in permits.values() if r["cid"])
    assert store.load_raw(TODAY.isoformat(), "11680")["windows"][0]["body"]["resultCnt"] == 25
    assert store.load_raw(TODAY.isoformat(), "11650")["windows"][0]["body"]["resultCnt"] == 0
    assert (data / "toheo" / "saeol_params.json").exists() and (data / "toheo" / "seoul_permits_cfg.json").exists()
    assert store.load_progress()["runs"][-1]["at"] == NOW.isoformat(timespec="seconds")
    assert store.load_params()["11680"]["page_param"] == "pageIndex"

    # 다음날 light 실행: 중복 없음, 새 허가 1건만 추가, 새올 탐지 재사용
    now2 = NOW + dt.timedelta(days=1)
    land.add("11680", "7777777", now2.date().isoformat(), "강남구 대치동 316")
    calls_before = http.calls
    res2 = run_toheo_sites(deep=False, data_dir=data, region_filter="11680,41135", complexes=cxs, fetch_log=flog,
                           http=http, now=now2)
    assert res2["deep"] is False and res2["new"] == 0
    assert flog["toheo"]["permits"]["added"] == 1 and len(store.load_permits()) == 26
    assert len(store.load_cases()) == len(cases)
    assert flog["toheo"]["sites"]["11680"]["deep"] is False and len(flog["toheo"]["runs"]) == 2
    assert http.calls - calls_before < calls_before          # 탐지 생략·얕은 수집
    assert len(store.load_progress()["runs"]) == 2
    # permits=False 면 허가 수집 생략(기존 기록 유지)
    res3 = run_toheo_sites(deep=False, data_dir=data, region_filter="11680", complexes=cxs, fetch_log=flog,
                           http=http, now=now2 + dt.timedelta(days=1), permits=False)
    assert res3["permits"] is None and flog["toheo"]["permits"]["added"] == 1


def test_runner_region_filter_without_seoul_skips_permits(tmp_path):
    rng = random.Random(6)
    http, land = _world(rng)
    res = run_toheo_sites(deep=True, data_dir=tmp_path / "d", region_filter="성남", complexes={}, fetch_log={},
                          http=http, now=NOW)
    assert res["sites"] == 1 and res["permits"] is None and land.hits["list"] == 0


def test_run_toheo_with_engine_like_object(tmp_path):
    """engine 인터페이스(complexes, fetch_log, live, store.root, _save)만 있으면 run_toheo 가 동작."""
    rng = random.Random(7)
    http, land = _world(rng)

    class FakeStore:
        root = tmp_path / "data"

    class FakeEngine:
        def __init__(self):
            self.store = FakeStore()
            self.complexes = _complexes()
            self.fetch_log = {}
            self.live = {}
            self.saved = 0

        def _save(self):
            self.saved += 1

    eng = FakeEngine()
    import collector.toheo.runner as rn
    orig = rn.now_kst
    rn.now_kst = lambda: NOW
    try:
        res = run_toheo(eng, deep=True, region_filter="11680", permits=True, http=http)
    finally:
        rn.now_kst = orig
    assert eng.saved == 1 and res["sites"] == 1 and eng.fetch_log["toheo"]["permits"]["ok"]
    assert (tmp_path / "data" / "toheo" / "cases.csv").exists()
