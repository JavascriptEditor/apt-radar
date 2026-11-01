<#
.SYNOPSIS
  집 PC(Windows)에서 네이버 매물을 수집해 data 브랜치에 올리는 한 방 스크립트. (bash 판: home_listings.sh)

.DESCRIPTION
  저장소 clone(없으면) → main 갱신 → data 브랜치 worktree → .venv + pip install → python -m collector listings
  → data 브랜치 커밋·푸시.  export·Pages 배포는 다음 GitHub Actions 실행이 처리합니다(또는 Actions 에서 mode=export 수동 실행).

  사용법
    1) 이미 clone 한 저장소 안에서:   powershell -ExecutionPolicy Bypass -File scripts\home_listings.ps1
    2) 아무 데서나(처음):            $env:REPO_URL="https://github.com/<아이디>/<저장소>.git"; .\home_listings.ps1
       → $env:APT_RADAR_DIR(기본 $HOME\apt-radar) 에 clone 한 뒤 실행

  환경변수(모두 선택)
    REPO_URL         저장소 주소(처음 clone 할 때만). 푸시하려면 Git Credential Manager 로 로그인돼 있어야 함
    APT_RADAR_DIR    저장소를 둘 폴더 (기본 $HOME\apt-radar; 저장소 안에서 실행하면 그 저장소)
    REGION_FILTER    수집 지역 (예: 서울 / 서울,경기 / 11680,41135). 비우면 수도권 전체
    NAVER_MAX_CALLS  실행당 호출 상한 (기본 1500, 약 20분)
    NAVER_MAX_SEC    실행당 시간 상한(초, 기본 2400)
    NAVER_DELAY_SEC  요청 간격(초, 기본 0.7). 더 줄이지 마세요
    NAVER_LAND_TOKEN (선택) 브라우저에서 복사한 Bearer 토큰
    PYTHON           파이썬 실행 파일 (기본 python; py -3.12 도 가능)
    NO_PUSH=1        커밋만 하고 푸시하지 않음(확인용)

  주기 실행: 작업 스케줄러(taskschd.msc) → 기본 작업 만들기 → 매일, 트리거를 09:40·13:40·17:40·21:40 네 개로 →
    프로그램: powershell.exe  인수: -ExecutionPolicy Bypass -File "C:\Users\<이름>\apt-radar\scripts\home_listings.ps1"
  네이버 비공식 API 는 약관상 허용되지 않으니 하루 4회 이상으로 늘리지 마세요.
#>
# 주의: Windows PowerShell 5.1 에서 ErrorActionPreference=Stop 이면 git 의 stderr(진행 메시지)가 예외가 되므로 Continue 로 두고 종료코드를 직접 검사한다
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:DATA_DIR = "data"

function Log($msg) { Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) }
function Run($cmd, [string[]]$argv, [switch]$Quiet) {
    # 외부 명령 실행. 실패(종료코드≠0)하면 예외
    $out = & $cmd @argv 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$cmd $($argv -join ' ') 실패: $out" }
    if (-not $Quiet) { $out | ForEach-Object { Write-Host $_ } }
    return $out
}

$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }
$Dir = if ($env:APT_RADAR_DIR) { $env:APT_RADAR_DIR } else { Join-Path $HOME "apt-radar" }

# ---- 1) 저장소 위치: 스크립트가 저장소 안에 있으면 그 저장소, 아니면 APT_RADAR_DIR
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Candidate = Split-Path -Parent $ScriptDir
if ((Test-Path (Join-Path $Candidate "collector\__main__.py")) -and (Test-Path (Join-Path $Candidate ".git"))) {
    $Root = $Candidate
} else {
    $Root = $Dir
    if (-not (Test-Path (Join-Path $Root ".git"))) {
        if (-not $env:REPO_URL) { Write-Error "REPO_URL 을 지정하세요 (예: `$env:REPO_URL='https://github.com/<아이디>/<저장소>.git')"; exit 2 }
        Log "clone $($env:REPO_URL) → $Root"
        Run git @("clone", $env:REPO_URL, $Root)
    }
}
Set-Location $Root
Log "저장소: $Root"

# ---- 2) 코드(main) 갱신 — 로컬 수정이 있으면 건드리지 않음
& git diff --quiet; $clean1 = ($LASTEXITCODE -eq 0)
& git diff --cached --quiet; $clean2 = ($LASTEXITCODE -eq 0)
if ($clean1 -and $clean2) {
    $branch = (& git rev-parse --abbrev-ref HEAD).Trim()
    & git pull -q --ff-only origin $branch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Log "main 갱신 실패(무시)" }
} else {
    Log "작업 트리에 수정이 있어 main 갱신은 건너뜀"
}

# ---- 3) data 브랜치 worktree (data\ 폴더)
if (-not (Test-Path "data\.git")) {
    if (Test-Path "data") { Remove-Item -Recurse -Force "data" }
    & git ls-remote --exit-code --heads origin data 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log "원격에 data 브랜치가 없습니다. 먼저 GitHub Actions 에서 수집을 한 번 실행하세요."
        exit 3
    }
    & git fetch -q origin data:data --update-head-ok 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Run git @("fetch", "-q", "origin", "data") -Quiet | Out-Null }
    Run git @("worktree", "add", "data", "data") -Quiet | Out-Null
}
Push-Location data
try {
    Run git @("fetch", "-q", "origin", "data") -Quiet | Out-Null
    Run git @("reset", "-q", "--hard", "origin/data") -Quiet | Out-Null
    Log ("data 브랜치: " + (& git log -1 --format='%h %cd' --date=short))
} finally { Pop-Location }

# ---- 4) 가상환경 + 의존성
$Venv = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Venv)) {
    Log "가상환경 생성(.venv)"
    Run $Python @("-m", "venv", ".venv") -Quiet | Out-Null
}
Run $Venv @("-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt") -Quiet | Out-Null

# ---- 5) 매물 수집
$region = if ($env:REGION_FILTER) { $env:REGION_FILTER } else { "전체" }
$maxCalls = if ($env:NAVER_MAX_CALLS) { $env:NAVER_MAX_CALLS } else { "1500" }
Log "네이버 매물 수집 시작 (REGION_FILTER=$region, NAVER_MAX_CALLS=$maxCalls)"
$out = & $Venv -m collector listings 2>&1
$result = ($out | Select-Object -Last 1)
Log "결과: $result"
if ("$result" -match '"status": "blocked') {
    Log "네이버가 요청을 막았습니다. 몇 시간 뒤 다시 시도하거나 NAVER_DELAY_SEC 를 늘리세요."
}

# ---- 6) 커밋·푸시
Push-Location data
try {
    Run git @("add", "-A") -Quiet | Out-Null
    & git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { Log "변경 없음"; exit 0 }
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mmZ")
    Run git @("-c", "user.name=collector-home", "-c", "user.email=collector-home@users.noreply.github.com",
              "commit", "-qm", "listings(home) $stamp") -Quiet | Out-Null
    if ($env:NO_PUSH -eq "1") { Log "NO_PUSH=1: 푸시 생략"; exit 0 }
    # Actions 커밋이 먼저 들어갔으면 그 위에 얹는다(충돌 시 집 PC 결과 우선)
    & git pull -q --rebase -X theirs origin data 2>&1 | Out-Null
    Run git @("push", "-q", "origin", "HEAD:data") -Quiet | Out-Null
    Log "푸시 완료. 다음 Actions 실행이 사이트를 다시 만듭니다 (바로 반영하려면 Actions 에서 mode=export 실행)."
} finally { Pop-Location }
