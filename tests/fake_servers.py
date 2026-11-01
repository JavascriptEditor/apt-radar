"""가짜 국토부 API + 가짜 네이버 매물 API (테스트·예시 데이터용). 실제 접속 없음.

진짜 구조(동 프리미엄·층 효과·시세 흐름)를 심어 두고, 모델이 그것을 되찾는지 확인합니다.
"""
from __future__ import annotations

import json
import math
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from collector.deals import parse_listing_price

# ------------------------------------------------------------------ 가짜 세계
# (코드, 법정동코드, 법정동, 단지명, 지번, 건축년도, 네이버id, 세대수, 최고층, 면적그룹→(기준 log가격 2022-01, 타입목록), 동목록→프리미엄)
WORLD = [
    {"sgg": "11680", "umdCd": "10300", "umd": "개포동", "name": "래미안블레스티지", "jibun": "12", "build": "2019",
     "naverId": "100001", "hh": 1957, "top": 35,
     "groups": {59: (math.log(230000), [59.97, 59.84]), 84: (math.log(320000), [84.94, 84.43]), 113: (math.log(420000), [113.07])},
     "dongs": {"101": 0.04, "102": 0.03, "103": 0.0, "104": -0.02, "105": -0.04, "106": 0.01, "107": 0.0, "108": -0.01}},
    {"sgg": "11680", "umdCd": "10300", "umd": "개포동", "name": "개포자이프레지던스", "jibun": "1234", "build": "2023",
     "naverId": "100002", "hh": 3375, "top": 35,
     "groups": {59: (math.log(240000), [59.92]), 84: (math.log(330000), [84.99, 84.59, 84.21])},
     "dongs": {"201": 0.05, "202": 0.02, "203": -0.03, "204": -0.01, "205": 0.0}},
    {"sgg": "11680", "umdCd": "10600", "umd": "대치동", "name": "은마", "jibun": "316", "build": "1979",
     "naverId": "100003", "hh": 4424, "top": 14,
     "groups": {76: (math.log(260000), [76.79]), 84: (math.log(290000), [84.43])},
     "dongs": {"1": 0.0, "2": 0.01, "3": -0.01, "5": 0.02, "7": -0.02, "11": 0.0}},
    {"sgg": "11650", "umdCd": "10700", "umd": "반포동", "name": "래미안퍼스티지", "jibun": "18-1", "build": "2009",
     "naverId": "100004", "hh": 2444, "top": 32,
     "groups": {59: (math.log(300000), [59.96]), 84: (math.log(420000), [84.93, 84.86]), 117: (math.log(520000), [117.25])},
     "dongs": {"101": 0.03, "102": 0.0, "103": -0.03, "104": 0.02, "105": 0.0}},
    {"sgg": "11650", "umdCd": "10700", "umd": "반포동", "name": "반포자이", "jibun": "20", "build": "2009",
     "naverId": "100005", "hh": 3410, "top": 32,
     "groups": {84: (math.log(400000), [84.98, 84.97, 84.9]), 132: (math.log(500000), [132.44])},
     "dongs": {"101": 0.02, "102": -0.02, "103": 0.0, "104": 0.01}},
]
FLOOR_TRUE = {"f1": -0.09, "f2": -0.05, "f3": -0.03, "low": -0.02, "mid": 0.0, "high": 0.02, "top": -0.01}


def floor_cat(f, top):
    if f == 1:
        return "f1"
    if f == 2:
        return "f2"
    if f == 3:
        return "f3"
    if f >= top:
        return "top"
    rel = f / top
    return "low" if rel < 0.34 else ("mid" if rel < 0.70 else "high")


def month_index(ym: str) -> int:
    return (int(ym[:4]) - 2006) * 12 + int(ym[4:6]) - 1


def region_index(mi: int, sgg: str) -> float:
    """2006-01 부터의 시군구 로그지수. 완만한 상승 + 주기."""
    base = 0.0045 if sgg == "11680" else 0.004
    return base * (mi - 192) + 0.05 * math.sin(mi / 11.0)  # 2022-01 (mi=192) 을 0 근처로


def gen_trades(sgg: str, ym: str, seed: int = 7) -> list[dict]:
    rnd = random.Random(f"{seed}-{sgg}-{ym}")
    mi = month_index(ym)
    out = []
    for cx in WORLD:
        if cx["sgg"] != sgg or int(ym[:4]) < int(cx["build"]) - 1:
            continue
        n = rnd.randint(2, 7) if mi >= 150 else rnd.randint(0, 3)
        for _ in range(n):
            g = rnd.choice(list(cx["groups"]))
            base, types = cx["groups"][g]
            area = rnd.choice(types)
            dong = rnd.choice(list(cx["dongs"]))
            floor = rnd.randint(1, cx["top"])
            lp = base + region_index(mi, sgg) + cx["dongs"][dong] + FLOOR_TRUE[floor_cat(floor, cx["top"])] + rnd.gauss(0, 0.035)
            if abs(area - types[0]) > 0.3:
                lp -= 0.01
            price = int(round(math.exp(lp) / 100) * 100)
            day = rnd.randint(1, 28)
            direct = rnd.random() < 0.03
            cancelled = rnd.random() < 0.02
            out.append({
                "sggCd": sgg, "umdCd": cx["umdCd"], "umdNm": cx["umd"], "aptNm": cx["name"], "jibun": cx["jibun"],
                "excluUseAr": f"{area:.2f}", "dealYear": ym[:4], "dealMonth": str(int(ym[4:6])), "dealDay": str(day),
                "dealAmount": f"{price:,}", "floor": str(floor), "buildYear": cx["build"],
                "aptDong": (dong + "동") if mi >= 200 else "",  # 2022-09 이후에만 동 정보
                "cdealType": "O" if cancelled else "", "cdealDay": f"{ym[:4]}.{ym[4:6]}.{day:02d}" if cancelled else "",
                "dealingGbn": "직거래" if direct else "중개거래", "aptSeq": f"{sgg}-{abs(hash(cx['name'])) % 9000}",
                "rgstDate": "",
            })
    return out


def gen_articles(naver_id: str, now_ym: str, seed: int = 11) -> list[dict]:
    """m.land 모바일(getComplexArticleList) 형식의 매물 목록."""
    cx = next((c for c in WORLD if c["naverId"] == naver_id), None)
    if not cx:
        return []
    rnd = random.Random(f"{seed}-{naver_id}")
    mi = month_index(now_ym)
    out = []
    for k in range(rnd.randint(6, 14)):
        g = rnd.choice(list(cx["groups"]))
        base, types = cx["groups"][g]
        area = rnd.choice(types)
        dong = rnd.choice(list(cx["dongs"]))
        floor = rnd.randint(1, cx["top"])
        fair = base + region_index(mi, cx["sgg"]) + cx["dongs"][dong] + FLOOR_TRUE[floor_cat(floor, cx["top"])]
        # 호가: 대체로 +3%, 일부는 급매(−8~−12%)
        off = rnd.choice([0.03, 0.05, 0.01, 0.0, -0.1, -0.08, 0.02, 0.04])
        price = int(round(math.exp(fair + off) / 500) * 500)
        eok, man = divmod(price, 10000)
        prc = f"{eok}억 {man:,}" if man else f"{eok}억"
        out.append({
            "atclNo": f"{naver_id}{k:03d}", "hscpNo": naver_id, "hscpNm": cx["name"], "bildNm": f"{dong}동",
            "tradTpCd": "A1", "tradTpNm": "매매", "spc1": f"{area * 1.3:.2f}", "spc2": f"{area:.2f}",
            "flrInfo": f"{floor}/{cx['top']}", "prcInfo": prc, "atclCfmYmd": "25.10.28.", "direction": "남향",
            "atclFetrDesc": "급매" if off < 0 else "올수리", "tagList": ["25년이상", "대단지"] if off >= 0 else ["급매"],
            "rltrNm": f"행복공인{k % 3}", "sameAddrCnt": rnd.randint(1, 4), "isDirectTrade": False,
        })
    return out


def to_fin_article(a: dict) -> dict:
    """모바일 형식 매물 → fin.land front-api 형식(중첩 구조). 같은 매물(atclNo) 이 두 형식에서 같은 id 를 갖는다.
    실제 fin.land 응답의 필드명은 공개 문서가 없어 2025~2026 커뮤니티 보고를 바탕으로 한 근사치이며,
    config/naver.json 의 alias 가 이 구조를 읽는지 확인하는 용도다."""
    floor, top = a["flrInfo"].split("/")
    return {
        "representativeArticleInfo": {
            "articleNumber": a["atclNo"], "complexNumber": a["hscpNo"], "articleName": a["hscpNm"],
            "realEstateTypeCode": "APT", "tradeType": a["tradTpCd"],
            "verificationInfo": {"verificationType": "OWNER", "exposureStartDate": "2025-10-28"},
            "spaceInfo": {"nameType": f"{int(float(a['spc2']))}", "supplySpace": float(a["spc1"]), "exclusiveSpace": float(a["spc2"])},
            "priceInfo": {"dealPrice": parse_listing_price(a["prcInfo"]), "priceChangeState": "SAME"},
            "articleDetail": {"articleFeatureDescription": a["atclFetrDesc"], "direction": a["direction"],
                              "buildingName": a["bildNm"], "floorInfo": {"targetFloor": floor, "totalFloor": top}},
            "brokerInfo": {"brokerageName": a["rltrNm"], "isDirectTrade": a["isDirectTrade"]},
            "tagList": a["tagList"],
        },
        "duplicatedArticlesInfo": {"count": a["sameAddrCnt"]},
    }


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    now_ym = "202611"
    calls = {"molit": 0, "naver": 0}
    limit_after: int | None = None  # molit 호출 n회 후 한도 초과 응답
    # 테스트에서 매물 응답을 조작하는 훅: {naverId: list|None}. None 이면 빈 목록, 키가 없으면 gen_articles.
    articles_override: dict[str, list | None] = {}
    # 테스트에서 특정 단지의 매물 요청을 실패시킴: {naverId: "500" | "html" | "json_error"}
    fail_articles: dict[str, str] = {}
    FIN_PAGE = 10  # fin.land 가짜 페이지 크기

    def log_message(self, *a):  # 조용히
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        return self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", code)

    def _articles_for(self, naver_id: str) -> list[dict]:
        if naver_id in Handler.articles_override:
            return list(Handler.articles_override[naver_id] or [])
        return gen_articles(naver_id, Handler.now_ym)

    def _maybe_fail(self, naver_id: str) -> bool:
        """fail_articles 에 등록된 단지면 실패 응답을 보내고 True."""
        mode = Handler.fail_articles.get(naver_id)
        if not mode:
            return False
        if mode == "500":
            self._send(b"server error", "text/plain", 500)
        elif mode == "html":
            self._send(b"<html><body>empty</body></html>", "text/html", 200)
        else:  # json_error: 구조를 알 수 없는 JSON
            self._send_json({"isSuccess": False, "detailCode": "ERR", "message": "temporary"})
        return True

    def _blocked(self):
        # /blocked → 403 캡차 HTML, /blocked429 → 429 JSON (둘 다 Blocked 로 처리돼야 함)
        if "429" in self.path:
            return self._send_json({"error": "Too Many Requests"}, 429)
        return self._send(b"<html>captcha</html>", "text/html", 403)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if "blocked" in u.path:
            return self._blocked()
        if u.path.endswith("/complex/article/list"):
            # fin.land: POST {complexNumber, tradeTypes, size, lastInfo?} → {result:{list, hasNextPage, lastInfo}}
            Handler.calls["naver"] += 1
            cno = str(body.get("complexNumber") or "")
            if self._maybe_fail(cno):
                return
            arts = [to_fin_article(a) for a in self._articles_for(cno)]
            last = body.get("lastInfo")
            offset = int(last.get("offset", 0)) if isinstance(last, dict) else 0
            chunk = arts[offset: offset + Handler.FIN_PAGE]
            has_next = offset + Handler.FIN_PAGE < len(arts)
            res = {"list": chunk, "hasNextPage": has_next, "totalCount": len(arts),
                   "lastInfo": {"offset": offset + Handler.FIN_PAGE,
                                "articleNumber": chunk[-1]["representativeArticleInfo"]["articleNumber"] if chunk else ""} if has_next else None}
            return self._send_json({"isSuccess": True, "detailCode": "0", "result": res})
        self._send(b"not found", "text/plain", 404)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.endswith("/complex/article/count"):
            Handler.calls["naver"] += 1
            cno = q.get("complexNumber", "")
            if self._maybe_fail(cno):
                return
            return self._send_json({"isSuccess": True, "detailCode": "0", "result": {"count": len(self._articles_for(cno))}})
        if "molit" in u.path:
            Handler.calls["molit"] += 1
            if Handler.limit_after is not None and Handler.calls["molit"] > Handler.limit_after:
                xml = ("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
                       "<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
                       "<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
                return self._send(xml.encode(), "application/xml")
            rows = gen_trades(q.get("LAWD_CD", ""), q.get("DEAL_YMD", "200601"))
            page, n = int(q.get("pageNo", 1)), int(q.get("numOfRows", 10))
            chunk = rows[(page - 1) * n: page * n]
            items = "".join("<item>" + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in r.items()) + "</item>" for r in chunk)
            xml = (f"<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body>"
                   f"<items>{items}</items><numOfRows>{n}</numOfRows><pageNo>{page}</pageNo>"
                   f"<totalCount>{len(rows)}</totalCount></body></response>")
            return self._send(xml.encode(), "application/xml")
        if "complexListByCortarNo" in u.path:
            Handler.calls["naver"] += 1
            cortar = q.get("cortarNo", "")
            res = [{"hscpNo": c["naverId"], "hscpNm": c["name"] + ("아파트" if c["name"] == "은마" else ""),
                    "cortarNo": cortar, "detailAddress": c["jibun"], "totHsehCnt": c["hh"], "totDongCnt": len(c["dongs"]),
                    "highFloor": c["top"], "useAprvYmd": f"{c['build']}0301", "lat": 37.49, "lng": 127.06, "hscpTypeCd": "A01"}
                   for c in WORLD if c["sgg"] + c["umdCd"] == cortar]
            return self._send(json.dumps({"result": res}, ensure_ascii=False).encode(), "application/json")
        if "getComplexArticleList" in u.path:
            Handler.calls["naver"] += 1
            hscp = q.get("hscpNo", "")
            if self._maybe_fail(hscp):
                return
            arts = self._articles_for(hscp)
            page = int(q.get("page", 1))
            chunk = arts[(page - 1) * 10: page * 10]
            more = "Y" if page * 10 < len(arts) else "N"
            body = {"result": {"list": chunk, "moreDataYn": more, "totAtclCnt": len(arts)}}
            return self._send(json.dumps(body, ensure_ascii=False).encode(), "application/json")
        if "blocked" in u.path:
            return self._blocked()
        self._send(b"not found", "text/plain", 404)


def start(now_ym: str = "202611") -> tuple[ThreadingHTTPServer, str]:
    Handler.now_ym = now_ym
    Handler.calls = {"molit": 0, "naver": 0}
    Handler.articles_override = {}
    Handler.fail_articles = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


NAVER_HOSTS = ("https://fin.land.naver.com", "https://m.land.naver.com", "https://new.land.naver.com")


def naver_cfg_for(base: str, cfg: dict, endpoints: list[str] | None = None) -> dict:
    """설정의 네이버 주소(fin.land·m.land)를 가짜 서버로 바꾼다. endpoint 순서는 실제 설정 그대로
    (fin_list 1순위, mobile 2순위). endpoints=['mobile'] 처럼 주면 그 이름의 endpoint 만 남긴다."""
    import copy
    c = copy.deepcopy(cfg)
    for kind in ("complexes_by_cortar", "articles", "complex_count"):
        eps = c.get(kind, [])
        if endpoints is not None:
            eps = [e for e in eps if e["name"] in endpoints or kind == "complexes_by_cortar"]
        for e in eps:
            for h in NAVER_HOSTS:
                e["url"] = e["url"].replace(h, base)
        c[kind] = eps
    c["delay_sec"] = 0
    return c
