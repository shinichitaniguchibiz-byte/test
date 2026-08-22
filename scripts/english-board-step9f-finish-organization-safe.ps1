$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step9f_{0}.txt" -f $Timestamp)
$ClipboardHash = "9FF7079F2EF4EB5DEF1083684249A98B924146D7C05E0F3632822473DF956F57"
$ExpectedRemoteHead = "4b5091a912af4cbf2828fb8d180d9fb2ddc383b6"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedTrackedBefore = 47
$ExpectedTrackedAfter = 52
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
        $output = & $script:GitExe -C $script:Root @GitArgs 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }

    $lines = @($output | ForEach-Object { $_.ToString() })
    if ($exitCode -ne 0) {
        Fail ("git failed ({0}): git {1}`n{2}" -f $exitCode, ($GitArgs -join " "), ($lines -join "`n"))
    }
    return $lines
}

function Get-GitText([string[]]$GitArgs) {
    return ((Invoke-GitNative -GitArgs $GitArgs) -join "`n").Trim()
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    Fail "Git repository missing."
}

$branch = Get-GitText -GitArgs @("symbolic-ref", "--short", "HEAD")
if ($branch -ne "main") {
    Fail "Unexpected branch: $branch"
}

$trackedDirty = @(Invoke-GitNative -GitArgs @("status", "--porcelain", "--untracked-files=no") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($trackedDirty.Count -ne 0) {
    Fail "Tracked or staged changes exist. Refusing organization."
}

Write-Host "PHASE=REMOVE_TRANSIENT_CLIPBOARD_TEXT"
$removedTransient = 0
$rootClipboard = Join-Path $Root "Clipboard Text.txt"
if (Test-Path -LiteralPath $rootClipboard -PathType Leaf) {
    if ((Get-Sha256 $rootClipboard) -ne $ClipboardHash) {
        Fail "Unexpected Clipboard Text.txt content in repository root."
    }
    Remove-Item -LiteralPath $rootClipboard -Force
    $removedTransient++
}

$archiveRoot = Join-Path $DevRoot "clipboard-archive"
if (Test-Path -LiteralPath $archiveRoot -PathType Container) {
    $copies = @(Get-ChildItem -LiteralPath $archiveRoot -Filter "Clipboard Text.txt" -File -Recurse -ErrorAction SilentlyContinue)
    foreach ($copy in $copies) {
        if ((Get-Sha256 $copy.FullName) -eq $ClipboardHash) {
            Remove-Item -LiteralPath $copy.FullName -Force
            $removedTransient++
        }
    }

    $dirs = @(Get-ChildItem -LiteralPath $archiveRoot -Directory -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)
    foreach ($dir in $dirs) {
        if (-not (Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
            Remove-Item -LiteralPath $dir.FullName -Force -ErrorAction SilentlyContinue
        }
    }
    if ((Test-Path -LiteralPath $archiveRoot) -and -not (Get-ChildItem -LiteralPath $archiveRoot -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Remove-Item -LiteralPath $archiveRoot -Force -ErrorAction SilentlyContinue
    }
}

$untrackedBeforeSync = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($untrackedBeforeSync.Count -ne 0) {
    Fail "Unexpected untracked files remain before sync: $($untrackedBeforeSync -join ', ')"
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

$localHead = Get-GitText -GitArgs @("rev-parse", "HEAD")
$mergeBase = Get-GitText -GitArgs @("merge-base", "HEAD", "origin/main")
if ($mergeBase -ne $localHead) {
    Fail "Local HEAD is not an ancestor of origin/main."
}

Invoke-GitNative -GitArgs @("merge", "--ff-only", "origin/main") | Out-Null
$headAfterSync = Get-GitText -GitArgs @("rev-parse", "HEAD")
if ($headAfterSync -ne $ExpectedRemoteHead) {
    Fail "Local sync failed: expected=$ExpectedRemoteHead actual=$headAfterSync"
}

$trackedBefore = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedBefore -ne $ExpectedTrackedBefore) {
    Fail "Unexpected tracked count before move: expected=$ExpectedTrackedBefore actual=$trackedBefore"
}

$audioBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioBefore -ne $ExpectedAudioCount) {
    Fail "Unexpected audio count: expected=$ExpectedAudioCount actual=$audioBefore"
}
if ($imageBefore -ne $ExpectedImageCount) {
    Fail "Unexpected image count: expected=$ExpectedImageCount actual=$imageBefore"
}

$Items = @(
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "samples\20260517095055.txt")
        Destination = (Join-Path $Root "samples\reading\20260517095055.txt")
        Relative = "samples/reading/20260517095055.txt"
        Hash = "D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "design\icon-check.html")
        Destination = (Join-Path $Root "tools\icon\icon-check.html")
        Relative = "tools/icon/icon-check.html"
        Hash = "6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "design\icon-concepts.html")
        Destination = (Join-Path $Root "docs\design\icon-concepts.html")
        Relative = "docs/design/icon-concepts.html"
        Hash = "DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "legacy-tools\make_zip.ps1")
        Destination = (Join-Path $Root "tools\legacy\make_zip.ps1")
        Relative = "tools/legacy/make_zip.ps1"
        Hash = "E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "legacy-tools\make_zip_all.ps1")
        Destination = (Join-Path $Root "tools\legacy\make_zip_all.ps1")
        Relative = "tools/legacy/make_zip_all.ps1"
        Hash = "F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356"
    }
)

Write-Host "PHASE=VERIFY_APPROVED_FILES"
foreach ($item in $Items) {
    if (-not (Test-Path -LiteralPath $item.Source -PathType Leaf)) {
        Fail "Missing source: $($item.Source)"
    }
    if ((Get-Sha256 $item.Source) -ne $item.Hash) {
        Fail "Source hash mismatch: $($item.Source)"
    }
    if (Test-Path -LiteralPath $item.Destination) {
        Fail "Destination already exists: $($item.Destination)"
    }
}

Write-Host "PHASE=COPY_VERIFY_STAGE_COMMIT"
foreach ($item in $Items) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $item.Destination) | Out-Null
    Copy-Item -LiteralPath $item.Source -Destination $item.Destination
    if ((Get-Sha256 $item.Destination) -ne $item.Hash) {
        Fail "Destination hash mismatch: $($item.Destination)"
    }
}

$expected = @($Items.Relative | Sort-Object)
$actual = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($expected -join "`n") -ne ($actual -join "`n")) {
    Fail "Unexpected untracked set after copy.`nExpected:`n$($expected -join "`n")`nActual:`n$($actual -join "`n")"
}

foreach ($item in $Items) {
    Invoke-GitNative -GitArgs @("add", "--", $item.Relative) | Out-Null
}

$staged = @(Invoke-GitNative -GitArgs @("diff", "--cached", "--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($expected -join "`n") -ne ($staged -join "`n")) {
    Fail "Unexpected staged set."
}

Invoke-GitNative -GitArgs @("commit", "-m", "Organize English Board non-runtime artifacts") | Out-Null
$newHead = Get-GitText -GitArgs @("rev-parse", "HEAD")

$trackedAfter = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedAfter -ne $ExpectedTrackedAfter) {
    Fail "Unexpected tracked count after commit: expected=$ExpectedTrackedAfter actual=$trackedAfter"
}

$statusAfterCommit = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($statusAfterCommit.Count -ne 0) {
    Fail "Worktree not clean after commit."
}

Write-Host "PHASE=PUSH"
Invoke-GitNative -GitArgs @("fetch", "--quiet", "origin", "main") | Out-Null
if ((Get-GitText -GitArgs @("rev-parse", "origin/main")) -ne $ExpectedRemoteHead) {
    Fail "Remote changed during operation."
}

Invoke-GitNative -GitArgs @("push", "--quiet", "origin", "main") | Out-Null
Invoke-GitNative -GitArgs @("fetch", "--quiet", "origin", "main") | Out-Null
if ((Get-GitText -GitArgs @("rev-parse", "origin/main")) -ne $newHead) {
    Fail "Remote verification failed."
}

Write-Host "PHASE=REMOVE_OLD_EXTERNAL_COPIES"
foreach ($item in $Items) {
    if ((Get-Sha256 $item.Destination) -ne $item.Hash) {
        Fail "Destination changed before cleanup: $($item.Destination)"
    }
    Remove-Item -LiteralPath $item.Source -Force
}

foreach ($item in $Items) {
    if (Test-Path -LiteralPath $item.Source) {
        Fail "Old external copy remains: $($item.Source)"
    }
    if ((Get-Sha256 $item.Destination) -ne $item.Hash) {
        Fail "Final hash mismatch: $($item.Destination)"
    }
}

$audioAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioAfter -ne $ExpectedAudioCount -or $imageAfter -ne $ExpectedImageCount) {
    Fail "Runtime media counts changed."
}

$finalStatus = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($finalStatus.Count -ne 0) {
    Fail "Final worktree not clean."
}

$Lines = @(
    "STEP`t9F_FINISH_ORGANIZATION_SAFE",
    "CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "TRANSIENT_CLIPBOARD_REMOVED`t$removedTransient",
    "PREVIOUS_REMOTE_HEAD`t$ExpectedRemoteHead",
    "NEW_HEAD`t$newHead",
    "TRACKED_BEFORE`t$trackedBefore",
    "TRACKED_AFTER`t$trackedAfter",
    "MOVED_COUNT`t$($Items.Count)",
    "IGNORED_AUDIO_COUNT`t$audioAfter",
    "IGNORED_IMAGE_COUNT`t$imageAfter",
    "WORKTREE_CLEAN`tYES",
    "REMOTE_PUSH`tYES"
)
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=9F_FINISH_ORGANIZATION_SAFE"
Write-Host "TRANSIENT_CLIPBOARD_REMOVED=$removedTransient"
Write-Host "NEW_HEAD=$newHead"
Write-Host "TRACKED_BEFORE=$trackedBefore"
Write-Host "TRACKED_AFTER=$trackedAfter"
Write-Host "MOVED_COUNT=$($Items.Count)"
Write-Host "FILENAMES_CHANGED=NO"
Write-Host "FILE_CONTENT_CHANGED=NO"
Write-Host "IGNORED_AUDIO_COUNT=$audioAfter"
Write-Host "IGNORED_IMAGE_COUNT=$imageAfter"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "REMOTE_PUSH=YES"
Write-Host "OUTPUT=$ReportPath"
