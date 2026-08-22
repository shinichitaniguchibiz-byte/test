$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_baseline_commit_{0}.txt" -f $Timestamp)

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    throw "Git repository is not initialized: $Root"
}

$Branch = (git symbolic-ref --short HEAD 2>$null).Trim()
if ($Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

$RemoteCount = @(git remote).Count
if ($RemoteCount -ne 0) {
    throw "Unexpected remote exists before local baseline commit: $RemoteCount"
}

# A repository with no commits is expected here. Check HEAD through cmd.exe so
# the normal non-zero exit for an unborn branch does not become a PowerShell error.
cmd.exe /d /c "git rev-parse --verify HEAD >nul 2>nul"
$HeadExists = ($LASTEXITCODE -eq 0)
if ($HeadExists) {
    $ExistingCommit = (git rev-parse HEAD).Trim()
    throw "Repository already has a commit: $ExistingCommit"
}

$Staged = @(git -c core.quotepath=false diff --cached --name-only)
if ($Staged.Count -ne 46) {
    throw "Unexpected staged count: expected=46 actual=$($Staged.Count)"
}

$Untracked = @(git -c core.quotepath=false ls-files --others --exclude-standard)
if ($Untracked.Count -ne 0) {
    throw "Unexpected untracked non-ignored files: $($Untracked.Count)"
}

$IgnoredAudioCount = @(git ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCount = @(git ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCount -ne 3747) {
    throw "Unexpected ignored audio count: $IgnoredAudioCount"
}
if ($IgnoredImageCount -ne 6480) {
    throw "Unexpected ignored image count: $IgnoredImageCount"
}

if (-not (Test-Path -LiteralPath (Join-Path $Root ".gitattributes") -PathType Leaf)) {
    throw ".gitattributes is missing"
}
if (-not (Test-Path -LiteralPath (Join-Path $Root "DEVELOPMENT_POLICY.md") -PathType Leaf)) {
    throw "DEVELOPMENT_POLICY.md is missing"
}

$AutoCrlf = (git config --local --get core.autocrlf 2>$null)
if ($AutoCrlf -ne "false") {
    throw "Unexpected local core.autocrlf: $AutoCrlf"
}

$UserName = ""
$UserEmail = ""
$NameLines = @(git config --get user.name 2>$null)
if ($NameLines.Count -gt 0) { $UserName = ($NameLines -join " ").Trim() }
$EmailLines = @(git config --get user.email 2>$null)
if ($EmailLines.Count -gt 0) { $UserEmail = ($EmailLines -join " ").Trim() }

if ([string]::IsNullOrWhiteSpace($UserName) -or [string]::IsNullOrWhiteSpace($UserEmail)) {
    Write-Host "RESULT=NEEDS_GIT_IDENTITY"
    Write-Host "STEP=6B_BASELINE_COMMIT_SAFE"
    Write-Host "USER_NAME=$UserName"
    Write-Host "USER_EMAIL=$UserEmail"
    Write-Host "STAGED_COUNT=$($Staged.Count)"
    Write-Host "COMMIT=NO"
    Write-Host "REMOTE=NO"
    exit 0
}

$CommitMessage = "Establish English Board local baseline"
git commit -m $CommitMessage
if ($LASTEXITCODE -ne 0) {
    throw "git commit failed"
}

$Head = (git rev-parse HEAD).Trim()
$HeadMessage = (git log -1 --pretty=%s).Trim()
$TrackedCount = @(git ls-files).Count
$StatusPorcelain = @(git status --porcelain)
if ($StatusPorcelain.Count -ne 0) {
    throw "Working tree is not clean after commit"
}
if ($TrackedCount -ne 46) {
    throw "Unexpected tracked count after commit: expected=46 actual=$TrackedCount"
}

$RemoteCountAfter = @(git remote).Count
if ($RemoteCountAfter -ne 0) {
    throw "Unexpected remote after commit: $RemoteCountAfter"
}

$IgnoredAudioCountAfter = @(git ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCountAfter = @(git ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCountAfter -ne 3747 -or $IgnoredImageCountAfter -ne 6480) {
    throw "Ignored media counts changed after commit: audio=$IgnoredAudioCountAfter image=$IgnoredImageCountAfter"
}

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t6B_BASELINE_COMMIT_SAFE")
$Lines.Add("BRANCH`t$Branch")
$Lines.Add("HEAD`t$Head")
$Lines.Add("MESSAGE`t$HeadMessage")
$Lines.Add("USER_NAME`t$UserName")
$Lines.Add("USER_EMAIL`t$UserEmail")
$Lines.Add("TRACKED_COUNT`t$TrackedCount")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCountAfter")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCountAfter")
$Lines.Add("CORE_AUTOCRLF_LOCAL`t$AutoCrlf")
$Lines.Add("WORKTREE_CLEAN`tYES")
$Lines.Add("REMOTE`tNO")
$Lines.Add("")
$Lines.Add("=== TRACKED FILES ===")
foreach ($Path in @(git -c core.quotepath=false ls-files)) { $Lines.Add($Path) }
$Lines.Add("")
$Lines.Add("=== COMMIT ===")
foreach ($Line in @(git -c core.quotepath=false show --stat --oneline --decorate --no-renames HEAD)) { $Lines.Add($Line) }
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=6B_BASELINE_COMMIT_SAFE"
Write-Host "ROOT=$Root"
Write-Host "BRANCH=$Branch"
Write-Host "HEAD=$Head"
Write-Host "TRACKED_COUNT=$TrackedCount"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioCountAfter"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageCountAfter"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "REMOTE=NO"
Write-Host "OUTPUT=$ReportPath"
