"""토지거래허가구역 지정 현황(config/designations.json) 읽기와 지역별 적용 판정.

- regions 패턴은 LAWD 코드 또는 fnmatch 패턴("11*", "41590:dongtan"). 토허 대상 지역 id(regions.ALL_SGGS)와
  비교해 region_ids 를 채웁니다.
- 아파트 거래에 실제로 걸리는지(level 판정)는 apt_relevant=true 이고 target_person 이 '외국인등' 이 아닌
  활성 지정만 봅니다(외국인 대상 지정·도로 지목·60㎡ 기준 토지 지정은 제외).
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
from pathlib import Path

from .regions import ALL_SGGS, base_code

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "designations.json"

FIELDS = ["id", "title", "authority", "notice_no", "start", "end", "scope", "target", "target_person",
          "threshold_m2", "apt_relevant", "regions", "partial", "note", "source", "verified_at", "renewal_status"]


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("DESIGNATIONS_PATH") or DEFAULT_PATH)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("items", [])
    for i, it in enumerate(data["items"]):
        # 사람이 편집하는 파일이므로 필수 필드를 검증한다(누락 시 사이트 토허 탭이 TypeError 로 멈추는 것보다 여기서 실패)
        for k in ("id", "start", "end"):
            if not it.get(k):
                raise ValueError(f"designations.json items[{i}] 에 '{k}' 가 없습니다 (id={it.get('id')!r})")
        for k in ("start", "end"):
            try:
                dt.date.fromisoformat(str(it[k]))
            except ValueError as e:
                raise ValueError(f"designations.json {it['id']}: {k}={it[k]!r} 는 YYYY-MM-DD 가 아닙니다") from e
        it.setdefault("title", str(it["id"]))
        it.setdefault("regions", [])
        it.setdefault("partial", False)
        it.setdefault("apt_relevant", it.get("scope") in ("apt", "all"))
        it.setdefault("target_person", "전체")
        it.setdefault("renewal_status", "-")
        it.setdefault("notice_no", "")
        it.setdefault("threshold_m2", None)
        it.setdefault("verified_at", "")
    return data


def _today(today) -> dt.date:
    if isinstance(today, dt.datetime):
        return today.date()
    if isinstance(today, dt.date):
        return today
    return dt.date.fromisoformat(str(today)[:10])


def matches(patterns: list[str], sgg: str) -> bool:
    """'11*' 는 서울 전부, '41590' 은 화성 전체(동탄 서브키 포함), '41590:dongtan' 은 동탄만."""
    for pat in patterns or []:
        if fnmatch.fnmatchcase(sgg, pat):
            return True
        if ":" in sgg and ":" not in pat and fnmatch.fnmatchcase(base_code(sgg), pat):
            return True
    return False


def status(item: dict, today) -> str:
    today = _today(today)
    start = dt.date.fromisoformat(item["start"])
    end = dt.date.fromisoformat(item["end"])
    if today < start:
        return "upcoming"
    if today > end:
        return "expired"
    return "active"


def with_status(data: dict, today) -> list[dict]:
    """items 에 status(active|upcoming|expired)·days_left·days_until_start·region_ids 를 붙인 목록."""
    today = _today(today)
    out = []
    for it in data["items"]:
        end = dt.date.fromisoformat(it["end"])
        start = dt.date.fromisoformat(it["start"])
        row = dict(it)
        row["status"] = status(it, today)
        row["days_left"] = (end - today).days
        row["days_until_start"] = (start - today).days
        row["region_ids"] = [s for s in ALL_SGGS if matches(it.get("regions", []), s)]
        out.append(row)
    return out


def applies(item: dict, sgg: str) -> bool:
    """지정 항목이 이 sgg 에 걸리는지(region_ids 가 있으면 그것을, 없으면 패턴을 직접 비교)."""
    if "region_ids" in item and sgg in item["region_ids"]:
        return True
    return matches(item.get("regions", []), sgg)


def region_designations(items_with_status: list[dict], sgg: str, scope: str = "apt",
                        include_foreign: bool = False) -> list[dict]:
    """해당 지역에 적용되는 지정 목록(활성·예정만). scope='apt' 면 아파트 거래에 걸리는 지정만."""
    rows = []
    for it in items_with_status:
        if it["status"] == "expired" or not applies(it, sgg):
            continue
        if scope == "apt":
            if not it.get("apt_relevant", it.get("scope") in ("apt", "all")):
                continue
            if not include_foreign and it.get("target_person") == "외국인등":
                continue
        rows.append(it)
    return rows


def region_apt_status(items_with_status: list[dict], sgg: str) -> dict:
    """구 전체 아파트가 허가 대상인지 요약: {level: full|partial|none, until, days_left, designation, partial_ids, renewal}."""
    rows = [it for it in region_designations(items_with_status, sgg) if it["status"] == "active"]
    full = [it for it in rows if not it.get("partial")]
    partial = [it for it in rows if it.get("partial")]
    if full:
        best = max(full, key=lambda x: x["end"])
        return {"level": "full", "until": best["end"], "days_left": best["days_left"], "designation": best["id"],
                "title": best.get("title", ""), "renewal": best.get("renewal_status", "-"),
                "partial_ids": [p["id"] for p in partial]}
    if partial:
        best = max(partial, key=lambda x: x["end"])
        return {"level": "partial", "until": best["end"], "days_left": best["days_left"], "designation": best["id"],
                "title": best.get("title", ""), "renewal": best.get("renewal_status", "-"),
                "partial_ids": [p["id"] for p in partial]}
    return {"level": "none", "until": None, "days_left": None, "designation": None, "title": "", "renewal": "-",
            "partial_ids": []}
