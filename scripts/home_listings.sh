#!/usr/bin/env bash
# 집 PC(macOS·Linux)에서 네이버 매물을 수집해 data 브랜치에 올리는 한 방 스크립트.
#
#   저장소 clone(없으면) → main 갱신 → data 브랜치 worktree → .venv + pip install → python -m collector listings
#   → data 브랜치 커밋·푸시.  export·Pages 배포는 다음 GitHub Actions 실행이 처리합니다(또는 Actions 에서 mode=export 수동 실행).
#
# 사용법
#   1) 이미 clone 한 저장소 안에서:   bash scripts/home_listings.sh
#   2) 아무 데서나(처음):            REPO_URL=https://github.com/<아이디>/<저장소>.git bash home_listings.sh
#      → $APT_RADAR_DIR(기본 ~/apt-radar) 에 clone 한 뒤 실행
#
# 환경변수(모두 선택)
#   REPO_URL         저장소 주소(처음 clone 할 때만 필요). 푸시하려면 로그인된 git(gh auth login / credential helper / SSH)
#   APT_RADAR_DIR    저장소를 둘 폴더 (기본 ~/apt-radar; 저장소 안에서 실행하면 그 저장소)
#   REGION_FILTER    수집 지역 (예: 서울 / 서울,경기 / 11680,41135). 비우면 수도권 전체
#   NAVER_MAX_CALLS  실행당 호출 상한 (기본 1500, 약 20분)
#   NAVER_MAX_SEC    실행당 시간 상한(초, 기본 2400)
#   NAVER_DELAY_SEC  요청 간격(초, 기본 0.7). 더 줄이지 마세요
#   NAVER_LAND_TOKEN (선택) 브라우저에서 복사한 Bearer 토큰
#   PYTHON           파이썬 실행 파일 (기본 python3)
#   NO_PUSH=1        커밋만 하고 푸시하지 않음(확인용)
#
# 주기 실행 예 (crontab -e):  40 9,13,17,21 * * *  /bin/bash $HOME/apt-radar/scripts/home_listings.sh >> $HOME/apt-radar.log 2>&1
#   macOS 는 launchd 로도 가능. 네이버 비공식 API 는 약관상 허용되지 않으니 하루 4회 이상으로 늘리지 마세요.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
APT_RADAR_DIR="${APT_RADAR_DIR:-$HOME/apt-radar}"
export DATA_DIR=data

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ---- 1) 저장소 위치 결정: 스크립트가 저장소 안에 있으면 그 저장소, 아니면 APT_RADAR_DIR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/../collector/__main__.py" ] && [ -d "$SCRIPT_DIR/../.git" ]; then
  ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  ROOT="$APT_RADAR_DIR"
  if [ ! -d "$ROOT/.git" ]; then
    [ -n "${REPO_URL:-}" ] || { echo "REPO_URL 을 지정하세요 (예: REPO_URL=https://github.com/<아이디>/<저장소>.git)"; exit 2; }
    log "clone $REPO_URL → $ROOT"
    git clone "$REPO_URL" "$ROOT"
  fi
fi
cd "$ROOT"
log "저장소: $ROOT"

# ---- 2) 코드(main) 갱신 — 로컬 수정이 있으면 건드리지 않음
if git diff --quiet && git diff --cached --quiet; then
  git pull -q --ff-only origin "$(git rev-parse --abbrev-ref HEAD)" || log "main 갱신 실패(무시)"
else
  log "작업 트리에 수정이 있어 main 갱신은 건너뜀"
fi

# ---- 3) data 브랜치 worktree (data/ 폴더)
git worktree prune
if [ ! -d data/.git ] && [ ! -f data/.git ]; then
  if ! git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
    log "원격에 data 브랜치가 없습니다. 먼저 GitHub Actions 에서 수집을 한 번 실행하세요."
    exit 3
  fi
  if [ -e data ]; then
    # worktree 가 아닌 data/ 가 이미 있음(로컬 `python -m collector run` 결과 등) → 지우지 않고 옆으로 옮긴다
    BAK="data.local-$(date '+%Y%m%d-%H%M%S')"
    log "data/ 가 git worktree 가 아니어서 $BAK 로 옮깁니다 (필요 없으면 직접 지우세요)"
    mv data "$BAK"
  fi
  git fetch -q origin data:data --update-head-ok 2>/dev/null || git fetch -q origin data
  git worktree add data data
fi
( cd data && { git rebase --abort >/dev/null 2>&1 || true; } && git fetch -q origin data && git reset -q --hard origin/data )
log "data 브랜치: $(cd data && git log -1 --format='%h %cd' --date=short)"

# ---- 4) 가상환경 + 의존성
if [ ! -x .venv/bin/python ]; then
  log "가상환경 생성(.venv)"
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

# ---- 5) 매물 수집 (파이썬 종료코드를 보존: 예외로 죽으면 커밋하지 않고 exit 4)
log "네이버 매물 수집 시작 (REGION_FILTER=${REGION_FILTER:-전체}, NAVER_MAX_CALLS=${NAVER_MAX_CALLS:-1500})"
set +e
OUT="$(.venv/bin/python -m collector listings)"
RC=$?
set -e
RESULT="$(printf '%s\n' "$OUT" | tail -n 1)"
log "결과: $RESULT"
if [ "$RC" -ne 0 ]; then
  log "매물 수집이 비정상 종료했습니다(exit $RC). 커밋하지 않습니다 — 위 오류 메시지를 확인하세요."
  exit 4
fi
case "$RESULT" in
  *'"status": "blocked'*) log "네이버가 요청을 막았습니다. 몇 시간 뒤 다시 시도하거나 NAVER_DELAY_SEC 를 늘리세요." ;;
  *'"status": "skipped'*) log "직전 수집이 최근이라 건너뛰었습니다 (LISTINGS_MIN_INTERVAL_MIN)." ;;
esac

# ---- 5b) (선택) 토허 수집도 집 PC 에서: HOME_TOHEO=1
# GitHub Actions(해외 IP)에서는 서울 자치구 새올·land.seoul.go.kr 연결이 막히는 날이 있다. 국내 IP 인 집 PC 에서
# 가벼운 토허 수집을 같이 돌리면(HOME_TOHEO=1, 깊은 수집은 HOME_TOHEO=deep) 그 날도 서울 허가·민원이 채워진다.
if [ "${HOME_TOHEO:-0}" != "0" ]; then
  TOHEO_ARGS=""
  [ "${HOME_TOHEO}" = "deep" ] && TOHEO_ARGS="--deep"
  log "토허 수집 시작 (HOME_TOHEO=${HOME_TOHEO})"
  set +e
  TOUT="$(.venv/bin/python -m collector toheo $TOHEO_ARGS)"
  TRC=$?
  set -e
  log "토허 결과: $(printf '%s\n' "$TOUT" | tail -n 1 | cut -c1-300)"
  [ "$TRC" -ne 0 ] && log "토허 수집이 비정상 종료했습니다(exit $TRC). 매물 결과만 커밋합니다."
fi

# ---- 6) 커밋·푸시
cd data
git add -A
if git diff --cached --quiet; then
  log "변경 없음"
  exit 0
fi
git -c user.name=collector-home -c user.email=collector-home@users.noreply.github.com \
  commit -qm "listings(home) $(date -u +'%Y-%m-%dT%H:%MZ')"
if [ "${NO_PUSH:-0}" = "1" ]; then
  log "NO_PUSH=1: 푸시 생략"
  exit 0
fi
# Actions 커밋이 먼저 들어갔으면 그 위에 얹는다(충돌 시 집 PC 결과 우선). 실패하면 되돌리고 재시도(최대 3회)
for i in 1 2 3; do
  if git pull -q --rebase -X theirs origin data && git push -q origin HEAD:data; then
    log "푸시 완료. 다음 Actions 실행이 사이트를 다시 만듭니다 (바로 반영하려면 Actions 에서 mode=export 실행)."
    exit 0
  fi
  git rebase --abort 2>/dev/null || true
  log "푸시 실패($i/3) — 잠시 뒤 다시 시도"
  sleep $((i * 10))
done
log "푸시에 실패했습니다. 커밋은 로컬 data 브랜치에 남아 있으니 다음 실행에서 다시 시도합니다."
exit 5
