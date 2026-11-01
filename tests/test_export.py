"""export 스키마 테스트: 토허 파일 전부 존재·필수 키·단지 toheo 블록·매물 toheo 필드, 토허 데이터 없는 export, CLI."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import engine as eng_mod  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.export import EMPTY_TOHEO, compact_listing, export_site  # noqa: E402
from collector.molit import MolitClient  # noqa: E402
from collector.naver import NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from tests import fake_servers as fs  # noqa: E402
from tests import make_demo  # noqa: E402

TOHEO_FILES = ("summary", "feed", "complexes", "pipeline", "daily", "open_cases", "recent_done")
SUMMARY_KEYS = {"generatedAt", "today", "totals", "regions", "estimates", "designations", "designationRefs", "permits",
                "accuracy", "runs", "coverage"}
TOTALS_KEYS = {"open", "received7", "received30", "receivedPrev30", "completed30", "permits7", "permits30", "goneEst30",
               "confirmed30"}
REGION_KEYS = {"sgg", "label", "sido", "zone", "open", "received7", "received30", "receivedPrev30", "completed30", "p25",
               "p50", "p75", "p90", "durationN", "deadlineDays", "statusCounts", "spark", "fetch", "permits30", "permitsTop"}
CX_ITEM_KEYS = {"cid", "name", "sgg", "umd", "n7", "n30", "n90", "total", "first", "last", "weekly", "goneEst30",
                "confirmed90", "zoneLevel"}
PIPE_ITEM_KEYS = {"id", "cid", "name", "sgg", "umd", "grp", "area", "dong", "floor", "price", "date", "score", "status",
                  "trade", "zoneLevel"}
LISTING_TOHEO_KEYS = {"zone", "until", "open", "est"}


def _j(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def server():
    srv, base = fs.start(now_ym=eng_mod.this_ym())
    yield base
    srv.shutdown()


@pytest.fixture(scope="module")
def world(server, tmp_path_factory):
    """make_demo 시나리오(허가 2건 + 내림 매물 + 95% 실거래) 로 전체 흐름을 돌린 engine."""
    root = tmp_path_factory.mktemp("world")
    e, sc = make_demo.build_world(root / "data", server)
    out = root / "site"
    res = export_site(e, out, site_url="https://example.github.io/x")
    return {"engine": e, "scenario": sc, "out": out, "res": res}


# ----------------------------------------------------------------------------- 토허 파일 전부·필수 키

def test_toheo_files_and_summary_schema(world):
    out, sc = world["out"], world["scenario"]
    tdir = out / "data" / "toheo"
    for name in TOHEO_FILES:
        assert (tdir / f"{name}.json").exists(), name
    s = _j(tdir / "summary.json")
    assert SUMMARY_KEYS <= set(s)
    assert s["today"] == sc["today"] and TOTALS_KEYS <= set(s["totals"])
    assert s["coverage"] == {"seoul": "허가 완료 단지 단위", "gyeonggi": "지정·접수 추세만", "incheon": "규제지역 아님"}
    assert {r["sgg"] for r in s["regions"]} == {"11680", "11650"}
    for r in s["regions"]:
        assert REGION_KEYS <= set(r), r["sgg"]
        assert r["zone"]["level"] == 2 and r["zone"]["until"] == "2026-12-31" and r["sido"] == "서울"
        assert r["fetch"]["ok"] is True and r["fetch"]["host"] and len(r["spark"]) == 28
        assert r["open"] > 0 and r["received30"] > 0
        assert r["permits30"] > 0 and r["permitsTop"] and set(r["permitsTop"][0]) == {"cid", "name", "n30"}
        assert r["durationN"] > 0 and r["p25"] <= r["p50"] <= r["p90"]      # 두 번째 수집에서 완료 관측
    assert s["totals"]["open"] == sum(r["open"] for r in s["regions"])
    assert s["totals"]["permits30"] > 0 and s["totals"]["goneEst30"] == 2 and s["totals"]["confirmed30"] == 1
    assert set(s["estimates"]) >= {"11680", "11650", "all"} and s["estimates"]["all"]["n"] > 0
    assert s["designations"] and all({"id", "status", "days_left", "region_ids"} <= set(d) for d in s["designations"])
    p = s["permits"]
    assert {"ok", "at", "error", "rowsTotal", "rows30", "matched", "byGu30"} <= set(p) and p["ok"] is True
    assert p["rowsTotal"] == len(world["engine"].fetch_log["toheo"]["permits"]) or p["rowsTotal"] > 0
    assert p["byGu30"].get("강남구", 0) > 0
    assert len(s["runs"]) == 2 and {"at", "deep", "sites", "new", "completed_observed", "errors", "http_calls"} <= set(s["runs"][-1])
    acc = s["accuracy"]
    assert acc["gone"]["n"] == 2 and acc["gone"]["confirmed"] == 1 and acc["gone"]["pending"] == 1 and acc["gone"]["hitRate"] == 1.0
    assert acc["permits"]["matched"] > 0 and 0 < acc["permits"]["matchRate"] <= 1


def test_feed_complexes_pipeline_daily_schema(world):
    out, sc = world["out"], world["scenario"]
    tdir = out / "data" / "toheo"
    feed = _j(tdir / "feed.json")
    assert "generatedAt" in feed and feed["days"]
    assert [d["date"] for d in feed["days"]] == sorted((d["date"] for d in feed["days"]), reverse=True)
    for d in feed["days"]:
        assert {"date", "n", "complexes", "items"} <= set(d) and d["n"] == sum(i["n"] for i in d["items"])
        for it in d["items"]:
            assert {"cid", "name", "sgg", "umd", "n", "purposes", "matched"} <= set(it)
    demo_days = {d["date"] for d in feed["days"] if any(i["cid"] == sc["cid"] for i in d["items"])}
    assert set(sc["permitDays"]) <= demo_days

    cx = _j(tdir / "complexes.json")
    assert cx["items"] and all(CX_ITEM_KEYS <= set(x) for x in cx["items"])
    assert [x["n30"] for x in cx["items"]] == sorted((x["n30"] for x in cx["items"]), reverse=True)
    demo = next(x for x in cx["items"] if x["cid"] == sc["cid"])
    assert demo["n30"] == 2 and demo["total"] == 2 and len(demo["weekly"]) == 12 and demo["goneEst30"] == 1
    assert demo["confirmed90"] == 1 and demo["zoneLevel"] == 2

    pipe = _j(tdir / "pipeline.json")
    assert {"generatedAt", "accuracy", "recent", "confirmed"} <= set(pipe)
    assert len(pipe["recent"]) == 2 and all(PIPE_ITEM_KEYS <= set(x) for x in pipe["recent"])
    conf = pipe["confirmed"]
    assert len(conf) == 1 and conf[0]["id"] == sc["atclNo"] and conf[0]["cid"] == sc["cid"] and conf[0]["status"] == "confirmed"
    assert conf[0]["trade"]["price"] == sc["trade"]["price"] and conf[0]["trade"]["date"] == sc["trade"]["date"]
    assert conf[0]["price"] == sc["price"] and 0 <= conf[0]["score"] <= 1 and conf[0]["zoneLevel"] == 2
    pend = next(x for x in pipe["recent"] if x["id"] == sc["pendingAtclNo"])
    assert pend["status"] == "pending" and pend["trade"] is None and pend["cid"] == sc["pendingCid"]

    daily = _j(tdir / "daily.json")
    for sgg in ("11680", "11650"):
        d = daily[sgg]
        assert set(d) == {"dates", "received", "completed"} and len(d["dates"]) == len(d["received"]) == len(d["completed"]) == 180
    oc = _j(tdir / "open_cases.json")
    assert oc["11680"] and {"no", "received", "deadline", "status", "dept", "days", "overdue"} <= set(oc["11680"][0])
    rd = _j(tdir / "recent_done.json")
    assert rd["11680"] and set(rd["11680"][0]) == {"d", "n"}


# ----------------------------------------------------------------------------- 단지 toheo 블록·매물 toheo 필드

def test_complex_page_toheo_block_and_listing_field(world):
    out, sc, e = world["out"], world["scenario"], world["engine"]
    page = _j(out / "data" / "c" / f"{sc['cid']}.json")
    t = page["toheo"]
    assert {"zone", "region", "permits", "byGrp", "timeline", "note"} <= set(t)
    assert t["zone"] == {"level": 2, "until": "2026-12-31", "daysLeft": t["zone"]["daysLeft"],
                         "title": t["zone"]["title"], "renewal": "연장 미확정"} and t["zone"]["daysLeft"] > 0
    r = t["region"]
    assert r["sgg"] == "11680" and r["label"] == "강남구" and r["open"] > 0 and r["est"] and r["p50"] == r["est"]["p50"]
    p = t["permits"]
    assert (p["n7"], p["n30"], p["total"]) == (2, 2, 2) and len(p["weekly"]) == 12 and p["byPurpose"] == {"주거용": 2}
    assert len(p["recent"]) == 2 and {"date", "purpose", "jibun", "jobGbn", "trade"} <= set(p["recent"][0])
    assert any(x["trade"] and x["trade"]["date"] == sc["trade"]["date"] for x in p["recent"])   # 허가 ↔ 실거래 매칭
    g = t["byGrp"][str(sc["grp"])]
    assert {"active", "active30ago", "gone30", "confirmed90", "trades30"} <= set(g)
    assert len(g["gone30"]) == 1 and g["gone30"][0]["id"] == sc["atclNo"] and g["gone30"][0]["status"] == "confirmed"
    assert g["gone30"][0]["trade"]["price"] == sc["trade"]["price"] and g["confirmed90"] == 1
    assert {x["type"] for x in t["timeline"]} >= {"permit", "gone", "trade", "zone"}
    assert all({"date", "type", "text", "grp"} <= set(x) for x in t["timeline"])
    assert "추정" in t["note"]
    # 매물 toheo 필드
    assert page["listings"]
    for l in page["listings"]:
        assert set(l["toheo"]) == LISTING_TOHEO_KEYS
        assert l["toheo"]["zone"] == 2 and l["toheo"]["until"] == "2026-12-31" and l["toheo"]["open"] == r["open"]
        assert l["toheo"]["est"] and set(l["toheo"]["est"]) == {"p50", "p90"}
    assert all(l["id"] != sc["atclNo"] for l in page["listings"])               # gone 매물은 사이트에 없음
    top = _j(out / "data" / "top.json")
    assert all("toheo" in l for l in top["listings"]) and top["total"] == world["res"]["listings"]
    # pending 단지 페이지
    page2 = _j(out / "data" / "c" / f"{sc['pendingCid']}.json")
    gl = [x for g_ in page2["toheo"]["byGrp"].values() for x in g_["gone30"]]
    assert len(gl) == 1 and gl[0]["status"] == "pending" and gl[0]["score"] > 0


def test_status_and_rss(world):
    out, sc, e = world["out"], world["scenario"], world["engine"]
    st = _j(out / "data" / "status.json")
    assert "toheo" in st["fetchLog"] and st["fetchLog"]["toheo"]["permits"]["ok"]
    assert st["counts"]["permits"] > 0 and st["counts"]["goneEvents"] == 2
    assert (out / "feed" / "toheo" / "all.xml").exists() and (out / "feed" / "toheo" / "11680.xml").exists()
    allxml = (out / "feed" / "toheo" / "all.xml").read_text(encoding="utf-8")
    assert allxml.startswith("<?xml") and allxml.count("<item>") > 0 and "permit-11680|" in allxml
    cx_xml = (out / "feed" / "c" / f"{sc['cid']}.xml").read_text(encoding="utf-8")
    assert cx_xml.count("permit-") == 2 and "<guid isPermaLink=\"false\">" + sc["atclNo"] not in cx_xml
    assert any(l for l in page_listing_ids(out, sc["cid"]) if f"<guid isPermaLink=\"false\">{l}</guid>" in cx_xml)
    # 재실행 멱등: 산출물 다시 써도 같은 구조·수치
    res2 = export_site(e, out, site_url="https://example.github.io/x")
    assert res2 == world["res"]
    s2 = _j(out / "data" / "toheo" / "summary.json")
    assert s2["totals"] == _j(out / "data" / "toheo" / "summary.json")["totals"] and s2["accuracy"]["gone"]["confirmed"] == 1


def page_listing_ids(out: Path, cid: str) -> list[str]:
    return [l["id"] for l in _j(out / "data" / "c" / f"{cid}.json")["listings"]]


# ----------------------------------------------------------------------------- 토허 데이터가 전혀 없는 export

def test_export_without_any_toheo_data(tmp_path, server):
    store = Store(tmp_path / "data")
    molit = MolitClient("key", url=server + "/molit", call_budget=9500, sleep=0)
    nv = NaverClient(fs.naver_cfg_for(server, load_cfg()), state_path=tmp_path / "data" / "naver_cfg.json")
    e = Engine(store, molit, nv, region_filter="11680", log=lambda *a: None)
    e.run_trades(reserve=100)
    e.run_model()
    e.run_match()
    fs.Handler.articles_override = {}
    e.run_listings()
    out = tmp_path / "site"
    res = export_site(e, out)
    assert res["listings"] > 0 and res["permits"] == 0 and res["cases"] == 0
    tdir = out / "data" / "toheo"
    for name in TOHEO_FILES:
        assert (tdir / f"{name}.json").exists(), name
    s = _j(tdir / "summary.json")
    assert SUMMARY_KEYS <= set(s) and s["hasData"] is False
    assert s["totals"] == {k: 0 for k in TOTALS_KEYS}
    assert [r["sgg"] for r in s["regions"]] == ["11680"]           # 조회 지역의 토허 대상 구는 빈 통계로라도 나온다
    r = s["regions"][0]
    assert r["open"] == 0 and r["fetch"]["ok"] is None and r["zone"]["level"] == 2 and r["permitsTop"] == []
    assert s["estimates"] == {"all": None} and s["designations"] and s["permits"]["ok"] is None and s["runs"] == []
    assert s["accuracy"]["gone"] == {"n": 0, "confirmed": 0, "relisted": 0, "unconfirmed": 0, "pending": 0, "hitRate": None}
    assert _j(tdir / "feed.json")["days"] == [] and _j(tdir / "complexes.json")["items"] == []
    pipe = _j(tdir / "pipeline.json")
    assert pipe["recent"] == [] and pipe["confirmed"] == []
    assert _j(tdir / "daily.json") == {} and _j(tdir / "open_cases.json") == {} and _j(tdir / "recent_done.json") == {}
    st = _j(out / "data" / "status.json")
    assert st["counts"]["permits"] == 0 and st["counts"]["goneEvents"] == 0 and "toheo" not in st["fetchLog"]
    cid = next(c for c in e.complexes if e.complexes[c]["name"] == "은마")
    page = _j(out / "data" / "c" / f"{cid}.json")
    t = page["toheo"]
    assert t["zone"]["level"] == 2 and t["permits"]["total"] == 0 and t["permits"]["recent"] == []
    assert t["region"]["sgg"] == "11680" and t["region"]["open"] is None and t["region"]["est"] is None
    assert all(g["gone30"] == [] for g in t["byGrp"].values())
    assert all(l["toheo"] == {"zone": 2, "until": "2026-12-31", "open": None, "est": None} for l in page["listings"])
    allxml = (out / "feed" / "toheo" / "all.xml").read_text(encoding="utf-8")
    assert allxml.startswith("<?xml") and "<item>" not in allxml
    # 완전히 빈 저장소(수집 전)도 성공
    e0 = Engine(Store(tmp_path / "empty"), None, None, region_filter="서울", log=lambda *a: None)
    res0 = export_site(e0, tmp_path / "site0")
    assert res0["complexes"] == 0 and (tmp_path / "site0" / "data" / "toheo" / "summary.json").exists()


def test_compact_listing_toheo_default():
    l = {"atclNo": "1", "cid": "c", "price": "100000", "grp": "84"}
    cl = compact_listing(l, {"name": "x", "sgg": "11680"}, None, "http://u")
    assert cl["toheo"] == EMPTY_TOHEO and cl["toheo"] is not EMPTY_TOHEO
    cl2 = compact_listing(l, {}, None, "u", {"zone": 1, "until": None, "open": 3, "est": {"p50": 10, "p90": 20}})
    assert cl2["toheo"]["zone"] == 1 and cl2["toheo"]["est"]["p90"] == 20


# ----------------------------------------------------------------------------- CLI

def test_cli_toheo_and_export_notify(tmp_path, server, monkeypatch):
    from collector.__main__ import main
    import collector.toheo.runner as rn
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MOLIT_SERVICE_KEY", raising=False)
    monkeypatch.setenv("NAVER_CFG", "")
    import datetime as dt
    import random
    http, land = make_demo.make_toheo_http(dt.date.fromisoformat(eng_mod.today_str()), random.Random(3))
    monkeypatch.setattr(rn, "Polite", lambda *a, **k: http)
    data = tmp_path / "data"
    # MOLIT_SERVICE_KEY 없이 toheo 가 동작(단지 매칭만 비어 있음)
    assert main(["--data", str(data), "--region", "11680", "toheo", "--deep"]) == 0
    assert (data / "toheo" / "cases.csv").exists() and (data / "toheo" / "permits_seoul.csv").exists()
    flog = json.loads((data / "fetch_log.json").read_text(encoding="utf-8"))
    assert flog["toheo"]["permits"]["matched"] == 0 and flog["toheo"]["sites"]["11680"]["ok"]
    assert main(["--data", str(data), "--region", "11680", "toheo", "--no-permits"]) == 0
    # export --notify: 토큰이 없으면 건너뛰고 성공
    out = tmp_path / "site"
    assert main(["--data", str(data), "--region", "11680", "export", "--out", str(out), "--notify"]) == 0
    s = _j(out / "data" / "toheo" / "summary.json")
    assert s["hasData"] is True and s["regions"][0]["open"] > 0 and s["permits"]["ok"] is True
