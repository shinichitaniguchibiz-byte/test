$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$ReportDir = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board\reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Report = Join-Path $ReportDir ("english_board_git_init_{0}.txt" -f $Timestamp)

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "English Board root not found: $Root"
}

Set-Location -LiteralPath $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git"))) {
    git init -b main | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git init failed" }
}

$Inside = (git rev-parse --is-inside-work-tree 2>$null).Trim()
if ($LASTEXITCODE -ne 0 -or $Inside -ne "true") {
    throw "Git repository verification failed"
}

$TopLevel = (git rev-parse --show-toplevel).Trim()
$Branch = (git symbolic-ref --short HEAD 2>$null).Trim()
if (-not $Branch) { $Branch = "(UNBORN_OR_DETACHED)" }

$Candidates = @(git ls-files --others --exclude-standard)
if ($LASTEXITCODE -ne 0) { throw "Unable to enumerate Git candidates" }

$Ignored = @(git ls-files --others --ignored --exclude-standard)
if ($LASTEXITCODE -ne 0) { throw "Unable to enumerate ignored files" }

$AudioIgnored = @($Ignored | Where-Object { $_ -like "audio/*" }).Count
$ImageIgnored = @($Ignored | Where-Object { $_ -like "image/*" }).Count
$SvgCandidates = @($Candidates | Where-Object { $_ -like "*.svg" -or $_ -like "*/*.svg" }).Count
$SvgIgnored = @($Ignored | Where-Object { $_ -like "*.svg" -or $_ -like "*/*.svg" }).Count

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t2_INIT_VERIFY_GIT")
$Lines.Add("GIT_TOPLEVEL`t$TopLevel")
$Lines.Add("BRANCH`t$Branch")
$Lines.Add("TRACK_CANDIDATE_COUNT`t$($Candidates.Count)")
$Lines.Add("IGNORED_FILE_COUNT`t$($Ignored.Count)")
$Lines.Add("IGNORED_AUDIO_COUNT`t$AudioIgnored")
$Lines.Add("IGNORED_IMAGE_COUNT`t$ImageIgnored")
$Lines.Add("SVG_TRACK_CANDIDATE_COUNT`t$SvgCandidates")
$Lines.Add("SVG_IGNORED_COUNT`t$SvgIgnored")
$Lines.Add("")
$Lines.Add("=== GIT STATUS ===")
foreach ($x in @(git status --short --branch)) { $Lines.Add([string]$x) }
$Lines.Add("")
$Lines.Add("=== TRACK CANDIDATES ===")
foreach ($x in $Candidates) { $Lines.Add([string]$x) }
$Lines.Add("")
$Lines.Add("=== IGNORED SUMMARY ===")
$Lines.Add("audio/*`t$AudioIgnored")
$Lines.Add("image/*`t$ImageIgnored")
$Lines.Add("")
$Lines.Add("=== ROOT IGNORE CHECKS ===")
foreach ($Pattern in @("filelists_*.txt", "gitinfo_*.txt", "english_board_inventory_*.txt")) {
    $Matches = @(Get-ChildItem -LiteralPath $Root -File -Filter $Pattern -ErrorAction SilentlyContinue)
    $Lines.Add("$Pattern`tROOT_MATCH_COUNT=$($Matches.Count)")
}

$Lines | Set-Content -LiteralPath $Report -Encoding UTF8

if ($AudioIgnored -lt 1) { throw "audio files are not ignored as expected" }
if ($ImageIgnored -lt 1) { throw "image files are not ignored as expected" }
if ($SvgCandidates -lt 1) { throw "SVG application assets are not visible as Git candidates" }
if ($SvgIgnored -ne 0) { throw "SVG application assets are incorrectly ignored" }

Write-Host "RESULT=PASS"
Write-Host "STEP=2_INIT_VERIFY_GIT"
Write-Host "ROOT=$Root"
Write-Host "BRANCH=$Branch"
Write-Host "TRACK_CANDIDATE_COUNT=$($Candidates.Count)"
Write-Host "IGNORED_FILE_COUNT=$($Ignored.Count)"
Write-Host "IGNORED_AUDIO_COUNT=$AudioIgnored"
Write-Host "IGNORED_IMAGE_COUNT=$ImageIgnored"
Write-Host "SVG_TRACK_CANDIDATE_COUNT=$SvgCandidates"
Write-Host "SVG_IGNORED_COUNT=$SvgIgnored"
Write-Host "GIT_ADD=NO"
Write-Host "COMMIT=NO"
Write-Host "REMOTE=NO"
Write-Host "APPLICATION_SOURCE_MOVED=NO"
Write-Host "MEDIA_MOVED=NO"
Write-Host "OUTPUT=$Report"
