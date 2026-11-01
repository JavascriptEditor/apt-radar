"""예시 데이터로 사이트를 만들어 봅니다 (실제 API 접속 없음 — 가짜 국토부·네이버·새올·서울허가 서버만).

python tests/make_demo.py demo_site [data_dir] && cd demo_site && python -m http.server 8000
(두 번째 인자를 주면 수집 데이터를 그 폴더에 남긴다 → python -m collector export --data <data_dir> --out <폴더> 로 재내보내기 확인 가능)

흐름(DESIGN 5절): run_trades → model → match → listings(1회) → [시나리오 심기] → listings(2회: 일부 매물 제거 → miss 2회 → gone)
→ toheo deep(새올·서울 허가) → export.

심어 두는 시나리오(허가·내림·실거래가 한 단지에서 연결돼 보이도록):
- 래미안블레스티지(11680-개포동-12-2019) 의 매물 하나가 두 번째 수집부터 사라져 gone 이벤트가 난다.
- 같은 단지·평형·층·동에 그 호가의 95% 실거래(3일 전)를 심는다 → 파이프라인이 confirmed 로 연결.
- 같은 단지(개포동 12)에 허가 2건(4·5일 전, 주거용)을 심는다 → 허가 ↔ 실거래 1:1 매칭.
- 은마(대치동 316) 매물 하나도 사라지지만 실거래는 없음 → pending 으로 남는다.
`build_world()` 는 tests/test_export.py 에서도 재사용한다.
"""
from __future__ import annotations

import datetime as dt
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector import engine as em  # noqa: E402
from collector.deals import normalize  # noqa: E402
from collector.engine import Engine, date_add, today_str  # noqa: E402
from collector.export import export_site  # noqa: E402
from collector.molit import MolitClient  # noqa: E402
from collector.naver import NaverClient, load_cfg  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.toheo.runner import run_toheo, run_toheo_sites  # noqa: E402
from tests import fake_servers as fs  # noqa: E402
from tests.fake_toheo import FakeHttp, FakeSeoulLand, FakeSite, make_permits, make_rows  # noqa: E402

DEMO_CID = "11680-개포동-12-2019"      # 래미안블레스티지 (fake_servers.WORLD)
DEMO_NAVER = "100001"
PENDING_NAVER = "100003"              # 은마: 매물은 사라지지만 실거래 없음 → pending


def _pick_listing(store: Store, cid: str, prefer_grp: str = "84") -> dict:
    rows = [r for r in store.read_listings().values() if r["cid"] == cid and r.get("price") and r.get("floor")]
    rows.sort(key=lambda r: (0 if r.get("grp") == prefer_grp else 1, r["atclNo"]))
    return rows[0]


def seed_trade(e: Engine, listing: dict, ratio: float = 0.95, days_ago: int = 3) -> dict:
    """매물과 같은 단지·면적·층·동에 호가 × ratio 의 실거래를 심고 모델을 다시 적합한다. 반환: 심은 거래 행."""
    c = e.complexes[listing["cid"]]
    d = dt.date.fromisoformat(date_add(today_str(), -days_ago))
    price = int(round(int(float(listing["price"])) * ratio / 100) * 100)
    raw = {"dealAmount": f"{price:,}", "excluUseAr": listing["area"], "dealYear": str(d.year), "dealMonth": str(d.month),
           "dealDay": str(d.day), "umdNm": c["umd"], "jibun": c["jibun"], "aptNm": c["name"], "buildYear": c["buildYear"],
           "floor": listing["floor"], "aptDong": f"{listing['dong']}동", "dealingGbn": "중개거래", "umdCd": c["umdCd"],
           "aptSeq": c.get("aptSeq", ""), "rgstDate": ""}
    row = normalize(raw, c["sgg"], e.group_of(c["sgg"]))
    assert row and row["cid"] == listing["cid"], row
    rows = e.store.read_trades_year(c["sgg"], d.year)
    rows = [r for r in rows if r["key"] != row["key"]] + [row]
    e.store.write_trades_year(c["sgg"], d.year, rows)
    e.run_model()
    return row


def make_toheo_http(today: dt.date, rng: random.Random, permit_targets: dict[str, list[tuple[str, str]]] | None = None,
                    extra_permits: list[tuple[str, str, str]] | None = None) -> tuple[FakeHttp, FakeSeoulLand]:
    """가짜 새올(강남·서초·성남) + 가짜 서울 허가. extra_permits: (sgg, 'YYYY-MM-DD', '강남구 개포동 12')."""
    http = FakeHttp()
    for host, code, dept, kw in (("gangnam.eminwon.seoul.kr", "32200", "도시환경국 부동산정보과", {}),
                                 ("seocho.eminwon.seoul.kr", "32300", "부동산정보과", {"page_param": "pageNo"}),
                                 ("eminwon.seongnam.go.kr", "38100", "분당구 시민봉사과", {"page_param": "pageNo"})):
        site = FakeSite(**kw)
        for r in make_rows(code, today - dt.timedelta(days=70), today, 6, dept, rng):
            site.add(r)
        http.route(host, site)
    land = FakeSeoulLand()
    targets = permit_targets or {"11680": [("대치동", "316"), ("개포동", "1234"), ("역삼동", "700")],
                                 "11650": [("반포동", "18-1"), ("반포동", "20")]}
    for i, (sgg, tg) in enumerate(targets.items()):
        make_permits(land, sgg, today, random.Random(100 + i), tg, n=30 if sgg == "11680" else 15)
    for j, (sgg, day, addr) in enumerate(extra_permits or []):
        land.add(sgg, f"{9000000 + j}", day, addr, use_purp="주거용", job="허가",
                 duty_end=(dt.date.fromisoformat(day) + dt.timedelta(days=730)).isoformat())
    http.route_seoul(land)
    return http, land


def build_world(data_root: Path, base: str, region: str = "11680,11650", log=None, seed: int = 5) -> tuple[Engine, dict]:
    """가짜 서버 base 로 전체 흐름을 돌린다(export 제외). 반환 (engine, scenario)."""
    today = today_str()
    store = Store(data_root)
    molit = MolitClient("demo", url=base + "/molit", call_budget=9500, sleep=0)
    naver = NaverClient(fs.naver_cfg_for(base, load_cfg()), state_path=data_root / "naver_cfg.json")
    e = Engine(store, molit, naver, region_filter=region, log=log or (lambda *a: None))
    e.run_trades(reserve=100)
    e.run_model()
    e.run_match()
    fs.Handler.articles_override = {}
    e.run_listings()                                    # 1회: 전부 new

    # ---- 시나리오: 내림 매물 + 95% 실거래 + 허가 2건
    gone = _pick_listing(store, DEMO_CID)
    pending_cid = next(c["cid"] for c in e.complexes.values() if c.get("naverId") == PENDING_NAVER)
    pend = _pick_listing(store, pending_cid)
    trade = seed_trade(e, gone, ratio=0.95, days_ago=3)
    base_arts = fs.gen_articles(DEMO_NAVER, em.this_ym())
    pend_arts = fs.gen_articles(PENDING_NAVER, em.this_ym())
    fs.Handler.articles_override = {DEMO_NAVER: [a for a in base_arts if a["atclNo"] != gone["atclNo"]],
                                    PENDING_NAVER: [a for a in pend_arts if a["atclNo"] != pend["atclNo"]]}
    e.run_listings()                                    # 2회: miss=1
    e.run_listings()                                    # 3회: miss=2 → gone (LISTING_MISS_GONE=2)
    fs.Handler.articles_override = {}

    # ---- 토허: 20일 전 깊은 수집(처리중으로 본 민원) → 오늘 깊은 수집(완료 관측 → 소요일·예상 승인일)
    tday = dt.date.fromisoformat(today)
    extra = [("11680", date_add(today, -5), "강남구 개포동 12"), ("11680", date_add(today, -4), "강남구 개포동 12")]
    http, land = make_toheo_http(tday, random.Random(seed), extra_permits=extra)
    past = dt.datetime.combine(tday - dt.timedelta(days=20), dt.time(9, 0))
    for site in http.sites.values():
        site.now = past.date().isoformat()
    run_toheo_sites(deep=True, data_dir=data_root, region_filter=region, complexes=e.complexes, fetch_log=e.fetch_log,
                    permits=False, http=http, live=e.live, now=past)
    for site in http.sites.values():
        site.now = None
    toheo = run_toheo(e, deep=True, region_filter=region, permits=True, http=http)
    scenario = {"today": today, "cid": DEMO_CID, "atclNo": gone["atclNo"], "grp": gone["grp"], "price": int(float(gone["price"])),
                "floor": gone["floor"], "dong": gone["dong"], "trade": trade, "pendingCid": pending_cid,
                "pendingAtclNo": pend["atclNo"], "permitDays": [x[1] for x in extra], "toheo": toheo, "http": http, "land": land}
    return e, scenario


def main(out: str, data_dir: str | None = None) -> None:
    """out 에 데모 사이트를 만든다. data_dir 을 주면 수집 데이터(data 브랜치 형식)를 거기 남긴다
    (`python -m collector export --data <data_dir> --out <폴더>` 로 다시 내보내 볼 수 있음)."""
    srv, base = fs.start(now_ym=em.this_ym())
    tmp = Path(data_dir) if data_dir else Path(tempfile.mkdtemp()) / "data"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        e, sc = build_world(tmp, base, log=print)
        out_p = Path(out)
        out_p.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "site" / "index.html", out_p / "index.html")
        print(export_site(e, out_p, site_url="http://localhost:8000"))
        print(f"시나리오: {sc['cid']} 매물 {sc['atclNo']} ({sc['grp']}㎡ {sc['dong']}동 {sc['floor']}층 호가 {sc['price']}) 내림 → "
              f"실거래 {sc['trade']['date']} {sc['trade']['price']} · 허가 {', '.join(sc['permitDays'])}")
    finally:
        srv.shutdown()
    print(f"→ cd {out} && python -m http.server 8000")
    if data_dir:
        print(f"→ 수집 데이터: {tmp} (python -m collector export --data {tmp} --out <폴더>)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_site", sys.argv[2] if len(sys.argv) > 2 else None)
