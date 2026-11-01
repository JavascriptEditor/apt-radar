"""텔레그램 알림(선택 기능, DESIGN.md 3절 `collector/notify.py`).

`python -m collector export --out site --notify` 끝에 `send_digest(engine, site_url)` 가 불린다.

- 환경변수 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 가 없으면 아무것도 하지 않고 `{"skipped": True}` 를 돌려준다.
- 구독 설정은 `config/alerts.json`(예시: `config/alerts.example.json`). 파일이 없으면 환경변수 `ALERTS_JSON`(JSON 문자열)을,
  그것도 없으면 기본값(구독 없음 → 보낼 것 없음)을 쓴다.
    {"complexes": [cid...], "regions": [sgg...], "minGrade": "S", "maxPrice": null, "grps": [],
     "toheo": {"permitsForComplexes": true, "designationDdays": [30, 7]}}
- 한 번 실행에 **메시지 1통**(요약)만 보낸다. 급매 매물·관심 단지 허가·지정 만료 D-day 를 한 통에 묶고,
  항목이 많으면 상위 몇 건 + "외 n건" 으로 줄인다(호갱노노 리뷰의 '알림 폭탄' 불만 반영).
- 이미 보낸 매물 id·허가 key·D-day 는 `data/notify_state.json` 에 남겨 중복 전송을 막는다. 전송이 실패하면 상태를
  갱신하지 않아 다음 실행에서 다시 시도한다.

이 모듈은 import 만으로는 아무 부작용이 없다(네트워크·파일 접근은 send_digest 안에서만).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
from typing import Callable

from . import regions
from .engine import Engine, today_str
from .judge import judge_listing

STATE_FILE = "notify_state.json"          # DATA_DIR 아래
STATE_KEEP_DAYS = 180                     # 보낸 기록 보관 기간
MAX_LISTINGS = 12                         # 메시지에 적는 매물 최대 수
MAX_PERMITS = 12                          # 메시지에 적는 단지(허가) 최대 수
TELEGRAM_API = "https://api.telegram.org"  # 테스트에서는 TELEGRAM_API_BASE 로 바꿈
TELEGRAM_MAX_CHARS = 4000                 # 텔레그램 한도 4096 보다 조금 작게
GRADE_ORDER = {"SS": 0, "S": 1, "A": 2, "B": 3, "C": 4}

DEFAULT_CFG: dict = {
    "complexes": [], "regions": [], "minGrade": "S", "maxPrice": None, "grps": [],
    "toheo": {"permitsForComplexes": True, "designationDdays": [30, 7]},
}


# ----------------------------------------------------------------------------- 설정·상태
def load_alerts(cfg_path: str | os.PathLike | None = "config/alerts.json") -> dict:
    """구독 설정. 파일 → ALERTS_JSON 환경변수 → 기본값 순. 잘못된 JSON 은 기본값으로 취급."""
    raw: dict | None = None
    p = Path(cfg_path) if cfg_path else None
    if p and p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raw = None
    if raw is None and os.environ.get("ALERTS_JSON"):
        try:
            raw = json.loads(os.environ["ALERTS_JSON"])
        except ValueError:
            raw = None
    cfg = json.loads(json.dumps(DEFAULT_CFG))  # 깊은 복사
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "toheo" and isinstance(v, dict):
                cfg["toheo"].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    cfg["complexes"] = [str(x) for x in (cfg.get("complexes") or [])]
    cfg["regions"] = [str(x) for x in (cfg.get("regions") or [])]
    cfg["grps"] = [str(x) for x in (cfg.get("grps") or [])]
    return cfg


def _load_state(store) -> dict:
    st = store.read_json(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    for k in ("listings", "permits", "ddays"):
        if not isinstance(st.get(k), dict):
            st[k] = {}
    return st


def _prune_state(state: dict, today: str) -> None:
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=STATE_KEEP_DAYS)).isoformat()
    for k in ("listings", "permits", "ddays"):
        state[k] = {i: d for i, d in state[k].items() if (d or "") >= cutoff}


# ----------------------------------------------------------------------------- 선별
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


def _grade_ok(grade: str | None, min_grade: str | None) -> bool:
    if not min_grade:
        return True
    if not grade:
        return False
    return GRADE_ORDER.get(grade, 9) <= GRADE_ORDER.get(min_grade, 9)


def pick_listings(engine: Engine, cfg: dict, state: dict) -> list[dict]:
    """구독 조건(관심 단지·지역, 등급, 예산, 평형)에 맞고 아직 보내지 않은 active 매물.

    등급은 기준가 모델로 그 자리에서 판정한다(export 와 같은 judge_listing). 모델이 없는 단지는 minGrade 가 비어 있을 때만.
    """
    want_cx = set(cfg.get("complexes") or [])
    want_sgg = set(regions.parse_filter(",".join(cfg["regions"]), engine.live)) if cfg.get("regions") else set()
    if not want_cx and not want_sgg:
        return []
    min_grade = cfg.get("minGrade") or ""
    max_price = _int(cfg.get("maxPrice"))
    grps = set(cfg.get("grps") or [])
    sent = state["listings"]
    models: dict[str, dict | None] = {}
    out = []
    for aid, l in engine.store.read_listings(active_only=True).items():
        if aid in sent:
            continue
        c = engine.complexes.get(l.get("cid") or "")
        if not c:
            continue
        if l["cid"] not in want_cx and c.get("sgg") not in want_sgg:
            continue
        price = _int(l.get("price"))
        if price is None or (max_price and price > max_price):
            continue
        if grps and str(_int(l.get("grp")) or "") not in grps:
            continue
        grp = engine.group_of(c.get("sgg", ""))
        if grp not in models:
            models[grp] = engine.store.read_model(grp)
        m = models[grp]
        l2 = {**l, "price": price, "grp": _int(l.get("grp")), "floor": _int(l.get("floor")), "top": _int(l.get("top")),
              "area": _float(l.get("area")), "sameCnt": _int(l.get("sameCnt")) or 0}
        j = judge_listing(m, l["cid"], l2) if m else None
        if not _grade_ok(j["grade"] if j else None, min_grade):
            continue
        out.append({"id": aid, "cid": l["cid"], "name": c.get("name", ""), "sgg": c.get("sgg", ""),
                    "grp": l2["grp"], "dong": l.get("dong", ""), "floor": l2["floor"], "price": price,
                    "grade": j["grade"] if j else "", "diff": j["diffPct"] if j else None,
                    "conf": j["conf"] if j else "", "first": l.get("firstSeen", "")})
    out.sort(key=lambda x: (GRADE_ORDER.get(x["grade"], 9), x["diff"] if x["diff"] is not None else 9))
    return out


def pick_permits(engine: Engine, cfg: dict, state: dict) -> list[dict]:
    """관심 단지에 새로 매칭된 서울 허가(JOB_GBN_NM=='허가') — 단지별로 묶어 돌려준다."""
    if not (cfg.get("toheo") or {}).get("permitsForComplexes", True):
        return []
    want_cx = set(cfg.get("complexes") or [])
    if not want_cx:
        return []
    from .toheo.store import ToheoStore
    tst = ToheoStore(Path(engine.store.root) / "toheo")
    sent = state["permits"]
    by_cx: dict[str, dict] = {}
    for key, p in tst.load_permits().items():
        cid = p.get("cid") or ""
        if cid not in want_cx or key in sent or (p.get("job_gbn") or "허가") != "허가":
            continue
        c = engine.complexes.get(cid, {})
        g = by_cx.setdefault(cid, {"cid": cid, "name": c.get("name") or p.get("cname") or cid, "sgg": c.get("sgg", p.get("sgg", "")),
                                   "keys": [], "dates": [], "purposes": {}})
        g["keys"].append(key)
        g["dates"].append(p.get("hndl_ymd", ""))
        purp = p.get("use_purp") or "-"
        g["purposes"][purp] = g["purposes"].get(purp, 0) + 1
    out = list(by_cx.values())
    for g in out:
        g["n"] = len(g["keys"])
        g["last"] = max(g["dates"]) if g["dates"] else ""
    out.sort(key=lambda g: (g["last"], g["n"]), reverse=True)
    return out


def pick_ddays(engine: Engine, cfg: dict, state: dict, today: str) -> list[dict]:
    """관심 지역(관심 단지의 구 포함)에 걸린 아파트 관련 지정 중 만료 D-day 가 설정값(30, 7 …)과 같은 것."""
    ddays = [int(d) for d in ((cfg.get("toheo") or {}).get("designationDdays") or []) if str(d).lstrip("-").isdigit()]
    if not ddays:
        return []
    from .toheo import designations
    try:
        items = designations.with_status(designations.load(), today)
    except (OSError, ValueError, KeyError):
        return []
    want_sgg = set(regions.parse_filter(",".join(cfg["regions"]), engine.live)) if cfg.get("regions") else set()
    for cid in cfg.get("complexes") or []:
        c = engine.complexes.get(cid)
        if c and c.get("sgg"):
            want_sgg.add(c["sgg"])
    sent = state["ddays"]
    out = []
    for it in items:
        if it.get("status") != "active" or not it.get("apt_relevant", it.get("scope") in ("apt", "all")):
            continue
        if it.get("target_person") == "외국인등":
            continue
        if it.get("days_left") not in ddays:
            continue
        if want_sgg and not any(designations.applies(it, s) for s in want_sgg):
            continue
        key = f"{it.get('id')}|D-{it['days_left']}"
        if key in sent:
            continue
        out.append({"key": key, "id": it.get("id"), "title": it.get("title", ""), "end": it.get("end", ""),
                    "daysLeft": it["days_left"], "renewal": it.get("renewal_status", "-"),
                    "regions": len(it.get("region_ids") or [])})
    out.sort(key=lambda x: x["daysLeft"])
    return out


def build_digest(engine: Engine, cfg: dict, state: dict, today: str) -> dict:
    return {"listings": pick_listings(engine, cfg, state), "permits": pick_permits(engine, cfg, state),
            "ddays": pick_ddays(engine, cfg, state, today)}


# ----------------------------------------------------------------------------- 메시지
def fmt_price(p) -> str:
    p = _int(p)
    if p is None:
        return "-"
    eok, man = divmod(p, 10000)
    if eok and man:
        return f"{eok}억 {man:,}"
    return f"{eok}억" if eok else f"{man:,}"


def _cx_link(site_url: str, cid: str, name: str) -> str:
    name = html.escape(name or cid)
    if site_url:
        return f'<a href="{html.escape(site_url)}/#c-{html.escape(cid)}">{name}</a>'
    return name


def format_message(digest: dict, site_url: str, today: str, live: dict | None = None) -> str:
    """텔레그램 HTML 한 통. 항목이 없으면 빈 문자열."""
    ls, ps, ds = digest.get("listings") or [], digest.get("permits") or [], digest.get("ddays") or []
    if not ls and not ps and not ds:
        return ""
    lines = [f"<b>APT 레이더</b> {today} 요약"]
    if ls:
        lines.append("")
        lines.append(f"<b>급매 매물 {len(ls)}건</b> (자체 등급, 호가 기준)")
        for l in ls[:MAX_LISTINGS]:
            where = " ".join(x for x in (f"{l['dong']}동" if l.get("dong") else "", f"{l['floor']}층" if l.get("floor") else "") if x)
            diff = f" {l['diff'] * 100:+.1f}%" if l.get("diff") is not None else ""
            grade = f"[{l['grade']}]" if l.get("grade") else "[판정없음]"
            lines.append(f"• {grade} {_cx_link(site_url, l['cid'], l['name'])} {l.get('grp') or '-'}㎡ {where} "
                         f"{fmt_price(l['price'])}{diff}".replace("  ", " "))
        if len(ls) > MAX_LISTINGS:
            lines.append(f"  … 외 {len(ls) - MAX_LISTINGS}건 (사이트 알림 탭)")
    if ps:
        lines.append("")
        lines.append(f"<b>관심 단지 허가 완료</b> {sum(g['n'] for g in ps)}건 (서울시 공개 자료, 평형·가격 없음)")
        for g in ps[:MAX_PERMITS]:
            purp = ", ".join(f"{k} {v}" for k, v in sorted(g["purposes"].items(), key=lambda kv: -kv[1]))
            lines.append(f"• {_cx_link(site_url, g['cid'], g['name'])} {g['n']}건 ({html.escape(purp)}) 처리일 {g['last']}")
        if len(ps) > MAX_PERMITS:
            lines.append(f"  … 외 {len(ps) - MAX_PERMITS}개 단지")
    if ds:
        lines.append("")
        lines.append("<b>토허구역 지정 만료</b>")
        for d in ds:
            lines.append(f"• D-{d['daysLeft']} {html.escape(d['title'])} (~{d['end']}, {html.escape(d['renewal'] or '-')})")
    if site_url:
        lines.append("")
        lines.append(f'<a href="{html.escape(site_url)}/#alerts">알림 탭 열기</a>')
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[:TELEGRAM_MAX_CHARS - 2] + " …"
    return text


# ----------------------------------------------------------------------------- 전송
def send_telegram(token: str, chat_id: str, text: str, api_base: str | None = None, timeout: int = 20) -> dict:
    """Telegram Bot API sendMessage. 반환 {"ok": bool, "error": str|None, "status": int|None}."""
    import requests  # 지연 import: import 만으로는 부작용 없음

    base = (api_base or os.environ.get("TELEGRAM_API_BASE") or TELEGRAM_API).rstrip("/")
    url = f"{base}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:  # 네트워크 오류
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "status": None}
    try:
        body = r.json()
    except ValueError:
        body = {}
    ok = bool(r.status_code == 200 and body.get("ok", True))
    err = None if ok else (body.get("description") or f"HTTP {r.status_code}")
    return {"ok": ok, "error": err, "status": r.status_code}


def send_digest(engine: Engine, site_url: str | None, cfg_path: str | os.PathLike | None = "config/alerts.json",
                *, sender: Callable[[str], dict] | None = None, today: str | None = None) -> dict:
    """요약 알림 1통 전송(DESIGN 3절 계약).

    - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없으면 {"skipped": True, "reason": ...}.
    - 보낼 항목이 없으면 {"skipped": True, "reason": "nothing"} (상태 파일은 정리만).
    - 그 외 {"sent": bool, "listings": n, "permits": n(허가 건수), "ddays": n, "chars": n, "error": str|None}.
    `sender(text) -> {"ok": bool, ...}` 를 넘기면 텔레그램 대신 그것을 호출한다(테스트용).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if sender is None and (not token or not chat_id):
        return {"skipped": True, "reason": "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음"}
    today = today or today_str()
    cfg = load_alerts(cfg_path)
    state = _load_state(engine.store)
    _prune_state(state, today)
    digest = build_digest(engine, cfg, state, today)
    text = format_message(digest, (site_url or os.environ.get("SITE_URL") or "").rstrip("/"), today, engine.live)
    n_permits = sum(g["n"] for g in digest["permits"])
    if not text:
        engine.store.write_json(STATE_FILE, state)
        return {"skipped": True, "reason": "nothing", "listings": 0, "permits": 0, "ddays": 0}
    res = sender(text) if sender else send_telegram(token, chat_id, text)
    out = {"sent": bool(res.get("ok")), "listings": len(digest["listings"]), "permits": n_permits,
           "ddays": len(digest["ddays"]), "chars": len(text), "error": res.get("error")}
    if out["sent"]:
        # 성공했을 때만 '보냈음' 기록 (메시지에 다 못 적은 항목도 요약에 포함됐으므로 전부 기록)
        for l in digest["listings"]:
            state["listings"][l["id"]] = today
        for g in digest["permits"]:
            for k in g["keys"]:
                state["permits"][k] = today
        for d in digest["ddays"]:
            state["ddays"][d["key"]] = today
        state["lastSentAt"] = today
        engine.store.write_json(STATE_FILE, state)
    return out
