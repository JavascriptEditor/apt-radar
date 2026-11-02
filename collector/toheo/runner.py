"""토허 수집 오케스트레이션: run_toheo(light/deep).

1. 새올: 대상 Site 마다 fetch(light: stop_before=today-LIGHT_DAYS, deep: today-DEEP_DAYS) → merge_rows → 저장.
2. 서울 허가(permits=True 이고 서울 코드 포함): SeoulPermits.fetch → raw 보관 → merge_permits → match_permits(complexes).
3. fetch_log['toheo'] 갱신, toheo_progress.json runs 추가. 어떤 사이트 실패도 전체를 중단시키지 않는다.

환경변수: TOHEO_DELAY_SEC(0.8) LIGHT_DAYS(2) DEEP_DAYS(45) MAX_PAGES PERMITS(1)
          TOHEO_REGION_FILTER(없으면 REGION_FILTER 중 토허 대상만) PERMITS_DAYS(60) PERMITS_BACKFILL_FROM
          TOHEO_RAW_KEEP_DAYS(120)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

from ..engine import now_kst
from . import cases as cs
from . import regions as tr
from .http import Polite
from .saeol import SaeolClient
from .seoul_permits import SeoulPermits, load_config, match_permits, merge_permits
from .store import ToheoStore

log = logging.getLogger("toheo.runner")
ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = ROOT / "config"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def load_manual_params(config_dir: str | os.PathLike | None = None) -> dict:
    """config/saeol_params.json(수동 지정)을 읽는다. 없으면 {}."""
    p = Path(config_dir or CONFIG_DIR) / "saeol_params.json"
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def target_sggs(region_filter: str | None, live: dict | None = None) -> list[str]:
    """TOHEO_REGION_FILTER → REGION_FILTER → 전체 순으로 해석한 토허 대상 sgg 목록."""
    spec = region_filter if region_filter is not None else (
        os.environ.get("TOHEO_REGION_FILTER") or os.environ.get("REGION_FILTER"))
    return tr.toheo_sggs(spec, live)


def run_toheo_sites(*, deep: bool, data_dir: str | os.PathLike, region_filter: str | None,
                    complexes: dict[str, dict], fetch_log: dict, permits: bool = True, http=None,
                    live: dict | None = None, now: dt.datetime | None = None, discover: bool = False,
                    max_pages: int | None = None, config_dir: str | os.PathLike | None = None) -> dict:
    """engine 없이도 도는 저수준 실행. complexes(cid→행)·fetch_log 는 호출자가 저장한다.

    반환: {"at", "deep", "sites", "new", "completed_observed", "errors", "http_calls", "permits": {...}|None,
           "site_info": {site.id: {...}}}
    """
    now = now or now_kst()
    today = now.date()
    today_s = today.isoformat()
    store = ToheoStore(Path(data_dir) / "toheo")
    http = http or Polite()
    cases = store.load_cases()
    if not cases:
        deep = True                      # 첫 실행은 항상 깊게
    light_days = _env_int("LIGHT_DAYS", 2)
    deep_days = _env_int("DEEP_DAYS", 45)
    stop_before = today - dt.timedelta(days=deep_days if deep else light_days)
    pages = max_pages or _env_int("MAX_PAGES", 400 if deep else 40)

    params = store.load_params()
    for k, v in load_manual_params(config_dir).items():
        params[k] = {**params.get(k, {}), **v}
    client = SaeolClient(http, params)

    # 실행 시간 상한(TOHEO_MAX_SEC, 0=없음). Polite 가 있으면 호출 단계에서도 지키고, 사이트 루프에서는 남은 사이트를 건너뛴다.
    max_sec = _env_int("TOHEO_MAX_SEC", 0)
    t0 = time.monotonic()
    if hasattr(http, "set_max_sec"):
        http.set_max_sec(max_sec)

    def past_deadline() -> bool:
        return max_sec > 0 and time.monotonic() - t0 > max_sec

    sggs = target_sggs(region_filter, live)
    sites = tr.sites_for(sggs)
    tlog = fetch_log.setdefault("toheo", {})
    tlog.setdefault("sites", {})
    run_rec = {"at": now.isoformat(timespec="seconds"), "deep": deep, "sites": len(sites), "new": 0,
               "completed_observed": 0, "errors": 0, "http_calls": 0, "skipped": 0, "max_sec": max_sec}
    site_info: dict[str, dict] = {}
    for site in sites:
        if past_deadline():
            # 남은 사이트는 이번 실행에서 건너뛴다. 직전 성공 기록(host·pages 등)은 지우지 않고 skipped 표시만 얹는다.
            prev = tlog["sites"].get(site.id, {})
            tlog["sites"][site.id] = {**prev, "skipped": f"시간 상한(TOHEO_MAX_SEC={max_sec}s) — 다음 실행에서 이어감",
                                      "skippedAt": now.isoformat(timespec="seconds")}
            site_info[site.id] = tlog["sites"][site.id]
            run_rec["skipped"] += 1
            continue
        try:
            if discover:
                client.discover(site, force=True)
            rows, info = client.fetch(site, stop_before, max_pages=pages)
        except Exception as e:  # noqa: BLE001
            rows, info = [], {"site": site.id, "error": str(e), "pages": 0, "rows": 0, "toheo_rows": 0,
                              "reached_cutoff": False, "host": None, "warning": None}
        covered = stop_before if info.get("reached_cutoff") else None
        try:
            st = cs.merge_rows(cases, site, rows, now, covered, bool(info.get("reached_cutoff")),
                               close_after_days=deep_days + _env_int("CLOSE_OPEN_AFTER_DAYS", 30))
        except Exception as e:  # noqa: BLE001
            st = {"new": 0, "updated": 0, "completed_observed": 0, "dropped": 0, "closed_out": 0}
            info["error"] = f"merge 실패: {e}"
        entry = {
            "at": now.isoformat(timespec="seconds"), "ok": not info.get("error"), "host": info.get("host"),
            "pages": info.get("pages"), "rows": info.get("rows"), "toheo_rows": info.get("toheo_rows"),
            "reached_cutoff": info.get("reached_cutoff"), "error": info.get("error"),
            "warning": info.get("warning"), "deep": deep, **st,
        }
        tlog["sites"][site.id] = entry
        site_info[site.id] = entry
        run_rec["new"] += st["new"]
        run_rec["completed_observed"] += st["completed_observed"]
        run_rec["errors"] += 1 if info.get("error") else 0
        log.info("%s(%s) host=%s pages=%s rows=%s toheo=%s new=%s done=%s err=%s", site.name, site.id,
                 info.get("host"), info.get("pages"), info.get("rows"), info.get("toheo_rows"), st["new"],
                 st["completed_observed"], info.get("error") or "")
        store.save_params(client.params)
        store.save_cases(cases)

    # 서울 허가 내역 — 어떤 단계가 죽어도 runs/progress 기록은 남긴다
    perm_rec = None
    want_permits = permits and (os.environ.get("PERMITS") or "1") == "1"
    seoul = [s for s in sggs if tr.is_seoul(s)]
    if want_permits and seoul:
        if past_deadline():
            perm_rec = {"at": now.isoformat(timespec="seconds"), "ok": False,
                        "error": f"시간 상한(TOHEO_MAX_SEC={max_sec}s) — 이번 실행에서 건너뜀",
                        "fetched": 0, "added": 0, "matched": 0, "by_gu": {}, "total": None, "kinds": {}, "skipped": True}
            run_rec["skipped"] += 1
        else:
            try:
                perm_rec = run_permits(store, http, seoul, complexes, now, live=live, discover=discover,
                                       config_dir=config_dir)
            except Exception as e:  # noqa: BLE001
                log.exception("permits 실패")
                perm_rec = {"at": now.isoformat(timespec="seconds"), "ok": False, "error": f"{type(e).__name__}: {e}",
                            "fetched": 0, "added": 0, "matched": 0, "by_gu": {}, "total": None, "kinds": {}}
        tlog["permits"] = perm_rec
        run_rec["errors"] += 0 if perm_rec.get("ok") else 1
    run_rec["http_calls"] = getattr(http, "calls", 0)
    run_rec["sec"] = int(time.monotonic() - t0)
    # 접속 불가 호스트·계열(해외 IP 차단 추정) 요약 — 사이트 내정보에서 '왜 0건인지' 보이게
    if hasattr(http, "summary"):
        net = http.summary()
        run_rec["unreachable"] = sorted(net.get("unreachable", {}))
        run_rec["blockedFamilies"] = net.get("blockedFamilies", {})
        tlog["network"] = {"at": run_rec["at"], **net}
    tlog.setdefault("runs", []).append(dict(run_rec))
    tlog["runs"] = tlog["runs"][-20:]
    tlog["at"] = run_rec["at"]
    prog = store.load_progress()
    prog.setdefault("runs", []).append(dict(run_rec))
    prog["updatedAt"] = run_rec["at"]
    prog["lastDeep"] = today_s if deep else prog.get("lastDeep")
    store.save_progress(prog)
    keep = _env_int("TOHEO_RAW_KEEP_DAYS", 120)
    if keep > 0:
        store.prune_raw(keep, today_s)
    return {**run_rec, "permits": perm_rec, "site_info": site_info}


def run_permits(store: ToheoStore, http, sggs: list[str], complexes: dict[str, dict], now: dt.datetime,
                live: dict | None = None, discover: bool = False, config_dir: str | os.PathLike | None = None,
                days: int | None = None) -> dict:
    """서울 허가 내역 수집 한 번: fetch → raw 보관 → merge → match → 저장. fetch_log['toheo'].permits 형식 반환.
    설정 로드·병합·매칭·저장 어느 단계가 실패해도 예외를 밖으로 내지 않고 ok=False, error 로 기록한다."""
    today_s = now.date().isoformat()
    rec = {"at": now.isoformat(timespec="seconds"), "ok": False, "error": None, "fetched": 0, "added": 0, "matched": 0,
           "by_gu": {}, "total": None, "kinds": {}}
    cfg_path = Path(config_dir or CONFIG_DIR) / "seoul_permits.json"
    try:
        cfg = load_config(cfg_path if cfg_path.exists() else None)
    except (ValueError, OSError) as e:
        rec["error"] = f"설정 로드 실패({cfg_path.name}): {e}"
        log.error("%s", rec["error"])
        return rec
    sp = SeoulPermits(http, cfg, state_path=store.permit_cfg_path())
    if discover:
        try:
            sp.discover(force=True, today=now.date())
            sp.save_state()
        except Exception as e:  # noqa: BLE001
            log.warning("discover 실패: %s", e)
    days = days or _env_int("PERMITS_DAYS", int(cfg.get("window_days") or 60))
    backfill = os.environ.get("PERMITS_BACKFILL_FROM") or None
    try:
        rows, info = sp.fetch(sggs, days=days, backfill_from=backfill, today=now.date())
    except Exception as e:  # noqa: BLE001
        rows, info = [], {"ok": False, "error": str(e), "by_gu": {}, "raw": {}, "fetched": 0, "kinds": {}}
    for sgg, raws in (info.get("raw") or {}).items():
        if raws:
            try:
                store.save_raw(today_s, sgg, {"sgg": sgg, "date": today_s, "windows": raws})
            except OSError as e:  # pragma: no cover
                log.warning("raw 저장 실패 %s: %s", sgg, e)
    added = matched = 0
    permits: dict[str, dict] = {}
    try:
        permits = store.load_permits()
        added = merge_permits(permits, rows, today_s)
        matched = match_permits(permits, complexes or {}, live)
        store.save_permits(permits)
    except Exception as e:  # noqa: BLE001
        log.exception("permits 병합/매칭/저장 실패")
        info["ok"] = False
        info["error"] = f"병합/매칭/저장 실패: {type(e).__name__}: {e}"
    if not info.get("ok"):
        sp.cfg["error"] = info.get("error")
    try:
        sp.save_state()
    except OSError as e:  # pragma: no cover
        log.warning("허가 설정 저장 실패: %s", e)
    rec.update({"ok": bool(info.get("ok")), "error": info.get("error"), "fetched": len(rows), "added": added,
                "matched": matched, "by_gu": info.get("by_gu", {}), "total": len(permits), "kinds": info.get("kinds", {})})
    log.info("permits ok=%s fetched=%s added=%s matched=%s err=%s", rec["ok"], len(rows), added, matched,
             info.get("error") or "")
    return rec


def run_toheo(engine, *, deep: bool, data_dir: str | os.PathLike | None = None, region_filter: str | None = None,
              permits: bool = True, http=None, discover: bool = False) -> dict:
    """급매판정 Engine 과 함께 도는 상위 실행. engine.complexes/engine.fetch_log 를 쓰고 engine._save() 로 저장."""
    data_dir = Path(data_dir) if data_dir else engine.store.root
    spec = region_filter
    if spec is None:
        spec = os.environ.get("TOHEO_REGION_FILTER") or os.environ.get("REGION_FILTER") or None
    res = run_toheo_sites(deep=deep, data_dir=data_dir, region_filter=spec, complexes=engine.complexes,
                          fetch_log=engine.fetch_log, permits=permits, http=http, live=getattr(engine, "live", None),
                          discover=discover)
    engine._save()
    return res
