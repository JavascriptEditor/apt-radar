"""가짜 새올(서식민원 처리 공개) + 가짜 서울부동산정보광장 getContractList.do (테스트·데모용). 실제 접속 없음.

- FakeSite: 지자체 하나의 민원 목록 페이지(HTML, 페이지 넘김 파라미터 포함).
- FakeSeoulLand: contractStatus.do(세션 쿠키) + getContractList.do(JSON). 세션 쿠키 없으면 빈 결과,
  mode='html' 이면 JSON 대신 HTML 표를 돌려줘 폴백 파서를 시험한다.
- FakeHttp: Polite 대체. 호스트별 라우팅, 쿠키 보관, 호출 수.
"""
from __future__ import annotations

import datetime as dt
import json
import random
from urllib.parse import urlsplit, parse_qs

HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>서식민원처리공개 목록조회 - 새올전자민원창구</title></head>
<body><div id="content"><h3>서식민원 처리 공개</h3>
<form name="form1" method="post"><input type="hidden" name="method" value="selectListMwOpn">
<input type="hidden" name="pageIndex" value="{page}"></form>
<p class="total">총 {total} 건</p>
<table class="list"><caption>전자민원창구 서식민원 목록</caption>
<thead><tr><th>접수번호</th><th>접수일자</th><th>민원사무명</th><th>처리기간</th><th>처리상태</th><th>처리부서</th></tr></thead><tbody>
"""
ROW = "<tr><td>{no}</td><td>{received} </td><td><a href='#'>{name}</a> </td><td>{deadline}</td><td>{status}</td><td>{dept}</td></tr>\n"
FOOT = """</tbody></table>
<div class="paging"><a href="javascript:goPage(1)">맨 처음페이지</a> <a href="javascript:goPage({prev})">이전 페이지</a>
{links}
<a href="javascript:goPage({next})">다음 페이지</a> <a href="javascript:goPage({last})">마지막 페이지</a></div>
<script>function goPage(n){{document.form1.pageIndex.value=n;document.form1.submit();}}</script>
</div></body></html>"""

OTHER_NAMES = ["부동산중개사무소이전신고", "개발행위허가 (토지형질변경·토석채취·공작물설치·토지분할·물건적치)",
               "옥외광고물 등의 표시허가(신고)", "도로점용허가 (굴착)"]


class FakeSite:
    """하나의 지자체. rows 는 (접수번호, 접수일, 민원명, 처리기한, 상태, 부서) dict 목록. 접수일 내림차순 정렬."""

    def __init__(self, page_size: int = 10, page_param: str = "pageIndex", size_param: str | None = "pageSize",
                 max_size: int = 100):
        self.rows: list[dict] = []
        self.page_size = page_size
        self.page_param = page_param
        self.size_param = size_param
        self.max_size = max_size
        self.hits = 0
        self.now: str | None = None   # 시뮬레이션 날짜(ISO). 설정하면 상태가 날짜에 따라 바뀜

    def add(self, row: dict) -> None:
        self.rows.append(row)
        self.rows.sort(key=lambda r: (r["received"], r["no"]), reverse=True)

    def _visible(self) -> list[dict]:
        if not self.now:
            return self.rows
        out = []
        for r in self.rows:
            if r["received"] > self.now:
                continue
            rr = dict(r)
            if r.get("_done_at"):
                rr["status"] = "처리중" if self.now < r["_done_at"] else r.get("_final", r["status"])
            out.append(rr)
        return out

    def render(self, query: dict) -> str:
        self.hits += 1
        page = int((query.get(self.page_param) or ["1"])[0])
        size = self.page_size
        if self.size_param and query.get(self.size_param):
            size = min(int(query[self.size_param][0]), self.max_size)
        rows = self._visible()
        total = len(rows)
        last = max(1, -(-total // size))
        page = min(max(page, 1), last)
        chunk = rows[(page - 1) * size: page * size]
        links = " ".join(f"<a href='javascript:goPage({i})'>{i}</a>" if i != page else f"<strong>{i}</strong>"
                         for i in range(max(1, page - 4), min(last, page + 5) + 1))
        html = HEAD.format(page=page, total=total)
        for r in chunk:
            html += ROW.format(**r)
        html += FOOT.format(prev=max(1, page - 1), next=min(last, page + 1), last=last, links=links)
        return html


class FakeResponse:
    def __init__(self, text: str, status: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.content = text.encode("utf-8")
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": ctype}


class FakeSeoulLand:
    """서울부동산정보광장 흉내. records 는 getContractList.do 가 돌려주는 원본 키(SGG_CD, ACC_YEAR …) 그대로."""

    PAGE = "<html><head><title>토지거래허가 내역 조회</title></head><body><form action='/land/wsklis/getContractList.do'></form></body></html>"

    def __init__(self, mode: str = "json"):
        self.records: list[dict] = []
        self.mode = mode            # 'json' | 'html' | 'error'
        self.hits = {"page": 0, "list": 0, "nosession": 0}
        self.require_xhr = True

    def add(self, sgg: str, acc_no: str, hndl_ymd: str, address: str, use_purp: str = "주거용", job: str = "허가",
            jimok: str = "대", seq: str = "1", acc_year: str | None = None, duty_end: str | None = None,
            exec_ymd: str | None = None) -> dict:
        """hndl_ymd: 'YYYY-MM-DD'. address: '강남구 대치동 316'."""
        d = hndl_ymd.replace("-", "")
        parts = address.split()
        jib = parts[2] if len(parts) > 2 else ""
        bon, _, bu = jib.replace("산", "").partition("-")
        rec = {
            "SGG_CD": sgg, "ACC_YEAR": acc_year or d[:4], "ACC_NO": acc_no, "OBJ_SEQNO": seq, "HNDL_YMD": d,
            "ADDRESS": address + " ", "BOBN": bon.zfill(4) if bon.isdigit() else bon, "BUBN": (bu or "0").zfill(4),
            "JIMOK": jimok, "USE_PURP": use_purp, "JOB_GBN_NM": job,
            "DEAL_END_YMD": (duty_end or "").replace("-", ""), "EXEC_YMD": (exec_ymd or hndl_ymd).replace("-", ""),
        }
        self.records.append(rec)
        return rec

    def _html(self, items: list[dict]) -> str:
        rows = ""
        for i, it in enumerate(items, 1):
            addr = it["ADDRESS"].strip()
            rows += (f"<tr><td>{i}</td><td>{it['HNDL_YMD']}</td><td>{addr}</td><td>{it['JIMOK']}</td>"
                     f"<td>{it['HNDL_YMD']}</td><td>{it['USE_PURP']}</td><td></td></tr>"
                     f"<tr><td>{addr.split()[-1]}</td><td>({it['JIMOK']})</td><td>{it['DEAL_END_YMD'] or ''}</td></tr>")
        return ("<html><body><table><tr><th>연번</th><th>허가일자</th><th>주소</th><th>지목</th><th>허가년월일</th>"
                "<th>이용목적</th><th>신고,고발 등</th></tr>" + rows + "</table></body></html>")

    def handle(self, method: str, url: str, q: dict, headers: dict, http: "FakeHttp") -> FakeResponse:
        path = urlsplit(url).path
        if path.endswith("contractStatus.do"):
            self.hits["page"] += 1
            http.cookies["JSESSIONID"] = "fake-session"
            return FakeResponse(self.PAGE)
        if path.endswith("getContractList.do"):
            self.hits["list"] += 1
            if "JSESSIONID" not in http.cookies:
                self.hits["nosession"] += 1
                return FakeResponse(json.dumps({"result": []}), ctype="application/json")
            if self.require_xhr and (headers or {}).get("X-Requested-With") != "XMLHttpRequest":
                return FakeResponse("<html>forbidden</html>", 403)
            if self.mode == "error":
                return FakeResponse("<html>server error</html>", 500)
            sgg = (q.get("sggCd") or [""])[0]
            b = (q.get("beginDate") or ["00000000"])[0]
            e = (q.get("endDate") or ["99999999"])[0]
            items = [r for r in self.records if r["SGG_CD"] == sgg and b <= r["HNDL_YMD"] <= e]
            items.sort(key=lambda r: (r["HNDL_YMD"], r["ACC_NO"]), reverse=True)
            if self.mode == "html":
                return FakeResponse(self._html(items))
            return FakeResponse(json.dumps({"result": items, "resultCnt": len(items)}, ensure_ascii=False),
                                ctype="application/json; charset=utf-8")
        return FakeResponse("not found", 404)


class FakeHttp:
    """Polite 대체: 호스트별 FakeSite / 핸들러로 라우팅. 등록되지 않은 호스트는 연결 실패."""

    def __init__(self):
        self.sites: dict[str, FakeSite] = {}
        self.handlers = {}   # host → callable(method, url, q, headers, http) → FakeResponse
        self.cookies: dict[str, str] = {}
        self.calls = 0

    def route(self, host: str, site: FakeSite) -> None:
        self.sites[host] = site

    def route_seoul(self, land: FakeSeoulLand, host: str = "land.seoul.go.kr") -> None:
        self.handlers[host] = land.handle

    def request(self, method: str, url: str, params=None, data=None, headers=None, **kw) -> FakeResponse:
        self.calls += 1
        u = urlsplit(url)
        q = parse_qs(u.query)
        if params:
            q.update({k: [str(v)] for k, v in params.items()})
        if data:
            q.update({k: [str(v)] for k, v in data.items()})
        if u.netloc in self.handlers:
            return self.handlers[u.netloc](method, url, q, headers or {}, self)
        site = self.sites.get(u.netloc)
        if site is None:
            raise ConnectionError(f"no route for {u.netloc}")
        return FakeResponse(site.render(q))

    def get(self, url: str, **kw) -> FakeResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> FakeResponse:
        return self.request("POST", url, **kw)


def make_rows(site_code: str, start: dt.date, end: dt.date, per_day: float, dept: str, rng: random.Random,
              other_share: float = 0.25, status_days: tuple[int, int] = (10, 20)) -> list[dict]:
    """접수일 범위에 걸쳐 토지거래계약허가 민원(+잡다한 다른 민원)을 만든다. 처리 완료는 end 기준."""
    rows = []
    seq = rng.randint(100000, 200000)
    d = start
    while d <= end:
        n = max(0, int(rng.gauss(per_day, per_day * 0.5))) if d.weekday() < 5 else 0
        for _ in range(n):
            seq += rng.randint(1, 40)
            is_toheo = rng.random() > other_share
            deadline = d + dt.timedelta(days=rng.randint(19, 22))
            done_after = rng.randint(*status_days)
            final = rng.choices(["해결", "취하", "반려"], weights=[90, 7, 3])[0]
            status = final if d + dt.timedelta(days=done_after) <= end else "처리중"
            rows.append({
                "no": f"{d.year}{site_code}{seq:011d}", "received": d.isoformat(),
                "name": "토지거래계약허가" if is_toheo else rng.choice(OTHER_NAMES),
                "deadline": deadline.isoformat(), "status": status, "dept": dept,
                "_done_at": (d + dt.timedelta(days=done_after)).isoformat(), "_final": final,
            })
        d += dt.timedelta(days=1)
    return rows


def make_permits(land: FakeSeoulLand, sgg: str, today: dt.date, rng: random.Random, targets: list[tuple[str, str]],
                 n: int = 30, days: int = 55) -> None:
    """targets: [(법정동, 지번)] 에 대해 최근 days 일 동안 n 건의 허가 레코드를 심는다."""
    gu = {"11680": "강남구", "11650": "서초구", "11170": "용산구", "11710": "송파구"}.get(sgg, "강남구")
    for i in range(n):
        dong, jib = rng.choice(targets)
        d = today - dt.timedelta(days=rng.randint(0, days))
        job = rng.choices(["허가", "불허가", "취하"], weights=[92, 3, 5])[0]
        land.add(sgg, f"{1000 + i:07d}", d.isoformat(), f"{gu} {dong} {jib}",
                 use_purp=rng.choice(["주거용", "주거용", "주거용", "복지편익시설용"]), job=job,
                 duty_end=(d + dt.timedelta(days=730)).isoformat())
