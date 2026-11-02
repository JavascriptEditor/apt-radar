"""새올 전자민원창구 '서식민원 처리 공개' 목록 수집 (토허 원본 포팅).

페이지 구조(모든 지자체 공통):
    | 접수번호 | 접수일자 | 민원사무명 | 처리기간 | 처리상태 | 처리부서 |
    총 N 건 / 맨 처음페이지 이전 페이지 1 2 3 ... 다음 페이지 마지막 페이지

페이지 이동 파라미터 이름은 지자체 시스템 버전에 따라 다를 수 있어 처음 한 번 자동 탐지하고
data/toheo/saeol_params.json 에 기억합니다(실패하면 config/saeol_params.json 으로 직접 지정 가능).
단지 정보는 없습니다(접수번호·접수일·민원명·처리기한·상태·처리부서 6열뿐). '처리중'부터 노출됩니다.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qsl

from ..engine import now_kst
from .http import HttpError, decode
from .regions import LIST_PATH, Site

log = logging.getLogger("toheo.saeol")

RE_RCPT = re.compile(r"^\d{12,22}$")
RE_DATE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
RE_TOTAL = re.compile(r"총\s*([\d,]+)\s*건")
KEYWORD = "토지거래"

PAGE_PARAMS = ["pageIndex", "pageNo", "page", "curPage", "currentPage", "pageNum", "cpage", "pageNumber", "pageidx"]
SIZE_PARAMS = ["pageSize", "ofr_pageSize", "pageUnit", "recordCountPerPage", "pageRowCnt", "rowCnt", "listSize", "pageSz"]
WANT_SIZE = 100


@dataclass
class Row:
    no: str
    received: str   # YYYY-MM-DD
    name: str
    deadline: str   # YYYY-MM-DD or ''
    status: str
    dept: str

    def to_dict(self) -> dict:
        return asdict(self)


class _Tables(HTMLParser):
    """모든 <table>의 행/셀 텍스트를 뽑는 단순 파서(중첩 표는 안쪽 셀만 평탄화)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.text_parts: list[str] = []
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
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("td", "th") and self._cell is not None:
            txt = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            if self._row is not None:
                self._row.append(txt)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self.tables:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)


def norm_date(s: str) -> str:
    m = RE_DATE.search(s or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def parse_list(html: str) -> tuple[list[Row], int | None]:
    """목록 HTML → (행 목록, 총 건수). 셀 순서는 접수번호·접수일자·민원사무명·처리기간·처리상태·처리부서."""
    p = _Tables()
    p.feed(html)
    rows: list[Row] = []
    for table in p.tables:
        for cells in table:
            if len(cells) < 6:
                continue
            c = [x.strip() for x in cells]
            # 접수번호 셀 찾기(앞에 체크박스/번호 열이 끼어 있을 수 있음)
            idx = next((i for i, x in enumerate(c) if RE_RCPT.match(x)), None)
            if idx is None or len(c) - idx < 6:
                continue
            no, rcpt, name, dead, status, dept = c[idx:idx + 6]
            if not RE_DATE.search(rcpt):
                continue
            rows.append(Row(no, norm_date(rcpt), name, norm_date(dead), status, dept))
    text = "".join(p.text_parts)
    m = RE_TOTAL.search(text)
    total = int(m.group(1).replace(",", "")) if m else None
    # 같은 접수번호가 두 번 잡히면(중첩 표) 하나만
    seen: set[str] = set()
    uniq: list[Row] = []
    for r in rows:
        if r.no in seen:
            continue
        seen.add(r.no)
        uniq.append(r)
    return uniq, total


def is_toheo(row: Row, keyword: str = KEYWORD) -> bool:
    return keyword in (row.name or "")


class SaeolClient:
    def __init__(self, http, params: dict | None = None):
        self.http = http
        self.params: dict[str, dict] = params or {}   # site.id → 탐지 결과

    # ---- 저수준 ---------------------------------------------------------
    def _get(self, url: str, extra: dict | None = None, method: str = "GET") -> tuple[list[Row], int | None, str]:
        if method == "POST":
            base, _, qs = url.partition("?")
            data = dict(parse_qsl(qs))
            data.update(extra or {})
            r = self.http.post(base, data=data)
        else:
            q = "&".join(f"{k}={v}" for k, v in (extra or {}).items())
            r = self.http.get(url + ("&" + q if q else ""))
        if r.status_code != 200:
            raise HttpError(f"HTTP {r.status_code} {url}")
        html = decode(r)
        rows, total = parse_list(html)
        return rows, total, html

    def _url_for(self, site: Site, cfg: dict) -> str:
        host = cfg.get("host")
        scheme = cfg.get("scheme") or "https"
        for u in site.list_urls():
            sp = urlsplit(u)
            if sp.netloc == host and sp.scheme == scheme:
                return u
        for u in site.list_urls():
            if urlsplit(u).netloc == host:
                return u
        return f"{scheme}://{host}{LIST_PATH}"

    # ---- 탐지 -----------------------------------------------------------
    def discover(self, site: Site, force: bool = False) -> dict:
        cfg = self.params.get(site.id)
        if cfg and not force and cfg.get("host") and not cfg.get("stale"):
            return cfg
        cfg = {"site": site.id, "host": None, "scheme": "https", "method": "GET", "page_param": None,
               "size_param": None, "page_size": None, "checked_at": now_kst().isoformat(timespec="seconds")}
        # 1) 호스트
        first_rows: list[Row] = []
        url = ""
        last_err = ""
        for u in site.list_urls():
            try:
                rows, total, html = self._get(u)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                log.info("%s 호스트 실패 %s: %s", site.id, u, e)
                continue
            if rows or "서식민원" in html:
                sp = urlsplit(u)
                cfg["host"], cfg["scheme"] = sp.netloc, sp.scheme
                cfg["total"] = total
                first_rows, url = rows, u
                break
        if not cfg["host"]:
            # 사유를 같이 남긴다(접속 불가/시간 상한/HTTP 오류를 사이트 내정보에서 구분할 수 있게)
            cfg["error"] = "접속 가능한 새올 호스트를 찾지 못함" + (f": {last_err[:160]}" if last_err else "")
            self.params[site.id] = cfg
            return cfg
        cfg["page_size"] = len(first_rows) or None
        first_nos = {r.no for r in first_rows}
        total = cfg.get("total")
        if first_rows and total is not None and total <= len(first_rows):
            # 목록이 한 페이지뿐이라 페이지 파라미터를 판정할 수 없음. 총 건수가 늘어 2페이지가 생기면
            # fetch() 가 stale 을 세워 다시 탐지한다(영구 캐시 방지).
            cfg["page_param"] = None
            cfg["single_page"] = True
            cfg["checked_total"] = total
            cfg["warning"] = None
            self.params[site.id] = cfg
            return cfg
        # 2) 페이지 파라미터
        for method in ("GET", "POST"):
            for cand in PAGE_PARAMS:
                try:
                    rows, _, _ = self._get(url, {cand: "2"}, method)
                except Exception:  # noqa: BLE001
                    continue
                nos = {r.no for r in rows}
                if rows and nos and not (nos & first_nos):
                    cfg["page_param"], cfg["method"] = cand, method
                    break
            if cfg["page_param"]:
                break
        if not cfg["page_param"]:
            cfg["warning"] = "페이지 이동 파라미터를 찾지 못해 첫 페이지만 읽습니다(config/saeol_params.json 으로 지정)"
            self.params[site.id] = cfg
            return cfg
        # 3) 페이지 크기 파라미터(선택)
        for cand in SIZE_PARAMS:
            try:
                rows, _, _ = self._get(url, {cand: str(WANT_SIZE), cfg["page_param"]: "1"}, cfg["method"])
            except Exception:  # noqa: BLE001
                continue
            if len(rows) > len(first_rows):
                cfg["size_param"], cfg["page_size"] = cand, len(rows)
                break
        self.params[site.id] = cfg
        return cfg

    # ---- 수집 -----------------------------------------------------------
    def fetch(self, site: Site, stop_before: dt.date, max_pages: int = 400,
              keyword: str = KEYWORD) -> tuple[list[Row], dict]:
        """접수일이 stop_before 보다 오래된 행이 나올 때까지 페이지를 넘기며 '토지거래' 민원만 모음."""
        cfg = self.discover(site)
        info = {"site": site.id, "host": cfg.get("host"), "pages": 0, "rows": 0, "toheo_rows": 0,
                "reached_cutoff": False, "error": cfg.get("error"), "warning": cfg.get("warning")}
        if not cfg.get("host"):
            return [], info
        url = self._url_for(site, cfg)
        out: list[Row] = []
        seen: set[str] = set()
        page = 1
        stop_iso = stop_before.isoformat()
        total_pages = None
        while page <= max_pages:
            extra: dict = {}
            if cfg.get("page_param"):
                extra[cfg["page_param"]] = str(page)
            if cfg.get("size_param"):
                extra[cfg["size_param"]] = str(cfg["page_size"])
            try:
                rows, total, _ = self._get(url, extra, cfg.get("method", "GET"))
            except Exception as e:  # noqa: BLE001
                info["error"] = f"{page}페이지 실패: {e}"
                break
            info["pages"] = page
            if page == 1 and rows and cfg.get("page_size") and total and len(rows) < total and len(rows) != cfg["page_size"]:
                # 서버가 더는 size 파라미터를 따르지 않거나 페이지 크기가 바뀜 → 실제 행 수로 보정하고 다음 실행에서 재탐지.
                # (옛 page_size 로 total_pages 를 계산하면 1페이지만 읽고 '끝까지 읽음' 으로 오판해 처리중 민원이
                #  '완료(목록이탈)' 로 둔갑한다)
                cfg["page_size"] = len(rows)
                cfg["stale"] = True
                info["warning"] = f"페이지 크기가 {len(rows)}행으로 바뀌어 다음 실행에서 파라미터를 다시 탐지합니다"
            if total and cfg.get("page_size"):
                total_pages = -(-total // cfg["page_size"])
            new = [r for r in rows if r.no not in seen]
            if not rows:
                break
            if not new:
                if page == 2:
                    # 2페이지가 1페이지와 같음 → 페이지 파라미터가 더는 안 맞음. 다음 실행에서 다시 탐지
                    cfg["stale"] = True
                    info["warning"] = "페이지 이동이 동작하지 않아 다음 실행에서 파라미터를 다시 탐지합니다"
                break
            for r in new:
                seen.add(r.no)
                if is_toheo(r, keyword):
                    out.append(r)
            info["rows"] += len(new)
            dates = [r.received for r in rows if r.received]
            if dates and min(dates) < stop_iso:
                info["reached_cutoff"] = True
                break
            if not cfg.get("page_param"):
                if total is not None and info["rows"] >= total:
                    info["reached_cutoff"] = True          # 목록 전체가 한 페이지에 들어옴
                elif total is not None and total > info["rows"]:
                    # 단일 페이지로 판정했던 사이트의 목록이 늘어남 → 다음 실행에서 페이지 파라미터 재탐지
                    cfg["stale"] = True
                    info["warning"] = f"목록이 {total}건으로 늘어 다음 실행에서 페이지 파라미터를 다시 탐지합니다"
                break
            if total_pages and page >= total_pages:
                # 마지막 페이지 판정은 실제 행 수로 한 번 더 확인(총 건수를 다 읽었거나 마지막 페이지가 덜 찬 경우)
                if (total is not None and info["rows"] >= total) or len(rows) < cfg["page_size"]:
                    info["reached_cutoff"] = True
                    break
            page += 1
        info["toheo_rows"] = len(out)
        return out, info
