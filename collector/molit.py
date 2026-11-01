"""국토교통부 아파트 매매 실거래가 API 클라이언트.

- 상세 자료(RTMSDataSvcAptTradeDev)를 기본으로 쓰고, 기본 자료 API 주소를 넣어도 동작합니다.
- 페이지 넘김, 429/5xx 재시도, 하루 한도 초과(코드 22) 감지, 호출 수 계산.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

DEFAULT_URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
ROWS = 1000


class BudgetExhausted(Exception):
    """하루 호출 한도(설정값 또는 API 응답 코드 22)에 도달."""


class ApiError(Exception):
    pass


def normalize_key(key: str) -> str:
    key = (key or "").strip()
    if "%" in key:  # Encoding 키가 들어오면 Decoding 키로
        key = urllib.parse.unquote(key)
    return key


class MolitClient:
    def __init__(self, service_key: str, url: str | None = None, call_budget: int = 9500,
                 calls_used: int = 0, timeout: int = 30, session: requests.Session | None = None,
                 sleep: float = 0.05):
        self.key = normalize_key(service_key)
        self.url = url or os.environ.get("MOLIT_API_URL") or DEFAULT_URL
        self.budget = int(call_budget)
        self.calls = int(calls_used)
        self.timeout = timeout
        self.s = session or requests.Session()
        self.sleep = sleep
        self.limited = False  # API 가 한도 초과를 알려 왔는지

    # ------------------------------------------------------------------
    def remaining(self) -> int:
        return max(0, self.budget - self.calls)

    def _get(self, params: dict) -> ET.Element:
        if self.calls >= self.budget or self.limited:
            raise BudgetExhausted(f"calls={self.calls} budget={self.budget} limited={self.limited}")
        q = dict(params)
        q["serviceKey"] = self.key
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                self.calls += 1
                r = self.s.get(self.url, params=q, timeout=self.timeout)
            except requests.RequestException as e:  # 네트워크 오류
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = ApiError(f"HTTP {r.status_code}")
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise ApiError(f"HTTP {r.status_code}: {r.text[:200]}")
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError as e:
                last_err = ApiError(f"XML 파싱 실패: {e}; body={r.text[:200]}")
                time.sleep(1.0)
                continue
            code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "").strip()
            msg = (root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or "").strip()
            if code in ("00", "000", ""):
                if self.sleep:
                    time.sleep(self.sleep)
                return root
            if code in ("22", "22 ") or "LIMITED_NUMBER" in msg.upper():
                self.limited = True
                raise BudgetExhausted(f"API 한도 초과: {msg}")
            if code in ("03",):  # NODATA
                return root
            if code in ("30", "31", "32", "33"):
                raise ApiError(f"인증키 오류({code}): {msg}")
            last_err = ApiError(f"API 오류({code}): {msg}")
            time.sleep(1.0 * (attempt + 1))
        raise last_err or ApiError("알 수 없는 오류")

    # ------------------------------------------------------------------
    def fetch_month(self, lawd_cd: str, deal_ymd: str) -> list[dict]:
        """시군구 코드 + YYYYMM 의 거래 전체 (페이지 넘김)."""
        out: list[dict] = []
        page = 1
        while True:
            root = self._get({"LAWD_CD": lawd_cd, "DEAL_YMD": deal_ymd, "pageNo": page, "numOfRows": ROWS})
            items = root.findall(".//item")
            for it in items:
                d = {child.tag: (child.text or "").strip() for child in it}
                out.append(d)
            total_txt = root.findtext(".//totalCount") or "0"
            try:
                total = int(total_txt)
            except ValueError:
                total = len(out)
            if len(items) < ROWS or len(out) >= total:
                break
            page += 1
        return out

    def count_month(self, lawd_cd: str, deal_ymd: str) -> tuple[int, list[dict]]:
        """numOfRows=1 로 totalCount 만 확인 (신설 구 탐색용). (건수, 첫 1건)."""
        root = self._get({"LAWD_CD": lawd_cd, "DEAL_YMD": deal_ymd, "pageNo": 1, "numOfRows": 1})
        total = int(root.findtext(".//totalCount") or "0")
        items = [{c.tag: (c.text or "").strip() for c in it} for it in root.findall(".//item")]
        return total, items
