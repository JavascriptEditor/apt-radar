"""네이버 부동산(비공식) 매물·단지 목록 클라이언트.

공식 API 가 아니므로 응답 구조가 바뀌거나 차단될 수 있습니다. 그래서
- 모든 주소·필드 이름은 config/naver.json 에서 읽고,
- 응답 안에서 '첫 번째 dict 목록'을 찾아 alias 로 필드를 뽑으며,
- 차단(403/429/HTML 응답/빈 응답 연속)을 감지하면 그 실행에서는 더 이상 호출하지 않고 상태를 남깁니다.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import requests

from .deals import area_group, floor_hint, norm_dong, parse_floor_info, parse_listing_price

DEFAULT_CFG = Path(__file__).resolve().parent.parent / "config" / "naver.json"


class Blocked(Exception):
    """차단되었거나 더 이상 호출하면 안 되는 상태."""


class CallBudgetExceeded(Exception):
    """이번 실행의 네이버 호출 상한(max_calls)에 도달. 차단이 아니므로 호출자는 'partial' 로 처리한다."""


# 중개사 자유 입력 설명(desc)에 섞여 들어오는 연락처·이메일은 저장하지 않는다(공개 data 브랜치에 그대로 실리므로).
# 연속 연결 실패(타임아웃·리셋·거부)가 이만큼이면 '차단' 으로 보고 즉시 멈춘다. GitHub Actions 에서 네이버가
# TCP 단계에서 막힌 날은 모든 호출이 타임아웃이라, 이게 없으면 400개 동 × 20초 = 2시간 넘게 헛돈다.
CONN_FAIL_BLOCK = 3

_RE_PHONE = re.compile(r"(?<!\d)0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}(?!\d)")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def mask_contacts(s: str) -> str:
    """전화번호·이메일 패턴을 '***' 로 가린다."""
    if not s:
        return ""
    return _RE_EMAIL.sub("***", _RE_PHONE.sub("***", s))


def load_cfg(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CFG
    return json.loads(p.read_text(encoding="utf-8"))


def _dig(obj, path: str):
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _has(d: dict, key: str) -> bool:
    """alias 가 'a.b.c' 면 중첩 경로로 존재 여부를 본다."""
    if "." in key:
        return _dig(d, key) is not None
    return key in d


def _find_list(obj, must_have: list[str], depth: int = 0):
    """응답 어딘가의 dict 목록 중 must_have 중 하나라도 키(또는 중첩 경로)로 가진 것."""
    if depth > 6:
        return None
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj[:3]) and any(_has(obj[0], k) for k in must_have):
            return obj
        for x in obj[:5]:
            r = _find_list(x, must_have, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_list(v, must_have, depth + 1)
            if r is not None:
                return r
    return None


def pick(d: dict, aliases: list[str]):
    """alias 순서대로 첫 번째 비어 있지 않은 값. 'a.b.c' 형태는 중첩 dict 경로."""
    for a in aliases:
        v = _dig(d, a) if "." in a else d.get(a)
        if v not in (None, ""):
            return v
    return None


def _fmt_json(obj, fmt: dict):
    """요청 json 템플릿의 문자열 값만 .format(**fmt). (숫자·목록·bool 은 그대로)"""
    if isinstance(obj, str):
        try:
            return obj.format(**fmt)
        except (KeyError, IndexError):
            return obj
    if isinstance(obj, dict):
        return {k: _fmt_json(v, fmt) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fmt_json(v, fmt) for v in obj]
    return obj


class NaverClient:
    def __init__(self, cfg: dict | None = None, token: str | None = None, delay: float | None = None,
                 session: requests.Session | None = None, state_path: str | os.PathLike | None = None,
                 timeout: int = 20, max_calls: int | None = None):
        self.cfg = cfg or load_cfg()
        self.token = token or os.environ.get("NAVER_LAND_TOKEN", "")
        # 환경변수가 빈 문자열(Actions 의 미설정 Variable)이면 설정 기본값
        self.delay = float(delay if delay is not None else (os.environ.get("NAVER_DELAY_SEC") or self.cfg.get("delay_sec", 0.7)))
        self.s = session or requests.Session()
        self.s.headers.update(self.cfg.get("headers", {}))
        self.timeout = timeout
        self.calls = 0
        self.max_calls = max_calls
        self.blocked_reason = ""
        self.empty_streak = 0
        self.conn_fail = 0  # 연속 연결 실패(타임아웃·리셋) 수 — CONN_FAIL_BLOCK 이면 Blocked
        self.state_path = Path(state_path) if state_path else None
        self.state = {"complexes_by_cortar": None, "articles": None}
        if self.state_path and self.state_path.exists():
            try:
                self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        self.log: list[str] = []
        self.last_data = None  # 마지막으로 받은 응답(JSON). probe 에서 구조를 확인할 때 씀.
        self.last_truncated = False  # 직전 articles_ok() 가 max_pages 에 걸려 목록이 잘렸는지(호출자가 miss 를 올리면 안 됨)

    # ----------------------------------------------------------------- io
    def _save_state(self) -> None:
        if self.state_path:
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay * random.uniform(0.7, 1.3))

    def _request(self, ep: dict, cursor=None, **fmt) -> tuple[object | None, str]:
        """endpoint 한 번 호출 → (json, 상태). 상태: ok / http:<code> / html / error.

        - ep.method 가 POST 면 ep.json 템플릿(문자열 값만 {complexNo} 등으로 포맷)을 본문으로 보낸다.
        - cursor 가 있으면(이전 페이지 응답의 ep.next_path 값) ep.next_param 이름으로 본문(POST) 또는
          쿼리(GET)에 넣는다. fin.land 의 lastInfo 커서 페이지가 이 방식.
        - 403/429/차단 안내 HTML 은 Blocked 상태로 기록하고 이후 호출은 모두 Blocked 예외.
        """
        if self.blocked_reason:
            raise Blocked(self.blocked_reason)
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise CallBudgetExceeded(f"이번 실행의 네이버 호출 상한 도달 ({self.max_calls})")
        fmt.setdefault("token", self.token)
        fmt.setdefault("page", 1)
        url = ep["url"].format(**fmt)
        headers = {k: v.format(**fmt) for k, v in ep.get("headers", {}).items()}
        if "Authorization" in headers and not self.token:
            del headers["Authorization"]
        method = str(ep.get("method", "GET")).upper()
        body = _fmt_json(ep.get("json"), fmt) if ep.get("json") is not None else None
        params = None
        if cursor is not None and ep.get("next_param"):
            if method == "POST":
                body = dict(body or {})
                body[ep["next_param"]] = cursor
            else:
                params = {ep["next_param"]: cursor if isinstance(cursor, str) else json.dumps(cursor, ensure_ascii=False)}
        self.calls += 1
        # 연결 타임아웃은 8초면 충분하다(응답 자체는 self.timeout). GitHub Actions 에서 네이버가 TCP 단계에서
        # 막히면 매 호출이 20초씩 타임아웃되므로, 연속 연결 실패 CONN_FAIL_BLOCK 회면 '차단' 으로 간주해 멈춘다.
        tmo = (min(8, self.timeout), self.timeout)
        try:
            if method == "POST":
                r = self.s.post(url, headers=headers, json=body, params=params, timeout=tmo)
            else:
                r = self.s.get(url, headers=headers, params=params, timeout=tmo)
        except requests.RequestException as e:
            self.conn_fail += 1
            self.log.append(f"error {url}: {type(e).__name__}: {str(e)[:160]}")
            if self.conn_fail >= CONN_FAIL_BLOCK:
                self.blocked_reason = (f"연결 실패 {self.conn_fail}회 연속 ({type(e).__name__}; "
                                       f"GitHub Actions 해외 IP 차단 추정 — 집 PC 수집을 쓰세요)")
                raise Blocked(self.blocked_reason)
            return None, "error"
        self.conn_fail = 0
        self._sleep()
        if r.status_code in (403, 429) or r.status_code >= 500:
            if r.status_code in (403, 429):
                self.blocked_reason = f"HTTP {r.status_code} ({ep.get('name')})"
            return None, f"http:{r.status_code}"
        if r.status_code != 200:
            return None, f"http:{r.status_code}"
        ctype = r.headers.get("Content-Type", "")
        text = r.text
        if "html" in ctype.lower() or text.lstrip().startswith("<"):
            # 로그인/캡차/안내 페이지
            if re.search(r"captcha|차단|비정상|로그인", text[:3000], re.I):
                self.blocked_reason = f"차단 안내 페이지 ({ep.get('name')})"
            return None, "html"
        try:
            data = r.json()
        except ValueError:
            return None, "error"
        self.last_data = data
        return data, "ok"

    def _extract_list(self, data, ep: dict, must_have: list[str]) -> list | None:
        """응답에서 목록을 꺼낸다. list_path 가 list 가 아니면 must_have 키를 가진 목록을 찾는다.
        빈 목록([])은 정상이고, 목록 자체를 못 찾으면 None(구조를 모름 = 실패)."""
        lst = _dig(data, ep.get("list_path", "")) if ep.get("list_path") else None
        if isinstance(lst, list):
            return lst
        return _find_list(data, must_have)

    def _try_endpoints(self, kind: str, must_have: list[str], **fmt) -> tuple[list[dict], dict | None, object | None]:
        """설정된 endpoint 들을 순서대로. 기억해 둔 endpoint 가 있으면 그것부터.
        반환 (목록, 성공한 endpoint 또는 None, 응답). 모두 실패하면 ([], None, 마지막 응답).
        endpoint 는 **비어 있지 않은 목록**을 돌려줬을 때만 기억한다(빈 목록은 '단지번호를 모름' 일 수도 있어 확인된 게 아님)."""
        eps = list(self.cfg.get(kind, []))
        remembered = self.state.get(kind)
        if remembered:
            eps.sort(key=lambda e: 0 if e.get("name") == remembered else 1)
        last_status, last_data = "none", None
        for ep in eps:
            data, status = self._request(ep, **fmt)
            last_status = status
            if status != "ok":
                if self.blocked_reason:
                    raise Blocked(self.blocked_reason)
                continue
            last_data = data
            lst = self._extract_list(data, ep, must_have)
            if lst is None:
                # 구조를 모르는 JSON(오류 응답 등) 은 실패로 보고 다음 endpoint 로. 응답은 last_data 에 남김.
                last_status = "nolist"
                self.log.append(f"{kind} {ep.get('name')}: 목록 구조를 찾지 못함")
                continue
            if lst and self.state.get(kind) != ep.get("name"):
                self.state[kind] = ep.get("name")
                self._save_state()
            return lst, ep, data
        self.log.append(f"{kind} 실패: {last_status} {fmt}")
        return [], None, last_data

    # --------------------------------------------------------- complexes
    def complexes(self, cortar_no: str) -> list[dict]:
        f = self.cfg["complex_fields"]
        lst, ep, _ = self._try_endpoints("complexes_by_cortar", f["id"], cortarNo=cortar_no)
        out = []
        for d in lst:
            if not isinstance(d, dict):
                continue
            cid = pick(d, f["id"])
            if cid is None:
                continue
            typ = str(pick(d, f.get("type", [])) or "")
            out.append({
                "naverId": str(cid), "naverName": str(pick(d, f["name"]) or "").strip(),
                "cortarNo": str(pick(d, f.get("cortarNo", [])) or cortar_no),
                "jibun": str(pick(d, f.get("jibun", [])) or "").strip(),
                "households": _int(pick(d, f.get("households", []))),
                "dongCount": _int(pick(d, f.get("dongCount", []))),
                "highFloor": _int(pick(d, f.get("highFloor", []))),
                "approveYmd": str(pick(d, f.get("approveYmd", [])) or ""),
                "lat": pick(d, f.get("lat", [])), "lng": pick(d, f.get("lng", [])),
                "type": typ,
            })
        return out

    # ---------------------------------------------------------- articles
    def articles_ok(self, complex_no: str, max_pages: int = 30) -> tuple[list[dict], bool]:
        """단지의 매매 매물 전부 → (목록, ok). ok=False 는 http 오류·HTML·구조 불명·중간 페이지 실패 등
        '응답을 믿을 수 없음' 이라는 뜻이며, 그때는 호출자가 매물 상태를 바꾸면 안 된다.
        차단(403/429/차단 페이지/호출 상한)은 Blocked 예외."""
        f = self.cfg["article_fields"]
        out: list[dict] = []
        seen = set()
        self.last_truncated = False
        # 1페이지: endpoint 선택(기억해 둔 것부터)
        lst, ep, data = self._try_endpoints("articles", f["id"], complexNo=complex_no, page=1)
        if ep is None:
            return [], False
        if not lst and self.state.get("articles") != ep.get("name"):
            # 아직 한 번도 매물을 돌려준 적 없는 endpoint 의 빈 목록은 '정상 0건' 으로 믿지 않는다.
            # (fin.land 가 m.land hscpNo 를 모르면 구조만 정상인 빈 목록을 돌려줄 수 있음 → 전 매물 gone 방지)
            self.log.append(f"articles {complex_no}: {ep.get('name')} 미확인 endpoint 의 0건 응답 — 반영 안 함")
            return [], False
        cursor = None
        more = None
        for page in range(1, max_pages + 1):
            if page > 1:
                # 2페이지부터는 같은 endpoint 로. 커서(next_path) 가 있으면 커서 페이지, 없으면 {page} 번호 페이지.
                data, status = self._request(ep, cursor=cursor, complexNo=complex_no, page=page)
                if status != "ok":
                    if self.blocked_reason:
                        raise Blocked(self.blocked_reason)
                    self.log.append(f"articles {complex_no} p{page}: {status}")
                    return out, False
                lst = self._extract_list(data, ep, f["id"])
                if lst is None:
                    self.log.append(f"articles {complex_no} p{page}: 목록 구조를 찾지 못함")
                    return out, False
            got = 0
            for d in lst:
                if not isinstance(d, dict):
                    continue
                tt = str(pick(d, f.get("tradeType", [])) or "A1")
                if tt not in ("A1", "매매"):
                    continue
                aid = str(pick(d, f["id"]) or "")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                got += 1
                out.append(self._norm_article(d, complex_no, f))
            more = _dig(data, ep.get("more_path", "")) if ep.get("more_path") else None
            if got == 0 or more is None or more not in ep.get("more_true", [True]):
                more = None
                break
            if ep.get("next_path"):
                cursor = _dig(data, ep["next_path"])
                if cursor in (None, "", [], {}):
                    more = None
                    break
        else:
            # max_pages 를 다 썼는데 다음 페이지가 남아 있음 → 목록이 잘림. 응답 자체는 믿을 수 있으나
            # '여기 없는 매물은 사라졌다' 고 볼 수 없으므로 호출자는 miss 를 올리면 안 된다(last_truncated).
            if more is not None:
                self.last_truncated = True
                self.log.append(f"articles {complex_no}: {max_pages}페이지 상한으로 목록 잘림 ({len(out)}건)")
        return out, True

    def articles(self, complex_no: str, max_pages: int = 30) -> list[dict]:
        """articles_ok() 의 목록만 (probe 등 기존 호출부 호환)."""
        return self.articles_ok(complex_no, max_pages)[0]

    def article_count(self, complex_no: str) -> int | None:
        """fin.land count endpoint(complex_count). 설정이 없거나 실패하면 None. 차단은 Blocked."""
        for ep in self.cfg.get("complex_count", []):
            data, status = self._request(ep, complexNo=complex_no)
            if status != "ok":
                if self.blocked_reason:
                    raise Blocked(self.blocked_reason)
                continue
            v = _dig(data, ep.get("count_path", "")) if ep.get("count_path") else None
            if v is None and isinstance(data, dict):
                res = data.get("result", data)
                if isinstance(res, (int, float)):
                    v = res
                elif isinstance(res, dict):
                    v = next((x for k, x in res.items() if isinstance(x, (int, float)) and "count" in k.lower()), None)
            n = _int(v)
            if n is not None:
                return n
        return None

    def _norm_article(self, d: dict, complex_no: str, f: dict) -> dict:
        finfo = str(pick(d, f.get("floorInfo", [])) or "")
        if not finfo:
            # fin.land: 층/최고층이 따로 옴 → '12/35' 로 합쳐 기존 파서 재사용
            fl, tp = pick(d, f.get("floorNo", [])), pick(d, f.get("topFloor", []))
            if fl is not None or tp is not None:
                finfo = f"{'' if fl is None else fl}/{'' if tp is None else tp}"
        floor, top = parse_floor_info(finfo)
        area = _float(pick(d, f.get("area", [])))
        price = parse_listing_price(pick(d, f.get("price", [])))
        direct_v = pick(d, f.get("direct", []))
        direct = 1 if (direct_v is True or str(direct_v).upper() in ("Y", "TRUE", "1")) else 0
        tags = pick(d, f.get("tags", [])) or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,|]", tags) if t]
        cfm = str(pick(d, f.get("confirmYmd", [])) or "")
        cfm = re.sub(r"[^0-9]", "", cfm)
        if len(cfm) == 6:  # 25.10.30. → 251030 → 20251030
            cfm = "20" + cfm
        dong_raw = str(pick(d, f.get("dong", [])) or "")
        return {
            "atclNo": str(pick(d, f["id"])), "naverId": str(pick(d, f.get("complexId", [])) or complex_no),
            "dongRaw": dong_raw, "dong": norm_dong(dong_raw), "floor": floor, "top": top,
            "floorHint": floor_hint(finfo), "area": area, "supply": _float(pick(d, f.get("supply", []))),
            "grp": area_group(area), "price": price, "confirmYmd": cfm, "direct": direct,
            "sameCnt": _int(pick(d, f.get("sameCnt", []))) or 1,
            "realtor": str(pick(d, f.get("realtor", [])) or ""),
            "direction": str(pick(d, f.get("direction", [])) or ""),
            "desc": mask_contacts(str(pick(d, f.get("desc", [])) or ""))[:80],
            "tags": "|".join(str(t) for t in tags)[:120],
        }

    def article_url(self, atcl_no: str) -> str:
        return self.cfg.get("article_url", "").format(atclNo=atcl_no)

    def complex_url(self, complex_no: str) -> str:
        return self.cfg.get("complex_url", "").format(complexNo=complex_no)


def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except ValueError:
        return None


def _float(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


# ----------------------------------------------------------------- 매칭
_NAME_STRIP = re.compile(r"[\s()\[\]·\-_,./]|아파트|APT|apt|단지$")


def name_key(s: str) -> str:
    t = _NAME_STRIP.sub("", s or "")
    t = re.sub(r"제?(\d+)단지", r"\1", t)
    return t.upper()


def _tokens(s: str) -> set[str]:
    t = name_key(s)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def match_score(mc: dict, nc: dict) -> float:
    """국토부 단지(mc) ↔ 네이버 단지(nc) 유사도 0~1."""
    score = 0.0
    a, b = _tokens(mc.get("name", "")), _tokens(nc.get("naverName", ""))
    if a and b:
        j = len(a & b) / len(a | b)
        score += 0.55 * j
        if name_key(mc.get("name", "")) == name_key(nc.get("naverName", "")):
            score += 0.15
    mj = (mc.get("jibun") or "").replace(" ", "")
    nj = (nc.get("jibun") or "").replace(" ", "")
    if mj and nj:
        if mj == nj or mj == nj.split("-")[0] or nj == mj.split("-")[0]:
            score += 0.25
        else:
            score -= 0.1
    by = str(mc.get("buildYear") or "")[:4]
    ay = str(nc.get("approveYmd") or "")[:4]
    if by and ay and by.isdigit() and ay.isdigit():
        diff = abs(int(by) - int(ay))
        score += 0.2 if diff == 0 else (0.1 if diff == 1 else -0.15)
    return max(0.0, min(1.0, score))


def match_complexes(molit_cx: list[dict], naver_cx: list[dict], threshold: float = 0.45) -> dict[str, tuple[dict, float]]:
    """같은 법정동 안에서 점수 높은 순으로 1:1 매칭. {cid: (naver, score)}."""
    pairs = []
    for m in molit_cx:
        for n in naver_cx:
            s = match_score(m, n)
            if s >= threshold:
                pairs.append((s, m["cid"], n["naverId"], n))
    pairs.sort(key=lambda p: -p[0])
    used_m, used_n, out = set(), set(), {}
    for s, cid, nid, n in pairs:
        if cid in used_m or nid in used_n:
            continue
        used_m.add(cid)
        used_n.add(nid)
        out[cid] = (n, round(s, 3))
    return out
