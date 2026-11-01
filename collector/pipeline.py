"""거래 파이프라인 연결 (DESIGN.md 3절 `collector/pipeline.py`). export 시점에 계산하고 저장하지 않는다.

매물 내림(추정, 단지·평형·호가) → 구 접수·처리중(새올, 단지 모름) → 허가 완료(서울, 지번→단지) → 실거래 확인(평형·가격)

- `zone_of`               : 단지(sgg, 법정동) 가 아파트 토허구역인지 (level 2 전체 / 1 일부 / 0 없음)
- `link_gone_to_trades`   : gone 이벤트 ↔ 실거래 (confirmed / pending / unconfirmed / relisted)
- `link_permits_to_trades`: 허가 완료 ↔ 실거래 (같은 단지, 허가일 이후 75일 안, 가장 가까운 것 1:1)
- `estimate_score`        : 매물 내림이 '허가 접수' 일 가능성 점수 [0, 1] (추정 라벨 필수)
- `complex_summary`       : c/<cid>.json 의 toheo 블록
- `accuracy_stats`        : 적중률(공개용)

trades 는 모델 파일의 complexes[cid].trades (최근 36개월, [date, price, area, floor, dong, direct, cancelled]) 를 쓴다.
해제·직거래는 연결에서 제외한다. 모든 날짜는 'YYYY-MM-DD'.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from .deals import area_group
from .toheo import designations as dz
from .toheo import regions as tr

ZONE_LEVEL = {"full": 2, "partial": 1, "none": 0}

# 내림 ↔ 실거래 연결 규칙(날짜 창·가격 비율)
GONE_BEFORE_DAYS = 14       # 계약일 >= goneAt - 14d (계약 뒤 매물이 늦게 내려가는 경우)
GONE_AFTER_DAYS = 60        # 계약일 <= goneAt + 60d
GONE_PENDING_DAYS = 75      # goneAt + 75d 까지는 pending, 지나면 unconfirmed
RELIST_DAYS = 21            # goneAt 뒤 21일 안에 relist 가 있으면 relisted
PRICE_LO, PRICE_HI = 0.85, 1.03   # 실거래가 / 호가
PERMIT_AFTER_DAYS = 75      # 허가일 ~ +75d 안의 실거래

# 추정 점수 가감(DESIGN 3절)
SCORE_ZONE, SCORE_FREE = 0.5, 0.2
SCORE_ACTIVE_DOWN, SCORE_OPEN_UP, SCORE_SAME_CNT, SCORE_STALE_CONFIRM = 0.15, 0.1, -0.1, -0.15
STALE_CONFIRM_DAYS = 30


# ----------------------------------------------------------------------------- 공통 유틸

def _date(s) -> dt.date | None:
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    s = str(s or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _iso(d) -> str:
    d = _date(d)
    return d.isoformat() if d else ""


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


def trade_dict(t) -> dict | None:
    """모델 파일 trades 행([date, price, area, floor, dong, direct, cancelled] 또는 dict) → 통일된 dict."""
    if t is None:
        return None
    if isinstance(t, dict):
        area = _float(t.get("area"))
        return {"date": _iso(t.get("date")), "price": _int(t.get("price")), "area": area,
                "floor": _int(t.get("floor")), "dong": str(t.get("dong") or ""), "direct": bool(_int(t.get("direct"))),
                "cancelled": bool(_int(t.get("cancelled"))), "grp": area_group(area)}
    area = _float(t[2]) if len(t) > 2 else None
    return {"date": _iso(t[0]), "price": _int(t[1]) if len(t) > 1 else None, "area": area,
            "floor": _int(t[3]) if len(t) > 3 else None, "dong": str(t[4] or "") if len(t) > 4 else "",
            "direct": bool(_int(t[5])) if len(t) > 5 else False, "cancelled": bool(_int(t[6])) if len(t) > 6 else False,
            "grp": area_group(area)}


def usable_trades(trades, today=None) -> list[dict]:
    """해제·직거래·가격/날짜 없는 거래 제외(today 를 주면 미래 계약일도 제외), 날짜 오름차순."""
    today_s = _iso(today) if today else ""
    out = []
    for t in trades or []:
        d = trade_dict(t)
        if not d or not d["date"] or not d["price"] or d["cancelled"] or d["direct"]:
            continue
        if today_s and d["date"] > today_s:
            continue
        out.append(d)
    out.sort(key=lambda x: x["date"])
    return out


def _trade_ref(t: dict | None, days: int | None = None) -> dict | None:
    """사이트에 내보내는 실거래 참조 {date, price, area, floor, dong[, days]}."""
    if not t:
        return None
    ref = {"date": t["date"], "price": t["price"], "area": t["area"], "floor": t["floor"], "dong": t.get("dong", "")}
    if days is not None:
        ref["days"] = days
    return ref


# ----------------------------------------------------------------------------- 지정(토허구역)

def _with_status(items, today) -> list[dict]:
    """with_status 를 거치지 않은 원본 items 가 와도 동작하도록."""
    if not items:
        return []
    if isinstance(items, dict):
        items = items.get("items", [])
    if items and "status" in items[0] and "region_ids" in items[0]:
        return list(items)
    return dz.with_status({"items": list(items)}, today)


def zone_of(sgg: str, umd: str | None, designations_items, today) -> dict:
    """단지(sgg, 법정동) 의 아파트 토허구역 상태 → {level(0|1|2), until, daysLeft, title, id, renewal, sgg}.

    `sgg` 는 LAWD 코드 또는 토허 지역 id('41590:dongtan'). 화성시 동탄 법정동이면 동탄 서브키로 판정한다.
    """
    items = _with_status(designations_items, today)
    tsgg = tr.toheo_sgg_for(sgg, umd) if umd else sgg
    st = dz.region_apt_status(items, tsgg)
    return {"level": ZONE_LEVEL.get(st.get("level"), 0), "until": st.get("until"), "daysLeft": st.get("days_left"),
            "title": st.get("title") or "", "id": st.get("designation"), "renewal": st.get("renewal", "-"), "sgg": tsgg}


# ----------------------------------------------------------------------------- 내림 ↔ 실거래

def _candidate_trades(trades: list[dict], grp, floor, price: int, lo: dt.date, hi: dt.date) -> list[dict]:
    out = []
    for t in trades:
        if grp is not None and t["grp"] is not None and t["grp"] != grp:
            continue
        if floor is not None and t["floor"] is not None and t["floor"] != floor:
            continue
        d = _date(t["date"])
        if not d or d < lo or d > hi:
            continue
        if not (PRICE_LO * price <= t["price"] <= PRICE_HI * price):
            continue
        out.append(t)
    return out


def link_gone_to_trades(events: list[dict], trades_by_cid: dict[str, list], today) -> list[dict]:
    """gone 이벤트마다 실거래를 찾아 상태를 붙인다.

    - confirmed : 같은 cid·grp, 층이 둘 다 있으면 같은 층, 계약일 ∈ [goneAt-14d, goneAt+60d], 실거래가 ∈ [0.85, 1.03]×호가.
                  실거래 하나는 내림 하나에만 붙는다(날짜가 가까운 순).
    - relisted  : goneAt 뒤 21일 안에 같은 매물의 relist 이벤트가 있으면(실거래 매칭보다 우선).
    - pending   : 아직 goneAt+75d 이전이고 실거래가 없음.
    - unconfirmed: goneAt+75d 가 지났는데 실거래가 없음.
    반환 행: {id, cid, grp, area, dong, floor, price, date(goneAt), status, trade|None, daysToTrade|None}
    """
    today = _date(today) or dt.date.today()
    relists: dict[str, list[dt.date]] = defaultdict(list)
    for e in events:
        if e.get("event") == "relist":
            d = _date(e.get("date"))
            if d:
                relists[str(e.get("atclNo"))].append(d)
    gones = [e for e in events if e.get("event") == "gone" and _date(e.get("date")) and _int(e.get("price"))]
    gones.sort(key=lambda e: (e.get("date") or "", str(e.get("atclNo"))))
    tcache: dict[str, list[dict]] = {}
    rows: list[dict] = []
    pairs: list[tuple[int, int, int, int]] = []      # (|간격|, gone idx, trade idx, 서명 간격)
    for gi, e in enumerate(gones):
        cid = str(e.get("cid") or "")
        gone_at = _date(e.get("date"))
        price = _int(e.get("price"))
        grp = _int(e.get("grp"))
        if grp is None:
            grp = area_group(_float(e.get("area")))
        floor = _int(e.get("floor"))
        row = {"id": str(e.get("atclNo") or ""), "cid": cid, "grp": grp, "area": _float(e.get("area")),
               "dong": str(e.get("dong") or ""), "floor": floor, "price": price, "date": gone_at.isoformat(),
               "status": "pending", "trade": None, "daysToTrade": None}
        rows.append(row)
        # relist 가 21일 안에 있으면 재등록(팔린 게 아님) — 실거래 매칭보다 우선
        if any(gone_at < d <= gone_at + dt.timedelta(days=RELIST_DAYS) for d in relists.get(row["id"], [])):
            row["status"] = "relisted"
            continue
        if cid not in tcache:
            tcache[cid] = usable_trades(trades_by_cid.get(cid), today)
        lo, hi = gone_at - dt.timedelta(days=GONE_BEFORE_DAYS), gone_at + dt.timedelta(days=GONE_AFTER_DAYS)
        for ti, t in enumerate(_candidate_trades(tcache[cid], grp, floor, price, lo, hi)):
            idx = tcache[cid].index(t)
            days = (_date(t["date"]) - gone_at).days
            pairs.append((abs(days), gi, idx, days))
    # 전체에서 간격이 가장 가까운 쌍부터 1:1 로 붙인다(실거래 하나는 내림 하나에만)
    used_t: set[tuple[str, int]] = set()
    used_g: set[int] = set()
    for _, gi, ti, days in sorted(pairs):
        cid = rows[gi]["cid"]
        if gi in used_g or (cid, ti) in used_t:
            continue
        used_g.add(gi)
        used_t.add((cid, ti))
        t = tcache[cid][ti]
        rows[gi].update({"status": "confirmed", "trade": _trade_ref(t, days), "daysToTrade": days})
    for row in rows:
        if row["status"] == "pending" and today > _date(row["date"]) + dt.timedelta(days=GONE_PENDING_DAYS):
            row["status"] = "unconfirmed"
    return rows


# ----------------------------------------------------------------------------- 허가 ↔ 실거래

def link_permits_to_trades(permits, trades_by_cid: dict[str, list], today=None) -> dict[str, dict | None]:
    """허가 완료 건(cid 가 있는 것) ↔ 같은 단지의 실거래, 계약일 ∈ [hndl_ymd, hndl_ymd+75d] 중 가장 가까운 것 1:1.

    permits: {key: row} 또는 row 목록. 반환 {key: {date, price, area, floor, dong, days} | None}.
    job_gbn 이 '허가' 가 아닌 건(불허가·취하 등)은 None. today 를 주면 미래 계약일은 무시.
    """
    rows = list(permits.values()) if isinstance(permits, dict) else list(permits or [])
    rows = [p for p in rows if p.get("key")]
    rows.sort(key=lambda p: (p.get("hndl_ymd") or "", p["key"]))
    tcache: dict[str, list[dict]] = {}
    out: dict[str, dict | None] = {}
    pairs: list[tuple[int, str, str, int]] = []       # (간격, hndl_ymd, key, trade idx)
    cid_of: dict[str, str] = {}
    for p in rows:
        out[p["key"]] = None
        cid = p.get("cid") or ""
        d0 = _date(p.get("hndl_ymd"))
        if not cid or not d0 or (p.get("job_gbn") or "허가") != "허가":
            continue
        cid_of[p["key"]] = cid
        if cid not in tcache:
            tcache[cid] = usable_trades(trades_by_cid.get(cid), today)
        hi = d0 + dt.timedelta(days=PERMIT_AFTER_DAYS)
        for i, t in enumerate(tcache[cid]):
            d = _date(t["date"])
            if d is None or d < d0 or d > hi:
                continue
            pairs.append(((d - d0).days, p["hndl_ymd"], p["key"], i))
    # 허가일 ↔ 계약일 간격이 가장 짧은 쌍부터 1:1(늦은 허가 우선)
    used_t: set[tuple[str, int]] = set()
    used_k: set[str] = set()
    for days, _, key, ti in sorted(pairs, key=lambda x: (x[0], x[1]), reverse=False):
        cid = cid_of[key]
        if key in used_k or (cid, ti) in used_t:
            continue
        used_k.add(key)
        used_t.add((cid, ti))
        out[key] = _trade_ref(tcache[cid][ti], days)
    return out


# ----------------------------------------------------------------------------- 추정 점수

def estimate_score(gone_event: dict, ctx: dict | None = None) -> float:
    """매물 내림이 '허가 접수(=계약)' 일 가능성 점수. [0, 1] 클램프. 어디까지나 추정.

    ctx:
      zone         zone_of() 결과(또는 level 숫자). level>0 이면 0.5, 비규제면 0.2 기본
      active       그 단지·평형의 현재 active 매물 수, active30ago 30일 전 매물 수 → 줄었으면 +0.15
      open         구 처리중 건수, openAvg30 최근 30일 평균 → 많으면 +0.1
      listing      매물 행(sameCnt, confirmYmd) — sameCnt>1 −0.1, 확인일로부터 30일 이상 지난 매물 −0.15
      sameCnt / confirmYmd 를 ctx 에 직접 줘도 된다.
    """
    ctx = ctx or {}
    zone = ctx.get("zone")
    level = zone.get("level", 0) if isinstance(zone, dict) else (ZONE_LEVEL.get(zone, zone) if zone is not None else 0)
    try:
        level = int(level or 0)
    except (TypeError, ValueError):
        level = 0
    score = SCORE_ZONE if level > 0 else SCORE_FREE
    active, ago = _int(ctx.get("active")), _int(ctx.get("active30ago"))
    if active is not None and ago is not None and active < ago:
        score += SCORE_ACTIVE_DOWN
    open_n, open_avg = _float(ctx.get("open")), _float(ctx.get("openAvg30"))
    if open_n is not None and open_avg is not None and open_n > open_avg:
        score += SCORE_OPEN_UP
    listing = ctx.get("listing") or {}
    same = _int(ctx.get("sameCnt", listing.get("sameCnt")))
    if same is not None and same > 1:
        score += SCORE_SAME_CNT
    confirm = _date(ctx.get("confirmYmd", listing.get("confirmYmd")))
    gone_at = _date(gone_event.get("date")) or _date(ctx.get("today"))
    if confirm and gone_at and (gone_at - confirm).days >= STALE_CONFIRM_DAYS:
        score += SCORE_STALE_CONFIRM
    return round(max(0.0, min(1.0, score)), 2)


def active_counts(listings: list[dict], events: list[dict], today, days: int = 30) -> dict[str, dict]:
    """평형별 {active: 지금 active 수, active30ago: 30일 전 추정(= 지금 − 그간 new − relist + gone)}."""
    today = _date(today) or dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()
    out: dict[str, dict] = defaultdict(lambda: {"active": 0, "active30ago": 0})
    for l in listings or []:
        if (l.get("status") or "active") != "active":
            continue
        g = str(_int(l.get("grp")) if _int(l.get("grp")) is not None else area_group(_float(l.get("area"))) or "")
        out[g]["active"] += 1
    delta: dict[str, int] = defaultdict(int)
    for e in events or []:
        if (e.get("date") or "") < since:
            continue
        g = str(_int(e.get("grp")) if _int(e.get("grp")) is not None else area_group(_float(e.get("area"))) or "")
        if e.get("event") in ("new", "relist"):
            delta[g] += 1
        elif e.get("event") == "gone":
            delta[g] -= 1
    for g in set(out) | set(delta):
        out[g]["active30ago"] = max(0, out[g]["active"] - delta.get(g, 0))
    return dict(out)


def open_series(cases: list[dict], today, days: int = 30) -> list[int]:
    """구 민원 목록 → 최근 days 일 동안 날짜별 '처리중' 건수(접수 <= d < 완료일). 평균용."""
    today = _date(today) or dt.date.today()
    recs = []
    for c in cases or []:
        r = _date(c.get("received"))
        if not r:
            continue
        done = _date(c.get("done_date")) if c.get("done_date") else None
        if done is None and c.get("status") and not _is_open(c.get("status")):
            done = _date(c.get("status_changed")) or _date(c.get("first_seen")) or r
        recs.append((r, done))
    out = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        out.append(sum(1 for r, done in recs if r <= d and (done is None or done > d)))
    return out


def _is_open(status: str) -> bool:
    return any(k in (status or "") for k in ("처리중", "접수", "진행"))


# ----------------------------------------------------------------------------- 단지 요약

def _week_starts(today: dt.date, n: int = 12) -> list[dt.date]:
    """월요일 시작 n 주(오래된 순)."""
    monday = today - dt.timedelta(days=today.weekday())
    return [monday - dt.timedelta(days=7 * i) for i in range(n - 1, -1, -1)]


def weekly_counts(dates: list[str], today, n: int = 12) -> list[int]:
    today = _date(today) or dt.date.today()
    ws = _week_starts(today, n)
    out = [0] * n
    for s in dates:
        d = _date(s)
        if not d:
            continue
        for i, w in enumerate(ws):
            if w <= d < w + dt.timedelta(days=7):
                out[i] += 1
                break
    return out


def permit_counts(permits: list[dict], today) -> dict:
    """허가(job_gbn=='허가') 건수 집계 {n7, n30, n90, total, weekly[12], byPurpose, first, last}."""
    today = _date(today) or dt.date.today()
    d7, d30, d90 = [(today - dt.timedelta(days=k)).isoformat() for k in (7, 30, 90)]
    ok = [p for p in permits if (p.get("job_gbn") or "허가") == "허가" and p.get("hndl_ymd")]
    purp: dict[str, int] = defaultdict(int)
    for p in ok:
        purp[p.get("use_purp") or "미상"] += 1
    dates = [p["hndl_ymd"] for p in ok]
    return {"n7": sum(1 for d in dates if d >= d7), "n30": sum(1 for d in dates if d >= d30),
            "n90": sum(1 for d in dates if d >= d90), "total": len(ok), "weekly": weekly_counts(dates, today),
            "byPurpose": dict(sorted(purp.items(), key=lambda kv: -kv[1])),
            "first": min(dates) if dates else "", "last": max(dates) if dates else ""}


def complex_summary(cid: str, c: dict, events: list[dict], permits: list[dict], trades, cases_stats: dict | None,
                    zone: dict, today, *, listings: list[dict] | None = None, links: list[dict] | None = None,
                    permit_links: dict[str, dict | None] | None = None, region_label: str = "",
                    estimate: dict | None = None, region_sgg: str = "", scores: dict[str, float] | None = None,
                    open_avg30: float | None = None) -> dict:
    """c/<cid>.json 의 toheo 블록(DESIGN 3절 2항).

    events: 이 단지의 매물 이벤트, permits: 이 단지로 매칭된 허가 행, trades: 모델 파일 trades,
    cases_stats: 구 통계(cases.stats_for 결과)|None, zone: zone_of() 결과.
    선택: listings(이 단지 active 매물), links(link_gone_to_trades 결과 중 이 단지), permit_links(key→trade),
          estimate(cases.estimate 결과), scores({gone id|date: score} 미리 계산한 점수), open_avg30.
    """
    today_d = _date(today) or dt.date.today()
    today_s = today_d.isoformat()
    d30 = (today_d - dt.timedelta(days=30)).isoformat()
    d90 = (today_d - dt.timedelta(days=90)).isoformat()
    tr_list = usable_trades(trades, today_d)
    trades_by_cid = {cid: tr_list}
    if links is None:
        links = link_gone_to_trades(events, trades_by_cid, today_d)
    links = [l for l in links if l.get("cid") == cid]
    if permit_links is None:
        permit_links = link_permits_to_trades(permits, trades_by_cid, today_d)
    counts = active_counts(listings or [], events, today_d)
    st = cases_stats or {}

    # ---- 평형별
    by_grp: dict[str, dict] = {}
    grps = set(counts) | {str(l["grp"]) for l in links if l.get("grp") is not None} | \
        {str(t["grp"]) for t in tr_list if t["grp"] is not None and t["date"] >= d30}
    for g in grps:
        if g in ("", "None"):
            continue
        cnt = counts.get(g, {"active": 0, "active30ago": 0})
        gl = [l for l in links if str(l.get("grp")) == g]
        gone30 = []
        for l in sorted(gl, key=lambda x: x["date"], reverse=True):
            if l["date"] < d30:
                continue
            sc = None
            if scores:
                sc = scores.get(f"{l['id']}|{l['date']}", scores.get(l["id"]))
            if sc is None:
                sc = estimate_score({"date": l["date"]}, {"zone": zone, "active": cnt["active"],
                                                          "active30ago": cnt["active30ago"], "open": st.get("open"),
                                                          "openAvg30": open_avg30})
            gone30.append({"id": l["id"], "date": l["date"], "price": l["price"], "dong": l["dong"], "floor": l["floor"],
                           "score": sc, "status": l["status"], "trade": l["trade"]})
        by_grp[g] = {"active": cnt["active"], "active30ago": cnt["active30ago"], "gone30": gone30,
                     "confirmed90": sum(1 for l in gl if l["status"] == "confirmed" and l["date"] >= d90),
                     "trades30": sum(1 for t in tr_list if str(t["grp"]) == g and t["date"] >= d30)}
    by_grp = dict(sorted(by_grp.items(), key=lambda kv: _int(kv[0]) or 0))

    # ---- 허가
    pc = permit_counts(permits, today_d)
    recent = []
    for p in sorted(permits, key=lambda p: (p.get("hndl_ymd") or "", p.get("key") or ""), reverse=True)[:60]:
        recent.append({"date": p.get("hndl_ymd", ""), "purpose": p.get("use_purp", ""), "jibun": p.get("jibun", ""),
                       "jobGbn": p.get("job_gbn", ""), "key": p.get("key", ""),
                       "trade": permit_links.get(p.get("key", "")) if permit_links else None})
    permits_blk = {"n7": pc["n7"], "n30": pc["n30"], "n90": pc["n90"], "total": pc["total"], "weekly": pc["weekly"],
                   "byPurpose": pc["byPurpose"], "recent": recent}

    # ---- 지역(구)
    region = None
    if st or region_sgg:
        est = estimate or None
        region = {"sgg": region_sgg or zone.get("sgg", ""), "label": region_label or tr.label(region_sgg or zone.get("sgg", "")),
                  "open": st.get("open"), "received7": st.get("received_7d"), "received30": st.get("received_30d"),
                  "p50": st.get("median_days"), "p90": st.get("p90_days"), "p25": st.get("p25_days"),
                  "p75": st.get("p75_days"), "est": est}

    # ---- 타임라인(최근 120일, 최대 40)
    timeline: list[dict] = []
    d120 = (today_d - dt.timedelta(days=120)).isoformat()
    by_day_permits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in permits:
        if (p.get("hndl_ymd") or "") >= d120 and (p.get("job_gbn") or "허가") == "허가":
            by_day_permits[p["hndl_ymd"]][p.get("use_purp") or "미상"] += 1
    for d, purp in by_day_permits.items():
        n = sum(purp.values())
        pp = ", ".join(f"{k}" for k, _ in sorted(purp.items(), key=lambda kv: -kv[1]))
        timeline.append({"date": d, "type": "permit", "text": f"허가 {n}건 ({pp})", "grp": None})
    for l in links:
        if l["date"] >= d120:
            txt = f"매물 내림(추정) {_fmt_price(l['price'])}"
            if l.get("dong"):
                txt += f" {l['dong']}동"
            if l.get("floor") is not None:
                txt += f" {l['floor']}층"
            txt += {"confirmed": " → 실거래 확인", "relisted": " → 재등록", "unconfirmed": " · 미확인", "pending": ""}.get(l["status"], "")
            timeline.append({"date": l["date"], "type": "gone", "text": txt, "grp": str(l["grp"]) if l.get("grp") is not None else None})
    for e in events:
        if e.get("event") == "relist" and (e.get("date") or "") >= d120:
            g = _int(e.get("grp"))
            timeline.append({"date": e["date"], "type": "relist", "text": f"매물 재등록 {_fmt_price(_int(e.get('price')))}",
                             "grp": str(g) if g is not None else None})
    for t in tr_list:
        if t["date"] >= d120:
            txt = f"실거래 {_fmt_price(t['price'])}"
            if t["floor"] is not None:
                txt += f" {t['floor']}층"
            timeline.append({"date": t["date"], "type": "trade", "text": txt,
                             "grp": str(t["grp"]) if t["grp"] is not None else None})
    if zone.get("level") and zone.get("until"):
        timeline.append({"date": zone["until"], "type": "zone",
                         "text": f"토허구역 지정 만료 예정 ({zone.get('title') or ''})".replace(" ()", ""), "grp": None})
    timeline.sort(key=lambda x: x["date"], reverse=True)
    timeline = timeline[:40]

    return {
        "zone": {"level": zone.get("level", 0), "until": zone.get("until"), "daysLeft": zone.get("daysLeft"),
                 "title": zone.get("title") or "", "renewal": zone.get("renewal", "-")},
        "region": region,
        "permits": permits_blk,
        "byGrp": by_grp,
        "timeline": timeline,
        "note": ("허가 내역에는 평형·가격이 없어 단지·지번 단위로만 맞춥니다. 매물 내림은 '허가 접수'가 아니라 추정이며, "
                 "실거래로 사후 확인된 비율(적중률)은 토허 탭에서 공개합니다. 접수 단계(새올)는 단지를 알 수 없습니다."),
        "today": today_s,
    }


def _fmt_price(p) -> str:
    if p is None:
        return "-"
    p = int(p)
    eok, man = divmod(p, 10000)
    if eok and man:
        return f"{eok}억{man:,}"
    if eok:
        return f"{eok}억"
    return f"{man:,}만"


# ----------------------------------------------------------------------------- 적중률

def accuracy_stats(links: list[dict], permit_links: dict[str, dict | None] | None = None,
                   permits_total: int | None = None) -> dict:
    """{gone:{n, confirmed, relisted, unconfirmed, pending, hitRate}, permits:{n, matched, matchRate, medianDaysToTrade}}.

    hitRate = confirmed ÷ (confirmed + unconfirmed + relisted). pending(판정 대기)은 분모에서 뺀다.
    permit_links: link_permits_to_trades 결과. n 은 그 dict 크기(또는 permits_total).
    """
    cnt = {"confirmed": 0, "relisted": 0, "unconfirmed": 0, "pending": 0}
    for l in links or []:
        s = l.get("status") or "pending"
        cnt[s] = cnt.get(s, 0) + 1
    decided = cnt["confirmed"] + cnt["unconfirmed"] + cnt["relisted"]
    gone = {"n": len(links or []), **{k: cnt[k] for k in ("confirmed", "relisted", "unconfirmed", "pending")},
            "hitRate": round(cnt["confirmed"] / decided, 3) if decided else None}
    pl = permit_links or {}
    matched = [v for v in pl.values() if v]
    days = sorted(v["days"] for v in matched if isinstance(v, dict) and v.get("days") is not None)
    n = permits_total if permits_total is not None else len(pl)
    permits = {"n": n, "matched": len(matched), "matchRate": round(len(matched) / n, 3) if n else None,
               "medianDaysToTrade": (days[len(days) // 2] if days else None)}
    return {"gone": gone, "permits": permits}
