# 인계 메모 (2026-11-28 밤 기준)

클라우드 세션에서 진행하던 작업을 맥(로컬) 세션으로 넘기기 위한 메모입니다.
새 세션은 이 파일, `README.md`, `DESIGN.md` 를 먼저 읽으세요. 저장소 위치는 `~/apt-radar`, 원격은 `JavascriptEditor/apt-radar`(Public).

## 이 프로젝트가 하는 일

1. **급매판정**: 국토부 실거래로 단지·평형별 기준가를 만들고, 네이버 매물 호가를 그와 비교해 SS/S/A/B/C 등급을 매깁니다.
2. **토허제 라이브**: 수도권 토지거래허가구역의 진행 상황을 추적합니다. "지금 허가 접수된 단지·평형"은 공개 데이터에 없으므로,
   `매물 내림(추정) → 구 접수·처리중(새올) → 허가 완료(서울, 지번→단지) → 실거래 확인` 파이프라인으로 추정하고 적중률을 공개합니다.

구성은 정적 사이트(`site/`) + GitHub Actions(`.github/workflows/collect.yml`) + `data` 브랜치(수집 데이터) 입니다. 서버·DB 없음.
사이트 주소: https://javascripteditor.github.io/apt-radar/

## 지금 상태

| 항목 | 상태 |
|---|---|
| 실거래·기준가 | 정상. 수도권 88개 시군구 2006년부터 적재 완료, 단지 19,526곳, Actions 에서 매일 갱신 |
| 경기 새올 민원 | 대체로 정상(Actions). 누적 약 5,400건 |
| 서울 새올 민원 | Actions 에서는 절반쯤 막힘. 집 맥에서는 35곳 중 25곳 성공 |
| **서울 허가 내역** | **원격 data 브랜치에 0건.** 집 맥에서는 8,594건 수집·7,758건 단지 매칭에 성공했으나 푸시 전에 유실됨(아래 사고 참고) |
| **네이버 단지 연결** | **원격에 0/19,526.** 집 맥에서 1,266개 연결에 성공했으나 같은 사고로 유실됨 |
| **네이버 매물** | **0건.** 단지 연결이 없어 아직 한 번도 수집된 적 없음 |
| 사이트 | 배포는 됨. 다만 매물·허가가 비어 있어 실거래 기준가와 경기 민원 현황만 보임 |

## 확인된 사실 (실측)

- **네이버는 GitHub Actions(해외 IP)를 연결 단계에서 전부 막습니다.** 매물·단지 연결은 국내 가정용 IP(이 맥)에서만 됩니다.
- **네이버 `m.land` 는 약 90회 연속 호출 뒤 HTTP 307(요청 제한)** 을 돌려줍니다(간격 0.7초 기준, 2026-11-28 23:50 실측).
  제한이 풀리는 데 걸리는 시간은 아직 모릅니다.
- 네이버 단지 목록 응답의 필드 이름은 `config/naver.json` 설정과 맞습니다(연결 1,266건으로 확인).
  **매물 목록(`fin.land` / `m.land` articles) 응답 필드는 아직 실측하지 못했습니다.**
- 서울 허가 조회 `POST land.seoul.go.kr/land/wsklis/getContractList.do` 는 JSON 으로 정상 응답합니다(집 IP, 그리고 Actions 에서도 11-02 에 1회 성공).
- 국토부 API 는 가끔 `API 오류(04): HTTP 에러` 를 냅니다. 다음 실행에서 회복되므로 Actions 의 간헐적 실패는 대부분 이것입니다.

## 오늘 있었던 사고와 수정

`scripts/home_listings.sh` 가 시작할 때 `data/` 를 `git reset --hard origin/data` 로 되돌려서, `NO_PUSH=1` 로 모은 허가 내역과
별도로 돌린 `collector match` 결과가 지워졌습니다. 수정 완료(커밋 이력 참고):

- 스크립트는 이제 로컬 미푸시 변경을 먼저 커밋하고 원격 위에 rebase 합니다(`reset --hard` 제거).
- 단지 연결을 스크립트에 넣었습니다: 실행당 `HOME_MATCH_DONGS`(기본 60)개 동.
- 307 응답은 `blocked` 로 처리해 즉시 멈추고, 요청이 실패한 동은 "조회함"으로 기록하지 않습니다.
- 서울 허가 사이트를 "서울 새올 계열 차단" 판정에서 분리했습니다(이 버그로 Actions 가 한 달간 허가 수집을 건너뛰었음).

## 2026-11-29 갱신: 매물 목록(호가) 수집 중단 → 건수 기반으로 전환

- **네이버가 매물 목록 조회를 의도적으로 막고 있습니다.** `fin.land` 는 개발자도구를 열면 오류 페이지로 보내고, 요청에
  브라우저 증명 헤더(`x-attest`)를 요구하며, 수집기 요청에는 응답하지 않습니다(20초 타임아웃). `m.land` 매물 목록은 `null`.
  이 차단을 우회(브라우저 위장·증명 토큰 재사용)하지 않기로 했습니다. `collector listings` 는 남겨 두되 집 맥 스크립트에서는 기본으로 끕니다(`HOME_LISTINGS=1` 로 켬).
- **대신 단지별 매매 매물 수를 기록합니다.** 단지 목록 응답(`m.land complexListByCortarNo`)의 `dealCnt` → `data/listing_counts.csv`.
  `python -m collector counts --max-dongs 50` (동 하나에 호출 1회, 오래 못 본 동부터, 토허구역 동 3배 우선). 단지 연결(`match`) 응답에서도 같이 쌓입니다.
- **매물 내림 추정은 건수 감소로 합니다**(`pipeline.link_count_drops`): 연속 관측에서 줄어든 건수만큼 내림, 21일 안에 다시 늘면 재등록,
  같은 단지의 실거래(계약일이 관측 구간 −14일 ~ +60일)를 1:1 로 붙여 확인. 평형·호가는 모릅니다. 거래가 잦은 대단지는 확인이 쉽게 붙으므로 적중률 해석에 주의.
- **급매판정(SS~C 등급) 목록은 비어 있습니다.** 호가가 없어서입니다. 단지 페이지의 직접 판정(사용자가 가격 입력)은 그대로 동작합니다.
- 집 맥 자동 실행: launchd `com.aptradar.home-collect` 매일 07:00 (`pmset repeat wakeorpoweron` 06:59). 실행당 단지 연결 30개 동 + 매물 수 관측 50개 동.
  네이버 한도는 약 12분 사이 100회쯤에서 307, 15분 안에 풀림(11-29 실측).
- 아래 "바로 할 일" 2·3번(매물 응답 실측, 매물 호출 한도)은 이 전환으로 더 이상 해당하지 않습니다.
- `tests/test_export.py::test_complex_page_toheo_block_and_listing_field` 는 이 변경 전부터 11-29 에 실패합니다(데모 시나리오의 허가↔실거래 날짜). 미해결.

## 바로 할 일

1. **집 맥에서 수집 실행** (네이버 제한이 풀린 뒤, 11-29 아침 이후 권장)
   ```bash
   cd ~/apt-radar && git pull -q && HOME_TOHEO=1 LISTINGS_MIN_INTERVAL_MIN=0 bash scripts/home_listings.sh
   ```
   단지 연결(60개 동) → 매물 수집 → 토허·서울 허가 → `data` 브랜치 푸시까지 한 번에 합니다.
   출력의 `단지 연결:` / `결과:` / `토허 결과:` 줄과 `data/fetch_log.json` 으로 결과를 확인하세요.
2. **매물 수집의 첫 실측.** 매물이 0건이거나 `suspect`/`failed` 가 많으면 응답 구조가 설정과 다른 것입니다.
   `python -m collector probe --complex <네이버단지번호>` 로 실제 응답을 보고 `config/naver.json` 의 `articles`·`article_fields` 를 고치세요.
3. **네이버 호출 한도에 맞추기.** 매물 수집 기본값은 실행당 1,500회(`NAVER_MAX_CALLS`)인데 실측 한도는 약 90회입니다.
   제한이 풀리는 시간과 간격(`NAVER_DELAY_SEC`)에 따른 한도를 재서, 실행당 호출 수·간격·하루 실행 횟수를 정해야 합니다.
   단지 연결과 매물 수집이 같은 한도를 나눠 쓰는지도 확인이 필요합니다.
4. **하루 1회 자동 실행 등록** (사용자와 합의: 4회가 아니라 1회). 한도가 정해진 뒤에:
   ```bash
   40 21 * * * HOME_TOHEO=1 /bin/bash $HOME/apt-radar/scripts/home_listings.sh >> $HOME/apt-radar.log 2>&1
   ```
5. **Actions 에서 서울 허가가 수집되는지 확인.** 수정 뒤 첫 토허 실행의 `fetch_log.json → toheo.permits.ok` 를 보세요.
   Actions 에서 계속 실패하면 서울 허가는 집 맥 경로(`HOME_TOHEO=1`)에만 의존하게 됩니다.
6. 사이트에 바로 반영하려면 Actions → `APT 레이더 수집` → Run workflow → mode `export`.

## 그 뒤에 볼 것

- 서울 단지 전체 연결(하루 60개 동이면 약 4일), 이후 경기·인천.
- 새올 실패 10곳(11140, 11200, 11260, 11290, 11350, 11680, 41290, 41130, 41430, 41310)의 호스트 주소 확인
  (`collector/toheo/regions.py` 의 후보 호스트가 틀렸을 가능성. DNS 실패·인증서 오류가 섞여 있음).
- `config/designations.json`: 10·15 대책 구역(국토부 2025-1219호)은 2026-12-31 만료이고 연장 여부 미확정. 고시가 나오면 `end`·`renewal_status` 갱신.
- 파이프라인 적중률은 매물 내림 뒤 실거래 신고(계약 후 30일)를 기다려야 하므로 한두 달 뒤부터 의미가 있습니다.

## 주의

- **API 키·토큰은 코드나 커밋에 넣지 않습니다.** 국토부 키는 GitHub Secret `MOLIT_SERVICE_KEY` 에 있습니다.
  로컬에서 실거래 수집을 돌릴 일은 없습니다(Actions 가 담당).
- 네이버 수집은 비공식 주소를 쓰며 약관상 허용되지 않습니다. 간격을 줄이거나 횟수를 늘리지 마세요. 막히면 멈추고 기다립니다.
- `data/` 는 `data` 브랜치의 git worktree 입니다. `git reset --hard`, `rm -rf data` 를 하지 마세요.
- 다른 도구로 만든 PR(#1 "Fix MOLIT rate limits and preserve resumable collection progress")이 main 에 병합돼 있습니다.
  `collector/engine.py`·`README.md` 등이 그때 바뀌었으니 수정 전 현재 파일을 읽으세요.
- 테스트: `python -m pytest -q` (96개 통과 상태). 데모 사이트: `python tests/make_demo.py <출력폴더>`.
- 커밋은 main 에 직접 푸시해 왔습니다. `data` 브랜치는 수집 스크립트만 씁니다.
