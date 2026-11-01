"""data/toheo/* 입출력. data 브랜치에 그대로 커밋되어 사람이 열어볼 수 있는 CSV/JSON 입니다.

data/toheo/
  cases.csv                 새올 민원 누적 (site, sgg, 접수번호 …)
  permits_seoul.csv         서울 허가 내역 누적 (key, sgg, 지번, 단지 cid …)
  raw/seoul/<YYYY-MM-DD>/<sgg>.json.gz   getContractList.do 원본 응답
  saeol_params.json         새올 페이지 파라미터 탐지 결과
  seoul_permits_cfg.json    허가 조회 설정(탐지 결과)
  toheo_progress.json       실행 이력(runs 60개)
"""
from __future__ import annotations

import csv
import gzip
import json
import os
from pathlib import Path

CASE_FIELDS = [
    "site", "sgg", "no", "received", "name", "deadline", "dept", "status",
    "first_seen", "last_seen", "status_changed", "done_date", "done_observed", "seen_in_progress", "missing",
]
PERMIT_FIELDS = [
    "key", "sgg", "gu", "dong", "jibun", "bobn", "bubn", "jimok", "hndl_ymd", "use_purp", "job_gbn",
    "duty_end", "exec_ymd", "address", "cid", "cname", "first_seen",
]
MAX_RUNS = 60


class ToheoStore:
    def __init__(self, root: str | os.PathLike):
        """root = data/toheo (DATA_DIR 아래 toheo 디렉터리)."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 공통 -------------------------------------------------------------
    def _csv_read(self, name: str, fields: list[str]) -> list[dict]:
        p = self.root / name
        if not p.exists():
            return []
        with open(p, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k in fields:
                r.setdefault(k, "")
            if "region" in r and not r.get("sgg"):      # 토허 원본 파일 호환
                r["sgg"] = r.pop("region")
        return rows

    def _csv_write(self, name: str, fields: list[str], rows: list[dict]) -> None:
        p = self.root / name
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
        os.replace(tmp, p)

    def read_json(self, name: str, default):
        p = self.root / name
        if not p.exists():
            return default
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return default

    def write_json(self, name: str, obj) -> None:
        p = self.root / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    # ---- 민원(새올) ---------------------------------------------------------
    def load_cases(self) -> dict[tuple[str, str], dict]:
        return {(r["site"], r["no"]): r for r in self._csv_read("cases.csv", CASE_FIELDS)}

    def save_cases(self, cases: dict[tuple[str, str], dict]) -> None:
        rows = sorted(cases.values(), key=lambda r: (r["site"], r["received"], r["no"]), reverse=True)
        self._csv_write("cases.csv", CASE_FIELDS, rows)

    def load_params(self) -> dict:
        return self.read_json("saeol_params.json", {})

    def save_params(self, params: dict) -> None:
        self.write_json("saeol_params.json", params)

    # ---- 서울 허가내역 -------------------------------------------------------
    def load_permits(self) -> dict[str, dict]:
        return {r["key"]: r for r in self._csv_read("permits_seoul.csv", PERMIT_FIELDS) if r.get("key")}

    def save_permits(self, permits: dict[str, dict]) -> None:
        rows = sorted(permits.values(), key=lambda r: (r.get("hndl_ymd", ""), r.get("sgg", ""), r.get("dong", ""),
                                                        r.get("jibun", ""), r["key"]), reverse=True)
        self._csv_write("permits_seoul.csv", PERMIT_FIELDS, rows)

    def permit_cfg_path(self) -> Path:
        return self.root / "seoul_permits_cfg.json"

    def load_permit_cfg(self) -> dict:
        return self.read_json("seoul_permits_cfg.json", {})

    def save_permit_cfg(self, cfg: dict) -> None:
        self.write_json("seoul_permits_cfg.json", cfg)

    def save_raw(self, date: str, sgg: str, obj) -> Path:
        """원본 응답을 raw/seoul/<date>/<sgg>.json.gz 로 보관(같은 날 재실행은 덮어씀)."""
        d = self.root / "raw" / "seoul" / date
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sgg}.json.gz"
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        return p

    def load_raw(self, date: str, sgg: str):
        p = self.root / "raw" / "seoul" / date / f"{sgg}.json.gz"
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)

    def prune_raw(self, keep_days: int, today: str) -> int:
        """raw/seoul/ 에서 today-keep_days 보다 오래된 날짜 디렉터리 삭제. 반환: 지운 디렉터리 수."""
        import datetime as dt
        import shutil
        base = self.root / "raw" / "seoul"
        if not base.exists():
            return 0
        cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=keep_days)).isoformat()
        n = 0
        for d in base.iterdir():
            if d.is_dir() and d.name < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n

    # ---- 진행 ---------------------------------------------------------------
    def load_progress(self) -> dict:
        return self.read_json("toheo_progress.json", {"runs": []})

    def save_progress(self, obj: dict) -> None:
        obj["runs"] = obj.get("runs", [])[-MAX_RUNS:]
        self.write_json("toheo_progress.json", obj)
