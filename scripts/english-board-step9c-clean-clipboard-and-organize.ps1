$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step9c_{0}.txt" -f $Timestamp)
$ClipboardArchiveDir = Join-Path $DevRoot ("clipboard-archive\{0}" -f $Timestamp)
$ClipboardSource = Join-Path $Root "Clipboard Text.txt"
$ClipboardDestination = Join-Path $ClipboardArchiveDir "Clipboard Text.txt"
$ExpectedClipboardHash = "9FF7079F2EF4EB5DEF1083684249A98B924146D7C05E0F3632822473DF956F57"

$ExpectedRemoteHead = "4b5091a912af4cbf2828fb8d180d9fb2ddc383b6"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedTrackedBefore = 47
$ExpectedTrackedAfter = 52
$ExpectedAudioCount = 3747
$ExpectedImageCount = 6480

function Fail([string]$Message) {
    throw $Message
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
    $output = & $script:GitExe -C $script:Root @GitArgs 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "git failed ($code): git $($GitArgs -join ' ')`n$($output -join "`n")"
    }
    return @($output)
}

function Get-GitText([string[]]$GitArgs) {
    return ((Invoke-GitNative $GitArgs) -join "`n").Trim()
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    Fail "Git repository is missing: $Root"
}

$Branch = Get-GitText @("symbolic-ref", "--short", "HEAD")
if ($Branch -ne "main") { Fail "Unexpected branch: $Branch" }

$TrackedDirty = @(Invoke-GitNative @("status", "--porcelain", "--untracked-files=no") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($TrackedDirty.Count -ne 0) {
    Fail "Tracked or staged changes exist. Refusing cleanup."
}

$UntrackedBefore = @(Invoke-GitNative @("ls-files", "--others", "--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($UntrackedBefore.Count -ne 1 -or $UntrackedBefore[0] -ne "Clipboard Text.txt") {
    Fail "Unexpected untracked file set before cleanup: $($UntrackedBefore -join ', ')"
}

if (-not (Test-Path -LiteralPath $ClipboardSource -PathType Leaf)) {
    Fail "Clipboard Text.txt is not present where expected."
}
$ClipboardHash = Get-FileSha256 $ClipboardSource
if ($ClipboardHash -ne $ExpectedClipboardHash) {
    Fail "Clipboard Text.txt hash mismatch. Refusing automatic relocation."
}

Write-Host "PHASE=RELOCATE_CLIPBOARD_ARTIFACT"
New-Item -ItemType Directory -Force -Path $ClipboardArchiveDir | Out-Null
Copy-Item -LiteralPath $ClipboardSource -Destination $ClipboardDestination
if ((Get-FileSha256 $ClipboardDestination) -ne $ExpectedClipboardHash) {
    Fail "Clipboard archive hash mismatch."
}
Remove-Item -LiteralPath $ClipboardSource -Force
if (Test-Path -LiteralPath $ClipboardSource) {
    Fail "Clipboard source still exists after relocation."
}
if ((Get-FileSha256 $ClipboardDestination) -ne $ExpectedClipboardHash) {
    Fail "Clipboard archive changed after relocation."
}

$StatusAfterClipboard = @(Invoke-GitNative @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($StatusAfterClipboard.Count -ne 0) {
    Fail "Worktree is not clean after relocating Clipboard Text.txt."
}

$Origin = Get-GitText @("remote", "get-url", "origin")
if ($Origin -ne $ExpectedOrigin) { Fail "Unexpected origin: $Origin" }

Write-Host "PHASE=SYNC_REMOTE"
Invoke-GitNative @("fetch", "origin", "main") | Out-Null
$RemoteHead = Get-GitText @("rev-parse", "origin/main")
if ($RemoteHead -ne $ExpectedRemoteHead) {
    Fail "Unexpected remote main HEAD: expected=$ExpectedRemoteHead actual=$RemoteHead"
}

& $GitExe -C $Root merge-base --is-ancestor HEAD origin/main 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "Local HEAD is not an ancestor of origin/main. Refusing non-fast-forward synchronization."
}
Invoke-GitNative @("merge", "--ff-only", "origin/main") | Out-Null
$HeadAfterSync = Get-GitText @("rev-parse", "HEAD")
if ($HeadAfterSync -ne $ExpectedRemoteHead) {
    Fail "Local HEAD did not synchronize to expected remote HEAD."
}

$TrackedBefore = @(Invoke-GitNative @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($TrackedBefore -ne $ExpectedTrackedBefore) {
    Fail "Unexpected tracked count before move: expected=$ExpectedTrackedBefore actual=$TrackedBefore"
}

$IgnoredAudioBefore = @(Invoke-GitNative @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$IgnoredImageBefore = @(Invoke-GitNative @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($IgnoredAudioBefore -ne $ExpectedAudioCount) { Fail "Unexpected ignored audio count: $IgnoredAudioBefore" }
if ($IgnoredImageBefore -ne $ExpectedImageCount) { Fail "Unexpected ignored image count: $IgnoredImageBefore" }

$Items = @(
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "samples\20260517095055.txt")
        Destination = (Join-Path $Root "samples\reading\20260517095055.txt")
        Relative = "samples/reading/20260517095055.txt"
        Sha256 = "D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "design\icon-check.html")
        Destination = (Join-Path $Root "tools\icon\icon-check.html")
        Relative = "tools/icon/icon-check.html"
        Sha256 = "6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "design\icon-concepts.html")
        Destination = (Join-Path $Root "docs\design\icon-concepts.html")
        Relative = "docs/design/icon-concepts.html"
        Sha256 = "DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "legacy-tools\make_zip.ps1")
        Destination = (Join-Path $Root "tools\legacy\make_zip.ps1")
        Relative = "tools/legacy/make_zip.ps1"
        Sha256 = "E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53"
    },
    [pscustomobject]@{
        Source = (Join-Path $DevRoot "legacy-tools\make_zip_all.ps1")
        Destination = (Join-Path $Root "tools\legacy\make_zip_all.ps1")
        Relative = "tools/legacy/make_zip_all.ps1"
        Sha256 = "F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356"
    }
)

Write-Host "PHASE=VERIFY_APPROVED_FILES"
foreach ($item in $Items) {
    if (-not (Test-Path -LiteralPath $item.Source -PathType Leaf)) {
        Fail "Approved source file is missing: $($item.Source)"
    }
    if ((Get-FileSha256 $item.Source) -ne $item.Sha256) {
        Fail "Source hash mismatch: $($item.Source)"
    }
    if (Test-Path -LiteralPath $item.Destination) {
        Fail "Destination already exists: $($item.Destination)"
    }
}

Write-Host "PHASE=COPY_AND_VERIFY"
foreach ($item in $Items) {
    $destDir = Split-Path -Parent $item.Destination
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Copy-Item -LiteralPath $item.Source -Destination $item.Destination
    if ((Get-FileSha256 $item.Destination) -ne $item.Sha256) {
        Fail "Destination hash mismatch after copy: $($item.Destination)"
    }
}

$ExpectedUntracked = @($Items.Relative | Sort-Object)
$ActualUntracked = @(Invoke-GitNative @("ls-files", "--others", "--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($ExpectedUntracked -join "`n") -ne ($ActualUntracked -join "`n")) {
    Fail "Unexpected untracked files after copy.`nExpected:`n$($ExpectedUntracked -join "`n")`nActual:`n$($ActualUntracked -join "`n")"
}

Write-Host "PHASE=STAGE_AND_COMMIT"
foreach ($item in $Items) {
    Invoke-GitNative @("add", "--", $item.Relative) | Out-Null
}
$Staged = @(Invoke-GitNative @("diff", "--cached", "--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($ExpectedUntracked -join "`n") -ne ($Staged -join "`n")) {
    Fail "Unexpected staged file set."
}

Invoke-GitNative @("commit", "-m", "Organize English Board non-runtime artifacts") | Out-Null
$NewHead = Get-GitText @("rev-parse", "HEAD")

$TrackedAfter = @(Invoke-GitNative @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($TrackedAfter -ne $ExpectedTrackedAfter) {
    Fail "Unexpected tracked count after commit: expected=$ExpectedTrackedAfter actual=$TrackedAfter"
}
$StatusAfterCommit = @(Invoke-GitNative @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($StatusAfterCommit.Count -ne 0) {
    Fail "Worktree is not clean after commit."
}

$IgnoredAudioAfter = @(Invoke-GitNative @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$IgnoredImageAfter = @(Invoke-GitNative @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($IgnoredAudioAfter -ne $ExpectedAudioCount) { Fail "Audio count changed unexpectedly." }
if ($IgnoredImageAfter -ne $ExpectedImageCount) { Fail "Image count changed unexpectedly." }

$RemoteBeforePush = (& $GitExe -C $Root ls-remote origin refs/heads/main 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { Fail "Unable to read remote main before push." }
$RemoteBeforeSha = if ($RemoteBeforePush) { ($RemoteBeforePush -split "\s+")[0] } else { "" }
if ($RemoteBeforeSha -ne $ExpectedRemoteHead) {
    Fail "Remote main changed during operation: expected=$ExpectedRemoteHead actual=$RemoteBeforeSha"
}

Write-Host "PHASE=PUSH"
Invoke-GitNative @("push", "origin", "main") | Out-Null
$RemoteAfterPush = (& $GitExe -C $Root ls-remote origin refs/heads/main 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($RemoteAfterPush)) {
    Fail "Unable to verify remote main after push."
}
$RemoteAfterSha = ($RemoteAfterPush -split "\s+")[0]
if ($RemoteAfterSha -ne $NewHead) {
    Fail "Remote HEAD mismatch after push: local=$NewHead remote=$RemoteAfterSha"
}

Write-Host "PHASE=REMOVE_EXTERNAL_COPIES"
foreach ($item in $Items) {
    if ((Get-FileSha256 $item.Destination) -ne $item.Sha256) {
        Fail "Destination hash changed before external cleanup: $($item.Destination)"
    }
    Remove-Item -LiteralPath $item.Source -Force
}
foreach ($item in $Items) {
    if (Test-Path -LiteralPath $item.Source) {
        Fail "External source still exists after cleanup: $($item.Source)"
    }
    if ((Get-FileSha256 $item.Destination) -ne $item.Sha256) {
        Fail "Final destination hash mismatch: $($item.Destination)"
    }
}

$FinalStatus = @(Invoke-GitNative @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($FinalStatus.Count -ne 0) { Fail "Final worktree is not clean." }

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t9C_CLEAN_CLIPBOARD_AND_ORGANIZE")
$Lines.Add("CLIPBOARD_ARCHIVE`t$ClipboardDestination")
$Lines.Add("CLIPBOARD_SHA256`t$ExpectedClipboardHash")
$Lines.Add("PREVIOUS_REMOTE_HEAD`t$ExpectedRemoteHead")
$Lines.Add("NEW_HEAD`t$NewHead")
$Lines.Add("TRACKED_BEFORE`t$TrackedBefore")
$Lines.Add("TRACKED_AFTER`t$TrackedAfter")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioAfter")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageAfter")
$Lines.Add("WORKTREE_CLEAN`tYES")
$Lines.Add("REMOTE_PUSH`tYES")
$Lines.Add("EXTERNAL_COPIES_REMOVED`tYES")
foreach ($item in $Items) {
    $Lines.Add("MOVED`t$($item.Relative)`t$($item.Sha256)")
}
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=9C_CLEAN_CLIPBOARD_AND_ORGANIZE"
Write-Host "CLIPBOARD_RELOCATED=YES"
Write-Host "CLIPBOARD_ARCHIVE=$ClipboardDestination"
Write-Host "PREVIOUS_REMOTE_HEAD=$ExpectedRemoteHead"
Write-Host "NEW_HEAD=$NewHead"
Write-Host "TRACKED_BEFORE=$TrackedBefore"
Write-Host "TRACKED_AFTER=$TrackedAfter"
Write-Host "MOVED_COUNT=$($Items.Count)"
Write-Host "FILENAMES_CHANGED=NO"
Write-Host "FILE_CONTENT_CHANGED=NO"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioAfter"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageAfter"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "REMOTE_PUSH=YES"
Write-Host "EXTERNAL_COPIES_REMOVED=YES"
Write-Host "OUTPUT=$ReportPath"
