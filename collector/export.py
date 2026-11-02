"""사이트용 JSON·RSS 생성 (site/data, site/feed).

급매판정 산출물(index/top/trend/status/c/<cid>/l/<sgg>, feed/*) 에 토허제 라이브 산출물을 더한다(DESIGN.md 3절 export).
- compact_listing.toheo, c/<cid>.json 의 toheo 블록
- site/data/toheo/{summary,feed,complexes,pipeline,daily,open_cases,recent_done}.json
- status.json 의 fetchLog.toheo · counts.permits · counts.goneEvents
- feed/toheo/all.xml, feed/toheo/<sgg>.xml, feed/c/<cid>.xml 에 허가 항목 혼합(guid permit-<key>)
토허 데이터(data/toheo/*)가 전혀 없어도 export 는 성공하고 빈 구조를 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

from . import pipeline as pl
from . import regions
from .engine import Engine, now_kst, today_str
from .judge import judge_listing, royal_dongs
from .toheo import cases as cs
from .toheo import designations as dz
from .toheo import regions as tr
from .toheo.store import ToheoStore

INDEX_FIELDS = ["cid", "name", "sgg", "umd", "buildYear", "households", "naverId", "hasModel", "nListings",
                "bestGrade", "lastYm", "fairs", "n"]
COVERAGE = {"seoul": "허가 완료 단지 단위", "gyeonggi": "지정·접수 추세만", "incheon": "규제지역 아님"}
EMPTY_TOHEO = {"zone": 0, "until": None, "open": None, "est": None}


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


_RE_UNSAFE = re.compile(r"[^0-9A-Za-z가-힣\-_.:]")


def safe_name(s) -> str | None:
    """외부 응답에서 온 값(cid·sgg)을 파일 이름 조각으로 쓸 수 있는지. 경로 구분자·'..'·제어문자가 섞이면 None.
    (`site/feed/toheo/<sgg>.xml` 처럼 그대로 경로가 되므로 site/ 밖에 쓰거나 export 가 예외로 죽는 일을 막는다)"""
    s = str(s or "")
    if not s or _RE_UNSAFE.search(s) or ".." in s or s.startswith("."):
        return None
    return s


def _check_out_dir(out: Path, data_root: Path) -> None:
    """out/data 와 수집 데이터 디렉터리(DATA_DIR)가 겹치면 rmtree 로 원본(data/toheo 등)을 지우게 되므로 거부."""
    out_data = (out / "data").resolve()
    root = Path(data_root).resolve()
    # 같은 폴더(--out . 에 DATA_DIR=data)거나 DATA_DIR 이 out/data 안에 있으면 rmtree 가 원본을 지운다
    if out_data == root or out_data in root.parents:
        raise ValueError(f"export 출력 {out_data} 이(가) 수집 데이터 디렉터리 {root} 와 겹칩니다 — --out 을 다른 폴더로 지정하세요")


def compact_listing(l: dict, c: dict, j: dict | None, naver_url: str, toheo: dict | None = None) -> dict:
    """사이트 매물 카드 1건. toheo = {"zone": 0|1|2, "until": str|None, "open": int|None, "est": {p50,p90}|None}."""
    return {
        "id": l["atclNo"], "cid": l["cid"], "name": c.get("name", ""), "sgg": c.get("sgg", ""), "umd": c.get("umd", ""),
        "dongRaw": l.get("dongRaw", ""), "dong": l.get("dong", ""), "floor": _int(l.get("floor")), "top": _int(l.get("top")),
        "area": _float(l.get("area")), "supply": _float(l.get("supply")), "grp": _int(l.get("grp")), "price": _int(l.get("price")),
        "confirm": l.get("confirmYmd", ""), "direct": _int(l.get("direct")) or 0, "sameCnt": _int(l.get("sameCnt")) or 1,
        "realtor": l.get("realtor", ""), "dir": l.get("direction", ""),
        # desc(중개사 자유 입력 설명)는 연락처 등이 섞일 수 있고 사이트가 쓰지 않으므로 내보내지 않는다
        "tags": [t for t in (l.get("tags") or "").split("|") if t], "first": l.get("firstSeen", ""), "last": l.get("lastSeen", ""),
        "url": naver_url, "toheo": dict(toheo or EMPTY_TOHEO),
        **({"fair": j["fair"], "fairBase": j["fairBase"], "diff": j["diffPct"], "diffAmt": j["diffAmt"], "grade": j["grade"],
            "conf": j["conf"], "z": j["z"], "reasons": j["reasons"], "floorLabel": j["floorLabel"], "n": j["n"], "n12": j["n12"],
            "dongB": j["dongB"], "floorB": j["floorB"], "typeB": j["typeB"],
            "royal": bool(j["royal"] and j["royal"].get("royal") == l.get("dong"))} if j else {"grade": "", "conf": ""}),
    }


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


GRADE_ORDER = {"SS": 0, "S": 1, "A": 2, "B": 3, "C": 4, "": 9}


# ----------------------------------------------------------------------------- 토허 데이터 적재(없어도 됨)

class ToheoData:
    """export 가 쓰는 토허 데이터 묶음. 파일이 없으면 전부 빈 값."""

    def __init__(self, engine: Engine, today: str):
        self.today = today
        root = Path(engine.store.root) / "toheo"
        self.tstore = ToheoStore(root) if root.exists() else None
        self.cases: dict[tuple[str, str], dict] = self.tstore.load_cases() if self.tstore else {}
        self.permits: dict[str, dict] = self.tstore.load_permits() if self.tstore else {}
        try:
            self.dz_data = dz.load()
        except OSError:
            self.dz_data = {"items": [], "references": []}
        # JSON 문법 오류·필수 필드 누락(ValueError)은 그대로 올린다: 사람이 고친 config 의 실수를 조용히 삼키면 사이트에서
        # 지정 현황이 통째로 사라지거나 토허 탭이 멈춘다.
        self.dz_items = dz.with_status(self.dz_data, today)
        self.events: list[dict] = engine.store.read_events()
        self.tlog: dict = engine.fetch_log.get("toheo") or {}
        self.region_stats: dict[str, dict] = cs.region_stats(self.cases, today) if self.cases else {}
        self.durations = cs.durations_by_sgg(self.region_stats) if self.region_stats else {"all": []}
        self.estimates: dict[str, dict | None] = {s: cs.estimate(self.durations, s) for s in self.region_stats}
        # 시 단위(성남·수원 …) 합산 추정: 구 코드로 추정이 없으면 시 전체로
        city_dur: dict[str, list[int]] = defaultdict(list)
        for s, d in self.durations.items():
            if s == "all":
                continue
            site = tr.SITE_BY_SGG.get(s)
            if site and len(site.sggs) > 1:
                city_dur[site.id].extend(d)
        for city, d in city_dur.items():
            if self.estimates.get(city) is None:
                self.estimates[city] = cs.estimate({city: d}, city)
        self.estimates["all"] = cs.estimate(self.durations, "all")
        self._zone: dict[str, dict] = {}
        self._open_avg: dict[str, float | None] = {}
        self.cases_by_sgg: dict[str, list[dict]] = defaultdict(list)
        for c in self.cases.values():
            self.cases_by_sgg[c.get("sgg") or ""].append(c)

    @property
    def has_any(self) -> bool:
        """토허 수집 산출물(민원·허가·수집 로그)이 하나라도 있는지. 매물 이벤트는 급매판정 쪽이라 세지 않는다."""
        return bool(self.cases or self.permits or self.tlog)

    def tsgg(self, sgg: str, umd: str | None) -> str:
        return tr.toheo_sgg_for(sgg, umd)

    def zone(self, sgg: str, umd: str | None = None) -> dict:
        t = self.tsgg(sgg, umd)
        if t not in self._zone:
            self._zone[t] = pl.zone_of(t, None, self.dz_items, self.today)
        return self._zone[t]

    def stats(self, tsgg: str) -> dict | None:
        return self.region_stats.get(tsgg)

    def estimate(self, tsgg: str) -> dict | None:
        e = self.estimates.get(tsgg)
        if e is None and ":" in tsgg:
            e = self.estimates.get(tr.base_code(tsgg))
        if e is None:
            site = tr.SITE_BY_SGG.get(tsgg)
            if site and len(site.sggs) > 1:
                e = self.estimates.get(site.id)
        return e

    def open_avg30(self, tsgg: str) -> float | None:
        if tsgg not in self._open_avg:
            rows = self.cases_by_sgg.get(tsgg)
            if not rows:
                self._open_avg[tsgg] = None
            else:
                s = pl.open_series(rows, self.today, 30)
                self._open_avg[tsgg] = round(sum(s) / len(s), 2) if s else None
        return self._open_avg[tsgg]

    def listing_toheo(self, sgg: str, umd: str | None) -> dict:
        """compact_listing.toheo."""
        z = self.zone(sgg, umd)
        t = self.tsgg(sgg, umd)
        st = self.stats(t)
        est = self.estimate(t)
        return {"zone": z["level"], "until": z["until"], "open": st["open"] if st else None,
                "est": {"p50": est["p50"], "p90": est["p90"]} if est else None}


# ----------------------------------------------------------------------------- export

def export_site(engine: Engine, out_dir: str | os.PathLike, site_url: str | None = None) -> dict:
    out = Path(out_dir)
    data = out / "data"
    feed = out / "feed"
    _check_out_dir(out, engine.store.root)
    for d in (data / "c", data / "l", data / "toheo", feed / "c", feed / "toheo"):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    # feed 루트의 <sgg>.xml 도 매번 새로 쓴다(지역이 빠지거나 active 매물이 0 이 된 시군구의 옛 RSS 가 남지 않게)
    for p in feed.glob("*.xml"):
        p.unlink()
    skipped_names: list[str] = []
    site_url = (site_url or os.environ.get("SITE_URL") or "").rstrip("/")
    store = engine.store
    gen = now_kst().isoformat(timespec="seconds")
    today = today_str()
    today_d = dt.date.fromisoformat(today)
    now_ym = engine.progress.get("date", today).replace("-", "")[:6]

    # ---- 모델 로드
    models: dict[str, dict] = {}
    for grp in store.model_groups():
        m = store.read_model(grp)
        if m:
            models[grp] = m
    cx_group = {cid: engine.group_of(c["sgg"]) for cid, c in engine.complexes.items()}
    naver_cfg = engine.naver.cfg if engine.naver else {}
    art_url = naver_cfg.get("article_url", "https://m.land.naver.com/article/info/{atclNo}")
    cx_url = naver_cfg.get("complex_url", "https://m.land.naver.com/complex/info/{complexNo}")
    trades_by_cid: dict[str, list] = {}
    for m in models.values():
        for cid, cm in m.get("complexes", {}).items():
            if cm.get("trades"):
                trades_by_cid[cid] = cm["trades"]

    # ---- 토허 데이터(없으면 빈 값) + 파이프라인 연결
    th = ToheoData(engine, today)
    events_by_cid: dict[str, list[dict]] = defaultdict(list)
    for e in th.events:
        events_by_cid[e.get("cid") or ""].append(e)
    permits_by_cid: dict[str, list[dict]] = defaultdict(list)
    for p in th.permits.values():
        if p.get("cid"):
            permits_by_cid[p["cid"]].append(p)
    links = pl.link_gone_to_trades(th.events, trades_by_cid, today_d)
    links_by_cid: dict[str, list[dict]] = defaultdict(list)
    for l in links:
        links_by_cid[l["cid"]].append(l)
    permit_links = pl.link_permits_to_trades(th.permits, trades_by_cid, today_d)
    n_permits_ok = sum(1 for p in th.permits.values() if (p.get("job_gbn") or "허가") == "허가" and p.get("cid"))
    accuracy = pl.accuracy_stats(links, permit_links, permits_total=n_permits_ok)

    # ---- 매물 판정 (사이트에는 active 만, gone 행은 점수 계산용으로만)
    all_rows_raw = store.read_listings()
    rows_by_cid: dict[str, dict[str, dict]] = defaultdict(dict)
    listings_by_cid: dict[str, list[dict]] = defaultdict(list)
    listings_raw: dict[str, dict] = {}
    for k, l in all_rows_raw.items():
        rows_by_cid[l.get("cid") or ""][k] = l
        if (l.get("status") or "active") == "active":
            listings_raw[k] = l
            listings_by_cid[l.get("cid") or ""].append(l)
    judged: dict[str, list[dict]] = defaultdict(list)   # cid → listings
    all_listings: list[dict] = []
    for l in listings_raw.values():
        c = engine.complexes.get(l.get("cid"))
        if not c:
            continue
        m = models.get(cx_group.get(l["cid"], ""))
        l2 = dict(l)
        for k in ("floor", "top", "grp", "price", "direct", "sameCnt"):
            l2[k] = _int(l.get(k))
        l2["area"] = _float(l.get("area"))
        j = judge_listing(m, l["cid"], l2) if m else None
        cl = compact_listing(l2, c, j, art_url.format(atclNo=l["atclNo"]), th.listing_toheo(c.get("sgg", ""), c.get("umd")))
        judged[l["cid"]].append(cl)
        all_listings.append(cl)

    def lkey(x):
        return (0 if x.get("fair") else 1, x.get("diff", 9), -(x.get("n") or 0))

    all_listings.sort(key=lkey)

    # ---- 내림 추정 점수(단지·평형 컨텍스트)
    scores: dict[str, float] = {}
    link_rows: list[dict] = []      # pipeline.json 용(단지 정보 포함)
    for cid, ls_ in links_by_cid.items():
        c = engine.complexes.get(cid)
        if not c:
            continue
        zone = th.zone(c.get("sgg", ""), c.get("umd"))
        tsgg = th.tsgg(c.get("sgg", ""), c.get("umd"))
        st = th.stats(tsgg)
        counts = pl.active_counts(listings_by_cid.get(cid, []), events_by_cid.get(cid, []), today_d)
        all_rows = rows_by_cid.get(cid, {})
        for l in ls_:
            cnt = counts.get(str(l["grp"]), {"active": 0, "active30ago": 0})
            row = all_rows.get(l["id"], {})
            sc = pl.estimate_score({"date": l["date"]}, {"zone": zone, "active": cnt["active"], "active30ago": cnt["active30ago"],
                                                         "open": st["open"] if st else None, "openAvg30": th.open_avg30(tsgg),
                                                         "listing": row})
            scores[f"{l['id']}|{l['date']}"] = sc
            link_rows.append({**l, "name": c.get("name", ""), "sgg": c.get("sgg", ""), "umd": c.get("umd", ""), "score": sc,
                              "zoneLevel": zone["level"]})

    # ---- 단지 페이지
    index_rows = []
    by_sgg_listings: dict[str, list[dict]] = defaultdict(list)
    complex_toheo: dict[str, dict] = {}
    for cid, c in engine.complexes.items():
        if c.get("sgg") not in engine.codes:
            continue
        if safe_name(cid) is None:
            skipped_names.append(cid)
            continue
        grp = cx_group.get(cid, "")
        m = models.get(grp)
        cm = (m or {}).get("complexes", {}).get(cid)
        ls = sorted(judged.get(cid, []), key=lkey)
        for l in ls:
            by_sgg_listings[c["sgg"]].append(l)
        fairs = {g: v["fair"] for g, v in (cm or {}).get("groups", {}).items()}
        best = min((l.get("grade") or "" for l in ls if l.get("fair")), key=lambda g: GRADE_ORDER.get(g, 9), default="")
        n_tr = _int(c.get("nTrades")) or (cm or {}).get("n") or 0
        index_rows.append([cid, c.get("name", ""), c.get("sgg", ""), c.get("umd", ""), c.get("buildYear", ""),
                           _int(c.get("households")), c.get("naverId", ""), bool(cm), len(ls), best, c.get("lastYm", ""),
                           fairs, n_tr])
        has_toheo = bool(permits_by_cid.get(cid) or events_by_cid.get(cid))
        if not cm and not ls and not has_toheo:
            continue
        zone = th.zone(c.get("sgg", ""), c.get("umd"))
        tsgg = th.tsgg(c.get("sgg", ""), c.get("umd"))
        tblock = pl.complex_summary(cid, c, events_by_cid.get(cid, []), permits_by_cid.get(cid, []),
                                    trades_by_cid.get(cid, []), th.stats(tsgg), zone, today_d,
                                    listings=listings_by_cid.get(cid, []), links=links_by_cid.get(cid, []),
                                    permit_links=permit_links, region_label=tr.label(tsgg, engine.live),
                                    estimate=th.estimate(tsgg), region_sgg=tsgg if (th.stats(tsgg) or tsgg in tr.ALL_SGGS) else "",
                                    scores=scores, open_avg30=th.open_avg30(tsgg))
        complex_toheo[cid] = tblock
        page = {
            "cid": cid, "name": c.get("name", ""), "sgg": c.get("sgg", ""), "sggName": regions.short_name(c.get("sgg", ""), engine.live),
            "umd": c.get("umd", ""), "jibun": c.get("jibun", ""), "buildYear": c.get("buildYear", ""),
            "households": _int(c.get("households")), "highFloor": _int(c.get("highFloor")), "naverId": c.get("naverId", ""),
            "naverUrl": cx_url.format(complexNo=c["naverId"]) if c.get("naverId") else "", "lat": _float(c.get("lat")),
            "lng": _float(c.get("lng")), "matchScore": _float(c.get("matchScore")), "listingsAt": c.get("listingsAt", ""),
            "generatedAt": gen, "listings": ls, "nTrades": n_tr, "toheo": tblock,
        }
        if cm:
            months = m["months"]
            page["model"] = {k: cm[k] for k in ("n", "first", "lastYm", "dong", "floor", "floorN", "top", "Dnow", "groups")}
            page["model"]["royal"] = royal_dongs(cm)
            page["model"]["floorPrior"] = m.get("floorPrior", {})
            page["months36"] = months[-36:]
            page["T36"] = cm.get("T36", [])
            page["S36"] = m["S"][-36:]
            page["trades"] = cm.get("trades", [])
            page["now"] = m["now"]
        _dump(data / "c" / f"{cid}.json", page)

    _dump(data / "index.json", {"generatedAt": gen, "fields": INDEX_FIELDS, "complexes": index_rows})

    # ---- 지역별 매물·전체 TOP
    for sgg, ls in by_sgg_listings.items():
        ls.sort(key=lkey)
        if safe_name(sgg) is None:
            skipped_names.append(sgg)
            continue
        _dump(data / "l" / f"{sgg}.json", {"sgg": sgg, "generatedAt": gen, "listings": ls})
    top = [l for l in all_listings if l.get("fair") and l.get("conf") in ("high", "mid") and l.get("grade") in ("SS", "S", "A")][:400]
    _dump(data / "top.json", {"generatedAt": gen, "listings": top, "total": len(all_listings),
                               "judged": sum(1 for l in all_listings if l.get("fair"))})

    # ---- 트렌드: 시군구 지수(36개월, 시작=100) + 매물 등급 분포 추이
    months36 = []
    trend_regions = {}
    for grp, m in models.items():
        ms = m["months"][-36:]
        if len(ms) > len(months36):
            months36 = ms
        S = m["S"][-36:]
        if not S:
            continue
        base = S[0]
        trend_regions[grp] = {"name": regions.short_name(grp, engine.live), "codes": m.get("codes", []),
                              "idx": [round(100 * math.exp(v - base), 1) for v in S], "months": ms}
    today_rows = _listing_stats(all_listings, engine, today)
    stats_rows = store.upsert_listing_stats(today_rows, today) if all_listings else store.read_listing_stats()
    by_date_sgg: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in stats_rows:
        by_date_sgg[r["date"]][r["sgg"]] = r
    dates = sorted(by_date_sgg)[-90:]
    stat_out = {"dates": dates, "bySgg": {}, "total": {k: [] for k in ("n", "SS", "S", "A", "B", "C")}}
    sggs = sorted({s for d in dates for s in by_date_sgg[d]})
    for s in sggs:
        stat_out["bySgg"][s] = {k: [] for k in ("n", "SS", "S", "A", "B", "C", "medDiff")}
    for d in dates:
        tot = defaultdict(int)
        for s in sggs:
            r = by_date_sgg[d].get(s)
            for k in ("n", "SS", "S", "A", "B", "C"):
                v = int(r[k]) if r else 0
                stat_out["bySgg"][s][k].append(v)
                tot[k] += v
            stat_out["bySgg"][s]["medDiff"].append(float(r["medDiff"]) if r and r.get("medDiff") not in ("", None) else None)
        for k in ("n", "SS", "S", "A", "B", "C"):
            stat_out["total"][k].append(tot[k])
    _dump(data / "trend.json", {"generatedAt": gen, "months": months36, "regions": trend_regions, "stats": stat_out})

    # ---- 토허 산출물
    th_out = _export_toheo(engine, th, data, gen, today_d, link_rows, permit_links, accuracy, complex_toheo)

    # ---- 상태
    bf = engine.progress.get("backfill", {})
    prog = {}
    for sido in regions.SIDO_ORDER:
        codes = [c for c in engine.codes if c[:2] == sido]
        if not codes:
            continue
        done = sum(1 for c in codes if bf.get(c, {}).get("done"))
        prog[sido] = {"name": regions.SIDO_NAME[sido], "codes": len(codes), "done": done,
                      "doneAt": engine.progress.get("regionsDone", {}).get(sido, "")}
    codes_out = [{"code": r["code"], "name": r["name"], "short": regions.short_name(r["code"], engine.live), "sido": r["sido"],
                  "group": r["group"], "live": r["code"] in engine.live, "note": r.get("note", "")}
                 for r in regions.all_codes(engine.live) if r["code"] in engine.codes]
    status = {
        "generatedAt": gen, "progress": prog, "callsToday": engine.progress.get("callsToday", 0),
        "fetchLog": engine.fetch_log, "codes": codes_out,
        "chips": [{"name": n, "codes": cs_} for n, cs_ in regions.AREA_CHIPS],
        "counts": {"complexes": len(index_rows), "matched": sum(1 for r in index_rows if r[6]),
                   "withModel": sum(1 for r in index_rows if r[7]), "listings": len(all_listings),
                   "judged": sum(1 for l in all_listings if l.get("fair")), "models": len(models),
                   "permits": len(th.permits), "permitsMatched": sum(1 for p in th.permits.values() if p.get("cid")),
                   "goneEvents": sum(1 for e in th.events if e.get("event") == "gone"), "events": len(th.events),
                   "cases": len(th.cases)},
        "modelNow": next(iter(models.values()))["now"] if models else now_ym,
        "toheo": {"hasData": th.has_any, "regions": len(th_out["regions"]), "accuracy": accuracy},
    }
    _dump(data / "status.json", status)

    # ---- RSS
    def item(l: dict) -> str:
        link = f"{site_url}/#c-{l['cid']}" if site_url else l["url"]
        title = f"[{l.get('grade') or '-'}] {l['name']} {l.get('dongRaw') or ''} {l.get('grp') or ''}㎡ {fmt_price(l['price'])}"
        if l.get("fair"):
            title += f" ({l['diff'] * 100:+.1f}%)"
        desc = " · ".join(filter(None, [f"기준가 {fmt_price(l['fair'])}" if l.get("fair") else "", *(l.get("reasons") or []),
                                         f"확인 {l.get('confirm')}" if l.get("confirm") else ""]))
        return (f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
                f"<guid isPermaLink=\"false\">{escape(l['id'])}</guid><description>{escape(desc)}</description></item>")

    def permit_item(p: dict) -> str:
        """허가 소식 항목(단지별·날짜별 묶음). guid permit-<key>."""
        link = f"{site_url}/#c-{p['cid']}" if (site_url and p.get("cid")) else (f"{site_url}/#toheo" if site_url else "#toheo")
        pp = ", ".join(f"{k} {v}" for k, v in sorted((p.get("purposes") or {}).items(), key=lambda kv: -kv[1]))
        title = f"{p['date'].replace('-', '.')} {p.get('name') or '단지 미확인'} 허가 {p['n']}건"
        desc = " · ".join(filter(None, [tr.label(p.get("sgg", ""), engine.live), p.get("umd", ""), pp]))
        return (f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
                f"<guid isPermaLink=\"false\">permit-{escape((p.get('keys') or [p['key']])[0])}</guid><pubDate>{_pub(p['date'])}</pubDate>"
                f"<description>{escape(desc)}</description></item>")

    def rss(title: str, body: str) -> str:
        return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(title)}</title>'
                f"<link>{escape(site_url or '')}</link><description>{escape(title)}</description>{body}</channel></rss>")

    cutoff = (today_d - dt.timedelta(days=7)).isoformat()
    fresh_good = [l for l in all_listings if l.get("fair") and l.get("grade") in ("SS", "S") and (l.get("first") or "") >= cutoff
                  and l.get("conf") != "low"]
    (feed / "all.xml").write_text(rss("급매판정 — 새 급매 (SS·S, 7일)", "".join(item(l) for l in fresh_good[:100])), encoding="utf-8")
    for sgg in sorted(set(by_sgg_listings) | set(engine.codes)):
        if safe_name(sgg) is None:
            continue
        ls = by_sgg_listings.get(sgg, [])
        good = [l for l in ls if l.get("fair") and l.get("grade") in ("SS", "S", "A") and (l.get("first") or "") >= cutoff][:60]
        (feed / f"{sgg}.xml").write_text(rss(f"급매판정 — {regions.short_name(sgg, engine.live)}", "".join(item(l) for l in good)),
                                         encoding="utf-8")
    feed_entries = th_out["feed_entries"]          # 날짜 내림차순 [{key,date,cid,name,sgg,umd,n,purposes}]
    by_cid_entries: dict[str, list[dict]] = defaultdict(list)
    by_sgg_entries: dict[str, list[dict]] = defaultdict(list)
    for p in feed_entries:
        if p.get("cid"):
            by_cid_entries[p["cid"]].append(p)
        by_sgg_entries[p.get("sgg", "")].append(p)
    for cid in set(judged) | set(by_cid_entries):
        ls = judged.get(cid, [])
        if not ls and not by_cid_entries.get(cid):
            continue
        if safe_name(cid) is None:
            continue
        name = engine.complexes.get(cid, {}).get("name", cid)
        body = "".join(item(l) for l in sorted(ls, key=lkey)[:40]) + "".join(permit_item(p) for p in by_cid_entries.get(cid, [])[:40])
        (feed / "c" / f"{cid}.xml").write_text(rss(f"APT 레이더 — {name} (급매·허가)", body), encoding="utf-8")
    (feed / "toheo" / "all.xml").write_text(rss("APT 레이더 — 토지거래허가 소식 (서울 단지별)",
                                                "".join(permit_item(p) for p in feed_entries[:300])), encoding="utf-8")
    for sgg, es in by_sgg_entries.items():
        if not sgg or safe_name(sgg) is None:
            if sgg:
                skipped_names.append(sgg)
            continue
        (feed / "toheo" / f"{sgg}.xml").write_text(rss(f"APT 레이더 — {tr.label(sgg, engine.live)} 허가 소식",
                                                      "".join(permit_item(p) for p in es[:200])), encoding="utf-8")

    res = {"complexes": len(index_rows), "listings": len(all_listings), "models": len(models),
           "permits": len(th.permits), "cases": len(th.cases), "goneLinks": len(links),
           "toheoRegions": len(th_out["regions"])}
    if skipped_names:
        res["skippedUnsafeNames"] = sorted(set(skipped_names))[:20]
    return res


def _pub(date_iso: str) -> str:
    try:
        d = dt.datetime.fromisoformat(date_iso + "T09:00:00+09:00")
    except ValueError:
        return ""
    return d.strftime("%a, %d %b %Y %H:%M:%S +0900")


def _export_toheo(engine: Engine, th: ToheoData, data: Path, gen: str, today_d: dt.date, link_rows: list[dict],
                  permit_links: dict, accuracy: dict, complex_toheo: dict[str, dict]) -> dict:
    """site/data/toheo/*.json. 반환 {regions, feed_entries}."""
    today = today_d.isoformat()
    d7, d30, d45, d60, d90 = [(today_d - dt.timedelta(days=k)).isoformat() for k in (7, 30, 45, 60, 90)]
    tdir = data / "toheo"
    live = engine.live

    # ---- 허가 → 단지별·날짜별 집계 ('허가' 만)
    permits_ok = [p for p in th.permits.values() if (p.get("job_gbn") or "허가") == "허가" and p.get("hndl_ymd")]
    per_cx: dict[str, dict] = {}
    by_date: dict[str, dict[str, dict]] = defaultdict(dict)
    by_sgg30: dict[str, int] = defaultdict(int)
    by_gu30: dict[str, int] = defaultdict(int)
    for p in permits_ok:
        cid = p.get("cid") or ""
        c = engine.complexes.get(cid, {}) if cid else {}
        key = cid or f"?|{p.get('sgg', '')}|{p.get('dong', '')}|{p.get('jibun', '')}"
        x = per_cx.get(key)
        if x is None:
            x = per_cx[key] = {"cid": cid, "name": c.get("name") or p.get("cname") or (f"{p.get('dong', '')} {p.get('jibun', '')}".strip()),
                               "sgg": c.get("sgg") or p.get("sgg", ""), "umd": c.get("umd") or p.get("dong", ""),
                               "matched": bool(cid), "dates": [], "purposes": defaultdict(int)}
        x["dates"].append(p["hndl_ymd"])
        x["purposes"][p.get("use_purp") or "미상"] += 1
        d = p["hndl_ymd"]
        if d >= d30:
            by_sgg30[x["sgg"]] += 1
            by_gu30[p.get("gu") or tr.label(p.get("sgg", ""), live)] += 1
        if d >= d45:
            e = by_date[d].get(key)
            if e is None:
                e = by_date[d][key] = {"key": key, "cid": cid, "name": x["name"], "sgg": x["sgg"], "umd": x["umd"], "n": 0,
                                       "purposes": defaultdict(int), "matched": x["matched"], "keys": []}
            e["n"] += 1
            e["keys"].append(p.get("key", ""))
            e["purposes"][p.get("use_purp") or "미상"] += 1

    gone_by_cid: dict[str, list[dict]] = defaultdict(list)
    for l in link_rows:
        gone_by_cid[l["cid"]].append(l)
    cx_items = []
    for key, x in per_cx.items():
        pc = pl.permit_counts([{"job_gbn": "허가", "hndl_ymd": d, "use_purp": ""} for d in x["dates"]], today_d)
        c = engine.complexes.get(x["cid"], {}) if x["cid"] else {}
        zone = th.zone(x["sgg"], c.get("umd") or x["umd"]) if x["sgg"] else {"level": 0}
        gl = gone_by_cid.get(x["cid"], []) if x["cid"] else []
        cx_items.append({"cid": x["cid"], "name": x["name"], "sgg": x["sgg"], "umd": x["umd"], "matched": x["matched"],
                         "n7": pc["n7"], "n30": pc["n30"], "n90": pc["n90"], "total": pc["total"], "first": pc["first"],
                         "last": pc["last"], "weekly": pc["weekly"], "purposes": dict(x["purposes"]),
                         "goneEst30": sum(1 for l in gl if l["date"] >= d30),
                         "confirmed90": sum(1 for l in gl if l["status"] == "confirmed" and l["date"] >= d90),
                         "zoneLevel": zone["level"]})
    cx_items.sort(key=lambda x: (-x["n30"], -x["n90"], -x["total"], x["name"]))
    top_by_sgg: dict[str, list[dict]] = defaultdict(list)
    for x in cx_items:
        if x["n30"] > 0 and len(top_by_sgg[x["sgg"]]) < 10:
            top_by_sgg[x["sgg"]].append({"cid": x["cid"], "name": x["name"], "n30": x["n30"]})

    feed_days = []
    feed_entries = []
    for d in sorted(by_date, reverse=True):
        items = sorted(by_date[d].values(), key=lambda x: (-x["n"], x["name"]))
        for it in items:
            it["purposes"] = dict(it["purposes"])
            feed_entries.append({**it, "date": d})
        feed_days.append({"date": d, "n": sum(x["n"] for x in items), "complexes": len(items), "items": items})

    # ---- 지역 목록: 토허 대상(조회 지역 안) + 민원이 있는 지역(시 코드 포함)
    codes = set(engine.codes)
    region_ids = [s for s in tr.ALL_SGGS if tr.base_code(s) in codes]
    for s in th.region_stats:
        if s and s not in region_ids:
            region_ids.append(s)
    regions_out = []
    daily, open_cases, recent_done = {}, {}, {}
    totals = {"open": 0, "received7": 0, "received30": 0, "receivedPrev30": 0, "completed30": 0,
              "permits7": sum(1 for p in permits_ok if p["hndl_ymd"] >= d7),
              "permits30": sum(1 for p in permits_ok if p["hndl_ymd"] >= d30),
              "goneEst30": sum(1 for l in link_rows if l["date"] >= d30),
              "confirmed30": sum(1 for l in link_rows if l["status"] == "confirmed" and l["date"] >= d30)}
    sites_log = th.tlog.get("sites", {}) if th.tlog else {}
    for sgg in region_ids:
        st = th.stats(sgg) or cs.stats_for([], today_d)
        site = tr.SITE_BY_SGG.get(sgg) or tr.SITE_BY_ID.get(sgg)
        flog = sites_log.get(site.id, {}) if site else {}
        zone = th.zone(sgg)
        row = {"sgg": sgg, "label": tr.label(sgg, live), "sido": tr.sido_of(sgg), "site": site.id if site else "",
               "zone": {"level": zone["level"], "until": zone["until"], "daysLeft": zone["daysLeft"], "title": zone["title"],
                        "renewal": zone["renewal"]},
               "open": st["open"], "received7": st["received_7d"], "received30": st["received_30d"],
               "receivedPrev30": st["received_prev_30d"], "completed30": st["completed_30d"],
               "p25": st["p25_days"], "p50": st["median_days"], "p75": st["p75_days"], "p90": st["p90_days"],
               "durationN": st["duration_n"], "deadlineDays": st["deadline_days"], "statusCounts": st["status_counts"],
               "spark": st["spark"], "casesTotal": len(th.cases_by_sgg.get(sgg, [])),
               "fetch": {"ok": flog.get("ok"), "at": flog.get("at"), "pages": flog.get("pages"), "error": flog.get("error"),
                         "warning": flog.get("warning"), "host": flog.get("host"), "reachedCutoff": flog.get("reached_cutoff"),
                         "skipped": flog.get("skipped")},
               "permits30": by_sgg30.get(sgg, 0), "permitsTop": top_by_sgg.get(sgg, [])}
        regions_out.append(row)
        if th.stats(sgg):
            daily[sgg] = st["series"]
            open_cases[sgg] = st["open_rows"][:800]
            recent_done[sgg] = st["recent_done"]
        for k in ("open", "received7", "received30", "receivedPrev30", "completed30"):
            totals[k] += row[k]

    plog = th.tlog.get("permits", {}) if th.tlog else {}
    summary = {
        "generatedAt": gen, "today": today, "totals": totals, "regions": regions_out,
        "estimates": {k: v for k, v in th.estimates.items()},
        "designations": th.dz_items, "designationRefs": th.dz_data.get("references", []),
        "designationNote": th.dz_data.get("unlisted_note", ""),
        "permits": {"ok": plog.get("ok"), "at": plog.get("at"), "error": plog.get("error"), "rowsTotal": len(th.permits),
                    "rows30": totals["permits30"], "matched": sum(1 for p in th.permits.values() if p.get("cid")),
                    "byGu30": dict(by_gu30), "byGu": plog.get("by_gu", {})},
        "accuracy": accuracy, "runs": (th.tlog.get("runs") or [])[-20:] if th.tlog else [],
        "coverage": COVERAGE, "hasData": th.has_any,
    }
    _dump(tdir / "summary.json", summary)
    _dump(tdir / "feed.json", {"generatedAt": gen, "days": feed_days})
    _dump(tdir / "complexes.json", {"generatedAt": gen, "items": cx_items[:500]})
    recent = [l for l in link_rows if l["date"] >= d60]
    recent.sort(key=lambda l: (l["zoneLevel"], l["date"], l["score"] or 0), reverse=True)   # 토허구역 우선, 최근순
    confirmed = sorted((l for l in link_rows if l["status"] == "confirmed"),
                       key=lambda l: (l["trade"] or {}).get("date", ""), reverse=True)
    fields = ("id", "cid", "name", "sgg", "umd", "grp", "area", "dong", "floor", "price", "date", "score", "status", "trade",
              "zoneLevel", "daysToTrade")
    pick = lambda l: {k: l.get(k) for k in fields}  # noqa: E731
    _dump(tdir / "pipeline.json", {"generatedAt": gen, "today": today, "accuracy": accuracy,
                                   "recent": [pick(l) for l in recent[:200]], "confirmed": [pick(l) for l in confirmed[:100]],
                                   "rules": {"goneWindowDays": [-pl.GONE_BEFORE_DAYS, pl.GONE_AFTER_DAYS],
                                             "priceRatio": [pl.PRICE_LO, pl.PRICE_HI], "pendingDays": pl.GONE_PENDING_DAYS,
                                             "relistDays": pl.RELIST_DAYS, "permitWindowDays": pl.PERMIT_AFTER_DAYS}})
    _dump(tdir / "daily.json", daily)
    _dump(tdir / "open_cases.json", open_cases)
    _dump(tdir / "recent_done.json", recent_done)
    return {"regions": regions_out, "feed_entries": feed_entries}


def fmt_price(p) -> str:
    if p is None:
        return "-"
    p = int(p)
    eok, man = divmod(p, 10000)
    if eok and man:
        return f"{eok}억{man:,}"
    if eok:
        return f"{eok}억"
    return f"{man:,}만"


def _listing_stats(all_listings: list[dict], engine: Engine, today: str) -> list[dict]:
    by = defaultdict(list)
    for l in all_listings:
        if l.get("fair"):
            by[l["sgg"]].append(l)
    rows = []
    for sgg, ls in by.items():
        cnt = defaultdict(int)
        for l in ls:
            cnt[l["grade"]] += 1
        diffs = sorted(l["diff"] for l in ls)
        med = diffs[len(diffs) // 2] if diffs else ""
        rows.append({"date": today, "sgg": sgg, "n": len(ls), **{g: cnt[g] for g in ("SS", "S", "A", "B", "C")},
                     "medDiff": round(med, 4) if med != "" else ""})
    return rows
