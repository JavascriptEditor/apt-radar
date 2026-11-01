"""실거래 레코드 정규화, 단지 식별자, 면적 그룹, 동·층 정규화."""
from __future__ import annotations

import re

TRADE_FIELDS = ["key", "cid", "aptSeq", "name", "sgg", "umdCd", "umd", "jibun", "buildYear",
                "area", "grp", "dong", "floor", "date", "ym", "price", "direct", "cancelled",
                "cdate", "rgst", "miss"]


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(s: str | None) -> int | None:
    """'140,000' → 140000 (만원)."""
    v = _num(s)
    return int(round(v)) if v is not None else None


_DONG_STRIP = re.compile(r"[\s()\[\]]")


def norm_dong(s: str | None) -> str:
    """'제101동' '101동' '101' → '101', '가동' → '가', 'A동' → 'A', 빈값 → ''."""
    if not s:
        return ""
    t = _DONG_STRIP.sub("", str(s)).upper()
    t = re.sub(r"^제", "", t)
    t = re.sub(r"동$", "", t) if len(t) > 1 else t
    t = re.sub(r"^0+(\d)", r"\1", t)  # 0101 → 101
    return t


def area_group(area: float | None) -> int | None:
    """전용면적 정수부. 59.97 → 59, 84.95 → 84."""
    if area is None:
        return None
    return int(area)


def floor_of(s: str | None) -> int | None:
    v = _num(s)
    if v is None:
        return None
    return int(v)


_CID_UNSAFE = re.compile(r"[^0-9A-Za-z가-힣\-_.]")


def complex_id(group: str, umd: str, jibun: str, build_year: str) -> str:
    """단지 키 '{묶음}-{법정동}-{지번}-{건축년도}'. 사이트 파일 이름(data/c/<cid>.json)이 되므로
    외부 응답 문자열에서 경로 구분자·'..' 같은 것은 '_' 로 바꾼다."""
    j = (jibun or "").strip().replace(" ", "")
    parts = [_CID_UNSAFE.sub("_", str(x or "")).replace("..", "_") for x in (umd, j, build_year)]
    return f"{group}-{parts[0]}-{parts[1]}-{parts[2]}"


def normalize(raw: dict, sgg: str, group: str) -> dict | None:
    """API item → 내부 레코드. 가격·면적·날짜가 없으면 None."""
    price = parse_price(raw.get("dealAmount") or raw.get("거래금액"))
    area = _num(raw.get("excluUseAr") or raw.get("전용면적"))
    y, m, d = raw.get("dealYear") or raw.get("년"), raw.get("dealMonth") or raw.get("월"), raw.get("dealDay") or raw.get("일")
    if price is None or area is None or not (y and m and d):
        return None
    try:
        date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None
    umd = (raw.get("umdNm") or raw.get("법정동") or "").strip()
    jibun = (raw.get("jibun") or raw.get("지번") or "").strip()
    name = (raw.get("aptNm") or raw.get("아파트") or "").strip()
    build = (raw.get("buildYear") or raw.get("건축년도") or "").strip()
    floor = floor_of(raw.get("floor") or raw.get("층"))
    dong = norm_dong(raw.get("aptDong") or raw.get("동"))
    cancelled = (raw.get("cdealType") or raw.get("해제여부") or "").strip().upper() == "O"
    cdate = (raw.get("cdealDay") or raw.get("해제사유발생일") or "").strip()
    gbn = (raw.get("dealingGbn") or raw.get("거래유형") or "").strip()
    direct = "직거래" in gbn
    rgst = (raw.get("rgstDate") or raw.get("등기일자") or "").strip()
    umd_cd = (raw.get("umdCd") or "").strip()
    apt_seq = (raw.get("aptSeq") or "").strip()
    cid = complex_id(group, umd, jibun, build)
    key = "|".join([cid, f"{area:.2f}", date, str(price), str(floor if floor is not None else ""), dong])
    return {
        "key": key, "cid": cid, "aptSeq": apt_seq, "name": name, "sgg": sgg, "umdCd": umd_cd, "umd": umd,
        "jibun": jibun, "buildYear": build, "area": round(area, 2), "grp": area_group(area), "dong": dong,
        "floor": floor if floor is not None else "", "date": date, "ym": date[:7].replace("-", ""),
        "price": price, "direct": 1 if direct else 0, "cancelled": 1 if cancelled else 0,
        "cdate": cdate, "rgst": rgst, "miss": 0,
    }


def parse_listing_price(s: str | None) -> int | None:
    """네이버 호가 문자열 → 만원. '14억 5,000' → 145000, '28억' → 280000, '9,500' → 9500."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace(" ", "").strip()
    if not t:
        return None
    m = re.match(r"^(?:(\d+(?:\.\d+)?)억)?(\d+)?(?:만)?$", t)
    if not m:
        v = _num(t)
        return int(v) if v is not None else None
    eok, man = m.group(1), m.group(2)
    total = 0.0
    if eok:
        total += float(eok) * 10000
    if man:
        total += float(man)
    return int(round(total)) if total > 0 else None


def parse_floor_info(s: str | None) -> tuple[int | None, int | None]:
    """'3/15' → (3, 15), '고/15' → (None, 15), '15' → (15, None)."""
    if not s:
        return None, None
    t = str(s).strip()
    if "/" in t:
        a, b = t.split("/", 1)
        fa, fb = _num(a), _num(b)
        return (int(fa) if fa is not None else None), (int(fb) if fb is not None else None)
    v = _num(t)
    return (int(v) if v is not None else None), None


def floor_hint(s: str | None) -> str:
    """'저/15' 같은 표기에서 저·중·고 힌트."""
    if not s:
        return ""
    t = str(s)
    for k in ("저", "중", "고"):
        if t.startswith(k):
            return k
    return ""
