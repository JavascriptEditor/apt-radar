"""파이프라인(collector/pipeline.py) 테스트: 링크 4상태(confirmed/pending/unconfirmed/relisted)·점수 범위·적중률·단지 요약."""
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import pipeline as pl  # noqa: E402
from collector.toheo import designations as dz  # noqa: E402

TODAY = dt.date(2026, 10, 27)
CID = "11680-개포동-12-2019"


def ev(kind, atcl, date, price=100000, grp="84", area="84.94", dong="101", floor="12", cid=CID):
    return {"date": date, "atclNo": atcl, "cid": cid, "grp": grp, "area": area, "dong": dong, "floor": floor,
            "event": kind, "price": str(price), "prevPrice": ""}


def tr(date, price, area=84.94, floor=12, dong="101", direct=0, cancelled=0):
    return [date, price, area, floor, dong, direct, cancelled]


# ----------------------------------------------------------------------------- 내림 ↔ 실거래

def test_link_gone_confirmed_pending_unconfirmed_relisted():
    events = [
        ev("gone", "A", "2026-10-01", 100000),                      # 실거래 10/10 95% → confirmed
        ev("gone", "B", "2026-10-15", 150000),                      # 맞는 실거래 없음(가격 창 밖), 75일 안 → pending
        ev("gone", "C", "2026-07-01", 100000),                      # 실거래 없음, 75일 지남 → unconfirmed
        ev("gone", "D", "2026-10-05", 100000), ev("relist", "D", "2026-10-20"),   # 21일 안 relist → relisted
        ev("gone", "E", "2026-08-01", 100000), ev("relist", "E", "2026-09-15"),   # 45일 뒤 relist → relist 무시, unconfirmed
    ]
    trades = {CID: [tr("2026-10-10", 95000), tr("2026-10-07", 97000, floor=12, dong="101", direct=1),   # 직거래 제외
                    tr("2026-10-08", 96000, cancelled=1)]}                                              # 해제 제외
    links = {l["id"]: l for l in pl.link_gone_to_trades(events, trades, TODAY)}
    assert set(links) == {"A", "B", "C", "D", "E"}
    assert links["A"]["status"] == "confirmed" and links["A"]["trade"]["date"] == "2026-10-10"
    assert links["A"]["trade"]["price"] == 95000 and links["A"]["daysToTrade"] == 9
    assert links["B"]["status"] == "pending" and links["B"]["trade"] is None
    assert links["C"]["status"] == "unconfirmed"
    assert links["D"]["status"] == "relisted" and links["D"]["trade"] is None
    assert links["E"]["status"] == "unconfirmed"
    for l in links.values():
        assert set(l) >= {"id", "cid", "grp", "area", "dong", "floor", "price", "date", "status", "trade"}


def test_link_gone_rules_window_price_floor_grp_and_one_to_one():
    g = ev("gone", "A", "2026-10-01", 100000)
    # 가격 창 [0.85, 1.03]
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 84000)]}, TODAY)[0]["status"] == "pending"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 85000)]}, TODAY)[0]["status"] == "confirmed"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 103000)]}, TODAY)[0]["status"] == "confirmed"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 104000)]}, TODAY)[0]["status"] == "pending"
    # 날짜 창 [goneAt-14d, goneAt+60d]
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-09-17", 95000)]}, TODAY)[0]["status"] == "confirmed"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-09-16", 95000)]}, TODAY)[0]["status"] == "pending"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-11-30", 95000)]}, dt.date(2026, 12, 1))[0]["status"] == "confirmed"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-12-01", 95000)]}, dt.date(2026, 12, 2))[0]["status"] == "pending"
    # 층: 둘 다 있으면 같아야, 층 모르면 무시. 평형은 같아야.
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 95000, floor=13)]}, TODAY)[0]["status"] == "pending"
    g2 = dict(g, floor="")
    assert pl.link_gone_to_trades([g2], {CID: [tr("2026-10-10", 95000, floor=13)]}, TODAY)[0]["status"] == "confirmed"
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-10", 95000, area=59.9)]}, TODAY)[0]["status"] == "pending"
    # 미래 계약일(today 이후)은 무시
    assert pl.link_gone_to_trades([g], {CID: [tr("2026-10-30", 95000)]}, TODAY)[0]["status"] == "pending"
    # 실거래 하나는 내림 하나에만(가까운 쪽), 다른 단지 거래는 안 붙음
    gs = [ev("gone", "A", "2026-10-01", 100000), ev("gone", "B", "2026-10-08", 100000)]
    links = {l["id"]: l for l in pl.link_gone_to_trades(gs, {CID: [tr("2026-10-10", 95000)]}, TODAY)}
    assert links["B"]["status"] == "confirmed" and links["A"]["status"] == "pending"
    other = pl.link_gone_to_trades([g], {"other": [tr("2026-10-10", 95000)]}, TODAY)
    assert other[0]["status"] == "pending"
    assert pl.link_gone_to_trades([], {}, TODAY) == []


# ----------------------------------------------------------------------------- 허가 ↔ 실거래

def _permit(key, day, cid=CID, job="허가"):
    return {"key": key, "cid": cid, "hndl_ymd": day, "job_gbn": job, "use_purp": "주거용", "jibun": "12", "sgg": "11680"}


def test_link_permits_to_trades_window_nearest_one_to_one():
    permits = {"p1": _permit("p1", "2026-09-01"), "p2": _permit("p2", "2026-09-20"),
               "p3": _permit("p3", "2026-10-01", job="불허가"), "p4": _permit("p4", "2026-10-01", cid=""),
               "p5": _permit("p5", "2026-05-01")}
    trades = {CID: [tr("2026-08-30", 90000), tr("2026-09-25", 95000), tr("2026-10-20", 96000)]}
    out = pl.link_permits_to_trades(permits, trades, TODAY)
    assert set(out) == set(permits)
    assert out["p2"]["date"] == "2026-09-25" and out["p2"]["days"] == 5       # 가장 가까운 것
    assert out["p1"]["date"] == "2026-10-20" and out["p1"]["days"] == 49      # 9/25 는 p2 가 가져감 → 다음 후보
    assert out["p3"] is None and out["p4"] is None                            # 불허가·단지 미매칭
    assert out["p5"] is None                                                  # 75일 창 밖
    # 8/30 거래는 어느 허가일보다 앞서므로 아무 데도 안 붙음
    assert all(v is None or v["date"] != "2026-08-30" for v in out.values())
    assert pl.link_permits_to_trades([], {}, TODAY) == {}


# ----------------------------------------------------------------------------- 점수

def test_estimate_score_rules_and_range():
    g = {"date": "2026-10-20"}
    zone = {"level": 2}
    assert pl.estimate_score(g, {"zone": zone}) == 0.5
    assert pl.estimate_score(g, {"zone": {"level": 0}}) == 0.2
    assert pl.estimate_score(g, {}) == 0.2
    assert pl.estimate_score(g, {"zone": zone, "active": 5, "active30ago": 8}) == 0.65
    assert pl.estimate_score(g, {"zone": zone, "active": 8, "active30ago": 5}) == 0.5
    assert pl.estimate_score(g, {"zone": zone, "open": 40, "openAvg30": 30.5}) == 0.6
    assert pl.estimate_score(g, {"zone": zone, "listing": {"sameCnt": "3"}}) == 0.4
    assert pl.estimate_score(g, {"zone": zone, "sameCnt": 3}) == 0.4
    assert pl.estimate_score(g, {"zone": zone, "confirmYmd": "2026-09-01"}) == 0.35      # 49일 전 확인 → −0.15
    assert pl.estimate_score(g, {"zone": zone, "confirmYmd": "2026-10-01"}) == 0.5       # 19일 → 감점 없음
    assert pl.estimate_score(g, {"zone": zone, "active": 1, "active30ago": 3, "open": 9, "openAvg30": 3}) == 0.75
    # 비규제 + 감점만 → 0 으로 클램프
    assert pl.estimate_score(g, {"zone": 0, "sameCnt": 2, "confirmYmd": "2026-01-01"}) == 0.0
    rng = random.Random(1)
    for _ in range(200):
        ctx = {"zone": {"level": rng.choice([0, 1, 2])}, "active": rng.randint(0, 9), "active30ago": rng.randint(0, 9),
               "open": rng.randint(0, 50), "openAvg30": rng.uniform(0, 50), "sameCnt": rng.randint(1, 4),
               "confirmYmd": (TODAY - dt.timedelta(days=rng.randint(0, 90))).isoformat()}
        s = pl.estimate_score({"date": TODAY.isoformat()}, ctx)
        assert 0.0 <= s <= 1.0


# ----------------------------------------------------------------------------- 적중률

def test_accuracy_stats():
    links = [{"status": "confirmed"}] * 3 + [{"status": "unconfirmed"}] * 2 + [{"status": "relisted"}] + [{"status": "pending"}] * 4
    pls = {"a": {"date": "2026-10-10", "days": 5}, "b": {"date": "2026-10-11", "days": 20}, "c": None, "d": {"days": 9}}
    acc = pl.accuracy_stats(links, pls)
    assert acc["gone"] == {"n": 10, "confirmed": 3, "relisted": 1, "unconfirmed": 2, "pending": 4, "hitRate": 0.5}
    assert acc["permits"] == {"n": 4, "matched": 3, "matchRate": 0.75, "medianDaysToTrade": 9}
    empty = pl.accuracy_stats([], {})
    assert empty["gone"]["hitRate"] is None and empty["gone"]["n"] == 0
    assert empty["permits"]["matchRate"] is None and empty["permits"]["medianDaysToTrade"] is None
    assert pl.accuracy_stats([{"status": "pending"}], {}, permits_total=10)["permits"]["n"] == 10


# ----------------------------------------------------------------------------- 지정·단지 요약

def test_zone_of_levels():
    items = dz.with_status(dz.load(), TODAY)
    z = pl.zone_of("11680", "개포동", items, TODAY)
    assert z["level"] == 2 and z["until"] == "2026-12-31" and z["daysLeft"] == 65 and z["title"] and z["id"]
    assert pl.zone_of("41590", "반송동", items, TODAY)["until"] == "2027-12-31"       # 동탄 법정동 → 동탄구
    assert pl.zone_of("41590", "병점동", items, TODAY)["level"] == 0
    assert pl.zone_of("28185", "송도동", items, TODAY)["level"] == 0                     # 인천: 규제 없음
    # with_status 를 안 거친 원본 items 도 받는다
    assert pl.zone_of("11680", None, dz.load()["items"], TODAY)["level"] == 2
    later = dz.with_status(dz.load(), dt.date(2027, 1, 15))
    assert pl.zone_of("11680", "대치동", later, dt.date(2027, 1, 15))["level"] == 1    # 재건축 단지 부분 지정만
    assert pl.ZONE_LEVEL == {"full": 2, "partial": 1, "none": 0}


def test_complex_summary_block():
    items = dz.with_status(dz.load(), TODAY)
    zone = pl.zone_of("11680", "개포동", items, TODAY)
    c = {"cid": CID, "name": "래미안블레스티지", "sgg": "11680", "umd": "개포동", "jibun": "12"}
    events = [ev("new", "A", "2026-09-01"), ev("new", "B", "2026-09-01", grp="59", area="59.97"),
              ev("gone", "A", "2026-10-01", 100000), ev("gone", "B", "2026-10-20", 70000, grp="59", area="59.97"),
              ev("relist", "C", "2026-10-22", 90000)]
    permits = [_permit("p1", "2026-10-05"), _permit("p2", "2026-10-06"), _permit("p3", "2026-08-01", job="취하"),
               _permit("p4", "2026-06-01")]
    trades = [tr("2026-10-10", 95000), tr("2026-10-24", 88000, area=59.97, floor=3), tr("2026-11-02", 99000)]
    listings = [{"atclNo": "X", "cid": CID, "grp": "84", "status": "active"}, {"atclNo": "Y", "cid": CID, "grp": "84", "status": "active"},
                {"atclNo": "Z", "cid": CID, "grp": "59", "status": "gone"}]
    from collector.toheo import cases as cs
    stats = cs.stats_for([{"sgg": "11680", "received": "2026-10-20", "deadline": "2026-11-09", "status": "처리중",
                           "done_date": "", "done_observed": "0", "no": "1", "dept": ""}], TODAY)
    blk = pl.complex_summary(CID, c, events, permits, trades, stats, zone, TODAY, listings=listings,
                             region_label="강남구", region_sgg="11680", estimate={"n": 5, "p25": 10, "p50": 12, "p75": 15, "p90": 21})
    assert set(blk) >= {"zone", "region", "permits", "byGrp", "timeline", "note"}
    assert blk["zone"]["level"] == 2 and blk["zone"]["until"] == "2026-12-31" and blk["zone"]["renewal"] == "연장 미확정"
    r = blk["region"]
    assert r["sgg"] == "11680" and r["label"] == "강남구" and r["open"] == 1 and r["received7"] == 1 and r["est"]["p50"] == 12
    p = blk["permits"]
    assert (p["n7"], p["n30"], p["n90"], p["total"]) == (0, 2, 2, 3)        # 취하 제외, 6/1 은 90일 밖이지만 누적엔 포함
    assert len(p["weekly"]) == 12 and sum(p["weekly"]) == 2 and p["byPurpose"] == {"주거용": 3}
    rec = {x["key"]: x for x in p["recent"]}
    assert rec["p2"]["trade"]["date"] == "2026-10-10" and rec["p2"]["trade"]["days"] == 4       # 가장 가까운 쌍 먼저
    assert rec["p1"]["trade"]["date"] == "2026-10-24" and rec["p3"]["trade"] is None and rec["p3"]["jobGbn"] == "취하"
    assert rec["p1"]["date"] == "2026-10-05" and rec["p1"]["jibun"] == "12"
    by = blk["byGrp"]
    assert set(by) == {"84", "59"}
    assert by["84"]["active"] == 2 and by["84"]["trades30"] == 1               # 11/2 미래 거래 제외
    g84 = by["84"]["gone30"]
    assert len(g84) == 1 and g84[0]["status"] == "confirmed" and g84[0]["trade"]["price"] == 95000 and 0 <= g84[0]["score"] <= 1
    assert by["84"]["confirmed90"] == 1
    g59 = by["59"]["gone30"][0]
    assert g59["status"] == "pending" and g59["trade"] is None                 # 88000/70000 = 1.26 → 가격 창 밖
    types = {t["type"] for t in blk["timeline"]}
    assert types >= {"permit", "gone", "relist", "trade", "zone"}
    assert all(set(t) == {"date", "type", "text", "grp"} for t in blk["timeline"])
    assert blk["timeline"] == sorted(blk["timeline"], key=lambda t: t["date"], reverse=True)
    # 데이터가 전혀 없어도 빈 구조
    empty = pl.complex_summary(CID, c, [], [], [], None, pl.zone_of("28185", None, items, TODAY), TODAY)
    assert empty["zone"]["level"] == 0 and empty["region"] is None and empty["byGrp"] == {} and empty["permits"]["total"] == 0


def test_active_counts_and_open_series():
    listings = [{"grp": "84", "status": "active"}, {"grp": "84", "status": "active"}, {"grp": "59", "status": "gone"}]
    events = [ev("new", "A", "2026-10-10"), ev("gone", "B", "2026-10-12"), ev("gone", "C", "2026-10-13"),
              ev("new", "D", "2026-08-01")]          # 30일 밖
    cnt = pl.active_counts(listings, events, TODAY)
    assert cnt["84"] == {"active": 2, "active30ago": 3}
    cases = [{"received": "2026-10-01", "status": "해결", "done_date": "2026-10-15"},
             {"received": "2026-10-20", "status": "처리중", "done_date": ""}]
    s = pl.open_series(cases, TODAY, 30)
    assert len(s) == 30 and s[-1] == 1 and s[0] == 0 and max(s) == 1
    assert pl.open_series([], TODAY) == [0] * 30
