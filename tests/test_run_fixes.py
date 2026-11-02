"""첫 실운영(2026-11-02 Actions 로그)에서 드러난 문제의 회귀 테스트.

- 서울 새올·land.seoul.go.kr 연결 타임아웃이 호스트마다 87초씩 95분을 끌던 것 → Polite 의 접속 불가 호스트·계열 빠른 포기, 시간 상한
- 네이버가 TCP 단계에서 막힌 날 400개 동 × 20초 타임아웃이 'ok, matched 0' 으로 2.5시간 돌던 것 → 연속 연결 실패 = Blocked, 0건 진단
- 실거래 API 가 돌려준 화성 4개 구·인천 3개 구 신설 코드 중 '신설구' 로 남은 것(41593 효행구, 28275 서구) 이름 붙이기
- 화성 신설 구 코드의 단지가 토허 지역(동탄/동탄 외)에 매핑되는지
"""
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import regions  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.naver import Blocked, NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.toheo import http as thttp  # noqa: E402
from collector.toheo import regions as tr  # noqa: E402
from collector.toheo import runner  # noqa: E402
from collector.toheo.runner import run_toheo_sites  # noqa: E402
from tests.fake_toheo import FakeHttp, FakeSite, make_rows  # noqa: E402


# ----------------------------------------------------------------------------- Polite: 접속 불가 빠른 포기

class FailSession:
    """requests.Session 대체: 호스트별로 정해진 예외를 던진다."""

    def __init__(self, exc_for):
        self.headers = {}
        self.calls = []
        self.exc_for = exc_for   # host → Exception 클래스 (없으면 200 응답)

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("timeout")))
        host = url.split("/")[2]
        exc = self.exc_for.get(host)
        if exc is requests.ReadTimeout:
            raise exc(f"HTTPSConnectionPool(host='{host}'): Read timed out. (read timeout=25)")
        if exc:
            raise exc(f"Connection to {host} timed out. (connect timeout=8)")

        class R:
            status_code = 200
            content = b"ok"
            text = "ok"
        return R()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(thttp.time, "sleep", lambda s: None)


def test_polite_connect_timeout_gives_up_after_second_failure_and_remembers_host():
    sess = FailSession({"gangnam.eminwon.seoul.kr": requests.ConnectTimeout})
    p = thttp.Polite(delay=0, session=sess)
    with pytest.raises(thttp.Unreachable):
        p.get("https://gangnam.eminwon.seoul.kr/list")
    assert len(sess.calls) == 2                       # 1회 재시도 뒤 포기(25초 × 3회가 아님)
    assert sess.calls[0][2] == (p.connect_timeout, p.read_timeout)
    assert "gangnam.eminwon.seoul.kr" in p.unreachable
    with pytest.raises(thttp.Unreachable):
        p.get("https://gangnam.eminwon.seoul.kr/other")
    assert len(sess.calls) == 2                       # 같은 호스트는 다시 시도하지 않음
    # 다른 호스트(경기)는 정상
    assert p.get("https://eminwon.suwon.go.kr/list").status_code == 200
    assert p.summary()["unreachable"] == {"gangnam.eminwon.seoul.kr": "ConnectTimeout"}


def test_polite_blocks_whole_seoul_family_after_three_hosts():
    hosts = ["gangnam.eminwon.seoul.kr", "seocho.eminwon.seoul.kr", "songpa.eminwon.seoul.kr"]
    sess = FailSession({h: requests.ConnectTimeout for h in hosts})
    p = thttp.Polite(delay=0, session=sess)
    for h in hosts:
        with pytest.raises(thttp.Unreachable):
            p.get(f"https://{h}/list")
    assert "seoul" in p.blocked_families and "차단 추정" in p.blocked_families["seoul"]
    n = len(sess.calls)
    # 4번째 서울 호스트(허가 사이트 포함)는 호출 없이 즉시 실패
    with pytest.raises(thttp.Unreachable) as ei:
        p.get("https://land.seoul.go.kr/land/other/contractStatus.do")
    assert "seoul 계열" in str(ei.value) and len(sess.calls) == n
    # 경기 계열은 영향 없음
    assert p.get("https://eminwon.yongin.go.kr/list").status_code == 200


def test_polite_read_timeout_still_retries_normally():
    sess = FailSession({"eminwon.suwon.go.kr": requests.ReadTimeout})
    p = thttp.Polite(delay=0, session=sess)
    with pytest.raises(thttp.HttpError) as ei:
        p.get("https://eminwon.suwon.go.kr/list")
    assert not isinstance(ei.value, thttp.Unreachable)
    assert len(sess.calls) == p.max_retry            # 응답은 오는데 느린 경우는 기존대로 3회


def test_polite_deadline():
    sess = FailSession({})
    p = thttp.Polite(delay=0, session=sess)
    p.set_max_sec(0.01)
    p.deadline = time.monotonic() - 1      # (autouse 픽스처가 time.sleep 을 무력화하므로 기다리지 않고 지난 시각으로)
    assert p.past_deadline()
    with pytest.raises(thttp.DeadlineExceeded):
        p.get("https://eminwon.suwon.go.kr/list")
    assert sess.calls == []
    p.set_max_sec(0)
    assert not p.past_deadline()


def test_family_of():
    assert thttp.family_of("gangnam.eminwon.seoul.kr") == "seoul"
    assert thttp.family_of("land.seoul.go.kr") == "seoul"
    assert thttp.family_of("eminwon.suwon.go.kr") is None


# ----------------------------------------------------------------------------- runner: TOHEO_MAX_SEC

def test_runner_skips_remaining_sites_after_max_sec(tmp_path, monkeypatch):
    rng = random.Random(3)
    today = dt.date(2026, 11, 2)
    http = FakeHttp()
    for sgg in ("11680", "11650"):
        site = tr.SITE_BY_ID[sgg]
        fs = FakeSite()
        for r in make_rows(sgg, today - dt.timedelta(days=10), today, 2, "부동산과", rng):
            fs.add(r)
        http.route(site.hosts[0], fs)
    monkeypatch.setenv("TOHEO_MAX_SEC", "100")
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 60.0        # 호출마다 1분씩 흐른다: 첫 사이트는 통과, 둘째부터 상한 초과
        return clock["t"]

    monkeypatch.setattr(runner.time, "monotonic", fake_monotonic)
    fetch_log = {}
    rec = run_toheo_sites(deep=False, data_dir=tmp_path, region_filter="11680,11650", complexes={}, fetch_log=fetch_log,
                          permits=True, http=http, now=dt.datetime(2026, 11, 2, 9, 0))
    sites = fetch_log["toheo"]["sites"]
    first, second = [s.id for s in tr.sites_for(["11680", "11650"])]   # 사이트 순서(코드 순)
    assert rec["max_sec"] == 100 and rec["skipped"] >= 1
    assert sites[first]["ok"] and "skipped" not in sites[first]
    assert "시간 상한" in sites[second]["skipped"]
    # 허가 수집도 건너뛰되 기록은 남는다
    assert fetch_log["toheo"]["permits"]["skipped"] is True and "시간 상한" in fetch_log["toheo"]["permits"]["error"]
    assert rec["sites"] == 2 and rec["new"] > 0
    # 다음 실행(상한 없음)에서는 건너뛴 사이트가 수집되고 skipped 표시가 지워진다
    monkeypatch.setenv("TOHEO_MAX_SEC", "0")
    rec2 = run_toheo_sites(deep=False, data_dir=tmp_path, region_filter="11680,11650", complexes={}, fetch_log=fetch_log,
                           permits=False, http=http, now=dt.datetime(2026, 11, 2, 11, 0))
    assert rec2["skipped"] == 0 and sites[second]["ok"] and "skipped" not in sites[second]


# ----------------------------------------------------------------------------- 신설 구 이름

SCAN_2026_11 = {   # 2026-11-02 실거래 API 탐색 결과(요약)
    "41591": ["남양읍 남양리", "새솔동", "송산면 육일리", "우정읍 조암리", "장안면 사곡리", "팔탄면 가재리", "향남읍 발안리"],
    "41593": ["기안동", "매송면 천천리", "봉담읍 내리", "봉담읍 동화리", "비봉면 삼화리", "정남면 발산리"],
    "41595": ["기산동", "능동", "반월동", "반정동", "병점동", "송산동", "안녕동", "진안동"],
    "41597": ["능동", "목동", "반송동", "산척동", "석우동", "송동", "신동", "여울동", "영천동", "장지동", "청계동"],
    "28125": ["내동", "만석동", "송림동", "송현동", "신흥동2가", "전동", "항동7가", "화수동"],
    "28155": ["운남동", "운서동", "중산동"],
    "28275": ["가정동", "가좌동", "검암동", "경서동", "공촌동", "석남동", "신현동", "심곡동", "연희동", "청라동"],
    "28290": ["금곡동", "당하동", "마전동", "백석동", "불로동", "오류동", "왕길동", "원당동"],
}


def test_guess_new_gu_names_all_2026_codes():
    want = {"41591": ("만세구", "41590"), "41593": ("효행구", "41590"), "41595": ("병점구", "41590"),
            "41597": ("동탄구", "41590"), "28125": ("제물포구", "28110"), "28155": ("영종구", "28110"),
            "28275": ("서구", "28260"), "28290": ("검단구", "28260")}
    for code, umds in SCAN_2026_11.items():
        fallback = "41590" if code.startswith("41") else None
        assert regions.guess_new_gu(set(umds), code[:2], fallback) == want[code], code


def test_refresh_live_names_fixes_old_fallback_entries():
    live = {"41593": {"name": "신설구", "group": "41590", "umds": SCAN_2026_11["41593"]},
            "28275": {"name": "신설구", "group": "28275", "umds": SCAN_2026_11["28275"]},
            "41597": {"name": "동탄구", "group": "41590", "umds": SCAN_2026_11["41597"]},
            "99999": {"name": "신설구", "group": "99999", "umds": []}}
    assert regions.refresh_live_names(live) == 2
    assert live["41593"]["name"] == "효행구" and live["41593"]["group"] == "41590"
    assert live["28275"]["name"] == "서구" and live["28275"]["group"] == "28260"
    assert live["41597"]["name"] == "동탄구" and live["99999"]["name"] == "신설구"
    assert regions.refresh_live_names(live) == 0
    assert regions.name_of("28275", live) == "서구" and regions.group_of("28275", live) == "28260"
    assert "28275" in dict(regions.AREA_CHIPS)["송도·청라"]


def test_engine_refreshes_live_names_on_load(tmp_path):
    store = Store(tmp_path / "data")
    store.write_json("regions_live.json", {"41593": {"name": "신설구", "group": "41590", "umds": SCAN_2026_11["41593"]}})
    eng = Engine(store, None, None, region_filter="11680", log=lambda *a: None)
    assert eng.live["41593"]["name"] == "효행구"
    assert json.loads((tmp_path / "data" / "regions_live.json").read_text(encoding="utf-8"))["41593"]["name"] == "효행구"


def test_toheo_sgg_for_hwaseong_new_codes():
    assert tr.toheo_sgg_for("41597", "청계동") == "41590:dongtan"
    assert tr.toheo_sgg_for("41597") == "41590:dongtan"
    assert tr.toheo_sgg_for("41595", "병점동") == "41590"
    assert tr.toheo_sgg_for("41593", "기안동") == "41590"
    assert tr.toheo_sgg_for("41590", "반송동") == "41590:dongtan"
    assert tr.toheo_sgg_for("11680", "개포동") == "11680"


# ----------------------------------------------------------------------------- 네이버: 연속 연결 실패 = 차단

class ConnFailSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def _fail(self, url, **kw):
        self.calls.append(kw.get("timeout"))
        raise requests.ConnectTimeout(f"HTTPSConnectionPool(host='fin.land.naver.com'): Max retries exceeded ({url})")

    get = post = _fail


def test_naver_consecutive_connection_failures_become_blocked():
    sess = ConnFailSession()
    nv = NaverClient(load_cfg(), session=sess, delay=0, max_calls=50)
    with pytest.raises(Blocked) as ei:
        for _ in range(5):
            nv.complexes("1168010300")
    assert "연결 실패" in str(ei.value) and "차단 추정" in nv.blocked_reason
    assert len(sess.calls) == 3                       # 3회 연속 실패에서 멈춤(400개 동 × 20초가 아님)
    assert sess.calls[0] == (8, 20)                   # 연결 8초 / 읽기 20초
    with pytest.raises(Blocked):                      # 이후 호출은 즉시 Blocked
        nv.complexes("1168010300")
    assert len(sess.calls) == 3


CX = {"11680-개포동-12-2019": {"name": "래미안블레스티지"}, "11680-대치동-316-1979": {"name": "은마"}}


def _engine_with(tmp_path, naver) -> Engine:
    store = Store(tmp_path / "data")
    cx = {cid: {"cid": cid, "sgg": "11680", "umdCd": "10300" if "개포동" in cid else "10600",
                "umd": cid.split("-")[1], "jibun": cid.split("-")[2], "buildYear": cid.split("-")[3],
                "firstYm": "201901", "nTrades": 100, **v} for cid, v in CX.items()}
    store.write_complexes(cx)
    return Engine(store, None, naver, region_filter="11680", log=lambda *a: None)


class EmptyNaver(NaverClient):
    """호출은 되는데(상태 ok) 단지 목록을 못 찾는 응답."""

    def complexes(self, cortar_no):
        self.calls += 1
        self.last_data = {"isSuccess": True, "result": {"complexList": None, "note": "fields renamed"}}
        self.log.append(f"complexes_by_cortar 실패: nolist {{'cortarNo': '{cortar_no}'}}")
        return []


class BlockedNaver(NaverClient):
    def complexes(self, cortar_no):
        self.calls += 1
        if self.calls >= 2:
            self.blocked_reason = "연결 실패 3회 연속 (ConnectTimeout; GitHub Actions 해외 IP 차단 추정 — 집 PC 수집을 쓰세요)"
            raise Blocked(self.blocked_reason)
        return []


def test_run_match_all_empty_is_reported_with_probe_sample(tmp_path):
    eng = _engine_with(tmp_path, EmptyNaver(load_cfg(), state_path=None, delay=0, max_calls=50))
    res = eng.run_match()
    assert res["status"].startswith("empty:") and res["matched"] == 0
    assert res["emptyDongs"] == res["dongs"] == 2
    assert any("nolist" in line for line in res["log"])
    probe = json.loads((tmp_path / "data" / "naver_probe.json").read_text(encoding="utf-8"))
    assert probe["kind"] == "match" and len(probe["diag"]) == 2
    assert "fields renamed" in probe["diag"][0]["sample"] and probe["diag"][0]["cortar"] == ["1168010300"]


def test_run_match_blocked_stops_immediately(tmp_path):
    eng = _engine_with(tmp_path, BlockedNaver(load_cfg(), state_path=None, delay=0, max_calls=50))
    res = eng.run_match()
    assert res["status"].startswith("blocked:") and "차단 추정" in res["status"]
    assert res["dongs"] == 1 and res["pending"] == 1 and res["emptyDongs"] == 1
