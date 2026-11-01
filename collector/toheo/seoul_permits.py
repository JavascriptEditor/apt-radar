"""서울부동산정보광장 '토지거래허가 내역 조회' 수집 (서울 25개 자치구, 처리 완료 건).

확정된 요청 방식(config/seoul_permits.json 기본값):
  1) GET  /land/other/contractStatus.do   → 세션 쿠키
  2) POST /land/wsklis/getContractList.do  form sggCd(5자리)·beginDate·endDate(YYYYMMDD, 최대 62일 창)
     헤더 X-Requested-With: XMLHttpRequest, Referer: https://land.seoul.go.kr/land/
  응답 JSON: result[] 의 SGG_CD, ACC_YEAR, ACC_NO, OBJ_SEQNO, HNDL_YMD(처리일), ADDRESS('용산구 한남동 810'),
  BOBN, BUBN, JIMOK, USE_PURP, JOB_GBN_NM(허가/불허가/취소/취하/반려/미결), DEAL_END_YMD, EXEC_YMD.
  면적·층·가격·단지명·접수일은 없습니다. 지번을 급매판정 단지 인덱스(complexes.csv)와 맞춰 단지를 붙입니다.

응답이 JSON 이 아니거나 목록을 못 찾으면 토허 원본의 HTML 표 파서(parse_html)로 폴백합니다.
요청 방식 자동 탐지(discover)는 `--discover` 플래그로만 돌립니다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path

from .. import regions as base
from ..engine import now_kst, today_str
from .http import decode

log = logging.getLogger("toheo.permits")

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "seoul_permits.json"
PAGE_URL = "https://land.seoul.go.kr/land/other/contractStatus.do"

DEFAULT_FIELDS = {
    "sgg": ["SGG_CD"], "acc_year": ["ACC_YEAR"], "acc_no": ["ACC_NO"], "seq": ["OBJ_SEQNO"],
    "hndl_ymd": ["HNDL_YMD", "EXEC_YMD", "PROCESS_DE"], "address": ["ADDRESS", "ADDR"],
    "bobn": ["BOBN"], "bubn": ["BUBN"], "jimok": ["JIMOK", "LAWD_JIMOK"], "use_purp": ["USE_PURP", "PRCUSE_NM"],
    "job_gbn": ["JOB_GBN_NM", "PROCESS_RSLT_NM", "JOB_GBN"], "duty_end": ["DEAL_END_YMD"], "exec_ymd": ["EXEC_YMD"],
}
DEFAULT_CFG = {
    "page_url": PAGE_URL,
    "url": "https://land.seoul.go.kr/land/wsklis/getContractList.do", "method": "POST",
    "params": {"sggCd": "{gu5}", "beginDate": "{from:%Y%m%d}", "endDate": "{to:%Y%m%d}"},
    "headers": {"X-Requested-With": "XMLHttpRequest", "Referer": "https://land.seoul.go.kr/land/"},
    "window_days": 60,
    "fields": DEFAULT_FIELDS,
}

SEOUL_GUS = {c: base.name_of(c) for c in base.codes_for("11")}           # '11680' → '강남구'
GU5 = {n: c for c, n in SEOUL_GUS.items()}                                # '강남구' → '11680'

JIMOK = {"대", "전", "답", "임야", "도로", "잡종지", "공장용지", "창고용지", "학교용지", "주차장", "주유소용지",
         "하천", "구거", "유지", "철도용지", "공원", "체육용지", "유원지", "종교용지", "사적지", "묘지", "과수원",
         "목장용지", "광천지", "염전", "양어장", "수도용지", "제방"}
RE_DATE = re.compile(r"(\d{4})[.\-/]?\s?(\d{2})[.\-/]?\s?(\d{2})")
RE_JIBUN = re.compile(r"^(산\s*)?\d{1,5}(-\d{1,5})?$")
RE_SEQ = re.compile(r"^\d{1,5}$")
RE_DONG = re.compile(r"([가-힣0-9]+(?:동|가|로))\b")

GU_PARAMS = ["sggCd", "cggCd", "guCd", "sigunguCd", "sggCode", "cgg_code", "sgg_cd", "guCode", "gu", "sggNm"]
DATE_PARAMS = [("beginDate", "endDate"), ("startDate", "endDate"), ("sDate", "eDate"), ("fromDt", "toDt"),
               ("srchStartDt", "srchEndDt"), ("strDt", "endDt"), ("searchStartDate", "searchEndDate"), ("stDt", "edDt")]
LIST_KEYS = ("result", "list", "data", "rows", "items", "resultList")


def load_config(path: str | os.PathLike | None = None) -> dict:
    """config/seoul_permits.json(없으면 내장 기본값). fields 는 기본값 위에 덮어씁니다."""
    p = Path(path or os.environ.get("SEOUL_PERMITS_CFG") or CONFIG_PATH)
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if p.exists():
        with open(p, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if k == "fields" and isinstance(v, dict):
                cfg["fields"].update(v)
            else:
                cfg[k] = v
    return cfg


# ----------------------------------------------------------------------------- 날짜·주소

def norm_date(s) -> str:
    """'20261030' '2026-10-30' '2026.10.30' → '2026-10-30'. 못 읽으면 ''."""
    m = RE_DATE.search(str(s or ""))
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _norm_jibun(j: str) -> tuple[str, str]:
    """'316-1' → ('316','1'), '산 12' → ('산12',''), '0810' → ('810','')."""
    j = (j or "").replace(" ", "")
    bon, _, bu = j.partition("-")
    san = bon.startswith("산")
    bon = bon[1:] if san else bon
    bon = bon.lstrip("0") or ("0" if bon else "")
    bu = bu.lstrip("0")
    return ("산" + bon if san else bon), bu


def jibun_from_bobn(bobn, bubn) -> str:
    b = str(bobn or "").strip().lstrip("0")
    u = str(bubn or "").strip().lstrip("0")
    if not b:
        return ""
    return f"{b}-{u}" if u else b


def split_address(addr: str) -> tuple[str, str, str]:
    """'용산구 한남동 810' → ('용산구', '한남동', '810'). '서울특별시 강남구 대치동 316-1' 도 처리."""
    parts = [p for p in (addr or "").replace(",", " ").split() if p]
    parts = [p for p in parts if p not in ("서울특별시", "서울시", "서울")]
    gu = next((p for p in parts if p.endswith("구") and p in GU5), "")
    gi = parts.index(gu) if gu else -1
    rest = parts[gi + 1:]
    dong = next((p for p in rest if RE_DONG.search(p) and not RE_JIBUN.match(p)), "")
    jib = ""
    for i, p in enumerate(rest):
        q = p.replace("번지", "")
        if q == "산" and i + 1 < len(rest) and RE_JIBUN.match(rest[i + 1]):
            jib = "산" + rest[i + 1]
            break
        if RE_JIBUN.match(q) or re.match(r"^산\d", q):
            jib = q.replace(" ", "")
            break
    return gu, dong, jib


def _pick(rec: dict, names: list[str]):
    low = {str(k).lower(): v for k, v in rec.items()}
    for n in names:
        v = rec.get(n)
        if v in (None, ""):
            v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return None


def parse_record(rec: dict, sgg: str, fields: dict | None = None) -> dict | None:
    """응답 1건(JSON dict) → permits_seoul.csv 행. 키 alias, 주소 '구 동 지번' 분해, 날짜 정규화. 쓸모없으면 None."""
    fields = fields or DEFAULT_FIELDS
    g = lambda k: _pick(rec, fields.get(k, DEFAULT_FIELDS.get(k, [])))  # noqa: E731
    sgg_cd = str(g("sgg") or sgg or "").strip()
    if not sgg_cd:
        sgg_cd = sgg
    address = str(g("address") or "").strip()
    gu, dong, jib = split_address(address)
    bobn, bubn = str(g("bobn") or "").strip(), str(g("bubn") or "").strip()
    if not jib:
        jib = jibun_from_bobn(bobn, bubn)
    hndl = norm_date(g("hndl_ymd"))
    exec_ymd = norm_date(g("exec_ymd"))
    duty_end = norm_date(g("duty_end"))
    use_purp = str(g("use_purp") or "").strip()
    job = str(g("job_gbn") or "").strip()
    jimok = str(g("jimok") or "").strip()
    if not (hndl or exec_ymd) or not (address or jib):
        return None
    parts = [sgg_cd, str(g("acc_year") or "").strip(), str(g("acc_no") or "").strip(), str(g("seq") or "").strip()]
    key = "|".join(parts) if any(parts[1:]) else f"{address}|{hndl or exec_ymd}|{use_purp}"
    return {
        "key": key, "sgg": sgg_cd, "gu": gu or SEOUL_GUS.get(sgg_cd, ""), "dong": dong, "jibun": jib,
        "bobn": bobn, "bubn": bubn, "jimok": jimok, "hndl_ymd": hndl or exec_ymd, "use_purp": use_purp,
        "job_gbn": job, "duty_end": duty_end, "exec_ymd": exec_ymd, "address": address,
        "cid": "", "cname": "", "first_seen": "",
    }


def find_list(obj):
    """JSON 응답에서 레코드 목록(dict 의 list)을 찾는다. result/list/data 우선, 없으면 첫 dict-list."""
    if isinstance(obj, list):
        return obj if (not obj or isinstance(obj[0], dict)) else None
    if not isinstance(obj, dict):
        return None
    for k in LIST_KEYS:
        v = obj.get(k)
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return v
        if isinstance(v, dict):
            sub = find_list(v)
            if sub is not None:
                return sub
    for v in obj.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    for v in obj.values():
        if isinstance(v, dict):
            sub = find_list(v)
            if sub:
                return sub
    return None


def parse_json_text(text: str, sgg: str, fields: dict | None = None) -> list[dict] | None:
    """JSON 텍스트 → 행 목록. JSON 이 아니거나 목록이 없으면 None(→ HTML 폴백)."""
    t = (text or "").strip()
    if not (t.startswith("{") or t.startswith("[")):
        return None
    try:
        obj = json.loads(t)
    except ValueError:
        return None
    items = find_list(obj)
    if items is None:
        return None
    out = []
    for it in items:
        if isinstance(it, dict):
            row = parse_record(it, sgg, fields)
            if row:
                out.append(row)
    return out


# ----------------------------------------------------------------------------- HTML 폴백(토허 원본)

@dataclass
class Permit:
    gu: str
    dong: str
    jibun: str
    jimok: str
    permit_date: str
    purpose: str
    duty_end: str
    extra: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._row = None
        self._cell = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self.tables:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if not self._skip and self._cell is not None:
            self._cell.append(data)


def classify(tokens: list[str], gu: str) -> Permit | None:
    """셀 토큰 묶음 → Permit. 날짜 2~3개(허가일, 의무종료일), 지번, 지목, 이용목적, 동 이름을 찾아낸다."""
    toks = []
    for t in tokens:
        for part in re.split(r"\s*\|\s*", t):
            part = part.strip()
            if part:
                toks.append(part)
    dates = [norm_date(t) for t in toks if norm_date(t)]
    dates = [d for d in dates if d]
    if not dates:
        return None
    # 1) '산 12' / '316-1' 처럼 지번임이 분명한 토큰
    jibun = next((t.replace(" ", "") for t in toks if RE_JIBUN.match(t) and ("-" in t or t.startswith("산"))), "")
    if not jibun:
        # 2) 주소 문자열 끝의 지번 (예: '대치동 316')
        for t in toks:
            m = re.search(r"(?:동|가|로)\s+((?:산\s*)?\d{1,5}(?:-\d{1,5})?)\b", t)
            if m:
                jibun = m.group(1).replace(" ", "")
                break
    if not jibun:
        # 3) 숫자만 있는 토큰: 첫 토큰이 연번이면 그 다음 숫자가 지번
        nums = [(i, t) for i, t in enumerate(toks) if RE_JIBUN.match(t)]
        if len(nums) >= 2:
            jibun = nums[1][1] if nums[0][0] == 0 else nums[0][1]
        elif len(nums) == 1 and nums[0][0] != 0:
            jibun = nums[0][1]
    jimok = next((t for t in toks if t in JIMOK), "")
    if not jimok:
        m = next((re.search(r"\((대|전|답|임야|도로|잡종지)\)", t) for t in toks
                  if re.search(r"\((대|전|답|임야|도로|잡종지)\)", t)), None)
        jimok = m.group(1) if m else ""
    purpose = next((t for t in toks if re.search(r"(주거|사업|농업|임업|축산|어업|기타|현상|임대|복지|편익)\s*(용|목적)?", t)
                    and not norm_date(t)), "")
    dong = ""
    for t in toks:
        m = RE_DONG.search(t)
        if m and not norm_date(t):
            dong = m.group(1)
            break
    permit_date = dates[0]
    duty_end = dates[-1] if len(dates) >= 2 and dates[-1] > dates[0] else ""
    extra = " / ".join(t for t in toks if t not in (jibun, jimok, purpose, dong) and not norm_date(t)
                       and not RE_SEQ.match(t))[:120]
    return Permit(gu, dong, jibun, jimok, permit_date, purpose, duty_end, extra)


def parse_html(html: str, gu: str) -> list[Permit]:
    p = _Tables()
    p.feed(html)
    out: list[Permit] = []
    for table in p.tables:
        groups: list[list[str]] = []
        last_seq: int | None = None
        for cells in table:
            if not cells:
                continue
            is_header = all(not norm_date(c) and not RE_JIBUN.match(c) for c in cells) and any(
                k in " ".join(cells) for k in ("연번", "허가일자", "주소", "지목"))
            if is_header:
                continue
            first = cells[0] or ""
            seq = int(first) if RE_SEQ.match(first) else None
            # 레코드 시작: 연번이 직전 연번 ±1 이거나(첫 레코드는) 셀이 넉넉하고 날짜가 있는 행
            starts = seq is not None and (
                (last_seq is not None and abs(seq - last_seq) == 1 and len(cells) >= 4) or
                (last_seq is None and len(cells) >= 5 and any(norm_date(c) for c in cells)))
            if starts or not groups:
                groups.append(list(cells))
                if seq is not None:
                    last_seq = seq
            else:
                groups[-1].extend(cells)
        for g in groups:
            pm = classify(g, gu)
            if pm and pm.permit_date:
                out.append(pm)
    return out


def permit_to_row(pm: Permit, sgg: str) -> dict:
    """HTML 폴백 결과 → permits_seoul.csv 행(키는 주소|처리일|목적 대체키)."""
    gu = pm.gu or SEOUL_GUS.get(sgg, "")
    address = " ".join(x for x in (gu, pm.dong, pm.jibun) if x)
    bon, bu = _norm_jibun(pm.jibun)
    return {
        "key": f"{address}|{pm.permit_date}|{pm.purpose}", "sgg": sgg, "gu": gu, "dong": pm.dong, "jibun": pm.jibun,
        "bobn": bon.replace("산", ""), "bubn": bu, "jimok": pm.jimok, "hndl_ymd": pm.permit_date,
        "use_purp": pm.purpose, "job_gbn": "허가", "duty_end": pm.duty_end, "exec_ymd": "", "address": address,
        "cid": "", "cname": "", "first_seen": "",
    }


def parse_any(text: str, sgg: str, fields: dict | None = None) -> tuple[list[dict], str]:
    """응답 텍스트 → (행 목록, 'json'|'html'). JSON 실패 시 HTML 표 파서 폴백."""
    rows = parse_json_text(text, sgg, fields)
    if rows is not None:
        return rows, "json"
    return [permit_to_row(pm, sgg) for pm in parse_html(text, SEOUL_GUS.get(sgg, ""))], "html"


# ----------------------------------------------------------------------------- 클라이언트

class SeoulPermits:
    def __init__(self, http, cfg: dict | None = None, state_path: str | os.PathLike | None = None):
        self.http = http
        self.cfg = json.loads(json.dumps(DEFAULT_CFG))
        self.cfg["fields"] = dict(DEFAULT_FIELDS)
        for k, v in (cfg or {}).items():
            if k == "fields" and isinstance(v, dict):
                self.cfg["fields"].update(v)
            elif not k.startswith("_"):
                self.cfg[k] = v
        self.state_path = Path(state_path) if state_path else None
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                saved = {}
            for k in ("url", "method", "params", "headers"):
                if saved.get(k) and not (cfg or {}).get(k):
                    self.cfg[k] = saved[k]
            self.cfg["discovered"] = saved.get("discovered")
            self.cfg["checked_at"] = saved.get("checked_at")
        self._session_ok = False

    # ---- 저수준 ---------------------------------------------------------
    def save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        keep = {k: self.cfg.get(k) for k in ("url", "method", "params", "headers", "discovered", "checked_at", "error")}
        self.state_path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")

    def _ensure_session(self) -> None:
        """세션 쿠키 GET 선행(한 번)."""
        if self._session_ok:
            return
        page = self.cfg.get("page_url")
        if page:
            r = self.http.get(page)
            if getattr(r, "status_code", 200) != 200:
                raise RuntimeError(f"세션 페이지 HTTP {r.status_code}")
        self._session_ok = True

    def _fill(self, template: dict, sgg: str, frm: dt.date, to: dt.date) -> dict:
        gu = SEOUL_GUS.get(sgg, "")
        out = {}
        for k, v in template.items():
            s = str(v)
            s = s.replace("{gu5}", sgg).replace("{gu3}", sgg[-3:]).replace("{gu}", gu)
            s = re.sub(r"\{from:([^}]+)\}", lambda m: frm.strftime(m.group(1)), s)
            s = re.sub(r"\{to:([^}]+)\}", lambda m: to.strftime(m.group(1)), s)
            out[k] = s
        return out

    def _call(self, url: str, method: str, params: dict) -> str:
        headers = dict(self.cfg.get("headers") or {})
        if method.upper() == "POST":
            r = self.http.post(url, data=params, headers=headers)
        else:
            r = self.http.get(url, params=params, headers=headers)
        if getattr(r, "status_code", 200) != 200:
            raise RuntimeError(f"HTTP {r.status_code} {url}")
        ctype = (r.headers.get("Content-Type") or "") if getattr(r, "headers", None) else ""
        return r.text if "json" in ctype else decode(r)

    def query(self, sgg: str, frm: dt.date, to: dt.date) -> tuple[list[dict], str, str]:
        """한 자치구·한 기간 조회 → (행, 원문 텍스트, 'json'|'html')."""
        self._ensure_session()
        text = self._call(self.cfg["url"], self.cfg.get("method", "POST"), self._fill(self.cfg["params"], sgg, frm, to))
        rows, kind = parse_any(text, sgg, self.cfg.get("fields"))
        return rows, text, kind

    # ---- 수집 -----------------------------------------------------------
    def fetch(self, sggs: list[str] | None = None, days: int = 60, backfill_from: dt.date | str | None = None,
              today: dt.date | str | None = None) -> tuple[list[dict], dict]:
        """구별로 [today-days, today] 창 조회. backfill_from 이 있으면 60일 단위로 과거까지.

        반환 info: {ok, error, fetched, by_gu:{구:n|'오류: ..'}, raw:{sgg:[{from,to,kind,body}]}, kinds:{sgg:kind}}
        """
        today = dt.date.fromisoformat(str(today)) if today else dt.date.fromisoformat(today_str())
        window = int(self.cfg.get("window_days") or 60)
        window = max(1, min(window, 62))
        info = {"ok": True, "error": None, "fetched": 0, "by_gu": {}, "raw": {}, "kinds": {}}
        if not self.cfg.get("url"):
            info["ok"], info["error"] = False, "조회 주소(url) 설정 없음"
            return [], info
        start = today - dt.timedelta(days=max(0, days - 1))     # 오늘 포함 days 일치 → 한 창(window_days)에 맞춤
        if backfill_from:
            bf = dt.date.fromisoformat(str(backfill_from)[:10]) if not isinstance(backfill_from, dt.date) else backfill_from
            start = min(start, bf)
        targets = [s for s in (sggs or list(SEOUL_GUS)) if s in SEOUL_GUS]
        out: list[dict] = []
        try:
            self._ensure_session()
        except Exception as e:  # noqa: BLE001
            info["ok"], info["error"] = False, f"세션 페이지 실패: {e}"
            return [], info
        errors = 0
        for sgg in targets:
            gu = SEOUL_GUS[sgg]
            n = 0
            raws = []
            try:
                hi = today
                while hi >= start:
                    lo = max(start, hi - dt.timedelta(days=window - 1))
                    rows, text, kind = self.query(sgg, lo, hi)
                    body = text
                    if kind == "json":
                        try:
                            body = json.loads(text)
                        except ValueError:
                            body = text
                    raws.append({"from": lo.isoformat(), "to": hi.isoformat(), "kind": kind, "body": body})
                    info["kinds"][sgg] = kind
                    out.extend(rows)
                    n += len(rows)
                    hi = lo - dt.timedelta(days=1)
                info["by_gu"][gu] = n
            except Exception as e:  # noqa: BLE001
                info["by_gu"][gu] = f"오류: {e}"
                errors += 1
            info["raw"][sgg] = raws
        info["fetched"] = len(out)
        if targets and errors == len(targets):
            info["ok"] = False
            info["error"] = "모든 자치구 조회 실패"
        return out, info

    # ---- 탐지(--discover 전용) ------------------------------------------
    def discover(self, probe_sgg: str = "11680", force: bool = False, today: dt.date | None = None) -> dict:
        """조회 요청 방식 자동 탐지(토허 원본 포팅). 기본 설정이 동작하면 그것을 확정하고, 아니면 후보를 시도."""
        if self.cfg.get("discovered") and not force:
            return self.cfg
        today = today or dt.date.fromisoformat(today_str())
        frm = today - dt.timedelta(days=60)
        try:
            self._ensure_session()
            rows, _, kind = self.query(probe_sgg, frm, today)
            if rows:
                self.cfg["discovered"] = True
                self.cfg["checked_at"] = now_kst().isoformat(timespec="seconds")
                self.cfg.pop("error", None)
                return self.cfg
        except Exception as e:  # noqa: BLE001
            log.info("기본 설정 실패: %s", e)
        html = ""
        try:
            html = decode(self.http.get(self.cfg.get("page_url") or PAGE_URL))
        except Exception:  # noqa: BLE001
            pass
        tried = 0
        for url in self._endpoints(html):
            for method in ("POST", "GET"):
                for gp in GU_PARAMS:
                    for gv in ("{gu5}", "{gu3}", "{gu}"):
                        for dp in DATE_PARAMS:
                            for fmt in ("%Y%m%d", "%Y-%m-%d"):
                                tmpl = {gp: gv, dp[0]: "{from:" + fmt + "}", dp[1]: "{to:" + fmt + "}"}
                                tried += 1
                                if tried > 400:
                                    break
                                try:
                                    text = self._call(url, method, self._fill(tmpl, probe_sgg, frm, today))
                                except Exception:  # noqa: BLE001
                                    continue
                                rows, kind = parse_any(text, probe_sgg, self.cfg.get("fields"))
                                if len(rows) >= 2 and all(r["hndl_ymd"] >= frm.isoformat() for r in rows[:5]):
                                    self.cfg.update({"url": url, "method": method, "params": tmpl, "discovered": True,
                                                     "checked_at": now_kst().isoformat(timespec="seconds")})
                                    self.cfg.pop("error", None)
                                    return self.cfg
        self.cfg["error"] = "조회 요청 방식을 찾지 못함 — config/seoul_permits.json 을 확인하세요"
        self.cfg["checked_at"] = now_kst().isoformat(timespec="seconds")
        self.cfg["tried"] = tried
        return self.cfg

    def _endpoints(self, html: str) -> list[str]:
        urls = [self.cfg.get("url") or DEFAULT_CFG["url"]]
        for m in re.finditer(r"""['"]([^'"\s]*?/land/[^'"\s]*?\.do)['"]""", html or ""):
            u = m.group(1)
            if not u.startswith("http"):
                u = "https://land.seoul.go.kr" + (u if u.startswith("/") else "/" + u)
            if u not in urls and re.search(r"ontract|ermit|tatus|lnd|land", u, re.I):
                urls.append(u)
        return urls[:8]


# ----------------------------------------------------------------------------- 누적·매칭

def merge_permits(existing: dict[str, dict], new: list[dict], today: str | dt.date | None = None) -> int:
    """key 기준으로 중복 없이 누적. 기존 행의 빈 칸은 새 값으로 채움. 반환: 추가 건수."""
    today = str(today or today_str())[:10]
    added = 0
    for row in new:
        k = row.get("key")
        if not k:
            continue
        cur = existing.get(k)
        if cur is None:
            r = dict(row)
            r["first_seen"] = r.get("first_seen") or today
            existing[k] = r
            added += 1
            continue
        for f, v in row.items():
            if f in ("cid", "cname", "first_seen"):
                continue
            if v and not cur.get(f):
                cur[f] = v
        if row.get("job_gbn") and row["job_gbn"] != cur.get("job_gbn"):
            cur["job_gbn"] = row["job_gbn"]     # 미결 → 허가 등 처리 결과 갱신
    return added


def match_permits(permits: dict[str, dict], complexes: dict[str, dict], live: dict | None = None) -> int:
    """허가 행에 단지(cid, cname)를 채움. (묶음, 법정동, 지번) 정확 일치 → (묶음, 법정동, 본번) → nTrades 최다.

    complexes: 급매판정 complexes.csv (cid → {sgg, umd, jibun, name, nTrades}). 반환: cid 가 채워진 허가 건수.
    """
    by_exact: dict[tuple[str, str, str], list[dict]] = {}
    by_bon: dict[tuple[str, str, str], list[dict]] = {}
    for c in complexes.values():
        sgg, umd, jib = c.get("sgg", ""), (c.get("umd") or "").strip(), (c.get("jibun") or "").strip()
        if not (sgg and umd):
            continue
        grp = base.group_of(sgg, live)
        bon, bu = _norm_jibun(jib)
        by_exact.setdefault((grp, umd, bon + ("-" + bu if bu else "")), []).append(c)
        by_bon.setdefault((grp, umd, bon), []).append(c)
    n = 0
    for p in permits.values():
        if p.get("cid"):
            if p["cid"] in complexes:
                n += 1
                continue
            p["cid"], p["cname"] = "", ""   # 사라진 단지 → 다시 매칭
        if not p.get("dong") or not p.get("jibun"):
            continue
        grp = base.group_of(p.get("sgg", ""), live)
        bon, bu = _norm_jibun(p["jibun"])
        cands = by_exact.get((grp, p["dong"], bon + ("-" + bu if bu else ""))) or by_bon.get((grp, p["dong"], bon))
        if not cands:
            continue
        best = max(cands, key=lambda c: int(float(c.get("nTrades") or 0)))
        p["cid"], p["cname"] = best["cid"], best.get("name", "")
        n += 1
    return n
