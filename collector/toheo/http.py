"""공손한 HTTP 세션 — 호출 간격, 재시도, 인코딩 처리.

새올 전자민원창구·서울부동산정보광장은 호출 간격을 두고 조용히 읽습니다.
환경변수: TOHEO_DELAY_SEC(0.8) TOHEO_TIMEOUT_SEC(25) TOHEO_MAX_RETRY(3) TOHEO_USER_AGENT
"""
from __future__ import annotations

import os
import random
import time

import requests

USER_AGENT = os.environ.get(
    "TOHEO_USER_AGENT",
    "Mozilla/5.0 (compatible; apt-radar/1.0; +https://github.com/)",
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class HttpError(RuntimeError):
    pass


class Polite:
    """requests.Session 래퍼. 매 호출 사이에 delay(±30%) 대기, 5xx/429/연결오류 재시도. 쿠키는 세션에 남습니다."""

    def __init__(self, delay: float | None = None, session: requests.Session | None = None):
        self.delay = _env_float("TOHEO_DELAY_SEC", 0.8) if delay is None else delay
        self.timeout = _env_float("TOHEO_TIMEOUT_SEC", 25)
        self.max_retry = int(_env_float("TOHEO_MAX_RETRY", 3))
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })
        self.calls = 0
        self._last = 0.0

    def _wait(self) -> None:
        gap = self.delay * random.uniform(0.7, 1.3)
        now = time.monotonic()
        rest = self._last + gap - now
        if rest > 0:
            time.sleep(rest)
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        err: Exception | None = None
        for attempt in range(self.max_retry):
            self._wait()
            self.calls += 1
            try:
                r = self.session.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:  # pragma: no cover - network
                err = e
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                err = HttpError(f"HTTP {r.status_code} {url}")
                time.sleep(3 * (attempt + 1))
                continue
            return r
        raise HttpError(f"{method} {url} 실패: {err}")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)


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
