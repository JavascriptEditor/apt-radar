"""수집·적합 흐름.

trades  : 최근 6개월 재조회(신규·해제 반영) → 남은 호출로 과거 적재(서울→경기→인천, 2006-01 까지)
scan    : 신설 구 코드 탐색 (월 1회)
match   : 국토부 단지 ↔ 네이버 단지 매칭 (법정동 단위)
listings: 네이버 매물 수집 (실행당 호출 상한, 오래된 단지부터 순환) + 상태 기계(active/gone 소프트 삭제, listing_events.csv)
model   : 적재가 끝난 묶음별 기준가 모델 적합
"""
from __future__ import annotations

import datetime as dt
import os
import time
from collections import defaultdict

from . import regions
from .deals import normalize
from .model import GroupModel, ym_add
from .molit import BudgetExhausted, MolitClient
from .naver import Blocked, NaverClient, match_complexes
from .store import Store, group_rows_by_year

KST = dt.timezone(dt.timedelta(hours=9))
BACKFILL_START = "200601"
REFRESH_MONTHS = 6
MISS_LIMIT = 3            # 실거래: 재조회에서 연속 n회 사라지면 삭제
LISTING_MISS_GONE = 2     # 매물: 성공 조회에서 연속 n회 없으면 gone
GONE_KEEP_DAYS = 120      # gone 매물 행을 listings.csv 에 남겨 두는 기간
EVENT_KEEP_DAYS = 400     # listing_events.csv 보관 기간
SUSPECT_MIN_PREV = 5      # 직전 listingsN 이 이 이상인데 0건이면 의심 응답
SUSPECT_ACCEPT_AFTER = 3  # 의심 응답이 연속 n회 이어지면 실제 0건으로 받아들임(영구 정체 방지)


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)


def today_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


def date_add(day: str, days: int) -> str:
    """'YYYY-MM-DD' ± days."""
    return (dt.date.fromisoformat(day) + dt.timedelta(days=days)).isoformat()


def _int0(v) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _price(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def apply_articles(listings: dict[str, dict], cid: str, arts: list[dict], today: str) -> tuple[list[dict], dict]:
    """매물 상태 기계(DESIGN 2절). 단지 cid 의 **성공한** 조회 결과 arts 를 listings(atclNo→row, 제자리 수정)에 반영.

    - 응답에 있는 매물: status=active, miss=0, lastSeen=today. 신규 → new 이벤트. gone 이었으면 relist 이벤트.
      호가가 바뀌면 price 이벤트(priceChanges+1).
    - 응답에 없는 기존 active 매물: miss+1, miss>=LISTING_MISS_GONE 이 되는 순간 gone(goneAt=today).
      sameCnt>1 이고 같은 cid·grp·dong·floor·price 의 active 매물이 남아 있으면 대표 매물 교체(swap)로 보고 이벤트 없음.
    반환: (이벤트 행 목록, {"new","relist","price","gone","swap"} 카운트). 실패/의심 응답이면 호출하지 말 것.
    """
    events: list[dict] = []
    cnt = {"new": 0, "relist": 0, "price": 0, "gone": 0, "swap": 0}
    fresh_ids: set[str] = set()

    def ev(kind: str, row: dict, price, prev) -> None:
        events.append({"date": today, "atclNo": row["atclNo"], "cid": cid, "grp": row.get("grp", ""),
                       "area": row.get("area", ""), "dong": row.get("dong", ""), "floor": row.get("floor", ""),
                       "event": kind, "price": "" if price is None else price, "prevPrice": "" if prev is None else prev})
        cnt[kind] += 1

    for a in arts:
        aid = a["atclNo"]
        a["cid"] = cid
        old = listings.get(aid)
        new_price = _price(a.get("price"))
        if not old:
            row = {**a, "firstSeen": today, "lastSeen": today, "status": "active", "goneAt": "", "miss": 0,
                   "priceFirst": "" if new_price is None else new_price, "priceChanges": 0}
            listings[aid] = row
            fresh_ids.add(aid)
            ev("new", row, new_price, None)
            continue
        old_status = old.get("status") or "active"
        old_price = _price(old.get("price"))
        row = {**old, **a, "lastSeen": today, "status": "active", "goneAt": "", "miss": 0,
               "firstSeen": old.get("firstSeen") or today,
               "priceFirst": old.get("priceFirst") if old.get("priceFirst") not in (None, "") else (old.get("price") if old.get("price") not in (None, "") else ("" if new_price is None else new_price)),
               "priceChanges": _int0(old.get("priceChanges"))}
        listings[aid] = row
        fresh_ids.add(aid)
        if old_status == "gone":
            ev("relist", row, new_price, old_price)
        if new_price is not None and old_price is not None and new_price != old_price:
            row["priceChanges"] += 1
            ev("price", row, new_price, old_price)

    # 응답에 없는 기존 active 매물
    for aid, row in listings.items():
        if row.get("cid") != cid or aid in fresh_ids or (row.get("status") or "active") != "active":
            continue
        miss = _int0(row.get("miss")) + 1
        row["miss"] = miss
        if miss < LISTING_MISS_GONE:
            continue
        row["status"] = "gone"
        row["goneAt"] = today
        price = _price(row.get("price"))
        if _int0(row.get("sameCnt")) > 1:
            key = (str(row.get("grp", "")), str(row.get("dong", "")), str(row.get("floor", "")), str(price if price is not None else ""))
            twin = any(
                k in fresh_ids and (str(r.get("grp", "")), str(r.get("dong", "")), str(r.get("floor", "")),
                                    str(_price(r.get("price")) if _price(r.get("price")) is not None else "")) == key
                for k, r in listings.items())
            if twin:
                cnt["swap"] += 1  # 같은 집 대표 매물 교체: 상태만 gone, 이벤트 없음
                continue
        ev("gone", row, price, None)
    return events, cnt


def this_ym() -> str:
    return now_kst().strftime("%Y%m")


class Engine:
    def __init__(self, store: Store, molit: MolitClient | None = None, naver: NaverClient | None = None,
                 region_filter: str | None = None, log=print):
        self.store = store
        self.molit = molit
        self.naver = naver
        self.log = log
        self.progress = store.read_json("progress.json", {})
        self.live = store.read_json("regions_live.json", {})
        self.codes = regions.parse_filter(region_filter or os.environ.get("REGION_FILTER"), self.live)
        self.complexes = store.read_complexes()
        self.fetch_log = store.read_json("fetch_log.json", {})
        self._roll_day()

    # ------------------------------------------------------------ helpers
    def _roll_day(self) -> None:
        d = today_str()
        if self.progress.get("date") != d:
            self.progress["date"] = d
            self.progress["callsToday"] = 0
        if self.molit:
            self.molit.calls = int(self.progress.get("callsToday", 0))

    def _save(self) -> None:
        if self.molit:
            self.progress["callsToday"] = self.molit.calls
        self.progress["updatedAt"] = now_kst().isoformat(timespec="seconds")
        self.store.write_json("progress.json", self.progress)
        self.store.write_json("fetch_log.json", self.fetch_log)
        self.store.write_complexes(self.complexes)

    def group_of(self, code: str) -> str:
        return regions.group_of(code, self.live)

    def groups(self) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for c in self.codes:
            g[self.group_of(c)].append(c)
        return dict(g)

    def _touch_complex(self, row: dict) -> None:
        c = self.complexes.get(row["cid"])
        if not c:
            c = {"cid": row["cid"], "aptSeq": row["aptSeq"], "name": row["name"], "sgg": row["sgg"],
                 "umdCd": row["umdCd"], "umd": row["umd"], "jibun": row["jibun"], "buildYear": row["buildYear"],
                 "firstYm": row["ym"], "lastYm": row["ym"], "nTrades": "", "naverId": "", "naverName": "",
                 "households": "", "highFloor": "", "lat": "", "lng": "", "matchScore": "", "matchedAt": "",
                 "listingsAt": "", "listingsN": ""}
            self.complexes[row["cid"]] = c
            return
        if row["ym"] >= (c.get("lastYm") or ""):
            c["lastYm"] = row["ym"]
            if row["name"]:
                c["name"] = row["name"]
            c["sgg"] = row["sgg"]
            if row["aptSeq"]:
                c["aptSeq"] = row["aptSeq"]
            if row["umdCd"]:
                c["umdCd"] = row["umdCd"]
        if row["ym"] < (c.get("firstYm") or "999999"):
            c["firstYm"] = row["ym"]

    # ------------------------------------------------------------ trades
    def _merge_months(self, code: str, fresh_by_ym: dict[str, list[dict]], refresh: bool) -> None:
        """fresh_by_ym: 조회한 달 → 레코드. refresh=True 면 그 달의 기존 행과 비교해 사라진 행은 miss+1."""
        by_year: dict[str, dict[str, list[dict]]] = defaultdict(dict)
        for ym, rows in fresh_by_ym.items():
            by_year[ym[:4]][ym] = rows
        for year, months in by_year.items():
            existing = self.store.read_trades_year(code, year)
            keep = [r for r in existing if r["ym"] not in months]
            old = {r["key"]: r for r in existing if r["ym"] in months}
            merged = keep
            for ym, rows in months.items():
                fresh = {}
                for r in rows:
                    if r["key"] in fresh and not r["cancelled"]:
                        continue
                    fresh[r["key"]] = r
                for k, r in fresh.items():
                    r["miss"] = 0
                    merged.append(r)
                    self._touch_complex(r)
                if refresh:
                    for k, r in old.items():
                        if r["ym"] != ym or k in fresh:
                            continue
                        r["miss"] = int(r.get("miss") or 0) + 1
                        if r["miss"] < MISS_LIMIT:
                            merged.append(r)
            self.store.write_trades_year(code, year, merged)

    def _fetch_month(self, code: str, ym: str) -> list[dict]:
        raw = self.molit.fetch_month(code, ym)
        grp = self.group_of(code)
        out = []
        for d in raw:
            r = normalize(d, code, grp)
            if r:
                out.append(r)
        return out

    def run_trades(self, reserve: int = 800) -> dict:
        assert self.molit, "MOLIT 클라이언트 필요"
        bf = self.progress.setdefault("backfill", {})
        last_refresh = self.progress.setdefault("lastRefresh", {})
        cur = this_ym()
        refresh_months = [ym_add(cur, -k) for k in range(REFRESH_MONTHS)]
        summary = {"refreshed": [], "backfilled": {}, "stopped": ""}
        t0 = time.time()
        try:
            # 1) 최근 6개월 재조회 — 가장 오래된 것부터. (reserve 는 적재에만 적용: 하루 두 번째 실행은 남겨 둔 몫으로 재조회)
            order = sorted(self.codes, key=lambda c: last_refresh.get(c, ""))
            for code in order:
                if self.molit.remaining() <= 0:
                    summary["stopped"] = "budget"
                    break
                fresh = {}
                for ym in refresh_months:
                    fresh[ym] = self._fetch_month(code, ym)
                self._merge_months(code, fresh, refresh=True)
                last_refresh[code] = now_kst().isoformat(timespec="seconds")
                summary["refreshed"].append(code)
                ntot = sum(len(v) for v in fresh.values())
                self.fetch_log.setdefault("trades", {})[code] = {"at": last_refresh[code], "n6m": ntot}
                if ntot == 0:
                    self.fetch_log.setdefault("warnings", {})[code] = "최근 6개월 거래 0건 — 코드가 바뀌었을 수 있음"
                else:
                    self.fetch_log.get("warnings", {}).pop(code, None)
                self._save()
            # 2) 과거 적재 (서울 → 경기 → 인천)
            for code in self.codes:
                st = bf.setdefault(code, {"done": False, "cursor": ym_add(refresh_months[-1], -1)})
                if st["done"]:
                    continue
                batch: dict[str, list[dict]] = {}
                cur_year = st["cursor"][:4]
                while not st["done"]:
                    if self.molit.remaining() <= reserve:
                        summary["stopped"] = "reserve"
                        break
                    ym = st["cursor"]
                    if ym[:4] != cur_year:
                        self._merge_months(code, batch, refresh=False)
                        batch = {}
                        cur_year = ym[:4]
                    batch[ym] = self._fetch_month(code, ym)
                    summary["backfilled"][code] = summary["backfilled"].get(code, 0) + 1
                    nxt = ym_add(ym, -1)
                    if nxt < BACKFILL_START:
                        st["done"] = True
                        st["doneAt"] = today_str()
                    st["cursor"] = nxt
                if batch:
                    self._merge_months(code, batch, refresh=False)
                self._save()
                if summary["stopped"]:
                    break
        except BudgetExhausted as e:
            summary["stopped"] = f"budget: {e}"
            self.log(f"[trades] 한도 도달: {e}")
        self._update_region_done()
        self.fetch_log["tradesRun"] = {"at": now_kst().isoformat(timespec="seconds"), "sec": int(time.time() - t0),
                                       "calls": self.molit.calls, **{k: (v if not isinstance(v, list) else len(v)) for k, v in summary.items()}}
        self._save()
        return summary

    def _update_region_done(self) -> None:
        bf = self.progress.get("backfill", {})
        done = self.progress.setdefault("regionsDone", {})
        for sido in regions.SIDO_ORDER:
            codes = [c for c in self.codes if c[:2] == sido]
            if codes and all(bf.get(c, {}).get("done") for c in codes) and sido not in done:
                done[sido] = today_str()

    def region_done(self, sido: str) -> bool:
        return sido in self.progress.get("regionsDone", {})

    def group_done(self, group: str) -> bool:
        bf = self.progress.get("backfill", {})
        codes = [c for c in self.codes if self.group_of(c) == group]
        return bool(codes) and all(bf.get(c, {}).get("done") for c in codes)

    # -------------------------------------------------------------- scan
    def run_scan(self, force: bool = False) -> dict:
        """신설 구 코드 탐색. 월 1회."""
        assert self.molit
        month = this_ym()
        if not force and self.progress.get("scanAt") == month:
            return {"skipped": True}
        probe_ym = ym_add(month, -1)
        known = {r["code"] for r in regions.REGIONS} | set(self.live)
        found = {}
        try:
            for rg in regions.SCAN_RANGES:
                for n in range(rg["start"], rg["end"] + 1):
                    code = str(n)
                    if code in known:
                        continue
                    if self.molit.remaining() <= 300:
                        break
                    total, _ = self.molit.count_month(code, probe_ym)
                    if total <= 0:
                        continue
                    rows = self.molit.fetch_month(code, probe_ym)
                    umds = {(r.get("umdNm") or "").strip() for r in rows}
                    name, grp = regions.guess_new_gu(umds, rg["sido"], rg.get("fallback_group"))
                    self.live[code] = {"name": name, "group": grp or code, "foundAt": today_str(), "umds": sorted(umds)[:20]}
                    found[code] = self.live[code]
                    self.log(f"[scan] 새 코드 {code} → {name} ({len(rows)}건)")
        except BudgetExhausted:
            pass
        self.progress["scanAt"] = month
        self.store.write_json("regions_live.json", self.live)
        self._save()
        return {"found": found}

    # ------------------------------------------------------------- match
    def run_match(self, max_dongs: int = 400, stale_days: int = 180, retry_days: int = 30) -> dict:
        assert self.naver
        today = today_str()
        by_dong: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for c in self.complexes.values():
            if not c.get("umdCd") or c["sgg"] not in self.codes:
                continue
            by_dong[(c["sgg"], c["umdCd"])].append(c)

        def need(c: dict) -> bool:
            at = c.get("matchedAt") or ""
            if not at:
                return True
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(at)).days
            return age > (stale_days if c.get("naverId") else retry_days)

        todo = [(k, v) for k, v in by_dong.items() if any(need(c) for c in v)]
        todo.sort(key=lambda kv: min(c.get("matchedAt") or "" for c in kv[1]))
        done, matched, status = 0, 0, "ok"
        try:
            for (sgg, umd_cd), cxs in todo[:max_dongs]:
                cands = {sgg + umd_cd}
                grp = self.group_of(sgg)
                for code in self.codes:
                    if self.group_of(code) == grp:
                        cands.add(code + umd_cd)
                for code in self.live:
                    if self.live[code].get("group") == grp:
                        cands.add(code + umd_cd)
                naver_cx: list[dict] = []
                for cortar in sorted(cands):
                    naver_cx = self.naver.complexes(cortar)
                    if naver_cx:
                        break
                m = match_complexes(cxs, naver_cx)
                for c in cxs:
                    c["matchedAt"] = today
                    if c["cid"] in m:
                        n, score = m[c["cid"]]
                        c.update({"naverId": n["naverId"], "naverName": n["naverName"],
                                  "households": n.get("households") or "", "highFloor": n.get("highFloor") or "",
                                  "lat": n.get("lat") or "", "lng": n.get("lng") or "", "matchScore": score})
                        matched += 1
                done += 1
                if done % 50 == 0:
                    self._save()
        except Blocked as e:
            status = f"blocked: {e}"
            self.log(f"[match] {status}")
        self.fetch_log["match"] = {"at": now_kst().isoformat(timespec="seconds"), "dongs": done, "matched": matched,
                                   "status": status, "calls": self.naver.calls, "pending": max(0, len(todo) - done)}
        self._save()
        return self.fetch_log["match"]

    # ---------------------------------------------------------- listings
    def run_listings(self, max_calls: int = 1500, max_sec: int = 2400) -> dict:
        """네이버 매물 수집 + 상태 기계(소프트 삭제·이벤트). DESIGN 2절.

        - articles_ok() 가 ok=False(http 오류·HTML·구조 불명·예외) 면 그 단지의 매물 상태는 전혀 바꾸지 않는다.
        - 의심 응답 가드: 직전 listingsN >= SUSPECT_MIN_PREV 인데 0건이면 실패로 취급(suspect). 단, 같은 단지에서
          의심이 SUSPECT_ACCEPT_AFTER 회 연속이면 실제 0건으로 받아들인다(progress.json 의 listingsSuspect 로 추적).
        - gone 후 GONE_KEEP_DAYS 지난 행 삭제, EVENT_KEEP_DAYS 지난 이벤트 정리.
        """
        assert self.naver
        today = today_str()
        listings = self.store.read_listings()
        cands = [c for c in self.complexes.values() if c.get("naverId") and c["sgg"] in self.codes]
        # 최근 거래가 있는 단지 우선, 그 안에서 오래 안 본 순
        cands.sort(key=lambda c: (c.get("listingsAt") or "", -(int(c.get("lastYm") or 0))))
        suspect_streak: dict[str, int] = self.progress.setdefault("listingsSuspect", {})
        t0 = time.time()
        status, done = "ok", 0
        cnt = {"new": 0, "relist": 0, "price": 0, "gone": 0, "swap": 0, "suspect": 0, "failed": 0}
        pending_events: list[dict] = []

        def flush() -> None:
            self.store.write_listings(listings)
            self.store.append_events(pending_events)
            pending_events.clear()
            self._save()

        try:
            for c in cands:
                if self.naver.calls >= max_calls or time.time() - t0 > max_sec:
                    status = "partial"
                    break
                nid = c["naverId"]
                try:
                    arts, ok = self.naver.articles_ok(nid)
                except Blocked:
                    raise
                except Exception as e:  # 파싱 예외 등: 그 단지만 실패로
                    self.log(f"[listings] {c['cid']} 예외: {e}")
                    arts, ok = [], False
                prev_n = _int0(c.get("listingsN"))
                suspect = False
                if ok and not arts and prev_n >= SUSPECT_MIN_PREV:
                    streak = suspect_streak.get(nid, 0) + 1
                    if streak < SUSPECT_ACCEPT_AFTER:
                        ok, suspect = False, True
                        cnt["suspect"] += 1
                        suspect_streak[nid] = streak
                        self.log(f"[listings] {c['cid']} 의심 응답: 직전 {prev_n}건 → 0건 ({streak}회)")
                c["listingsAt"] = now_kst().isoformat(timespec="seconds")
                done += 1
                if not ok:
                    if not suspect:
                        cnt["failed"] += 1
                    if done % 100 == 0:
                        flush()
                    continue
                suspect_streak.pop(nid, None)
                events, k = apply_articles(listings, c["cid"], arts, today)
                pending_events += events
                for key in k:
                    cnt[key] += k[key]
                c["listingsN"] = len(arts)
                if done % 100 == 0:
                    flush()
        except Blocked as e:
            status = f"blocked: {e}"
            self.log(f"[listings] {status}")
        # gone 120일 정리(이벤트는 남음) + 이벤트 400일 정리
        cutoff = date_add(today, -GONE_KEEP_DAYS)
        stale = [k for k, v in listings.items() if v.get("status") == "gone" and (v.get("goneAt") or today) < cutoff]
        for k in stale:
            del listings[k]
        flush()
        pruned_events = self.store.prune_events(date_add(today, -EVENT_KEEP_DAYS))
        n_active = sum(1 for v in listings.values() if (v.get("status") or "active") == "active")
        self.fetch_log["listings"] = {"at": now_kst().isoformat(timespec="seconds"), "complexes": done,
                                      "total": len(cands), "status": status, "calls": self.naver.calls,
                                      "n": n_active, "nAll": len(listings), "nGone": len(listings) - n_active,
                                      "pruned": len(stale), "prunedEvents": pruned_events,
                                      "endpoint": self.naver.state.get("articles"), **cnt}
        self._save()
        return self.fetch_log["listings"]

    # ------------------------------------------------------------- model
    def run_model(self, partial: bool = False, groups: list[str] | None = None) -> dict:
        out = {}
        now = this_ym()
        for grp, codes in self.groups().items():
            if groups and grp not in groups:
                continue
            if not partial and not self.group_done(grp):
                continue
            rows = self.store.read_trades_group(codes)
            if not rows:
                continue
            meta = {cid: c for cid, c in self.complexes.items() if c.get("sgg") in codes}
            gm = GroupModel(rows, now, complex_meta=meta)
            res = gm.fit()
            # 표시용 최근 거래(36개월, 단지당 최대 240건) 를 모델 파일에 함께 저장
            cutoff = ym_add(now, -36)
            recent: dict[str, list] = defaultdict(list)
            for r in rows:
                if r["ym"] >= cutoff:
                    recent[r["cid"]].append(r)
            stats: dict[str, dict] = defaultdict(lambda: {"n": 0, "first": "999999", "last": "000000"})
            for r in rows:
                if r["cancelled"]:
                    continue
                s = stats[r["cid"]]
                s["n"] += 1
                s["first"] = min(s["first"], r["ym"])
                s["last"] = max(s["last"], r["ym"])
            for cid, cx in res["complexes"].items():
                rs = sorted(recent.get(cid, []), key=lambda r: r["date"], reverse=True)[:240]
                cx["trades"] = [[r["date"], r["price"], r["area"], r["floor"] if r["floor"] is not None else "",
                                 r["dong"], r["direct"], r["cancelled"]] for r in rs]
            for cid, s in stats.items():
                c = self.complexes.get(cid)
                if c:
                    c["nTrades"] = s["n"]
                    c["firstYm"] = s["first"]
                    c["lastYm"] = s["last"]
            res["codes"] = codes
            res["fittedAt"] = now_kst().isoformat(timespec="seconds")
            self.store.write_model(grp, res)
            out[grp] = {"complexes": len(res["complexes"]), "n": res["n"]}
            self.log(f"[model] {grp}: 단지 {len(res['complexes'])}, 거래 {res['n']}")
        self.fetch_log["model"] = {"at": now_kst().isoformat(timespec="seconds"), "groups": out}
        self._save()
        return out
