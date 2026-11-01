import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.export import export_site  # noqa: E402
from collector.judge import judge_listing  # noqa: E402
from collector.molit import MolitClient  # noqa: E402
from collector.naver import NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.deals import parse_listing_price, norm_dong, parse_floor_info  # noqa: E402
from tests import fake_servers as fs  # noqa: E402


@pytest.fixture(scope="module")
def server():
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    yield base
    srv.shutdown()


def make_engine(tmp, base, region="11680,11650", budget=9500, naver=True):
    store = Store(tmp / "data")
    molit = MolitClient("key", url=base + "/molit", call_budget=budget, sleep=0)
    nv = NaverClient(fs.naver_cfg_for(base, load_cfg()), state_path=tmp / "data" / "naver_cfg.json") if naver else None
    return Engine(store, molit, nv, region_filter=region, log=lambda *a: None)


def test_parsers():
    assert parse_listing_price("14억 5,000") == 145000
    assert parse_listing_price("28억") == 280000
    assert parse_listing_price("9,500") == 9500
    assert parse_listing_price("14.5억") == 145000
    assert norm_dong("제101동") == "101" and norm_dong("101동") == "101" and norm_dong("가동") == "가"
    assert parse_floor_info("3/15") == (3, 15) and parse_floor_info("고/15") == (None, 15)


def test_full_flow(tmp_path, server):
    e = make_engine(tmp_path, server)
    summary = e.run_trades(reserve=100)
    assert not summary["stopped"], summary
    assert all(e.progress["backfill"][c]["done"] for c in ("11680", "11650"))
    assert e.region_done("11")
    assert len(e.store.years_for("11680")) >= 20

    # 모델
    res = e.run_model()
    assert "11680" in res and "11650" in res
    m = e.store.read_model("11680")
    cid = next(cid for cid, c in e.complexes.items() if c["name"] == "래미안블레스티지")
    cm = m["complexes"][cid]
    assert abs(cm["dong"]["101"]["b"] - 0.04) < 0.03, cm["dong"]
    assert abs(cm["dong"]["105"]["b"] - (-0.04)) < 0.03, cm["dong"]
    assert cm["floor"]["f1"] < -0.05, cm["floor"]
    assert cm["floor"]["high"] > cm["floor"]["low"]
    g84 = cm["groups"]["84"]
    assert g84["n"] > 50 and g84["sigma"] < 0.07
    assert 280000 < g84["fair"] < 420000  # 2022 기준 32억 + 지수 상승
    # 시군구 지수: 최근 달 = 0 이고 과거는 음수(상승장)
    assert abs(m["S"][-1]) < 1e-6 and m["S"][0] < -0.3

    # 네이버 매칭
    mt = e.run_match()
    assert mt["status"] == "ok"
    assert all(c["naverId"] for c in e.complexes.values()), [c for c in e.complexes.values() if not c["naverId"]]
    eunma = next(c for c in e.complexes.values() if c["name"] == "은마")
    assert eunma["naverId"] == "100003" and float(eunma["matchScore"]) > 0.6

    # 매물
    ls = e.run_listings()
    assert ls["status"] == "ok" and ls["n"] > 20
    listings = e.store.read_listings()
    # 판정: 급매(−10%) 매물은 SS/S
    any_ss = False
    for l in listings.values():
        l2 = {**l, "floor": int(l["floor"]), "top": int(l["top"]), "grp": int(l["grp"]), "price": int(l["price"]),
              "area": float(l["area"])}
        grp = e.group_of(e.complexes[l["cid"]]["sgg"])
        j = judge_listing(e.store.read_model(grp), l["cid"], l2)
        assert j and j["fair"] > 0
        if l["desc"] == "급매":
            assert j["diffPct"] < -0.04, (l, j)
            any_ss |= j["grade"] in ("SS", "S")
        else:
            assert j["diffPct"] > -0.04, (l, j)
    assert any_ss

    # export
    out = tmp_path / "site"
    r = export_site(e, out, site_url="https://example.github.io/x")
    assert r["listings"] > 20
    idx = json.loads((out / "data" / "index.json").read_text(encoding="utf-8"))
    assert len(idx["complexes"]) == 5
    page = json.loads((out / "data" / "c" / f"{cid}.json").read_text(encoding="utf-8"))
    assert page["model"]["groups"]["84"]["fair"] == g84["fair"]
    assert page["listings"] and page["listings"][0]["grade"] in ("SS", "S")
    assert "roy" in json.dumps(page["model"]["royal"]) or page["model"]["royal"] == {} or page["model"]["royal"]["royal"] == "101"
    top = json.loads((out / "data" / "top.json").read_text(encoding="utf-8"))
    assert top["listings"] and top["listings"][0]["diff"] <= top["listings"][-1]["diff"]
    assert (out / "feed" / "all.xml").exists() and (out / "feed" / "c" / f"{cid}.xml").exists()
    trend = json.loads((out / "data" / "trend.json").read_text(encoding="utf-8"))
    assert trend["regions"]["11680"]["idx"][0] == 100.0
    status = json.loads((out / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["progress"]["11"]["done"] == 2

    # 재조회: 다시 실행해도 중복 없음, 해제 반영
    n_before = len(e.store.read_trades_group(["11680"]))
    e2 = make_engine(tmp_path, server)
    e2.run_trades(reserve=100)
    n_after = len(e2.store.read_trades_group(["11680"]))
    assert n_after == n_before


def test_budget_resume(tmp_path, server):
    e = make_engine(tmp_path, server, region="11680", budget=120, naver=False)
    s = e.run_trades(reserve=20)
    assert s["stopped"] in ("reserve", "budget")
    assert not e.progress["backfill"]["11680"]["done"]
    cursor = e.progress["backfill"]["11680"]["cursor"]
    assert cursor < "202501"
    # 다음날: 호출 수 초기화된 것처럼 새 예산
    e.progress["date"] = "2000-01-01"
    e.store.write_json("progress.json", e.progress)
    e2 = make_engine(tmp_path, server, region="11680", budget=9500, naver=False)
    assert e2.molit.calls == 0
    s2 = e2.run_trades(reserve=20)
    assert e2.progress["backfill"]["11680"]["done"]
    assert not s2["stopped"]


def test_api_limit_response(tmp_path, server):
    fs.Handler.limit_after = fs.Handler.calls["molit"] + 30
    try:
        e = make_engine(tmp_path, server, region="11650", naver=False)
        s = e.run_trades(reserve=0)
        assert s["stopped"].startswith("budget")
        assert e.molit.limited
    finally:
        fs.Handler.limit_after = None


def test_naver_blocked(tmp_path, server):
    cfg = fs.naver_cfg_for(server, load_cfg())
    for ep in cfg["articles"]:
        ep["url"] = server + "/blocked?x={complexNo}&page={page}"
    nv = NaverClient(cfg, state_path=tmp_path / "n.json")
    from collector.naver import Blocked
    with pytest.raises(Blocked):
        nv.articles("100001")
    assert nv.blocked_reason
