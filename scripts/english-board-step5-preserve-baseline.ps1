$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_baseline_preflight_{0}.txt" -f $Timestamp)

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "Git repository is not initialized: $Root"
}

$Branch = (git symbolic-ref --short HEAD 2>$null).Trim()
if ($Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

$PreStaged = @(git -c core.quotepath=false diff --cached --name-only)
if ($PreStaged.Count -ne 44) {
    throw "Unexpected staged count before baseline metadata: expected=44 actual=$($PreStaged.Count)"
}

$PreUntracked = @(git -c core.quotepath=false ls-files --others --exclude-standard)
if ($PreUntracked.Count -ne 0) {
    throw "Unexpected untracked candidates before baseline metadata: $($PreUntracked.Count)"
}

$IgnoredAudioCount = @(git ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCount = @(git ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCount -ne 3747) {
    throw "Unexpected ignored audio count: $IgnoredAudioCount"
}
if ($IgnoredImageCount -ne 6480) {
    throw "Unexpected ignored image count: $IgnoredImageCount"
}

$AttributesPath = Join-Path $Root ".gitattributes"
$PolicyPath = Join-Path $Root "DEVELOPMENT_POLICY.md"
if (Test-Path -LiteralPath $AttributesPath) {
    throw ".gitattributes already exists; refusing overwrite"
}
if (Test-Path -LiteralPath $PolicyPath) {
    throw "DEVELOPMENT_POLICY.md already exists; refusing overwrite"
}

$Attributes = @"
# Preserve the local implementation baseline exactly.
# Do not perform automatic line-ending normalization in this repository.
* -text
"@

$Policy = @"
# English Board Development Policy

## Role of the local version

The local English Board implementation is a permanent, actively maintained development and execution environment. It is not a disposable migration source.

Its speed and direct local execution are important. For behavior that is shared by Local and Web, the normal development flow is to implement and validate the change in the Local version first, then deliberately reflect that change in the Web version.

## Relationship to the Web version

Both Local and Web versions are maintained.

They are not expected to be byte-for-byte identical because the Web version is integrated into the Learning Platform and may require Web-specific authentication, API, storage, routing, security, and shell/UI integration.

The Web runtime must not reference or load files from this local directory. Changes are ported and integrated into the Learning Platform source tree.

Local and Web must not be treated as unrelated products. Shared behavior, UI intent, and feature changes should be kept aligned, with the Local implementation normally serving as the fast implementation and validation origin before Web adaptation.

## Local runtime media

The local audio and image directories remain part of the local runtime environment but are intentionally excluded from Git because of their volume. Their exclusion from Git does not mean they are unused or disposable.
"@

# Windows PowerShell 5.1 Set-Content UTF8 adds a BOM. These files are ASCII-only,
# so write ASCII to avoid introducing any encoding ambiguity.
$Attributes | Set-Content -LiteralPath $AttributesPath -Encoding ASCII
$Policy | Set-Content -LiteralPath $PolicyPath -Encoding ASCII

# Keep this repository from rewriting local implementation line endings on checkout/add.
git config --local core.autocrlf false
if ($LASTEXITCODE -ne 0) {
    throw "Failed to set repository-local core.autocrlf=false"
}

# Re-stage after .gitattributes so the initial baseline records the actual local file bytes.
git add -A
if ($LASTEXITCODE -ne 0) {
    throw "git add -A failed"
}

$Staged = @(git -c core.quotepath=false diff --cached --name-only)
if ($Staged.Count -ne 46) {
    throw "Unexpected staged count after baseline metadata: expected=46 actual=$($Staged.Count)"
}

$RemainingUntracked = @(git -c core.quotepath=false ls-files --others --exclude-standard)
if ($RemainingUntracked.Count -ne 0) {
    throw "Unexpected remaining untracked candidates: $($RemainingUntracked.Count)"
}

$RemoteCount = @(git remote).Count
if ($RemoteCount -ne 0) {
    throw "Unexpected remote exists before baseline commit. REMOTE_COUNT=$RemoteCount"
}

$AutoCrlf = (git config --local --get core.autocrlf).Trim()
if ($AutoCrlf -ne "false") {
    throw "Unexpected repository-local core.autocrlf value: $AutoCrlf"
}

$AttrCheck = (git check-attr text -- "English_Reading.html") -join "`n"
if ($AttrCheck -notmatch 'text: unset') {
    throw "Expected text attribute to be unset for source files. RESULT=$AttrCheck"
}

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t5_PRESERVE_BASELINE")
$Lines.Add("BRANCH`t$Branch")
$Lines.Add("PRE_STAGED_COUNT`t$($PreStaged.Count)")
$Lines.Add("STAGED_COUNT`t$($Staged.Count)")
$Lines.Add("REMAINING_UNTRACKED_COUNT`t$($RemainingUntracked.Count)")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCount")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCount")
$Lines.Add("CORE_AUTOCRLF_LOCAL`t$AutoCrlf")
$Lines.Add("GITATTRIBUTES`t$AttributesPath")
$Lines.Add("DEVELOPMENT_POLICY`t$PolicyPath")
$Lines.Add("COMMIT`tNO")
$Lines.Add("REMOTE`tNO")
$Lines.Add("")
$Lines.Add("=== STAGED FILES ===")
foreach ($Path in $Staged) { $Lines.Add($Path) }
$Lines.Add("")
$Lines.Add("=== GIT STATUS ===")
foreach ($Line in @(git -c core.quotepath=false status --short --ignored)) { $Lines.Add($Line) }
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=5_PRESERVE_BASELINE"
Write-Host "ROOT=$Root"
Write-Host "PRE_STAGED_COUNT=$($PreStaged.Count)"
Write-Host "STAGED_COUNT=$($Staged.Count)"
Write-Host "REMAINING_UNTRACKED_COUNT=$($RemainingUntracked.Count)"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioCount"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageCount"
Write-Host "CORE_AUTOCRLF_LOCAL=$AutoCrlf"
Write-Host "GITATTRIBUTES_ADDED=YES"
Write-Host "DEVELOPMENT_POLICY_ADDED=YES"
Write-Host "COMMIT=NO"
Write-Host "REMOTE=NO"
Write-Host "OUTPUT=$ReportPath"
