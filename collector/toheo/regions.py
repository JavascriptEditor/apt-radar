"""토허 수집 대상 지역 정의 — 새올 Site 와 LAWD 시군구 코드(sgg) 의 대응.

- 지역 식별자는 급매판정 `collector/regions.py` 의 LAWD 5자리 코드 하나로 통일합니다.
  화성시만 시 전체 코드 `41590` 에 동탄구 서브키 `"41590:dongtan"` 을 둡니다.
- Site: 새올 전자민원창구 하나(서울 자치구 25 + 경기 시 10). 시 단위 새올(성남·수원·안양·용인·화성)은
  처리부서 문자열(dept_rules)로 구 코드를 정합니다. 구를 알 수 없으면 시 코드(site.id)를 그대로 둡니다.
- Site.id 는 서울 자치구/단일 구 시는 그 LAWD 코드, 시 단위 새올은 시 코드(성남 41130, 수원 41110,
  안양 41170, 용인 41460, 화성 41590) 입니다. cases.csv 의 site 열과 fetch_log['toheo'].sites 의 키로 쓰입니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import regions as base

LIST_PATH = (
    "/emwp/gov/mogaha/ntis/web/caf/mwwd/action/CafMwWdOpenAction.do"
    "?method=selectListMwOpn&menu_id=CAFOPNWebMwOpenL&jndinm=CafMwWdOpenEJB"
    "&methodnm=selectListMwOpn&context=NTIS"
)

DONGTAN_KEY = "41590:dongtan"
UNKNOWN_SUB = "unknown"        # '<시코드>:unknown' = 시 단위 새올의 구 미분류(시 코드가 지역 id 와 겹치는 화성시용)

# 화성시 동탄구 법정동(시 전체 코드로 조회한 단지를 동탄구로 분류할 때 사용)
DONGTAN_DONGS = {
    "동탄동", "반송동", "석우동", "능동", "오산동", "청계동", "영천동", "중동", "신동", "목동",
    "산척동", "장지동", "송동", "방교동", "금곡동", "동탄면", "신리", "장지리",
}


@dataclass
class Site:
    id: str                       # '11680' (서울 강남구), '41130' (성남시)
    sido: str                     # '서울' | '경기'
    name: str                     # '강남구', '성남시'
    hosts: list[str]              # 후보 호스트(우선순위 순). 'http://' 로 시작하면 http 폴백
    sggs: list[str]               # 이 사이트가 담당하는 sgg 목록 ('41590:dongtan' 서브키 포함)
    dept_rules: dict[str, list[str]] = field(default_factory=dict)   # sgg → 처리부서 포함 문자열

    def list_urls(self) -> list[str]:
        out = []
        for h in self.hosts:
            if h.startswith("http://") or h.startswith("https://"):
                out.append(h.rstrip("/") + LIST_PATH)
            else:
                out.append(f"https://{h}{LIST_PATH}")
        return out

    @property
    def city_code(self) -> str:
        """구 미분류 민원에 붙일 코드(시 단위 새올은 시 코드, 그 외는 담당 sgg).

        화성시처럼 시 코드(41590)가 그 자체로 '동탄 외' 지역 id 이기도 하면 '41590:unknown' 을 써서
        '구 미분류' 와 '동탄 외' 를 구분한다(그렇지 않으면 부서가 없는 민원이 전부 '동탄 외' 로 확정 표시됨)."""
        if len(self.sggs) > 1 and self.id in self.sggs:
            return f"{self.id}:{UNKNOWN_SUB}"
        return self.id


_SEOUL = [
    ("jongno", "jongno.go.kr", "11110"), ("junggu", "junggu.seoul.kr", "11140"), ("yongsan", "yongsan.go.kr", "11170"),
    ("sd", "sd.go.kr", "11200"), ("gwangjin", "gwangjin.go.kr", "11215"), ("ddm", "ddm.go.kr", "11230"),
    ("jungnang", "jungnang.go.kr", "11260"), ("sb", "sb.go.kr", "11290"), ("gangbuk", "gangbuk.go.kr", "11305"),
    ("dobong", "dobong.go.kr", "11320"), ("nowon", "nowon.go.kr", "11350"), ("ep", "ep.go.kr", "11380"),
    ("sdm", "sdm.go.kr", "11410"), ("mapo", "mapo.go.kr", "11440"), ("yangcheon", "yangcheon.go.kr", "11470"),
    ("gangseo", "gangseo.seoul.kr", "11500"), ("guro", "guro.go.kr", "11530"), ("geumcheon", "geumcheon.go.kr", "11545"),
    ("ydp", "ydp.go.kr", "11560"), ("dongjak", "dongjak.go.kr", "11590"), ("gwanak", "gwanak.go.kr", "11620"),
    ("seocho", "seocho.go.kr", "11650"), ("gangnam", "gangnam.go.kr", "11680"), ("songpa", "songpa.go.kr", "11710"),
    ("gangdong", "gangdong.go.kr", "11740"),
]

SITES: list[Site] = []

for _slug, _dom, _code in _SEOUL:
    _hosts = [f"{_slug}.eminwon.seoul.kr", f"eminwon.{_dom}", f"eminwon.{_slug}.go.kr"]
    if _slug == "gwanak":
        _hosts.insert(1, "gwanakgu.eminwon.seoul.kr")
    if _slug == "nowon":
        _hosts.append(f"http://{_slug}.eminwon.seoul.kr")   # 노원구 http 폴백
    SITES.append(Site(_code, "서울", base.name_of(_code), _hosts, [_code]))


def _gg(city_code: str, slug: str, domain: str, city: str, subs: list[tuple[str, str]]) -> None:
    """경기 시 단위 새올. subs: (sgg, 처리부서 매칭 키). 구가 없는 시는 subs 한 개(키 '')."""
    sggs = [s for s, _ in subs]
    rules = {s: [k] for s, k in subs if k}
    SITES.append(Site(city_code, "경기", city, [f"eminwon.{domain}", f"eminwon.{slug}.go.kr"], sggs, rules))


_gg("41290", "gccity", "gccity.go.kr", "과천시", [("41290", "")])
_gg("41210", "gm", "gm.go.kr", "광명시", [("41210", "")])
_gg("41130", "seongnam", "seongnam.go.kr", "성남시", [("41135", "분당구"), ("41131", "수정구"), ("41133", "중원구")])
_gg("41110", "suwon", "suwon.go.kr", "수원시", [("41117", "영통구"), ("41111", "장안구"), ("41115", "팔달구"), ("41113", "권선구")])
_gg("41170", "anyang", "anyang.go.kr", "안양시", [("41173", "동안구"), ("41171", "만안구")])
_gg("41460", "yongin", "yongin.go.kr", "용인시", [("41465", "수지구"), ("41463", "기흥구"), ("41461", "처인구")])
_gg("41430", "uw21", "uw21.net", "의왕시", [("41430", "")])
_gg("41450", "hanam", "hanam.go.kr", "하남시", [("41450", "")])
_gg("41590", "hscity", "hscity.go.kr", "화성시", [(DONGTAN_KEY, "동탄구"), ("41590", "")])
_gg("41310", "guri", "guri.go.kr", "구리시", [("41310", "")])

SITE_BY_ID: dict[str, Site] = {s.id: s for s in SITES}
SITE_BY_SGG: dict[str, Site] = {}
for _s in SITES:
    SITE_BY_SGG.setdefault(_s.id, _s)
    for _g in _s.sggs:
        SITE_BY_SGG.setdefault(_g, _s)

# 사이트 표시 순서의 토허 지역 id 전체(실제 구 단위 지역만. 구 미분류용 시 코드 41130 등은 SITE_IDS 에만 있음)
ALL_SGGS: list[str] = []
for _s in SITES:
    for _g in _s.sggs:
        if _g not in ALL_SGGS:
            ALL_SGGS.append(_g)
SITE_IDS: list[str] = [s.id for s in SITES]


def sgg_for_case(site: Site, dept: str) -> str:
    """처리부서 문자열 → sgg. 규칙에 안 맞으면 단일 구 사이트는 sggs[0], 시 단위 새올은 시 코드."""
    if len(site.sggs) == 1:
        return site.sggs[0]
    for sgg, keys in site.dept_rules.items():
        if any(k and k in (dept or "") for k in keys):
            return sgg
    return site.city_code


def base_code(sgg: str) -> str:
    """'41590:dongtan' → '41590'. 실거래·단지 인덱스와 맞출 때 쓰는 LAWD 코드."""
    return sgg.split(":")[0]


def toheo_sgg_for(sgg: str, umd: str | None = None) -> str:
    """단지(sgg, 법정동) → 토허 지역 id. 화성시 동탄 법정동이면 '41590:dongtan'."""
    if sgg == "41590" and umd and umd in DONGTAN_DONGS:
        return DONGTAN_KEY
    return sgg


def sido_of(sgg: str) -> str:
    return "서울" if sgg.startswith("11") else ("경기" if sgg.startswith("41") else base.SIDO_NAME.get(sgg[:2], sgg[:2]))


def label(sgg: str, live: dict | None = None) -> str:
    """'강남구', '성남 분당구', '화성 동탄구', 시 코드는 '성남시(구 미분류)'."""
    if sgg == DONGTAN_KEY:
        return "화성 동탄구"
    if sgg.endswith(":" + UNKNOWN_SUB):
        site = SITE_BY_ID.get(base_code(sgg))
        return f"{site.name if site else base_code(sgg)}(구 미분류)"
    site = SITE_BY_ID.get(sgg)
    if site and len(site.sggs) > 1 and sgg not in site.sggs:
        return f"{site.name}(구 미분류)"
    if sgg == "41590":
        return "화성시(동탄 외)"
    if sgg in base.BY_CODE or (live and sgg in live):
        return base.short_name(sgg, live)
    return sgg


toheo_region_label = label


def toheo_sggs(filter_spec: str | None, live: dict | None = None) -> list[str]:
    """REGION_FILTER 해석(급매판정 regions.parse_filter 재사용) → 토허 대상 sgg 목록(사이트 순서).

    '화성' 또는 '41590' 이 들어오면 '41590:dongtan' 도 함께 포함합니다. 인천 등 비대상 코드는 버립니다.
    """
    codes = set(base.parse_filter(filter_spec, live))
    # 사람이 서브키를 직접 적은 경우
    for tok in (filter_spec or "").replace(" ", "").split(","):
        if tok in SITE_BY_SGG:
            codes.add(tok)
    out: list[str] = []
    for sgg in ALL_SGGS:
        site = SITE_BY_SGG[sgg]
        if sgg in codes or base_code(sgg) in codes or (site.id in codes and sgg in site.sggs):
            out.append(sgg)
    return out


def sites_for(sggs: list[str]) -> list[Site]:
    """sgg 목록이 걸리는 Site 들(정의 순서, 중복 없이)."""
    want = set(sggs)
    out = []
    for s in SITES:
        if s.id in want or any(g in want for g in s.sggs):
            out.append(s)
    return out


def is_seoul(sgg: str) -> bool:
    return sgg.startswith("11")
