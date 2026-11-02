"""공손한 HTTP 세션 — 호출 간격, 재시도, 인코딩 처리, 접속 불가 호스트 빠른 포기.

새올 전자민원창구·서울부동산정보광장은 호출 간격을 두고 조용히 읽습니다.

GitHub Actions(해외 IP)에서는 서울 자치구 새올(*.eminwon.seoul.kr)·land.seoul.go.kr 이 **연결 자체가
타임아웃**되는 날이 있습니다(러너 IP 대역에 따라 다름). 그때 호스트마다 25초 × 3회 재시도를 기다리면 한 실행이
90분을 넘기므로:
- 연결 타임아웃/연결 오류는 1회만 재시도하고 그 호스트를 이번 실행에서 '접속 불가' 로 기억해 즉시 실패시킵니다.
- 같은 계열(서울 계열: *.seoul.kr, *.seoul.go.kr) 호스트가 FAMILY_FAIL_LIMIT 개 이상 접속 불가면 계열 전체를
  건너뜁니다(해외 IP 차단 추정). 결과는 fetch_log 에 남아 사이트 내정보에서 보입니다.
- deadline(단조시계 기준)이 지나면 모든 호출을 즉시 실패시켜 실행 시간 상한(TOHEO_MAX_SEC)을 지킵니다.

환경변수: TOHEO_DELAY_SEC(0.8) TOHEO_TIMEOUT_SEC(25, 읽기) TOHEO_CONNECT_TIMEOUT_SEC(8) TOHEO_MAX_RETRY(3)
          TOHEO_FAMILY_FAIL_LIMIT(3) TOHEO_USER_AGENT
"""
from __future__ import annotations

import os
import random
import time
from urllib.parse import urlsplit

import requests

USER_AGENT = os.environ.get(
    "TOHEO_USER_AGENT",
    "Mozilla/5.0 (compatible; apt-radar/1.0; +https://github.com/)",
)

# 접속 불가 판정을 계열 단위로 묶는 규칙: 호스트 접미사 → 계열 이름
FAMILIES = (
    (".eminwon.seoul.kr", "seoul"),
    (".seoul.go.kr", "seoul"),
    ("land.seoul.go.kr", "seoul"),
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class HttpError(RuntimeError):
    pass


class Unreachable(HttpError):
    """이번 실행에서 접속 불가로 기억된 호스트/계열 — 재시도 없이 즉시 실패."""


class DeadlineExceeded(HttpError):
    """실행 시간 상한(TOHEO_MAX_SEC) 초과."""


def family_of(host: str) -> str | None:
    h = (host or "").lower()
    for suffix, fam in FAMILIES:
        if h == suffix.lstrip(".") or h.endswith(suffix):
            return fam
    return None


class Polite:
    """requests.Session 래퍼. 매 호출 사이에 delay(±30%) 대기, 5xx/429 재시도, 연결 실패는 빠르게 포기.
    쿠키는 세션에 남습니다."""

    def __init__(self, delay: float | None = None, session: requests.Session | None = None,
                 deadline: float | None = None):
        self.delay = _env_float("TOHEO_DELAY_SEC", 0.8) if delay is None else delay
        self.read_timeout = _env_float("TOHEO_TIMEOUT_SEC", 25)
        self.connect_timeout = _env_float("TOHEO_CONNECT_TIMEOUT_SEC", 8)
        self.timeout = (self.connect_timeout, self.read_timeout)
        self.max_retry = int(_env_float("TOHEO_MAX_RETRY", 3))
        self.family_fail_limit = int(_env_float("TOHEO_FAMILY_FAIL_LIMIT", 3))
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })
        self.calls = 0
        self._last = 0.0
        self.deadline = deadline                     # time.monotonic() 기준, None 이면 없음
        self.unreachable: dict[str, str] = {}        # host → 사유
        self.blocked_families: dict[str, str] = {}   # family → 사유
        self._family_hosts: dict[str, set[str]] = {}

    # ---- 상태 ---------------------------------------------------------
    def set_max_sec(self, max_sec: float | None) -> None:
        self.deadline = (time.monotonic() + max_sec) if max_sec and max_sec > 0 else None

    def past_deadline(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def reachable(self, url: str) -> tuple[bool, str]:
        """이 호스트에 요청을 보내도 되는지. (False, 사유) 면 호출자가 재시도 없이 건너뛸 수 있다."""
        if self.past_deadline():
            return False, "시간 상한(TOHEO_MAX_SEC) 초과"
        host = urlsplit(url).netloc.lower()
        if host in self.unreachable:
            return False, f"{host} 접속 불가(이번 실행에서 건너뜀): {self.unreachable[host]}"
        fam = family_of(host)
        if fam and fam in self.blocked_families:
            return False, self.blocked_families[fam]
        return True, ""

    def summary(self) -> dict:
        return {"unreachable": dict(self.unreachable), "blockedFamilies": dict(self.blocked_families)}

    def _mark_unreachable(self, host: str, reason: str) -> None:
        self.unreachable[host] = reason
        fam = family_of(host)
        if not fam:
            return
        hosts = self._family_hosts.setdefault(fam, set())
        hosts.add(host)
        if len(hosts) >= self.family_fail_limit and fam not in self.blocked_families:
            self.blocked_families[fam] = (f"{fam} 계열 호스트 {len(hosts)}개 연속 접속 불가 — 이번 실행에서 계열 전체를 건너뜀 "
                                          f"(GitHub Actions 해외 IP 차단 추정; 집 PC 수집은 정상)")

    # ---- 호출 ---------------------------------------------------------
    def _wait(self) -> None:
        gap = self.delay * random.uniform(0.7, 1.3)
        now = time.monotonic()
        rest = self._last + gap - now
        if rest > 0:
            time.sleep(rest)
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> requests.Response:
        ok, why = self.reachable(url)
        if not ok:
            if self.past_deadline():
                raise DeadlineExceeded(why)
            raise Unreachable(why)
        kw.setdefault("timeout", self.timeout)
        host = urlsplit(url).netloc.lower()
        err: Exception | None = None
        conn_fail = 0
        for attempt in range(self.max_retry):
            if self.past_deadline():
                raise DeadlineExceeded("시간 상한(TOHEO_MAX_SEC) 초과")
            self._wait()
            self.calls += 1
            try:
                r = self.session.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                err = e
                conn_fail += 1
                if self._looks_blocked(e) and conn_fail >= 2:
                    # 연결 자체가 안 되는 호스트(연결 타임아웃·DNS 실패·거부·리셋)는 두 번째 실패에서 포기한다
                    self._mark_unreachable(host, _short(e))
                    raise Unreachable(f"{host} 접속 불가: {_short(e)}") from e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                err = HttpError(f"HTTP {r.status_code} {url}")
                time.sleep(3 * (attempt + 1))
                continue
            return r
        if conn_fail and conn_fail >= self.max_retry:
            self._mark_unreachable(host, _short(err))
        raise HttpError(f"{method} {url} 실패: {err}")

    @staticmethod
    def _looks_blocked(e: Exception) -> bool:
        """연결 타임아웃·DNS 실패·연결 거부 = 재시도해도 소용없는 종류."""
        s = str(e).lower()
        return isinstance(e, requests.ConnectTimeout) or any(k in s for k in (
            "connect timeout", "connecttimeout", "name or service not known", "nodename nor servname",
            "connection refused", "no route to host", "network is unreachable", "failed to resolve",
            "connection reset", "remotedisconnected", "connection aborted"))

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)


def _short(e: Exception | None) -> str:
    s = str(e or "")
    # urllib3 의 긴 메시지에서 핵심만
    for key in ("ConnectTimeoutError", "NewConnectionError", "NameResolutionError", "ConnectionResetError"):
        if key in s:
            return key.replace("Error", "")
    if isinstance(e, requests.RequestException):
        return type(e).__name__
    return s[:120]


def decode(resp) -> str:
    """UTF-8 우선, 깨지면 EUC-KR(CP949)로 디코딩."""
    raw = resp.content
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # 한글이 전혀 안 나오고 깨진 패턴이 보이면 다음 인코딩 시도
        if enc == "utf-8" and "�" in text and not any("가" <= ch <= "힣" for ch in text[:5000]):
            continue
        return text
    return raw.decode("utf-8", errors="replace")
