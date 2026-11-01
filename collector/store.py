"""누적 상태 저장(data 브랜치). 사람이 열어볼 수 있게 CSV/JSON 으로 둡니다.

data/
  trades/<코드>/<연도>.csv.gz   거래 원본 (해제 포함, miss 카운터)
  complexes.csv                 단지 인덱스 + 네이버 단지 매칭
  listings.csv                  매물 스냅샷 (소프트 삭제: status active/gone, miss 카운터)
  listing_events.csv            매물 이벤트(new/price/gone/relist) 날짜 오름차순 append
  listing_stats.csv             일별 등급 분포 (트렌드)
  model/<묶음코드>.json.gz       기준가 모델
  progress.json                 적재 진행·호출 수
  regions_live.json             자동 탐색한 신설 구 코드
  fetch_log.json                최근 수집 로그 (사이트 수집 상태 탭)
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
from collections import defaultdict
from pathlib import Path

from .deals import TRADE_FIELDS

COMPLEX_FIELDS = ["cid", "aptSeq", "name", "sgg", "umdCd", "umd", "jibun", "buildYear", "firstYm", "lastYm",
                  "nTrades", "naverId", "naverName", "households", "highFloor", "lat", "lng", "matchScore",
                  "matchedAt", "listingsAt", "listingsN"]
LISTING_FIELDS = ["atclNo", "cid", "naverId", "dongRaw", "dong", "floor", "top", "floorHint", "area", "supply",
                  "grp", "price", "confirmYmd", "direct", "sameCnt", "realtor", "direction", "desc", "tags",
                  "firstSeen", "lastSeen", "status", "goneAt", "miss", "priceFirst", "priceChanges"]
# 구 listings.csv(판정 컬럼 fair/diffPct/grade/reasons/conf 가 있던 파일)를 읽을 때 채우는 기본값
LISTING_DEFAULTS = {"status": "active", "goneAt": "", "miss": "0", "priceFirst": "", "priceChanges": "0"}
EVENT_FIELDS = ["date", "atclNo", "cid", "grp", "area", "dong", "floor", "event", "price", "prevPrice"]
EVENT_TYPES = ("new", "price", "gone", "relist")


class Store:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "trades").mkdir(exist_ok=True)
        (self.root / "model").mkdir(exist_ok=True)

    # ---------------------------------------------------------------- json
    def read_json(self, name: str, default):
        p = self.root / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def write_json(self, name: str, obj) -> None:
        p = self.root / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    # -------------------------------------------------------------- trades
    def trade_path(self, code: str, year: str | int) -> Path:
        return self.root / "trades" / code / f"{year}.csv.gz"

    def read_trades_year(self, code: str, year: str | int) -> list[dict]:
        p = self.trade_path(code, year)
        if not p.exists():
            return []
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            _fix_types(r)
        return rows

    def write_trades_year(self, code: str, year: str | int, rows: list[dict]) -> None:
        p = self.trade_path(code, year)
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(rows, key=lambda r: (r["date"], r["cid"], r["area"], r["price"]))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        os.replace(tmp, p)

    def years_for(self, code: str) -> list[str]:
        d = self.root / "trades" / code
        if not d.exists():
            return []
        return sorted(p.name[:-7] for p in d.glob("*.csv.gz"))

    def read_trades_code(self, code: str) -> list[dict]:
        out: list[dict] = []
        for y in self.years_for(code):
            out += self.read_trades_year(code, y)
        return out

    def read_trades_group(self, codes: list[str]) -> list[dict]:
        """묶음(여러 코드)의 거래를 합치고 key 로 중복 제거."""
        seen: dict[str, dict] = {}
        for c in codes:
            for r in self.read_trades_code(c):
                k = r["key"]
                if k not in seen or (r["cancelled"] and not seen[k]["cancelled"]):
                    seen[k] = r
        return list(seen.values())

    def trade_codes(self) -> list[str]:
        d = self.root / "trades"
        return sorted(p.name for p in d.iterdir() if p.is_dir())

    # ----------------------------------------------------------- complexes
    def read_complexes(self) -> dict[str, dict]:
        p = self.root / "complexes.csv"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        return {r["cid"]: r for r in rows}

    def write_complexes(self, cx: dict[str, dict]) -> None:
        p = self.root / "complexes.csv"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COMPLEX_FIELDS, extrasaction="ignore")
            w.writeheader()
            for cid in sorted(cx):
                row = {k: cx[cid].get(k, "") for k in COMPLEX_FIELDS}
                w.writerow(row)
        os.replace(tmp, p)

    # ------------------------------------------------------------ listings
    def read_listings(self, active_only: bool = False) -> dict[str, dict]:
        """{atclNo: row}. 값은 전부 문자열(CSV 그대로). 구 파일(status 컬럼 없음)은 기본값으로 채운다.
        active_only=True 면 status=='active' 행만(사이트 export 용)."""
        p = self.root / "listings.csv"
        if not p.exists():
            return {}
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        out = {}
        for r in rows:
            for k, v in LISTING_DEFAULTS.items():
                if r.get(k) in (None, ""):
                    r[k] = v
            if not r.get("priceFirst"):
                r["priceFirst"] = r.get("price", "")
            if active_only and r["status"] != "active":
                continue
            out[r["atclNo"]] = r
        return out

    def write_listings(self, ls: dict[str, dict]) -> None:
        p = self.root / "listings.csv"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LISTING_FIELDS, extrasaction="ignore")
            w.writeheader()
            for k in sorted(ls, key=lambda a: (ls[a].get("cid", ""), a)):
                row = {f: ls[k].get(f, "") for f in LISTING_FIELDS}
                for f, v in LISTING_DEFAULTS.items():
                    if row[f] in (None, ""):
                        row[f] = v
                w.writerow(row)
        os.replace(tmp, p)

    # ------------------------------------------------------ listing events
    def read_events(self, since: str | None = None) -> list[dict]:
        """listing_events.csv 전체(또는 date >= since). 날짜 오름차순. 값은 문자열."""
        p = self.root / "listing_events.csv"
        if not p.exists():
            return []
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if since:
            rows = [r for r in rows if (r.get("date") or "") >= since]
        return rows

    def append_events(self, rows: list[dict]) -> None:
        """이벤트 행 append(파일 없으면 헤더부터). 날짜 오름차순은 호출자가 지킨다(실행마다 '오늘' 이므로 자연히 유지)."""
        if not rows:
            return
        p = self.root / "listing_events.csv"
        new = not p.exists() or p.stat().st_size == 0
        with open(p, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EVENT_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in EVENT_FIELDS})

    def prune_events(self, before: str) -> int:
        """date < before 인 이벤트 행 삭제. 지운 행 수를 돌려준다."""
        p = self.root / "listing_events.csv"
        if not p.exists():
            return 0
        rows = self.read_events()
        keep = [r for r in rows if (r.get("date") or "") >= before]
        if len(keep) == len(rows):
            return 0
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=EVENT_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(keep)
        os.replace(tmp, p)
        return len(rows) - len(keep)

    def upsert_listing_stats(self, rows: list[dict], date: str) -> list[dict]:
        """같은 날짜 행은 덮어쓴다(하루 여러 번 실행). 전체 행을 돌려준다."""
        old = [r for r in self.read_listing_stats() if r.get("date") != date]
        allrows = old + rows
        p = self.root / "listing_stats.csv"
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "sgg", "n", "SS", "S", "A", "B", "C", "medDiff"], extrasaction="ignore")
            w.writeheader()
            w.writerows(allrows)
        os.replace(tmp, p)
        return allrows

    def read_listing_stats(self) -> list[dict]:
        p = self.root / "listing_stats.csv"
        if not p.exists():
            return []
        with open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    # --------------------------------------------------------------- model
    def read_model(self, group: str) -> dict | None:
        p = self.root / "model" / f"{group}.json.gz"
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)

    def write_model(self, group: str, obj: dict) -> None:
        p = self.root / "model" / f"{group}.json.gz"
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)

    def model_groups(self) -> list[str]:
        return sorted(p.name[:-8] for p in (self.root / "model").glob("*.json.gz"))


def _fix_types(r: dict) -> None:
    for k in ("price", "direct", "cancelled", "miss", "grp"):
        v = r.get(k, "")
        r[k] = int(float(v)) if v not in ("", None) else (0 if k in ("direct", "cancelled", "miss") else None)
    r["area"] = float(r["area"]) if r.get("area") not in ("", None) else None
    f = r.get("floor", "")
    r["floor"] = int(float(f)) if f not in ("", None) else None


def group_rows_by_year(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["date"][:4]].append(r)
    return out
