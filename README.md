# APT 레이더 — 급매판정 · 토허제 라이브

수도권 아파트의 **네이버 매물 호가가 기준가 대비 얼마나 싼지**(급매판정)와, **토지거래허가구역에서 지금 무슨 일이 일어나고 있는지**
(토허제 라이브: 구별 접수·처리 현황, 단지별 허가 완료 소식, 지정 만료 일정)를 한 화면에서 보여주는 정적 사이트입니다.
두 기능을 잇는 **거래 파이프라인**이 핵심입니다: *매물이 내려감(추정) → 구청 접수·처리중 → 허가 완료(단지) → 실거래 확인(평형·가격)*.

- 서버·DB 없이 **GitHub Actions(수집) + GitHub Pages(사이트)** 로 동작, 무료
- 대상: 서울·경기·인천 전체 시군구(실거래·매물), 서울 25개 구 + 경기 토허구역 시·구(토허)
- 화면 7개 탭: **탐색**(저평가 매물·지역·평형·예산·등급·토허구역 필터) · **단지**(기준가, 동·층 효과, 매물 순위, 토허 파이프라인, 실거래) ·
  **토허**(파이프라인 · 소식 · 단지 TOP · 지역 현황 · 예상 승인일 · 지정 현황) · **알림**(관심 단지 새 매물·급매·새 허가, RSS) ·
  **비교** · **트렌드**(시군구 지수, 급매 비율) · **내정보**(표기 설정, 데이터·수집 상태)
- 선택: **텔레그램 요약 알림** 1일 몇 통 수준(알림 폭탄 없음)

> 모든 등급·점수·'추정' 라벨은 **이 저장소가 자체 정의한 기준**입니다. 공식 지표가 아니며 투자 판단의 근거로 쓰지 마세요.

## 목차

1. [설치 (약 10분)](#설치-약-10분)
2. [집 PC 매물 수집 (공식 1순위 경로)](#집-pc-매물-수집-공식-1순위-경로)
3. [데이터 한계 — 꼭 읽어 주세요](#데이터-한계--꼭-읽어-주세요)
4. [판정 방식 (급매)](#판정-방식-급매)
5. [토허 파이프라인과 적중률 해석](#토허-파이프라인과-적중률-해석)
6. [텔레그램 알림 (선택)](#텔레그램-알림-선택)
7. [환경변수·설정](#환경변수설정)
8. [구조](#구조)
9. [로컬 실행·테스트·데모](#로컬-실행테스트데모)
10. [알아둘 점](#알아둘-점)
11. [참고 저장소](#참고-저장소)

## 설치 (약 10분)

1. **실거래 API 키** — [공공데이터포털](https://www.data.go.kr)에서 `국토교통부_아파트 매매 실거래가 상세 자료` 활용신청 → 마이페이지에서
   **일반 인증키(Decoding)** 복사. (토허 수집만 쓸 거면 없어도 되지만, 그러면 허가↔단지 매칭·급매판정이 비어 있습니다.)
2. **저장소 만들기** — GitHub에서 새 **Public** 저장소를 만들고 이 폴더 내용을 올립니다.
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<아이디>/<저장소>.git
   git push -u origin main
   ```
3. **Secrets 등록** — `Settings → Secrets and variables → Actions → New repository secret`

   | Secret | 필수 | 설명 |
   |---|---|---|
   | `MOLIT_SERVICE_KEY` | 예 | 국토부 실거래 API 인증키 |
   | `NAVER_LAND_TOKEN` | 아니오 | 네이버 PC 웹 Bearer 토큰(모바일 주소가 막힐 때만, 금방 만료) |
   | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 아니오 | 텔레그램 요약 알림(아래 [알림](#텔레그램-알림-선택)) |

4. **Pages 켜기** — `Settings → Pages → Build and deployment → Source: GitHub Actions`
5. **첫 실행** — `Actions` 탭 → `APT 레이더 수집` → `Run workflow` (mode `auto` = 실거래 수집 + 토허 수집. deep 체크 안 해도 토허 데이터가 없으면 첫 실행은 자동으로 깊게 읽습니다.
   `MOLIT_SERVICE_KEY` 가 없으면 실거래 단계는 경고만 남기고 건너뛰어 토허만 수집됩니다).
   끝나면 `https://<아이디>.github.io/<저장소>/` 에서 열립니다. `Variables` 에 `SITE_URL` 을 그 주소로 넣으면 RSS·알림 링크가 절대 주소가 됩니다.

### 설치 체크리스트

- [ ] `MOLIT_SERVICE_KEY` Secret 등록 (키를 코드에 직접 쓰지 않기)
- [ ] Pages Source = GitHub Actions
- [ ] 첫 실행 뒤 사이트 **내정보 → 데이터 상태** 에서 실거래 적재 진행률, 매물 수집 상태(`ok` / `blocked`), 토허 수집 상태 확인
- [ ] 매물 수집이 `blocked` 로 나오면 [집 PC 매물 수집](#집-pc-매물-수집-공식-1순위-경로) 설정 (GitHub Actions 에서는 2026-10 말 이후 거의 항상 막힙니다)
- [ ] 내정보 → 토허 수집 상태에 **"조회 요청 방식을 찾지 못함"** 또는 허가 0건이 계속 뜨면 **서울 허가 요청 방식이 바뀐 것** —
      브라우저 개발자도구(Network)에서 `land.seoul.go.kr` 의 조회 요청(주소·form 파라미터·헤더)을 보고 **`config/seoul_permits.json` 을 수정**하세요
      (기본값: `POST /land/wsklis/getContractList.do`, `sggCd`·`beginDate`·`endDate`, 먼저 `GET /land/other/contractStatus.do` 로 세션 쿠키).
      응답 필드 이름이 바뀌었으면 같은 파일의 `fields` alias 에 새 이름을 추가하면 됩니다. 자동 탐지는 `python -m collector toheo --discover`
- [ ] 새올 사이트가 `1페이지만` 으로 표시되면 `config/saeol_params.json` 에 페이지 파라미터 이름을 적기(`config/saeol_params.example.json` 참고)
- [ ] 내정보 → 토허 상태에 **`접속 불가` / `seoul 계열 … 차단 추정`** 이 자주 뜨면 러너 IP 가 서울 사이트에 막히는 것 — 경기 새올·국토부 실거래는
      영향 없고, 서울 허가·민원은 다음 실행(다른 러너 IP)에서 채워지거나 [집 PC 토허 수집](#집-pc-매물-수집-공식-1순위-경로)(`HOME_TOHEO=1`)으로 보완
- [ ] 내정보 → 단지 매칭 상태가 `blocked: 연결 실패 …` 면 네이버가 Actions IP 를 막은 것(정상 범위, 집 PC 경로 사용). `empty: …` 면 응답 구조가 바뀐 것 —
      `data` 브랜치의 `naver_probe.json` 에 남은 응답 샘플을 보고 `config/naver.json` 의 필드 이름을 고치세요
- [ ] 토허구역 연장·해제 고시가 나오면 `config/designations.json` 의 `end`·`renewal_status` 갱신 (2026-11-01 기준 항목이 들어 있고, 10·15 대책 구역은 **2026-12-31 만료·연장 미확정**)
- [ ] (선택) `config/alerts.json` + 텔레그램 Secrets

### 처음 며칠은 이렇게 채워집니다

- **실거래**: 개발계정 한도(10,000회/일) 안에서 2006년 이후 수도권 이력(약 2.4만 회)을 **서울 → 경기 → 인천** 순으로 받습니다. 적재가 끝난 지역부터 기준가가 나옵니다(서울은 첫날).
- **매물**: 실거래 적재 뒤 네이버 단지 매칭(법정동 단위, 실행당 400개 동) → 매칭된 단지부터 매물 수집이 4시간마다 순환합니다. 단, Actions 가 막히면 집 PC 경로가 필요합니다.
- **토허**: 접수·처리중 건수와 서울 허가 완료 건은 첫날부터 보입니다. **처리 소요일·예상 승인일은 2~3주 뒤**부터 채워집니다(처리중 → 완료로 바뀌는 것을 직접 관측한 민원만 세기 때문).
- **파이프라인 적중률**: 매물 내림(추정)이 실거래로 확인되려면 실거래 신고(계약 후 30일)까지 기다려야 하므로 **한두 달 뒤**부터 의미 있는 숫자가 나옵니다.

## 집 PC 매물 수집 (공식 1순위 경로)

네이버 부동산에는 공식 API 가 없습니다. 이 수집기는 네이버 웹이 쓰는 비공식 JSON 주소(`fin.land.naver.com/front-api/v1`, 2순위로 `m.land`)를 호출하며,
이는 **네이버 약관상 허용되지 않고 언제든 막히거나 바뀔 수 있습니다.** 2026-10 말 이후 **GitHub Actions(해외 IP)에서는 429/차단이 일관되게 보고**되어,
Actions 의 매물 수집은 "시도하되 차단되면 즉시 중단·상태 표시"만 하고 **집 PC(국내 IP) 수집을 공식 1순위 경로**로 둡니다.

### 방법 A. 스크립트 (권장, runner 등록 불필요)

집 PC 에 Python 3.12 와 git 이 있고 GitHub 에 push 할 수 있게 로그인(`gh auth login` 또는 Git Credential Manager / SSH)돼 있으면 됩니다.

```bash
# macOS / Linux — 처음 한 번 (clone 부터 푸시까지 한 번에)
REPO_URL=https://github.com/<아이디>/<저장소>.git bash scripts/home_listings.sh
# 이후에는 저장소 안에서
bash scripts/home_listings.sh
```

```powershell
# Windows PowerShell — 처음 한 번
$env:REPO_URL="https://github.com/<아이디>/<저장소>.git"; powershell -ExecutionPolicy Bypass -File scripts\home_listings.ps1
# 이후
powershell -ExecutionPolicy Bypass -File scripts\home_listings.ps1
```

스크립트가 하는 일: 저장소 clone/갱신 → `data` 브랜치를 `data/` worktree 로 받기 → `.venv` 만들고 `pip install` →
`python -m collector listings` → `data` 브랜치 커밋·푸시. **사이트 반영은 다음 Actions 실행이 합니다**(바로 보려면 Actions 에서 mode `export` 수동 실행).

주기 실행은 cron(macOS/Linux) 또는 작업 스케줄러(Windows)에 **하루 4회**(09:40·13:40·17:40·21:40 권장)로 거세요. 스크립트 머리말에 예시가 있습니다.
환경변수 `REGION_FILTER`(지역 제한), `NAVER_MAX_CALLS`(실행당 호출, 기본 1500 ≈ 20분), `NAVER_DELAY_SEC`(간격 0.7초), `NO_PUSH=1`(확인용)을 쓸 수 있습니다.

**토허도 집 PC 에서(`HOME_TOHEO=1`)** — GitHub Actions(해외 IP)에서는 서울 자치구 새올(`*.eminwon.seoul.kr`)과 `land.seoul.go.kr` 이
**연결 자체가 막히는 날**이 있습니다(러너 IP 대역에 따라 다름; 내정보 탭 토허 상태에 `접속 불가` · `seoul 계열 … 차단 추정` 으로 표시).
`HOME_TOHEO=1`(가벼운 수집) 또는 `HOME_TOHEO=deep` 을 주면 같은 스크립트가 매물 뒤에 `python -m collector toheo` 도 돌려
서울 허가·민원을 국내 IP 로 채웁니다. 경기 시 새올은 Actions 에서도 대체로 열립니다.

### 방법 B. self-hosted runner

> **Public 저장소에서는 쓰지 마세요.** self-hosted runner 는 저장소 단위로 등록되므로, 포크 PR 이 추가한 워크플로(`runs-on: self-hosted`)가
> 집 PC(git 푸시 자격증명·`NAVER_LAND_TOKEN` 보유)에서 실행될 수 있습니다(GitHub 공식 문서도 공개 저장소에서의 self-hosted runner 를 권장하지 않음).
> 이 저장소는 Pages 때문에 Public 이 기본이니 **방법 A 를 쓰세요.** 굳이 쓰려면 Private 저장소 + `Settings → Actions → Fork pull request workflows → Require approval for all outside collaborators` + 러너를 별도 사용자/컨테이너/VM 에 격리하세요.

`Settings → Actions → Runners → New self-hosted runner` 로 집 PC 를 등록하고 **Variables 에 `HOME_RUNNER` = `1`** 을 넣으면
`.github/workflows/listings-home.yml` 이 그 PC 에서 하루 4회(10:00·14:00·18:00·22:00 KST) 매물을 수집해 `data` 브랜치에 푸시합니다
(변수가 없으면 예약 실행은 건너뜁니다 — 러너 없이 켜 두면 job 이 24시간 '대기 중' 으로 쌓이기 때문). Actions 수집(`collect.yml` listings 모드, 09:40 …)과 같이 켜 두어도
됩니다: 양쪽 모두 **직전 2시간 안에 성공한 매물 수집이 있으면 건너뛰므로**(`LISTINGS_MIN_INTERVAL_MIN`, 기본 120) 같은 상태기계가 20분 간격으로 두 번 돌아
'사라진 매물' 판정(연속 2회 미출현)이 앞당겨지는 일은 없고, Actions 가 `blocked` 일 때만 집 PC 가 실제로 수집합니다. 방법 A 의 스크립트도 같은 규칙을 따릅니다.

### 지켜 주세요

- 요청 간격 0.7초(±30%), 실행당 호출 상한, 단지별 순환으로 부하를 낮춥니다. **주기를 더 촘촘하게 하지 마세요.**
- 403/429·안내 페이지가 오면 그 실행은 즉시 멈추고 내정보 탭에 `blocked` 로 표시됩니다. 매물 목록은 마지막 성공분이 남고, 단지 페이지의 '매물 직접 판정'은 계속 동작합니다.
- 주소·필드 이름은 `config/naver.json` 에 있습니다. 구조가 바뀌면 브라우저 Network 탭을 보고 이 파일만 고치면 됩니다. 확인: `python -m collector probe --cortar 1168010300 --complex <단지번호>`
- 매물 자료는 공개된 호가를 통계 목적으로 읽는 용도입니다. 저장하는 것은 매물번호·동·층·면적·호가·확인일·중개사 상호·태그와 매물 설명 앞 80자(`listings.csv` 의 `desc`, 전화번호·이메일 패턴은 `***` 로 가림)이며,
  설명은 사이트 데이터로 내보내지 않습니다. `data` 브랜치는 공개되므로 그 밖의 개인정보가 보이면 `config/naver.json` 의 `desc` alias 를 비워 수집을 끄세요. 상업적 이용은 피하세요.

## 데이터 한계 — 꼭 읽어 주세요

| 출처 | 알 수 있는 것 | 알 수 없는 것 / 한계 | 이 사이트의 처리 |
|---|---|---|---|
| **국토부 실거래 API** | 계약일·가격·전용면적·층·동(2023년 이후)·해제·직거래 | 신고 기한 30일 → 최근 한두 달은 계속 늘어남. 정정·삭제 있음 | 매일 최근 6개월 재조회, 3회 연속 사라지면 삭제. 해제·직거래는 모델에서 제외 |
| **네이버 매물(비공식)** | 단지·평형·호가·동·층·확인일 | 약관상 비허용, Actions(해외 IP) 차단. '내려감'의 이유(거래/만료/철회)는 모름 | 집 PC 수집 1순위. 실패·의심 응답(직전 5건↑→0건)은 상태를 바꾸지 않음. 성공 조회에서 2회 연속 없으면 `gone`(추정) |
| **서울 허가 내역** (`land.seoul.go.kr` getContractList.do) | 처리일·주소(구 동 지번)·이용목적·허가/불허가/취소 | **면적·층·가격·단지명·접수일 없음.** 처리 완료 건만, 최대 62일 창 | 매일 누적 보관(raw 포함). 지번 → `complexes.csv` 단지로 매칭(같은 지번에 여러 단지면 거래 많은 쪽, 실패 시 미매칭) |
| **새올 서식민원처리공개** (구청 전자민원) | 접수번호·접수일·민원명·처리기한·상태·처리부서 | **단지·주소 없음 → 접수 단계에서는 어떤 단지인지 알 수 없음.** '처리중'부터 노출(접수 단계는 안 보임). robots.txt 제한 많음 | 구 단위 처리중 건수·접수 추세·소요일·예상 승인일만. 시 단위 새올(성남·수원·안양·용인·화성)은 처리부서명으로 구 분류 |
| **경기도** | 지정 고시, 시·구별 접수 건수(새올) | **허가 내역 비공개**(일부 시가 월·분기 집계만) | '지정 + 접수 추세'만 표시하고 UI 에 그렇게 적음 |
| **인천** | — | 내국인 아파트 토허구역 없음(2026-08 관보까지) | 토허 수집 대상 제외, "규제지역 아님" 표시 |
| **지정 현황** (`config/designations.json`) | 고시 단위 기간·범위·대상 | 손으로 관리. 신통기획·공공재개발 같은 구역 단위 지정은 일부만 | `verified_at` 에 확인 시점, 원문 미확인은 `(2차 자료)` 표기. 2026-11-01 기준 |

핵심 한 줄: **'어느 단지·평형이 접수됐는지'는 어떤 공개 데이터에도 없습니다.** 단지·평형 수준의 유일한 선행 신호는 *네이버 매물이 내려간 것*이고,
그 다음에 *허가(지번→단지)* 와 *실거래(평형·가격)* 로 사후 확인할 수 있을 뿐입니다. 그래서 이 사이트의 '내림'은 전부 **추정** 라벨이 붙고 적중률을 공개합니다.

## 판정 방식 (급매)

```
log(가격) = μ[단지·면적그룹] + S[시군구,월] + D[단지,월] + β동 + β층 + β타입 + ε
```

| 항목 | 규칙 |
|---|---|
| 비교 단위 | 같은 단지 + **전용면적 정수부**(59.97·59.84 → `59`). 그 안의 정확한 전용면적은 `타입` 효과로 보정 |
| 시점 | 시군구 월별 지수 S(랜덤워크 평활) + 단지 편차 D(평활, 0으로 수축). 최근 거래가 없는 단지는 시군구 흐름을 따라감 |
| 동 | 릿지 회귀, 표본 적으면 0으로 수축. 동 정보는 2023년 이후 거래에만 있음. 프리미엄이 가장 높은 동이 **로얄동** |
| 층 | 1층·2층·3층·저층·중층(기준)·고층·최상층. 최상층은 동별 관측 최고층(또는 네이버 최고층). 표본 적으면 시군구 공통 층 효과로 수축 |
| 제외 | 해제 거래, 직거래(저가 편향). 표시 목록에는 남김 |
| 기준가 | 지금 달·중층·평균 동으로 되돌린 가격. 매물의 동·층·타입을 넣어 보정한 기준가와 호가를 비교 |
| 등급(**자체 정의**) | SS ≤ −8%, S ≤ −5%, A ≤ −2%, B ±2%, C > +2% |
| 신뢰 | 거래 수·최근 12개월 거래·잔차 분산·동 정보 유무로 높음/보통/표본 부족. 탐색의 기본값은 표본 부족 숨김 |
| 기준가 없음 | 거래 8건 미만, 또는 그 지역 이력 적재가 끝나지 않음 |

단지 식별: `묶음코드 + 법정동 + 지번 + 건축년도`. 행정구역 코드가 바뀌어도(부천 구 부활, 화성·인천 구 신설) 같은 단지로 이어집니다.
토허 허가 내역의 지번도 이 키로 단지에 연결합니다.

## 토허 파이프라인과 적중률 해석

토허구역에서 아파트를 사려면 *허가 신청(구청) → 허가 → 계약 → 실거래 신고* 순서를 밟습니다. 공개 데이터로 볼 수 있는 단계는 다음 네 개뿐이고, 각 단계마다 아는 것이 다릅니다.

```
① 매물 내림 (추정)        ② 구 접수·처리중          ③ 허가 완료              ④ 실거래 확인
단지·평형·호가·동·층 앎   구 단위 건수만, 단지 모름  단지(지번)만, 평형·가격 모름  평형·가격·층 앎 (2~5주 뒤)
네이버 매물                새올                       서울시 공개 (서울만)          국토부
```

- **① 내림(추정)**: 성공 조회에서 2회 연속 보이지 않은 active 매물(`gone`). 단지·평형 수준의 유일한 선행 신호지만, 거래 때문인지(계약) 광고 만료·철회인지는 모릅니다.
  그래서 **추정 점수**(0~1)를 붙입니다: 토허구역이면 0.5 기본(비규제 0.2), 그 단지 매물 수가 30일 전보다 줄었으면 +0.15, 구 처리중 건수가 30일 평균보다 많으면 +0.1,
  같은 집 중복 매물이었으면(`sameCnt>1`) −0.1, 확인일로부터 30일 이상 지난 매물이면(광고 만료 가능) −0.15. 점수는 **자체 정의**이며 확률이 아닙니다.
- **②**: 새올 목록은 단지를 모르니 "그 구의 처리중 건수가 많아지고 있다" 정도의 배경 정보입니다. 예상 승인일은 최근 완료 관측 분포(P25/P50/P75/P90)입니다.
- **③**: 서울만. 허가 내역의 지번으로 단지를 알지만 평형·가격은 없습니다. 단지 페이지에서 허가 건과 같은 단지의 75일 안 실거래를 1:1 로 이어 보여줍니다.
- **④**: 실거래가 들어오면 ①을 사후 확인합니다. 같은 단지·평형, 층이 알려졌으면 같은 층, 계약일이 `[내린 날−14일, +60일]`, 가격이 호가의 85~103% 이면 **confirmed**.
  내린 지 75일이 안 됐고 아직 없으면 **pending**, 지나면 **unconfirmed**, 21일 안에 같은 매물이 다시 올라오면 **relisted**(거래 아님).

**적중률(accuracy)** 은 토허 탭 파이프라인 카드와 `site/data/toheo/pipeline.json` 에 있습니다.

- `gone.hitRate = confirmed / (confirmed + unconfirmed + relisted)` — pending 은 분모에서 뺍니다. 처음 한두 달은 대부분 pending 이라 숫자가 작거나 비어 있습니다.
- 실거래 신고는 계약 후 30일까지, 공개는 그 뒤이므로 **최근 5주의 unconfirmed 는 아직 '미확인'일 뿐**입니다.
- 실거래 동·층이 없는 거래(2022년 이전 양식, 일부 누락)는 조건을 느슨하게 맞추므로 confirmed 가 과대평가될 수 있습니다. 반대로 호가와 15% 넘게 깎인 거래는 잡지 못합니다.
- `permits.matchRate` 는 서울 허가 건 중 단지로 연결된 비율, `medianDaysToTrade` 는 허가→실거래 계약일 중위 일수입니다. 신축·재건축·상가 등 `complexes.csv` 에 없는 지번은 매칭되지 않습니다.
- 매물 수집이 며칠 멈추면(`blocked`) 그 기간의 내림은 몰아서 잡히고 날짜가 늦어집니다. 실패·의심 응답에서는 상태를 바꾸지 않으므로 '가짜 내림'은 적지만 '늦은 내림'은 생깁니다.

커버리지: **서울 = 허가 완료 단지 단위**, **경기 = 지정·접수 추세만**(허가 내역 비공개), **인천 = 규제지역 아님**. 사이트 토허 탭에도 같은 문구가 붙습니다.

## 텔레그램 알림 (선택)

1. 텔레그램에서 `@BotFather` 로 봇을 만들어 토큰을 받고, 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 `chat.id` 를 확인합니다.
2. Secrets 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록.
3. `config/alerts.example.json` 을 `config/alerts.json` 으로 복사해 관심 단지(`complexes`, 단지 페이지 주소의 `#c-` 뒤 값)·지역(`regions`)·등급(`minGrade`)·
   예산(`maxPrice`, 만원)·평형(`grps`)·토허 옵션(`permitsForComplexes`, `designationDdays`)을 적어 커밋합니다. 공개 저장소에 관심 단지를 올리기 싫으면
   파일 대신 Variables 에 `ALERTS_JSON`(같은 내용의 JSON 문자열)을 넣어도 됩니다.

매 수집 끝에 `export --notify` 가 **요약 1통**(새 급매 매물 · 관심 단지 새 허가 · 지정 만료 D-30/D-7)만 보냅니다. 보낸 매물·허가·D-day 는
`data/notify_state.json` 에 기록해 다시 보내지 않고, 전송에 실패하면 다음 실행에 다시 시도합니다. Secrets 가 없으면 아무것도 하지 않습니다.

## 환경변수·설정

`Settings → Secrets and variables → Actions → Variables` (비우면 기본값)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `REGION_FILTER` | (전체) | 실거래·매물 지역. 예: `서울`, `서울,경기`, `11680,41135` |
| `TOHEO_REGION_FILTER` | `REGION_FILTER` 중 토허 대상 | 토허 수집 지역만 따로 (예: `서울`, `11680,41135,41590:dongtan`) |
| `DAILY_CALL_LIMIT` | 9500 | 국토부 하루 호출 상한 |
| `MOLIT_API_URL` | 상세 자료 API | 기본 자료 API 키만 있다면 그 주소(동 정보가 없어 동 보정이 안 됨) |
| `NAVER_MAX_CALLS` / `NAVER_DELAY_SEC` | 1500 / 0.7 | 매물 수집 실행당 호출 상한 / 요청 간격(초). 상한에 걸리면 `partial`(차단 아님) |
| `LISTINGS_MIN_INTERVAL_MIN` | 0 (워크플로·집 PC 는 120) | 직전 성공 매물 수집이 이 분 안이면 건너뜀(`skipped`). Actions·집 PC 이중 수집 방지 |
| `TRADES_MAX_SEC` | 0 (워크플로는 6000) | 실거래 적재 시간 상한(초). 다음 예약 실행이 대기열에서 취소되지 않게 100분 안에 끝냄 |
| `TOHEO_MAX_SEC` | 0 (워크플로는 1500) | 토허 수집 시간 상한(초). 서울 사이트가 러너 IP 를 막는 날 연결 타임아웃으로 90분 넘게 끌지 않게. 남은 지역은 `skipped` 로 표시되고 다음 실행에서 이어감 |
| `HOME_RUNNER` | (없음) | `1` 이면 `listings-home.yml`(self-hosted runner) 예약 실행을 켬. 러너 없이 켜 두면 job 이 24시간 '대기 중' 으로 쌓이므로 기본은 꺼짐 |
| `MATCH_MAX_DONGS` | 400 | 단지 매칭 실행당 법정동 수 |
| `TOHEO_DELAY_SEC` | 0.8 | 새올·서울시 요청 간격(초). 줄이지 마세요 |
| `LIGHT_DAYS` / `DEEP_DAYS` | 2 / 45 | 가벼운/깊은 토허 수집이 거슬러 읽는 접수일 범위 |
| `MAX_PAGES` | 40 / 400 | 새올 사이트당 페이지 상한(light / deep) |
| `PERMITS` | 1 | 서울 허가 내역 수집(0 이면 끔) |
| `ALERTS_JSON` | (없음) | `config/alerts.json` 대신 쓰는 알림 설정 JSON |
| `SITE_URL` | (없음) | RSS·알림 링크 기준 주소 (`https://<아이디>.github.io/<저장소>`) |

Secrets: `MOLIT_SERVICE_KEY`(필수), `NAVER_LAND_TOKEN`·`TELEGRAM_BOT_TOKEN`·`TELEGRAM_CHAT_ID`(선택).

수집 시각은 `.github/workflows/collect.yml` 의 `cron`(UTC)에서 바꿉니다. 기본(KST): 실거래 06:10·20:10, 매물 09:40·13:40·17:40·21:40,
토허 짝수시 10분(가벼운 수집, **20:10 은 깊은 수집**; 06:10·20:10 은 실거래 실행 안에서 이어 돕니다). 모든 실행 끝에 사이트 데이터 생성 → `data` 브랜치 커밋 → Pages 배포.
수동 실행(`Run workflow`)의 mode: `auto`(예약 실행 시각에 따라; 수동이면 실거래 + 토허) / `trades` / `listings` / `toheo`(+ `deep`) / `export`(사이트만 다시) / `model` / `scan`.
모든 실행이 한 `concurrency` 그룹을 쓰므로 실행이 겹치면 **대기열에는 1개만 남고 그 전에 대기하던 실행은 취소**됩니다(Actions 목록에 `cancelled`). 그래서 실거래 적재는
`TRADES_MAX_SEC`(기본 100분)로 끊어 다음 예약과 겹치지 않게 했습니다. 수집 스텝이 예외로 죽으면 export·커밋·배포는 그대로 진행하고(진행 상황·호출 수 보존)
마지막 스텝이 job 을 실패로 표시하며, 내정보 탭의 상태가 `error: …` 로 바뀝니다.

국토부 API의 HTTP 429는 요청 제한입니다. `Retry-After`에 맞춰 최대 4회 재시도하고, 제한이 계속되거나 대기 안내가 60초보다 길면
이번 실행은 `rate_limit` 상태로 마칩니다. 성공적으로 받은 달과 호출 수를 저장하고 다음 실행에서 실패한 달부터 이어갑니다.
이 경우 export·배포는 계속 진행합니다. 한도 자체가 늘어나는 것은 아니므로 제한이 반복되면 공공데이터포털의 승인된 호출량도 확인하세요.

설정 파일(`config/`): `naver.json`(네이버 주소·필드), `designations.json`(지정 현황, 직접 관리), `seoul_permits.json`(서울 허가 조회 요청),
`saeol_params.example.json`(새올 페이지 파라미터 수동 지정), `alerts.example.json`(알림).

## 구조

```
collector/                     단일 패키지
  regions.py  molit.py  deals.py  model.py  judge.py  naver.py   실거래·기준가·판정·네이버(fin.land → m.land)
  engine.py                    trades(재조회→적재) · scan · match · listings(상태 기계: active/gone, listing_events) · model
  store.py                     data 브랜치 CSV/JSON (listings.csv 소프트 삭제, listing_events.csv)
  pipeline.py                  허가↔단지, 내림↔실거래, 허가↔실거래 연결, 추정 점수, 적중률 (export 때 계산)
  export.py                    site/data/*.json + 토허 JSON + RSS
  notify.py                    텔레그램 요약 알림(선택)
  __main__.py                  run / listings / toheo / model / match / export / scan / probe / status
  toheo/                       토허제 라이브 포팅
    regions.py                 새올 Site 정의(서울 25 + 경기 10), 처리부서→구 분류, 화성 동탄 서브키(41590:dongtan)
    http.py saeol.py cases.py  Polite 세션 · 새올 파서·파라미터 탐지 · 민원 상태 누적·지역 통계·예상 승인일
    seoul_permits.py           서울 허가 내역 수집·파싱·단지 매칭
    designations.py store.py runner.py   지정 판정 · data/toheo 입출력 · run_toheo(light/deep)
config/                        naver.json designations.json seoul_permits.json saeol_params.example.json alerts.example.json
site/index.html                정적 사이트 (빌드 없음, 외부 라이브러리 없음, 폰트만 CDN)
scripts/home_listings.sh .ps1  집 PC 매물 수집 → data 브랜치 푸시
.github/workflows/collect.yml  Actions: trades / listings / toheo / export      listings-home.yml: self-hosted runner(선택)
tests/                         가짜 국토부·네이버·새올·서울시 서버로 전체 흐름 검증, make_demo.py 예시 데이터
```

`data` 브랜치: `trades/<코드>/<연도>.csv.gz`(거래 원본), `complexes.csv`(단지 인덱스·네이버 매칭), `listings.csv`(매물, `status` active/gone),
`listing_events.csv`(new/price/gone/relist 이벤트), `listing_stats.csv`, `model/<묶음>.json.gz`, `progress.json`, `regions_live.json`, `fetch_log.json`(토허 포함),
`notify_state.json`, `toheo/cases.csv`(새올 민원 누적), `toheo/permits_seoul.csv`, `toheo/raw/seoul/<날짜>/<구>.json.gz`(원본 응답), `toheo/saeol_params.json`, `toheo/toheo_progress.json`.

사이트 데이터(`site/data/`): `index.json`(단지 검색), `c/<단지>.json`(단지 페이지, `toheo` 블록 포함), `l/<시군구>.json`, `top.json`, `trend.json`, `status.json`,
`toheo/summary.json`·`feed.json`·`complexes.json`·`pipeline.json`·`daily.json`·`open_cases.json`·`recent_done.json`, RSS `feed/all.xml`·`feed/c/<단지>.xml`·`feed/toheo/all.xml`·`feed/toheo/<구>.xml`.

## 로컬 실행·테스트·데모

```bash
pip install -r requirements.txt            # requests, numpy 뿐
export MOLIT_SERVICE_KEY=발급받은키 REGION_FILTER=11680        # 강남구만 빠르게
export DATA_DIR=$HOME/apt-data             # 로컬 수집 데이터 위치. 저장소 안 data/ 는 집 PC 스크립트가 data 브랜치 worktree 로 쓰는 자리라 분리 권장
python -m collector run --partial          # 실거래 재조회·적재 + 모델 + 단지 매칭
python -m collector listings               # 네이버 매물 (집 PC 에서)
python -m collector toheo --deep           # 새올 + 서울 허가 (MOLIT 키 없이도 동작, 단지 매칭만 비어 있음)
python -m collector export --out site      # 사이트 데이터 (+ --notify 로 텔레그램). --out 은 DATA_DIR 과 겹치면 안 됨(겹치면 거부)
cd site && python -m http.server 8000      # http://localhost:8000
```

테스트(실제 접속 없음, 가짜 서버만): `pip install pytest && python -m pytest -q`
예시 화면(허가·내림·실거래가 연결된 시나리오 포함): `python tests/make_demo.py demo_site && cd demo_site && python -m http.server 8000`

## 알아둘 점

- 실거래 신고 기한은 계약 후 30일이라 최근 한두 달 지수는 거래가 더 쌓이면 바뀝니다. 기준가는 매일 다시 계산됩니다. 국토부가 거래를 정정·삭제하면 연속 3회 조회에서 사라진 뒤 삭제합니다.
- 동 정보(aptDong)는 2023년 이후 거래에만 있어 거래가 뜸한 단지는 동 보정이 비어 있을 수 있습니다. 네이버 동 표기(`101동`, `가동`)와 국토부 표기(`101`, `제101동`)는 정규화해 맞춥니다.
- 매물 `gone` 은 "성공 조회에서 2회 연속 없음" 입니다. 조회 실패·차단·의심 응답(직전 5건 이상이었는데 0건)에서는 아무 상태도 바꾸지 않으므로, 수집이 멈춘 기간의 내림은 재개 뒤에 몰아서 잡힙니다. gone 행은 120일, 이벤트는 400일 보관합니다.
- 같은 집에 중복 매물(`sameCnt>1`)이 있다가 대표 매물만 바뀐 경우는 내림으로 세지 않습니다.
- 서울 허가 내역은 접수번호당 1개 지번·처리 완료 건만 보여 주며 62일 창이라 매일 누적해야 합니다. 허가 건수는 `JOB_GBN_NM == '허가'` 만 셉니다(불허가·취하·반려 제외). 이 저장소가 집계를 시작한 날 이전 건은 없습니다(`PERMITS_BACKFILL_FROM=YYYY-MM-DD` 로 과거 창을 한 번 더 읽을 수 있음).
- 새올은 '접수' 단계를 보여주지 않고 '처리중'부터 보이므로 실제 신청은 표시보다 많습니다. 처리중이던 민원이 3회 연속 사라지면 '완료(목록이탈)'로 봅니다. 처음 봤을 때 이미 완료였던 건은 소요일 통계에서 뺍니다.
- 시 단위 새올(성남·수원·안양·용인·화성)은 처리부서명으로 구를 나눕니다. 부서명에 구가 없으면 시 코드 그대로(구 미분류). 화성시는 `41590` + 동탄 법정동으로 `41590:dongtan` 을 가려내고, 신설 구 코드가 `regions_live.json` 에서 발견되면 그 코드를 우선합니다.
- 서울시는 매월 10일 전후 자치구별 허가 신청 현황(월간 집계) 보도자료를 냅니다. 이 사이트의 '처리중'은 새올 공개 목록 기준이라 월간 집계와 정확히 일치하지 않습니다.
- 신설 구 코드는 매월 1일 전후 실행에서 자동 탐색(화성 41591~41599, 인천 28100~28299)합니다. 재건축으로 건축년도가 바뀌면 새 단지로 봅니다. 네이버 매칭이 틀렸으면 `data/complexes.csv` 의 `naverId` 를 고치세요(180일마다 재매칭).
- 공개 저장소에 키를 직접 쓰지 말고 반드시 Secret 으로 등록하세요. GitHub 은 60일 동안 활동이 없는 공개 저장소의 예약 실행을 멈춥니다.
- 새올·서울시·네이버 모두 자동 접근을 약관이나 robots.txt 로 제한할 수 있습니다. 공개 정보를 통계 목적으로 낮은 빈도로 읽는 용도로만 쓰고, 지역을 크게 늘리거나 주기를 촘촘하게 하지 마세요.

## 참고 저장소

- 급매판정(apt-geummae) — 이 저장소의 베이스. 기준가 모델·매물 판정·사이트 골격
- 토허제 라이브(apt-toheo-live) — `collector/toheo/` 로 포팅. 새올 파서·서울 허가 수집·지정 현황
- [hannamhyundai/hannamhyundai.github.io](https://github.com/hannamhyundai/hannamhyundai.github.io) — 서울시 `getContractList.do` 실수집 사례(요청·응답 필드 확인)
- 참고한 서비스: [급매캐치](https://www.geummaecatch.com)(판정 방식), [호갱노노](https://hogangnono.com)(단지별 토허 내역·알림 UX), [land.awenify.com](https://land.awenify.com)(구별 접수 현황)
- 데이터: 공공데이터포털 국토교통부 실거래가, 서울부동산정보광장 토지거래허가 내역, 각 지자체 새올 전자민원 서식민원처리공개, 국토부·서울시·경기도 토지거래허가구역 고시
