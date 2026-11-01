# apt-radar 설계서 (구현 계약)

급매판정(apt-geummae) + 토허제 라이브(apt-toheo-live)를 **하나의 저장소·하나의 수집기·하나의 정적 사이트**로 합친다.
이 문서는 구현 에이전트들이 공유하는 계약이다. 여기 적힌 파일명·필드명·함수 시그니처는 바꾸지 말 것(필요하면 추가만).

참조 원본: `/home/user/_ref_geummae` (급매판정, 이 저장소의 베이스로 이미 복사됨), `/home/user/_ref_toheo` (토허제 라이브, 포팅 대상).

## 0. 조사로 확정된 사실 (설계 전제)

| 사실 | 설계 반영 |
|---|---|
| 서울 허가 내역은 `POST https://land.seoul.go.kr/land/wsklis/getContractList.do` form `sggCd`(5자리)·`beginDate`·`endDate`(YYYYMMDD), 먼저 `GET /land/other/contractStatus.do`로 세션 쿠키, 헤더 `X-Requested-With: XMLHttpRequest`, `Referer: https://land.seoul.go.kr/land/`. 응답 JSON 필드: `SGG_CD, ACC_YEAR, ACC_NO, OBJ_SEQNO, HNDL_YMD(처리일), ADDRESS('용산구 한남동 810'), BOBN, BUBN, JIMOK, USE_PURP, JOB_GBN_NM(허가/불허가/취소/취하/반려/미결), DEAL_END_YMD, EXEC_YMD`. 최대 62일 창, **면적·층·가격·단지명·접수일 없음**, 처리 완료 건만. (hannamhyundai/hannamhyundai.github.io 가 2026-10-31까지 실수집) | `collector/toheo/seoul_permits.py` 는 이 엔드포인트를 기본값으로 쓰고, 응답 키는 alias 로 읽는다. 원본 JSON 을 `data/toheo/raw/seoul/<YYYY-MM-DD>/<sgg>.json.gz` 로 보관. 허가 건수는 `JOB_GBN_NM=='허가'` 만. |
| 새올 서식민원처리공개는 접수번호·접수일·민원명·처리기한·상태·처리부서 6열뿐, 단지 없음. '처리중'부터 노출. | 구 단위 처리중 건수·소요일·예상 승인일만 산출(토허 원본 로직 유지). |
| 경기도는 허가 내역 비공개. 인천은 내국인 아파트 토허구역 없음(2026-08 관보까지). | 경기는 '지정 + 접수 추세'만, UI 에 명시. 인천은 토허 수집 대상 제외. |
| **'접수된 단지·평형'은 어떤 공개 데이터에도 없다.** 단지·평형 수준의 유일한 선행 신호는 **네이버 매물이 내려감**(평형·호가·동·층을 이미 안다). 허가(지번→단지) 와 실거래(평형·가격)로 사후 확인 가능. | 핵심 신기능 = **거래 파이프라인**: 매물 내림(추정, 단지·평형) → 구 처리중(새올) → 허가 완료(서울, 단지) → 실거래 확인(평형·가격). 모든 추정에 '추정' 라벨 + 적중률 공개. |
| 네이버 비공식 API 는 2026-10-28 이후 `fin.land.naver.com/front-api/v1` 로 단일화, GitHub Actions(해외 IP)에서는 429/차단이 일관되게 보고됨. m.land 모바일 경로 생존 여부 미확인. | 매물 수집은 Actions 에서도 시도하되(차단 감지 시 즉시 중단) **집 PC 수집을 공식 1순위 경로**로 문서화·스크립트화. config/naver.json 에 fin.land 엔드포인트 추가. |
| 토허구역: 국토부 2025-1219호 서울 전역+경기 12곳 2025-10-20~2026-12-31(연장 미확정), 경기도 2026-06-30 공고 동탄·기흥·구리 ~2027-12-31, 서울시 압구정·여의도·목동·성수 ~2027-04-26, 외국인등 2026-1105호 ~2027-08-25, 신규 주택공급 후보지 2026-1079호 ~2026-12-31(허가면적 60㎡라 아파트 무관) 등. 허가 처리기한 15일, 실거주 2년. | `config/designations.json` 확장 필드(notice_no, threshold_m2, target_person, apt_relevant, verified_at, renewal_status). |

## 1. 저장소 구조

```
collector/                      단일 패키지 (급매판정 베이스 유지)
  regions.py  molit.py  deals.py  model.py  judge.py  naver.py   ← 유지(naver.py 일부 수정)
  engine.py                     run_trades/scan/match/listings/model — listings 소프트 삭제·이벤트 기록으로 수정
  store.py                      LISTING_FIELDS 확장 + listing_events.csv 입출력 추가
  pipeline.py                   [신규] 파이프라인 연결(허가↔단지, 내림↔실거래, 허가↔실거래, 추정 점수, 적중률)
  export.py                     site/data/*.json + 토허 JSON + RSS (확장)
  notify.py                     [신규] 텔레그램 알림(선택)
  __main__.py                   run / listings / model / match / export / scan / probe / status / toheo
  toheo/                        [신규 서브패키지: 토허제 라이브 포팅]
    __init__.py
    regions.py                  새올 Site 정의, LAWD 코드 ↔ site, 처리부서→구 분류
    http.py                     Polite 세션(간격·재시도·디코딩)
    saeol.py                    새올 목록 파서·파라미터 탐지·페이지 넘김
    cases.py                    민원 상태 누적(merge_rows)·지역 통계(region_stats, estimate)
    seoul_permits.py            서울 허가 내역(getContractList.do) 수집·파싱·단지 매칭
    designations.py             지정 현황 판정
    store.py                    data/toheo/* 입출력
    runner.py                   run_toheo(light/deep) 오케스트레이션 + fetch_log['toheo'] 작성
config/
  naver.json                    (fin.land 엔드포인트 추가)
  designations.json             (확장 스키마, 2026-11 기준 항목)
  seoul_permits.json            getContractList.do 기본 요청 설정(수동 수정 가능)
  saeol_params.example.json
  alerts.example.json           텔레그램 알림 구독 설정 예시
site/index.html                 단일 정적 페이지: 탐색·단지·토허·알림·비교·트렌드·내정보
scripts/home_listings.sh, home_listings.ps1   집 PC 매물 수집 → data 브랜치 푸시
.github/workflows/collect.yml   Actions: trades / listings / toheo / export
.github/workflows/listings-home.yml   self-hosted 러너용(선택)
tests/  fake_servers.py(국토부·네이버) fake_toheo.py(새올·서울허가) test_flow.py test_listings.py test_toheo.py test_pipeline.py test_export.py make_demo.py
README.md  DESIGN.md
```

지역 식별자는 **LAWD 5자리 시군구 코드(`sgg`)** 하나로 통일한다. 토허 원본의 `seoul-gangnam` 식 id 는 쓰지 않는다. 시 단위 새올(성남·수원·안양·용인·화성)은 처리부서 문자열로 구 코드를 정한다. 화성시는 `41590`(시 전체) 코드에 `dongtan`(동탄구) 서브키를 둔다: 토허 region id = `"41590"` 과 `"41590:dongtan"` 을 허용(regions.py 에서 `toheo_region_label()` 로 이름 변환). 신설 구 코드가 `regions_live.json` 에서 발견되면 그 코드를 우선한다.

## 2. data 브랜치 레이아웃 (`DATA_DIR`, 기본 `data/`)

기존(급매판정) 파일은 그대로. 추가·변경:

### listings.csv (변경: 소프트 삭제)
`LISTING_FIELDS = [atclNo,cid,naverId,dongRaw,dong,floor,top,floorHint,area,supply,grp,price,confirmYmd,direct,sameCnt,realtor,direction,desc,tags,firstSeen,lastSeen,status,goneAt,miss,priceFirst,priceChanges]`
(기존 fair/diffPct/grade/reasons/conf 죽은 컬럼은 제거)
- `status`: `active` | `gone`
- `miss`: 이 단지를 **성공적으로** 조회했는데 이 매물이 없었던 연속 횟수
- `goneAt`: status 가 gone 으로 바뀐 날(YYYY-MM-DD)
- `priceFirst`: 처음 본 호가, `priceChanges`: 호가 변경 횟수

### listing_events.csv (신규)
`EVENT_FIELDS = [date,atclNo,cid,grp,area,dong,floor,event,price,prevPrice]`, `event ∈ {new, price, gone, relist}`. 날짜 오름차순 append, 400일 지난 행은 정리.

### 매물 상태 기계 (engine.run_listings)
1. `NaverClient.articles()` 는 `(arts, ok: bool)` 를 돌려주도록 수정(`articles_ok()` 신설, 기존 `articles()` 는 그대로 list 반환 유지). `ok=False`(http 오류·빈 HTML·예외)면 **그 단지의 매물 상태를 전혀 바꾸지 않는다.**
2. 의심 응답 가드: 직전 `listingsN >= 5` 였는데 이번 결과가 0건이면 `ok=False` 로 취급하고 `fetch_log.listings.suspect += 1`.
3. ok 인 경우: 응답에 있는 매물 → `status=active, miss=0, lastSeen=today`; 기존 `gone` 이었으면 `relist` 이벤트. 호가 변경 시 `price` 이벤트(`priceChanges += 1`). 신규면 `new` 이벤트.
4. 응답에 없는 기존 active 매물 → `miss += 1`; `miss >= 2` 가 되는 순간 `status=gone, goneAt=today`, `gone` 이벤트.
5. `gone` 상태 행은 `goneAt` 로부터 120일 지나면 listings.csv 에서 삭제(이벤트는 남음).
6. 같은 집 중복 매물 교체(대표 매물 변경)는 `sameCnt>1` 이고 같은 cid·grp·dong·floor·price 의 active 매물이 남아 있으면 `gone` 대신 `swap` 으로 간주해 이벤트를 남기지 않는다(상태만 gone).

### data/toheo/ (신규, 토허 원본 포팅)
- `cases.csv` `CASE_FIELDS = [site, sgg, no, received, name, deadline, dept, status, first_seen, last_seen, status_changed, done_date, done_observed, seen_in_progress, missing]` (`region`→`sgg`; 시 단위 사이트의 구 미분류 건은 `sgg` 에 시 코드 그대로)
- `permits_seoul.csv` `PERMIT_FIELDS = [key, sgg, gu, dong, jibun, bobn, bubn, jimok, hndl_ymd, use_purp, job_gbn, duty_end, exec_ymd, address, cid, cname, first_seen]`
  - `key = f"{SGG_CD}|{ACC_YEAR}|{ACC_NO}|{OBJ_SEQNO}"` (없는 값은 빈 문자열; 전부 비면 `address|hndl_ymd|use_purp` 로 대체)
  - `cid` 는 급매판정 `complexes.csv` 의 단지 키(`{묶음}-{법정동}-{지번}-{건축년도}`), 매칭 실패 시 ''
- `raw/seoul/<YYYY-MM-DD>/<sgg>.json.gz` 원본 응답
- `saeol_params.json`, `seoul_permits_cfg.json`, `toheo_progress.json`(runs 60개)
- `fetch_log.json` 의 `toheo` 키(급매판정 fetch_log.json 안에 합침): `{sites:{sgg:{at,ok,host,pages,rows,toheo_rows,reached_cutoff,error,warning,deep,new,updated,completed_observed,dropped}}, permits:{at,ok,error,fetched,added,matched,by_gu:{구:n|오류}}, runs:[{at,deep,sites,new,completed_observed,errors,http_calls}]}`

## 3. 모듈 계약

### collector/naver.py
- `NaverClient.articles_ok(complexNo, max_pages=30) -> tuple[list[dict], bool]`; `articles()` 는 `articles_ok()[0]`.
- `config/naver.json` 의 `articles` 엔드포인트 목록 맨 앞에 fin.land 를 추가: `{"name":"fin_list","method":"POST","url":"https://fin.land.naver.com/front-api/v1/complex/article/list","json":{"complexNumber":"{complexNo}","tradeTypes":["A1"],"size":30,"realEstateTypes":["APT"]},"headers":{"Origin":"https://fin.land.naver.com","Referer":"https://fin.land.naver.com/complexes/{complexNo}"},"list_path":"result.list","more_path":"result.hasNextPage","next_path":"result.lastInfo","next_param":"lastInfo"}` 와 count 엔드포인트 `complex_count: GET https://fin.land.naver.com/front-api/v1/complex/article/count?complexNumber={complexNo}&tradeTypes=A1`. `_request` 가 `method`/`json`/`next_path` 를 지원하도록 확장(기존 GET 엔드포인트와 호환). fin.land 의 단지번호가 m.land `hscpNo` 와 다를 수 있으므로 `complex_fields.id` alias 에 `complexNumber` 를 추가하고, 매칭 결과 `naverId` 는 응답에서 온 id 를 그대로 쓴다. 429 는 Blocked 로 취급(기존 403 과 동일).
- 모바일(m.land) 엔드포인트는 그대로 두 번째 후보로 유지. `new.land` PC(Bearer) 엔드포인트는 삭제.

### collector/store.py
- `LISTING_FIELDS`, `EVENT_FIELDS` 위와 같이. `read_listings()`/`write_listings()` 는 새 컬럼을 기본값(`status='active'`, `miss=0`)으로 채움(구 파일 호환).
- `append_events(rows: list[dict])`, `read_events(since: str|None=None) -> list[dict]`, `prune_events(before: str)`.

### collector/toheo/regions.py
```python
@dataclass class Site: id: str; sido: str; name: str; hosts: list[str]; sggs: list[str]; dept_rules: dict[str, list[str]]  # sgg → 처리부서 포함 문자열
SITES: list[Site]           # 서울 25 + 경기 과천·광명·성남·수원·안양·용인·의왕·하남·화성·구리
SITE_BY_SGG: dict[str, Site]
def sgg_for_case(site: Site, dept: str) -> str      # 처리부서 → sgg (없으면 site.sggs[0] 또는 시 코드)
def toheo_sggs(filter_spec: str|None) -> list[str]  # REGION_FILTER 해석(급매판정 regions.parse_filter 재사용)
def label(sgg: str) -> str                            # '강남구', '성남 분당구', '화성 동탄구'
```
관악구 호스트 후보에 `gwanakgu.eminwon.seoul.kr` 추가. 노원구 http 폴백 허용(`http://` 후보 마지막에).

### collector/toheo/saeol.py, cases.py, http.py, designations.py
토허 원본을 포팅하되 `region` → `sgg`. 함수 시그니처:
- `saeol.parse_list(html) -> (rows, total)`, `SaeolClient.discover(site, force=False) -> cfg`, `SaeolClient.fetch(site, stop_before, max_pages, keyword='토지거래') -> (rows, info)`
- `cases.merge_rows(cases, site, rows, now, covered_from, reached_cutoff) -> stats`, `cases.region_stats(cases, today, series_days=180) -> dict[sgg, stats]`, `cases.estimate(durations_by_sgg, sgg) -> dict|None`
- `designations.load(path=None)`, `with_status(data, today)`, `region_apt_status(items, sgg) -> {level: full|partial|none, until, days_left, designation, partial_ids}`; `regions` 패턴은 LAWD 코드 또는 fnmatch(`"11*"`, `"41590:dongtan"`).
- 모든 '오늘' 은 `collector.engine.today_str()`(KST) 를 쓴다. `datetime.now()` 직접 사용 금지.

### config/designations.json (확장)
항목 필드: `id, title, authority, notice_no, start, end, scope(apt|all|land), target, target_person(내국인|외국인등|전체), threshold_m2(6|60|...), apt_relevant(bool), regions[], partial(bool), note, source, verified_at, renewal_status(연장 미확정|재지정|해제|-)`. 2026-11-01 기준으로 조사된 항목을 모두 넣는다(10·15 대책, 경기도 동탄·기흥·구리, 압구정·여의도·목동·성수, 서울 도로, 모아타운 도로, 외국인등 2026-1105호, 신규주택공급 후보지 2026-1079호(apt_relevant=false), 성남 금토2·여수2/과천경마장 2026-106·107호(apt_relevant=false), 용인 처인 2026-312호(apt_relevant=false), 잠실·삼성·대치·청담 재건축 14개 단지 재지정(서울시 2026-1510호, partial)). 원문 미확인 항목은 `verified_at` 에 `"2026-11-01 (2차 자료)"`.

### collector/toheo/seoul_permits.py
- `config/seoul_permits.json`(기본 제공):
```json
{"page_url":"https://land.seoul.go.kr/land/other/contractStatus.do",
 "url":"https://land.seoul.go.kr/land/wsklis/getContractList.do","method":"POST",
 "params":{"sggCd":"{gu5}","beginDate":"{from:%Y%m%d}","endDate":"{to:%Y%m%d}"},
 "headers":{"X-Requested-With":"XMLHttpRequest","Referer":"https://land.seoul.go.kr/land/"},
 "window_days":60,
 "fields":{"sgg":["SGG_CD"],"acc_year":["ACC_YEAR"],"acc_no":["ACC_NO"],"seq":["OBJ_SEQNO"],"hndl_ymd":["HNDL_YMD","EXEC_YMD","PROCESS_DE"],"address":["ADDRESS","ADDR"],"bobn":["BOBN"],"bubn":["BUBN"],"jimok":["JIMOK","LAWD_JIMOK"],"use_purp":["USE_PURP","PRCUSE_NM"],"job_gbn":["JOB_GBN_NM","PROCESS_RSLT_NM","JOB_GBN"],"duty_end":["DEAL_END_YMD"],"exec_ymd":["EXEC_YMD"]}}
```
- `SeoulPermits(http, cfg, state_path).fetch(sggs, days=60, backfill_from=None) -> (permits: list[dict], info)`: 구별로 `[today-days, today]` 창(필요하면 60일 단위로 분할해 backfill_from 까지 과거로) 조회. 응답이 JSON 이 아니거나 `result/list/data` 아래 목록을 못 찾으면 토허 원본의 HTML 표 파서(`parse_html`)로 폴백. 자동 탐지(discover)는 `--discover` 플래그로만.
- `parse_record(rec: dict, sgg: str, fields) -> dict | None` (키 alias, 주소 '구 동 지번' 분해, 날짜 정규화 YYYY-MM-DD)
- `merge_permits(existing: dict[key,row], new: list[dict], today) -> added:int`
- `match_permits(permits: dict, complexes: dict[cid,row]) -> matched:int` — `(sgg, umd, jibun)` 정확 일치 → `(sgg, umd, 본번)` → 후보 중 `nTrades` 최다. `complexes.csv` 의 `umd`, `jibun` 컬럼 사용. 부천·인천 묶음코드는 `regions.group_of` 로 맞춤.

### collector/toheo/runner.py
```python
def run_toheo(engine, *, deep: bool, data_dir: Path, region_filter: str|None, permits: bool=True, http=None) -> dict
```
1. 새올: 대상 Site 마다 `fetch`(light: stop_before=today-LIGHT_DAYS(2), deep: DEEP_DAYS(45)) → `merge_rows` → 저장.
2. 서울 허가(permits=True 이고 서울 코드 포함): `SeoulPermits.fetch` → raw 보관 → `merge_permits` → `match_permits(engine.complexes)`.
3. `engine.fetch_log['toheo']` 갱신, `toheo_progress.json` runs 추가. 어떤 사이트 실패도 전체를 중단시키지 않는다. 반환: 요약 dict.
환경변수: `TOHEO_DELAY_SEC(0.8) LIGHT_DAYS(2) DEEP_DAYS(45) MAX_PAGES PERMITS(1) TOHEO_REGION_FILTER(없으면 REGION_FILTER 중 토허 대상만)`.

### collector/pipeline.py (export 시점에 계산, 저장 안 함)
```python
ZONE_LEVEL = {"full":2, "partial":1, "none":0}
def zone_of(sgg, umd, designations_items, today) -> dict   # {level, until, daysLeft, title, id}
def link_gone_to_trades(events, trades_by_cid, today) -> list[dict]
   # gone 이벤트 g 에 대해 같은 cid·grp, |floor 차|<=0(층 모르면 무시), 계약일 ∈ [goneAt-14d, goneAt+60d], price ∈ [0.85, 1.03]×호가 인 실거래 → status='confirmed'(trade 참조); 없고 goneAt+75d 이전 → 'pending'; 지나면 'unconfirmed'; relist 이벤트가 21일 안에 있으면 'relisted'
def link_permits_to_trades(permits, trades_by_cid) -> dict[key, trade|None]
   # 같은 cid, 계약일 ∈ [hndl_ymd, hndl_ymd+75d], 가장 가까운 것 1:1
def estimate_score(gone_event, ctx) -> float
   # 토허구역(level>0) 0.5 기본, 그 단지 active 매물 수가 30일 전보다 줄었으면 +0.15, 구 처리중 건수가 30일 평균보다 많으면 +0.1, sameCnt>1 이었으면 −0.1, 확인일(confirmYmd)로부터 30일 이상 지난 매물이면 −0.15(광고 만료 가능), 비규제면 0.2 기본. [0,1] 클램프.
def complex_summary(cid, c, events, permits, trades, cases_stats, zone, today) -> dict   # c/<cid>.json 의 toheo 블록 (아래 4절)
def accuracy_stats(links) -> dict   # {gone:{n, confirmed, relisted, unconfirmed, pending, hitRate}, permits:{n, matched, matchRate, medianDaysToTrade}}
```
`trades` 는 모델 파일의 `complexes[cid].trades`(최근 36개월, `[date,price,area,floor,dong,direct,cancelled]`) 를 쓴다(해제·직거래 제외).

### collector/export.py 확장
기존 산출물 유지 + 다음 추가:
1. `compact_listing` 에 `toheo: {"zone": level(0|1|2), "until": "YYYY-MM-DD"|null, "open": 구 처리중 건수|null, "est": {"p50": 일수, "p90": 일수}|null}` 추가(사이트 계약: 추가 필드).
2. `c/<cid>.json` 에 `toheo` 블록:
```json
{"zone":{"level":2,"until":"2026-12-31","daysLeft":60,"title":"서울 전역·경기 12곳 (10·15 대책)","renewal":"연장 미확정"},
 "region":{"sgg":"11680","label":"강남구","open":123,"received7":40,"received30":150,"p50":12,"p90":21,"est":{"p25":..,"p50":..,"p75":..,"p90":..}},
 "permits":{"n7":1,"n30":5,"n90":12,"total":40,"weekly":[..12..],"byPurpose":{"주거용":30},"recent":[{"date":"2026-10-28","purpose":"주거용","jibun":"12","jobGbn":"허가","trade":{"date":"20261105","price":398000,"area":84.94,"floor":12}|null}]},
 "byGrp":{"84":{"active":9,"active30ago":12,"gone30":[{"id":"...","date":"2026-10-20","price":398000,"dong":"101","floor":12,"score":0.65,"status":"pending|confirmed|relisted|unconfirmed","trade":{...}|null}],"confirmed90":2,"trades30":1}},
 "timeline":[{"date":"2026-10-28","type":"permit|gone|relist|trade|zone","text":"허가 2건 (주거용)","grp":"84"|null}],
 "note":"허가 내역에는 평형이 없어 ..." }
```
3. `site/data/toheo/summary.json`: `{generatedAt, today, totals:{open, received7, received30, receivedPrev30, completed30, permits7, permits30, goneEst30, confirmed30}, regions:[{sgg,label,sido,zone:{level,until,daysLeft},open,received7,received30,receivedPrev30,completed30,p25,p50,p75,p90,durationN,deadlineDays,statusCounts,spark[28],fetch:{ok,at,pages,error,warning,host},permits30, permitsTop:[{cid,name,n30}]}], estimates:{sgg|'all':{n,p25,p50,p75,p90}|null}, designations:[with_status 항목], designationRefs, permits:{ok,at,error,rowsTotal,rows30,matched,byGu30}, accuracy:{...accuracy_stats}, runs:[최근 20], coverage:{seoul:"허가 완료 단지 단위", gyeonggi:"지정·접수 추세만", incheon:"규제지역 아님"}}`
4. `site/data/toheo/feed.json`: `{generatedAt, days:[{date,n,complexes,items:[{cid,name,sgg,umd,n,purposes:{},matched}]}]}` 최근 45일 허가일 내림차순.
5. `site/data/toheo/complexes.json`: `{generatedAt, items:[{cid,name,sgg,umd,n7,n30,n90,total,first,last,weekly[12],goneEst30,confirmed90,zoneLevel}]}` n30 내림차순 상위 500.
6. `site/data/toheo/pipeline.json`: `{generatedAt, accuracy, recent:[최근 60일 gone 이벤트(토허구역 우선) 200건: {id,cid,name,sgg,umd,grp,area,dong,floor,price,date,score,status,trade,zoneLevel}], confirmed:[최근 confirmed 100건]}`
7. `site/data/toheo/daily.json` `{sgg:{dates[180],received[180],completed[180]}}`, `open_cases.json` `{sgg:[{no,received,deadline,status,dept,days,overdue}]}`, `recent_done.json` `{sgg:[{d,n}]}`.
8. `status.json` 의 `fetchLog.toheo` 포함, `counts.permits`, `counts.goneEvents` 추가.
9. RSS: `feed/toheo/all.xml`(허가 소식 300), `feed/toheo/<sgg>.xml`, 기존 `feed/c/<cid>.xml` 에 허가 항목도 섞음(guid `permit-<key>`).
10. 디렉터리 정리 목록에 `data/toheo` 와 `feed/toheo` 추가.

### collector/notify.py (선택 기능)
`send_digest(engine, site_url, cfg_path='config/alerts.json') -> dict`. 환경변수 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 가 없으면 아무것도 안 하고 `{skipped:true}`. `config/alerts.json`: `{"complexes":[cid...], "regions":[sgg...], "minGrade":"S", "maxPrice":null, "grps":[], "toheo":{"permitsForComplexes":true,"designationDdays":[30,7]}}`. 직전 전송 상태는 `data/notify_state.json`(보낸 매물 id·허가 key)로 중복 방지. 메시지는 1회 요약(호갱노노 리뷰의 '알림 폭탄' 불만 반영).

### collector/__main__.py
- `run`: scan → trades → model → match (기존) 
- `listings` (기존) 
- `toheo [--deep] [--no-permits] [--discover]` → `run_toheo`
- `export --out site [--notify]`
- `status`, `probe`, `scan`, `model`, `match` 기존 유지.
- `toheo` 는 `MOLIT_SERVICE_KEY` 없이도 동작(단지 매칭만 비어 있음).

### .github/workflows/collect.yml
모드: `trades`(06:10·20:10 KST), `listings`(09:40·13:40·17:40·21:40 KST), `toheo`(짝수시 10분 KST, 20:10 은 deep), `export`. 모든 모드 끝에 `export` + data 브랜치 커밋 + Pages 배포(기존 방식: git worktree). concurrency group `collect`. `listings` 가 blocked 면 실패로 보지 않고 status 에만 표시. `listings-home.yml`: `runs-on: self-hosted`, 수동/cron, `python -m collector listings` 후 data 브랜치 푸시만(export·배포는 다음 Actions 실행이 처리).

## 4. 사이트 (site/index.html) 계약

- 탭 7개: `explore 탐색 · complex 단지 · toheo 토허 · alerts 알림 · compare 비교 · trend 트렌드 · me 내정보`. 해시 `#toheo`, `#toheo-r-<sgg>`(지역 상세), `#c-<cid>`.
- 브랜드: `APT 레이더 <small>급매판정 · 토허제 라이브</small>`.
- **토허 탭**: 상단 KPI 4개(처리중 민원, 7일 허가, 30일 허가, 10·15 구역 D-day) → 서브탭 `파이프라인 | 소식 | 단지 TOP | 지역 현황 | 예상 승인일 | 지정 현황`.
  - 파이프라인: 4단계 설명 스트립(매물 내림(추정) → 구 접수·처리중 → 허가 완료(단지) → 실거래 확인) + 적중률 카드(accuracy) + 최근 내림 추정 목록(단지·평형·호가·동층·점수·상태, 단지 링크) + 확인된 거래.
  - 소식/단지 TOP/지역 현황/예상 승인일/지정 현황: 토허 원본 UI 를 급매판정 스타일 토큰(.panel/.rows/.card/.chips)로 다시 그림. 지역 상세는 드로어 대신 `#toheo-r-<sgg>` 뷰.
  - 커버리지 안내 문구: 서울=허가 완료 단지 단위, 경기=지정·접수 추세만, 인천=규제지역 아님. '접수 단계는 단지를 알 수 없다'는 문장을 반드시 표시.
- **단지 페이지**: `.cx-head .sub` 에 지정 배지(`토허 D-60` / `일부 구역` / 없음), 기준가 패널 다음에 `토지거래허가 <small>파이프라인</small>` 패널: 평형별 표(평형 | 현재 매물 | 30일 내림(추정) | 확인된 거래 | 최근 실거래) + 허가 완료 요약(7/30/90일, 주간 12주 막대) + 허가↔실거래 매칭 목록 + 타임라인(최근 20) + 구 처리중·예상 승인일 + 한계 문구.
- **매물 카드**: `l.toheo.zone>0` 이면 `.tag` 로 `토허 · 허가 약 {p50}일` 표시, 탐색 필터에 `<select id="fZone">`(전체 / 토허구역만 / 비규제만).
- **알림 탭**: 관심 단지의 새 허가(7일)도 표시. **내정보**: 토허 수집 상태 행 추가.
- 다크모드: 토허 원본처럼 `prefers-color-scheme` 토큰 추가(선택이지만 권장).
- 모든 fetch 는 상대경로 `data/...`. `json()` 캐시 유지.

## 5. 테스트·데모
- `tests/fake_servers.py`: 기존 국토부·네이버 + fin.land 형식 응답(POST list, count) 추가, `/blocked` 는 429 도 재현.
- `tests/fake_toheo.py`: 토허 원본 `fake_saeol.py` 포팅 + 서울 `getContractList.do` 가짜(JSON, `SGG_CD/ACC_YEAR/ACC_NO/OBJ_SEQNO/HNDL_YMD/ADDRESS/BOBN/BUBN/JIMOK/USE_PURP/JOB_GBN_NM/DEAL_END_YMD`) — 세션 쿠키 없으면 빈 결과.
- 필수 테스트: 매물 상태기계(miss 2회→gone, 실패 응답은 무변화, 의심 응답 가드, relist, price 이벤트, 120일 정리), 새올 파싱·탐지·merge, 허가 파싱·매칭·중복키, designations(LAWD 패턴·partial·apt_relevant), 파이프라인 링크(confirmed/pending/relisted/unconfirmed)·점수 범위·적중률, export 스키마(위 4절 파일 전부 존재·필수 키), 전체 흐름(run_trades→model→match→listings×2→toheo→export) 재실행 멱등.
- `tests/make_demo.py`: 급매+토허 전부 포함한 demo_site 생성(허가·내림·실거래가 연결된 예시가 보이도록 시나리오 심기). `python tests/make_demo.py demo_site`.

## 6. 문서(README.md)
설치 10분 가이드(저장소→Secrets→Pages→첫 실행), 집 PC 매물 수집 안내(공식 1순위 경로, 스크립트), 데이터 한계 표(위 0절), 판정 방식, 토허 파이프라인 설명·적중률 해석, 환경변수 표, 구조, 로컬 실행, 알아둘 점, 참고 저장소 링크. 모든 등급 임계값은 '자체 정의'로 명시.
