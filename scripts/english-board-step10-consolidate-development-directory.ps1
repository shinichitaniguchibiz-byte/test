$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step10_{0}.txt" -f $Timestamp)

$ExpectedRemoteHead = "f0c79671e46766b9fccea2ef3bbeeb715e5df49b"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedTrackedCount = 52
$ExpectedMovedFileCount = 8
$ExpectedAudioCount = 3747
$ExpectedImageCount = 6480

function Fail([string]$Message) {
    throw $Message
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $GitCommand) {
    $GitCommand = Get-Command git -ErrorAction Stop
}
$GitExe = $GitCommand.Source
if ([string]::IsNullOrWhiteSpace($GitExe)) {
    Fail "Unable to resolve native Git executable."
}

function Invoke-GitNative([string[]]$GitArgs) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $script:GitExe -C $script:Root -c core.quotepath=false @GitArgs 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldPreference
    }
    $lines = @($output | ForEach-Object { $_.ToString() })
    if ($code -ne 0) {
        Fail "git failed ($code): git $($GitArgs -join ' ')`n$($lines -join "`n")"
    }
    return $lines
}

function Get-GitText([string[]]$GitArgs) {
    return ((Invoke-GitNative -GitArgs $GitArgs) -join "`n").Trim()
}

function Get-RelativePath([string]$BasePath, [string]$FullPath) {
    $baseUri = New-Object System.Uri(($BasePath.TrimEnd('\') + '\'))
    $fileUri = New-Object System.Uri($FullPath)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fileUri).ToString()).Replace('/', '\')
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    Fail "Git repository is missing: $Root"
}

$branch = Get-GitText -GitArgs @("symbolic-ref", "--short", "HEAD")
if ($branch -ne "main") {
    Fail "Unexpected branch: $branch"
}

$initialStatus = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($initialStatus.Count -ne 0) {
    Fail "Worktree must be clean before development-directory consolidation.`n$($initialStatus -join "`n")"
}

$origin = Get-GitText -GitArgs @("remote", "get-url", "origin")
if ($origin -ne $ExpectedOrigin) {
    Fail "Unexpected origin: $origin"
}

Write-Host "PHASE=SYNC_REMOTE"
Invoke-GitNative -GitArgs @("fetch", "--quiet", "origin", "main") | Out-Null
$remoteHead = Get-GitText -GitArgs @("rev-parse", "origin/main")
if ($remoteHead -ne $ExpectedRemoteHead) {
    Fail "Unexpected remote main HEAD: expected=$ExpectedRemoteHead actual=$remoteHead"
}

$localHeadBeforeSync = Get-GitText -GitArgs @("rev-parse", "HEAD")
$mergeBase = Get-GitText -GitArgs @("merge-base", "HEAD", "origin/main")
if ($mergeBase -ne $localHeadBeforeSync) {
    Fail "Local HEAD is not an ancestor of origin/main. Refusing non-fast-forward synchronization."
}

Invoke-GitNative -GitArgs @("merge", "--ff-only", "origin/main") | Out-Null
$headAfterSync = Get-GitText -GitArgs @("rev-parse", "HEAD")
if ($headAfterSync -ne $ExpectedRemoteHead) {
    Fail "Local HEAD did not synchronize to expected remote HEAD."
}

$trackedBefore = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedBefore -ne $ExpectedTrackedCount) {
    Fail "Unexpected tracked file count before move: expected=$ExpectedTrackedCount actual=$trackedBefore"
}

$audioBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioBefore -ne $ExpectedAudioCount) {
    Fail "Unexpected ignored audio count: expected=$ExpectedAudioCount actual=$audioBefore"
}
if ($imageBefore -ne $ExpectedImageCount) {
    Fail "Unexpected ignored image count: expected=$ExpectedImageCount actual=$imageBefore"
}

$sourceRoots = @(
    [pscustomobject]@{ Source = (Join-Path $Root "docs"); Destination = (Join-Path $Root "development\documents") },
    [pscustomobject]@{ Source = (Join-Path $Root "tools"); Destination = (Join-Path $Root "development\tools") },
    [pscustomobject]@{ Source = (Join-Path $Root "samples"); Destination = (Join-Path $Root "development\samples") }
)

foreach ($entry in $sourceRoots) {
    if (-not (Test-Path -LiteralPath $entry.Source -PathType Container)) {
        Fail "Expected source directory is missing: $($entry.Source)"
    }
    if (Test-Path -LiteralPath $entry.Destination) {
        Fail "Destination already exists: $($entry.Destination)"
    }
}

Write-Host "PHASE=CAPTURE_PREMOVE_HASHES"
$beforeFiles = New-Object System.Collections.Generic.List[object]
foreach ($entry in $sourceRoots) {
    $files = @(Get-ChildItem -LiteralPath $entry.Source -File -Recurse -Force)
    foreach ($file in $files) {
        $relativeInsideSource = Get-RelativePath -BasePath $entry.Source -FullPath $file.FullName
        $destinationFile = Join-Path $entry.Destination $relativeInsideSource
        $beforeFiles.Add([pscustomobject]@{
            Source = $file.FullName
            Destination = $destinationFile
            Hash = (Get-Sha256 -Path $file.FullName)
        })
    }
}

if ($beforeFiles.Count -ne $ExpectedMovedFileCount) {
    Fail "Unexpected development-only file count: expected=$ExpectedMovedFileCount actual=$($beforeFiles.Count)"
}

Write-Host "PHASE=MOVE_TO_DEVELOPMENT"
$developmentRoot = Join-Path $Root "development"
New-Item -ItemType Directory -Force -Path $developmentRoot | Out-Null
Invoke-GitNative -GitArgs @("mv", "--", "docs", "development/documents") | Out-Null
Invoke-GitNative -GitArgs @("mv", "--", "tools", "development/tools") | Out-Null
Invoke-GitNative -GitArgs @("mv", "--", "samples", "development/samples") | Out-Null

foreach ($entry in $sourceRoots) {
    if (Test-Path -LiteralPath $entry.Source) {
        Fail "Old directory still exists after move: $($entry.Source)"
    }
    if (-not (Test-Path -LiteralPath $entry.Destination -PathType Container)) {
        Fail "Destination directory missing after move: $($entry.Destination)"
    }
}

Write-Host "PHASE=VERIFY_MOVED_CONTENT"
foreach ($record in $beforeFiles) {
    if (-not (Test-Path -LiteralPath $record.Destination -PathType Leaf)) {
        Fail "Moved file missing: $($record.Destination)"
    }
    $afterHash = Get-Sha256 -Path $record.Destination
    if ($afterHash -ne $record.Hash) {
        Fail "Moved file hash changed: $($record.Destination)"
    }
}

$renameLines = @(Invoke-GitNative -GitArgs @("diff", "--cached", "--name-status", "-M") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($renameLines.Count -ne $ExpectedMovedFileCount) {
    Fail "Unexpected staged change count: expected=$ExpectedMovedFileCount actual=$($renameLines.Count)`n$($renameLines -join "`n")"
}
foreach ($line in $renameLines) {
    if ($line -notmatch '^R100\s+') {
        Fail "Expected byte-identical rename but found: $line"
    }
}

$unstaged = @(Invoke-GitNative -GitArgs @("diff", "--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($unstaged.Count -ne 0) {
    Fail "Unexpected unstaged changes after move.`n$($unstaged -join "`n")"
}

Write-Host "PHASE=COMMIT"
Invoke-GitNative -GitArgs @("commit", "-m", "Consolidate development-only assets under development") | Out-Null
$newHead = Get-GitText -GitArgs @("rev-parse", "HEAD")

$trackedAfter = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedAfter -ne $ExpectedTrackedCount) {
    Fail "Tracked file count changed unexpectedly: expected=$ExpectedTrackedCount actual=$trackedAfter"
}

$statusAfterCommit = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($statusAfterCommit.Count -ne 0) {
    Fail "Worktree is not clean after commit.`n$($statusAfterCommit -join "`n")"
}

$audioAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioAfter -ne $ExpectedAudioCount) {
    Fail "Audio count changed unexpectedly: expected=$ExpectedAudioCount actual=$audioAfter"
}
if ($imageAfter -ne $ExpectedImageCount) {
    Fail "Image count changed unexpectedly: expected=$ExpectedImageCount actual=$imageAfter"
}

Write-Host "PHASE=PUSH"
Invoke-GitNative -GitArgs @("fetch", "--quiet", "origin", "main") | Out-Null
$remoteBeforePush = Get-GitText -GitArgs @("rev-parse", "origin/main")
if ($remoteBeforePush -ne $ExpectedRemoteHead) {
    Fail "Remote main changed during operation: expected=$ExpectedRemoteHead actual=$remoteBeforePush"
}

Invoke-GitNative -GitArgs @("push", "--quiet", "origin", "main") | Out-Null
Invoke-GitNative -GitArgs @("fetch", "--quiet", "origin", "main") | Out-Null
$remoteAfterPush = Get-GitText -GitArgs @("rev-parse", "origin/main")
if ($remoteAfterPush -ne $newHead) {
    Fail "Remote verification failed: local=$newHead remote=$remoteAfterPush"
}

$finalStatus = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($finalStatus.Count -ne 0) {
    Fail "Final worktree is not clean.`n$($finalStatus -join "`n")"
}

$reportLines = @(
    "STEP`t10_CONSOLIDATE_DEVELOPMENT_DIRECTORY",
    "CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "PREVIOUS_REMOTE_HEAD`t$ExpectedRemoteHead",
    "NEW_HEAD`t$newHead",
    "TRACKED_COUNT`t$trackedAfter",
    "MOVED_FILE_COUNT`t$($beforeFiles.Count)",
    "RUNTIME_PATHS_CHANGED`tNO",
    "FILENAMES_CHANGED`tNO",
    "FILE_CONTENT_CHANGED`tNO",
    "IGNORED_AUDIO_COUNT`t$audioAfter",
    "IGNORED_IMAGE_COUNT`t$imageAfter",
    "WORKTREE_CLEAN`tYES",
    "REMOTE_PUSH`tYES"
)
$reportLines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=10_CONSOLIDATE_DEVELOPMENT_DIRECTORY"
Write-Host "NEW_HEAD=$newHead"
Write-Host "TRACKED_COUNT=$trackedAfter"
Write-Host "MOVED_FILE_COUNT=$($beforeFiles.Count)"
Write-Host "RUNTIME_PATHS_CHANGED=NO"
Write-Host "FILENAMES_CHANGED=NO"
Write-Host "FILE_CONTENT_CHANGED=NO"
Write-Host "IGNORED_AUDIO_COUNT=$audioAfter"
Write-Host "IGNORED_IMAGE_COUNT=$imageAfter"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "REMOTE_PUSH=YES"
Write-Host "OUTPUT=$ReportPath"
