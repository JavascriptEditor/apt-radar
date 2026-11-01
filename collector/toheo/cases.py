"""새올 민원 상태 누적(merge_rows)과 지역 통계(region_stats, estimate). 토허 원본 engine.py 포팅.

핵심 규칙
- '처리중'으로 먼저 본 민원이 나중에 해결·취하 등으로 바뀐 것을 확인한 날 = 실제 처리 완료 관측일
  (done_date, done_observed=1). 처음 봤을 때 이미 완료였던 민원은 완료일을 알 수 없어 done_observed=0 으로
  두고 처리기간 통계에서 뺍니다.
- 목록을 끝(cutoff)까지 읽었는데 '처리중' 민원이 3회 연속 보이지 않으면 '완료(목록이탈)'로 처리합니다.
- 지역 식별자는 sgg(LAWD 코드). 시 단위 새올의 구 미분류 건은 시 코드를 그대로 둡니다.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from .regions import Site, sgg_for_case
from .saeol import Row

IN_PROGRESS = ("처리중", "접수", "진행")
DROP_AFTER_MISSING = 3


def is_open(status: str) -> bool:
    return any(k in (status or "") for k in IN_PROGRESS)


def _as_date(d) -> dt.date:
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(str(d)[:10])


def merge_rows(cases: dict[tuple[str, str], dict], site: Site, rows: list[Row], now: dt.datetime,
               covered_from: dt.date | None, reached_cutoff: bool) -> dict:
    """fetch 결과를 cases 에 반영. key=(site.id, 접수번호). covered_from: 이번 수집이 빠짐없이 읽은 접수일 하한."""
    today = now.date().isoformat()
    ts = now.replace(tzinfo=None).isoformat(timespec="seconds")
    stats = {"new": 0, "updated": 0, "completed_observed": 0, "dropped": 0}
    seen_keys: set[tuple[str, str]] = set()
    for r in rows:
        key = (site.id, r.no)
        seen_keys.add(key)
        cur = cases.get(key)
        if cur is None:
            cases[key] = {
                "site": site.id, "sgg": sgg_for_case(site, r.dept), "no": r.no, "received": r.received,
                "name": r.name, "deadline": r.deadline, "dept": r.dept, "status": r.status,
                "first_seen": ts, "last_seen": ts, "status_changed": "", "done_date": "",
                "done_observed": "0", "seen_in_progress": "1" if is_open(r.status) else "0", "missing": "0",
            }
            stats["new"] += 1
            continue
        cur["last_seen"] = ts
        cur["missing"] = "0"
        if r.dept and not cur.get("dept"):
            cur["dept"] = r.dept
            cur["sgg"] = sgg_for_case(site, r.dept)
        if r.deadline and not cur.get("deadline"):
            cur["deadline"] = r.deadline
        if r.status != cur["status"]:
            was_open = is_open(cur["status"])
            cur["status"] = r.status
            cur["status_changed"] = ts
            stats["updated"] += 1
            if was_open and not is_open(r.status):
                cur["done_date"] = today
                cur["done_observed"] = "1" if cur.get("seen_in_progress") == "1" else "0"
                stats["completed_observed"] += 1
            if is_open(r.status):
                cur["seen_in_progress"] = "1"
                cur["done_date"] = ""
    # 목록에서 사라진 '처리중' 민원(이번 수집 범위 안)
    if covered_from is not None and reached_cutoff:
        lo = _as_date(covered_from).isoformat()
        for key, c in cases.items():
            if c["site"] != site.id or key in seen_keys:
                continue
            if not is_open(c["status"]) or c["received"] < lo:
                continue
            c["missing"] = str(int(c.get("missing") or 0) + 1)
            if int(c["missing"]) >= DROP_AFTER_MISSING:
                c["status"] = "완료(목록이탈)"
                c["status_changed"] = ts
                c["done_date"] = today
                c["done_observed"] = "1" if c.get("seen_in_progress") == "1" else "0"
                stats["dropped"] += 1
    return stats


# ----------------------------------------------------------------------------- 통계

def _d(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def stats_for(cases: list[dict], today, series_days: int = 180) -> dict:
    """한 지역의 민원 목록 → 요약 통계와 일별 시계열."""
    today = _as_date(today)
    d7, d30, d60 = today - dt.timedelta(days=7), today - dt.timedelta(days=30), today - dt.timedelta(days=60)
    d56 = today - dt.timedelta(days=56)
    recv: dict[dt.date, int] = defaultdict(int)
    done: dict[dt.date, int] = defaultdict(int)
    open_cnt = 0
    open_rows = []
    durations: list[int] = []
    deadline_gaps: list[int] = []
    statuses: dict[str, int] = defaultdict(int)
    recent_done: list[dict] = []
    for c in cases:
        r = _d(c.get("received", ""))
        if not r:
            continue
        recv[r] += 1
        dl = _d(c.get("deadline", ""))
        if dl and dl >= r:
            deadline_gaps.append((dl - r).days)
        if is_open(c.get("status", "")):
            open_cnt += 1
            open_rows.append({
                "no": c.get("no", ""), "received": c["received"], "deadline": c.get("deadline", ""),
                "status": c["status"], "dept": c.get("dept", ""), "days": (today - r).days,
                "overdue": bool(dl and today > dl),
            })
        else:
            statuses[c.get("status", "")] += 1
            dd = _d(c.get("done_date", ""))
            if dd and c.get("done_observed") == "1":
                done[dd] += 1
                dur = (dd - r).days
                if 0 <= dur <= 120:
                    if r >= d56:
                        durations.append(dur)
                    if dd >= d30:
                        recent_done.append({"d": c["done_date"], "n": dur})

    def count(m: dict, lo: dt.date, hi: dt.date | None = None) -> int:
        hi = hi or today
        return sum(v for k, v in m.items() if lo <= k <= hi)

    dates = [today - dt.timedelta(days=i) for i in range(series_days - 1, -1, -1)]
    return {
        "open": open_cnt,
        "received_7d": count(recv, d7),
        "received_30d": count(recv, d30),
        "received_prev_30d": count(recv, d60, d30 - dt.timedelta(days=1)),
        "completed_30d": count(done, d30),
        "median_days": _pct(durations, 0.5),
        "p25_days": _pct(durations, 0.25),
        "p75_days": _pct(durations, 0.75),
        "p90_days": _pct(durations, 0.9),
        "duration_n": len(durations),
        "durations": durations,
        "deadline_days": statistics.median(deadline_gaps) if deadline_gaps else None,
        "status_counts": dict(statuses),
        "spark": [recv.get(d, 0) for d in dates[-28:]],
        "series": {
            "dates": [d.isoformat() for d in dates],
            "received": [recv.get(d, 0) for d in dates],
            "completed": [done.get(d, 0) for d in dates],
        },
        "open_rows": sorted(open_rows, key=lambda x: (x["received"], x["no"])),
        "recent_done": sorted(recent_done, key=lambda x: x["d"], reverse=True)[:800],
    }


def region_stats(cases, today, series_days: int = 180) -> dict[str, dict]:
    """민원 전체(dict 또는 list) → sgg 별 통계. 각 값은 stats_for() 결과(durations 포함)."""
    rows = list(cases.values()) if isinstance(cases, dict) else list(cases)
    by_sgg: dict[str, list[dict]] = defaultdict(list)
    for c in rows:
        by_sgg[c.get("sgg") or ""].append(c)
    return {sgg: stats_for(lst, today, series_days) for sgg, lst in by_sgg.items()}


def durations_by_sgg(stats: dict[str, dict]) -> dict[str, list[int]]:
    """region_stats 결과 → sgg 별 처리 소요일 목록(+ 'all' 합산). estimate() 입력."""
    out: dict[str, list[int]] = {sgg: list(s.get("durations", [])) for sgg, s in stats.items()}
    out["all"] = [d for v in out.values() for d in v]
    return out


def estimate(durations_by_sgg: dict[str, list[int]], sgg: str) -> dict | None:
    """최근(접수 56일 내) 관측 처리 소요일로 예상 승인일 분위수. 표본 3건 미만이면 None."""
    vals = durations_by_sgg.get(sgg) or []
    if len(vals) < 3:
        return None
    return {"n": len(vals), "p25": _pct(vals, 0.25), "p50": _pct(vals, 0.5), "p75": _pct(vals, 0.75),
            "p90": _pct(vals, 0.9)}
