"""명령줄.

python -m collector run        실거래 재조회·적재 (+ 월 1회 신설 구 탐색) → 적재 끝난 묶음 모델 적합 → 단지 매칭
python -m collector listings   네이버 매물 수집
python -m collector model      기준가 모델만 다시 적합 (--partial: 적재 중인 묶음도)
python -m collector match      네이버 단지 매칭만
python -m collector export     사이트 데이터 생성 (--out site, --notify: 텔레그램 요약 알림)
python -m collector toheo      토허제 수집: 새올 접수 현황 + 서울 허가 내역 (--deep --no-permits --discover)
python -m collector scan       신설 구 코드 탐색 (--force)
python -m collector probe      네이버 응답 구조 확인 (--cortar 1168010300 / --complex 8928)
python -m collector status     진행 상황 출력
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import Engine
from .molit import MolitClient
from .naver import NaverClient, load_cfg
from .store import Store


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def build(args, need_molit=False, need_naver=False) -> Engine:
    store = Store(args.data)
    molit = naver = None
    key = os.environ.get("MOLIT_SERVICE_KEY", "")
    if need_molit:
        if not key:
            sys.exit("MOLIT_SERVICE_KEY 가 없습니다 (공공데이터포털 인증키).")
        molit = MolitClient(key, url=os.environ.get("MOLIT_API_URL"), call_budget=env_int("DAILY_CALL_LIMIT", 9500),
                            calls_used=0)
    if need_naver:
        cfg_path = os.environ.get("NAVER_CFG") or (args.naver_cfg if hasattr(args, "naver_cfg") else None)
        naver = NaverClient(load_cfg(cfg_path), state_path=os.path.join(args.data, "naver_cfg.json"),
                            max_calls=env_int("NAVER_MAX_CALLS", 1500))
    else:
        # export 에서 링크 형식을 쓰기 위해 설정만 로드
        naver = NaverClient(load_cfg(os.environ.get("NAVER_CFG")), state_path=os.path.join(args.data, "naver_cfg.json"),
                            max_calls=0)
    return Engine(store, molit, naver, region_filter=args.region)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="collector")
    p.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    p.add_argument("--region", default=os.environ.get("REGION_FILTER"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run")
    s.add_argument("--reserve", type=int, default=env_int("CALL_RESERVE", 800))
    s.add_argument("--no-model", action="store_true")
    s.add_argument("--no-match", action="store_true")
    s.add_argument("--partial", action="store_true", help="적재 중인 묶음도 모델 적합")
    s = sub.add_parser("listings")
    s.add_argument("--max-calls", type=int, default=env_int("NAVER_MAX_CALLS", 1500))
    s.add_argument("--max-sec", type=int, default=env_int("NAVER_MAX_SEC", 2400))
    s = sub.add_parser("model")
    s.add_argument("--partial", action="store_true")
    s.add_argument("--group", action="append")
    s = sub.add_parser("match")
    s.add_argument("--max-dongs", type=int, default=env_int("MATCH_MAX_DONGS", 400))
    s = sub.add_parser("export")
    s.add_argument("--out", default="site")
    s.add_argument("--notify", action="store_true", help="export 뒤 텔레그램 요약 알림(collector.notify.send_digest)")
    s = sub.add_parser("toheo")
    s.add_argument("--deep", action="store_true", help="깊은 수집(DEEP_DAYS, 기본 45일). 기본은 가벼운 수집(LIGHT_DAYS)")
    s.add_argument("--no-permits", action="store_true", help="서울 허가 내역 수집 생략")
    s.add_argument("--discover", action="store_true", help="새올 파라미터·허가 요청 방식 자동 탐지 다시 실행")
    s.add_argument("--toheo-region", default=os.environ.get("TOHEO_REGION_FILTER"),
                   help="토허 대상 지역 필터(없으면 REGION_FILTER 중 토허 대상만)")
    s = sub.add_parser("scan")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("probe")
    s.add_argument("--cortar")
    s.add_argument("--complex")
    sub.add_parser("status")
    args = p.parse_args(argv)

    if args.cmd == "run":
        eng = build(args, need_molit=True, need_naver=not args.no_match)
        print(json.dumps(eng.run_scan(), ensure_ascii=False))
        summary = eng.run_trades(reserve=args.reserve)
        print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in summary.items()}, ensure_ascii=False))
        if not args.no_model:
            print(json.dumps(eng.run_model(partial=args.partial), ensure_ascii=False))
        if not args.no_match:
            print(json.dumps(eng.run_match(), ensure_ascii=False))
        return 0
    if args.cmd == "listings":
        eng = build(args, need_naver=True)
        print(json.dumps(eng.run_listings(max_calls=args.max_calls, max_sec=args.max_sec), ensure_ascii=False))
        return 0
    if args.cmd == "model":
        eng = build(args)
        print(json.dumps(eng.run_model(partial=args.partial, groups=args.group), ensure_ascii=False))
        return 0
    if args.cmd == "match":
        eng = build(args, need_naver=True)
        print(json.dumps(eng.run_match(max_dongs=args.max_dongs), ensure_ascii=False))
        return 0
    if args.cmd == "export":
        from .export import export_site
        eng = build(args)
        print(json.dumps(export_site(eng, args.out), ensure_ascii=False))
        if args.notify:
            try:
                from .notify import send_digest
            except ImportError as e:   # notify.py 가 없는 배포에서도 export 는 성공
                print(json.dumps({"notify": {"skipped": True, "reason": f"notify 모듈 없음: {e}"}}, ensure_ascii=False))
            else:
                try:
                    res = send_digest(eng, os.environ.get("SITE_URL"))
                except Exception as e:  # noqa: BLE001 — 알림 실패가 export 를 실패로 만들지 않는다
                    res = {"sent": False, "error": str(e)}
                print(json.dumps({"notify": res}, ensure_ascii=False))
        return 0
    if args.cmd == "toheo":
        # MOLIT_SERVICE_KEY 없이도 동작(단지 매칭은 complexes.csv 가 있을 때만 채워짐)
        from .toheo.runner import run_toheo
        eng = build(args)
        res = run_toheo(eng, deep=args.deep, region_filter=args.toheo_region or args.region,
                        permits=not args.no_permits, discover=args.discover)
        print(json.dumps({k: v for k, v in res.items() if k != "site_info"}, ensure_ascii=False))
        for sid, info in (res.get("site_info") or {}).items():
            print(f"  {sid}: ok={info.get('ok')} host={info.get('host')} pages={info.get('pages')} "
                  f"rows={info.get('toheo_rows')} new={info.get('new')} err={info.get('error') or ''}")
        return 0
    if args.cmd == "scan":
        eng = build(args, need_molit=True)
        print(json.dumps(eng.run_scan(force=args.force), ensure_ascii=False))
        return 0
    if args.cmd == "probe":
        nv = NaverClient(load_cfg(os.environ.get("NAVER_CFG")), max_calls=20)
        if args.cortar:
            cx = nv.complexes(args.cortar)
            print(f"단지 {len(cx)}건 (endpoint={nv.state.get('complexes_by_cortar')})")
            for c in cx[:10]:
                print(" ", json.dumps(c, ensure_ascii=False))
        if args.complex:
            arts = nv.articles(args.complex, max_pages=2)
            print(f"매물 {len(arts)}건 (endpoint={nv.state.get('articles')})")
            for a in arts[:10]:
                print(" ", json.dumps(a, ensure_ascii=False))
        if nv.blocked_reason:
            print("차단:", nv.blocked_reason)
        for line in nv.log:
            print(" ", line)
        return 0
    if args.cmd == "status":
        eng = build(args)
        bf = eng.progress.get("backfill", {})
        for code in eng.codes:
            st = bf.get(code, {})
            print(code, "done" if st.get("done") else f"cursor={st.get('cursor', '-')}")
        print(json.dumps({k: v for k, v in eng.progress.items() if k != "backfill"}, ensure_ascii=False, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
