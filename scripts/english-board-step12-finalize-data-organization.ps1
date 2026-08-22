$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step12_{0}.txt" -f $Timestamp)

$ExpectedHead = "c5814f1fc1f0783d5d196755c4dd7f14b7cc557c"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedTrackedCount = 52
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

function Get-RemoteMainHead {
    $lines = @(Invoke-GitNative -GitArgs @("ls-remote", "origin", "refs/heads/main") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($lines.Count -ne 1) {
        Fail "Unable to resolve exactly one remote main ref."
    }
    return (($lines[0] -split "\s+")[0]).Trim()
}

function Read-Utf8FileInfo([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
    } else {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    }
    return [pscustomobject]@{
        Text = $text
        HasBom = $hasBom
    }
}

function Write-Utf8FilePreservingBom([string]$Path, [string]$Text, [bool]$HasBom) {
    $encoding = New-Object System.Text.UTF8Encoding($HasBom)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Get-Newline([string]$Text) {
    if ($Text.Contains("`r`n")) { return "`r`n" }
    return "`n"
}

function Assert-ExactBlockOnce([string]$Path, [string[]]$OldLines) {
    $info = Read-Utf8FileInfo -Path $Path
    $newline = Get-Newline -Text $info.Text
    $oldText = $OldLines -join $newline
    $first = $info.Text.IndexOf($oldText, [System.StringComparison]::Ordinal)
    if ($first -lt 0) {
        Fail "Expected governance block not found: $Path"
    }
    $second = $info.Text.IndexOf($oldText, $first + $oldText.Length, [System.StringComparison]::Ordinal)
    if ($second -ge 0) {
        Fail "Expected governance block occurs more than once: $Path"
    }
}

function Replace-ExactBlockOnce([string]$Path, [string[]]$OldLines, [string[]]$NewLines) {
    $info = Read-Utf8FileInfo -Path $Path
    $newline = Get-Newline -Text $info.Text
    $oldText = $OldLines -join $newline
    $newText = $NewLines -join $newline
    $first = $info.Text.IndexOf($oldText, [System.StringComparison]::Ordinal)
    if ($first -lt 0) {
        Fail "Expected governance block not found during update: $Path"
    }
    $second = $info.Text.IndexOf($oldText, $first + $oldText.Length, [System.StringComparison]::Ordinal)
    if ($second -ge 0) {
        Fail "Expected governance block occurs more than once during update: $Path"
    }
    $updated = $info.Text.Remove($first, $oldText.Length).Insert($first, $newText)
    Write-Utf8FilePreservingBom -Path $Path -Text $updated -HasBom $info.HasBom
}

function Count-OldRootMentions([string[]]$TrackedPaths) {
    $needles = @('`CEFR.txt`', '`LEAP.txt`', '`SVL.txt`')
    $count = 0
    foreach ($relative in $TrackedPaths) {
        $full = Join-Path $script:Root $relative
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        try {
            $text = (Read-Utf8FileInfo -Path $full).Text
        } catch {
            continue
        }
        foreach ($needle in $needles) {
            $offset = 0
            while ($true) {
                $index = $text.IndexOf($needle, $offset, [System.StringComparison]::Ordinal)
                if ($index -lt 0) { break }
                $count++
                $offset = $index + $needle.Length
            }
        }
    }
    return $count
}

$Moves = @(
    [pscustomobject]@{ Source = "LEAP.txt"; Destination = "development/data/source/LEAP.txt" },
    [pscustomobject]@{ Source = "CEFR.txt"; Destination = "development/data/reference/CEFR.txt" },
    [pscustomobject]@{ Source = "SVL.txt"; Destination = "development/data/reference/SVL.txt" }
)

$AgentsOldHold = @(
    "## Current HOLD data",
    "",
    "Do not move or reinterpret these files until their generation, maintenance, and recovery relationships are proven and the source/data move gate passes:",
    "",
    "- `CEFR.txt`",
    "- `LEAP.txt`",
    "- `SVL.txt`",
    "",
    "The fact that a browser does not directly load a file is not evidence that the file is unimportant."
)

$AgentsNewData = @(
    "## Organized source/reference data",
    "",
    "The source/reference data cleanup has passed the required source/data move gate and is organized at:",
    "",
    "- `development/data/source/LEAP.txt` - manual-maintenance/source dataset corresponding record-for-record with `scripts/words-leap.js` when row order is ignored;",
    "- `development/data/reference/CEFR.txt` - CEFR vocabulary-level reference dataset;",
    "- `development/data/reference/SVL.txt` - SVL vocabulary-level reference dataset.",
    "",
    "These development datasets are not browser runtime inputs. The runtime continues to use `scripts/words-leap.js` and `scripts/words-teppeki.js`."
)

$AgentsOldScope = @(
    "The following remain protected or HOLD and are not part of this consolidation:",
    "",
    "- `English_Reading.html`",
    "- `scripts/`",
    "- `icon/`",
    "- `audio/`",
    "- `image/`",
    "- `CEFR.txt`",
    "- `LEAP.txt`",
    "- `SVL.txt`",
    "",
    "If the HOLD datasets are later approved for movement, organize them under `development/data/` according to proven roles, not under documents."
)

$AgentsNewScope = @(
    "The following runtime paths remain protected and unchanged by this cleanup:",
    "",
    "- `English_Reading.html`",
    "- `scripts/`",
    "- `icon/`",
    "- `audio/`",
    "- `image/`",
    "",
    "The approved source/reference data are organized at:",
    "",
    "- `development/data/source/LEAP.txt`",
    "- `development/data/reference/CEFR.txt`",
    "- `development/data/reference/SVL.txt`",
    "",
    "These moves preserve filenames and file contents; only repository paths change."
)

$PolicyOldData = @(
    "The following source/reference data remain HOLD until their generation, maintenance, and recovery relationships are confirmed and the required second-opinion gate passes:",
    "",
    "- `CEFR.txt`",
    "- `LEAP.txt`",
    "- `SVL.txt`",
    "",
    "If approved later, these datasets should be organized under `development/data/` according to their proven roles rather than placed under developer documents."
)

$PolicyNewData = @(
    "The source/reference data classification and move gate have been satisfied for the following final locations:",
    "",
    "- `development/data/source/LEAP.txt` - manual-maintenance/source dataset corresponding record-for-record with `scripts/words-leap.js` when row order is ignored;",
    "- `development/data/reference/CEFR.txt` - CEFR vocabulary-level reference dataset;",
    "- `development/data/reference/SVL.txt` - SVL vocabulary-level reference dataset.",
    "",
    "These files are development source/reference data, not browser runtime inputs. Their filenames and contents are preserved; only repository paths change."
)

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    Fail "Git repository is missing: $Root"
}

$branch = Get-GitText -GitArgs @("symbolic-ref", "--short", "HEAD")
if ($branch -ne "main") {
    Fail "Unexpected branch: $branch"
}

$head = Get-GitText -GitArgs @("rev-parse", "HEAD")
if ($head -ne $ExpectedHead) {
    Fail "Unexpected local HEAD: expected=$ExpectedHead actual=$head"
}

$origin = Get-GitText -GitArgs @("remote", "get-url", "origin")
if ($origin -ne $ExpectedOrigin) {
    Fail "Unexpected origin: $origin"
}

$statusBefore = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($statusBefore.Count -ne 0) {
    Fail "Worktree must be clean before final data organization.`n$($statusBefore -join "`n")"
}

$remoteHead = Get-RemoteMainHead
if ($remoteHead -ne $ExpectedHead) {
    Fail "Unexpected remote main HEAD: expected=$ExpectedHead actual=$remoteHead"
}

$trackedBefore = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedBefore -ne $ExpectedTrackedCount) {
    Fail "Unexpected tracked file count: expected=$ExpectedTrackedCount actual=$trackedBefore"
}

$audioBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageBefore = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioBefore -ne $ExpectedAudioCount) { Fail "Unexpected audio count: expected=$ExpectedAudioCount actual=$audioBefore" }
if ($imageBefore -ne $ExpectedImageCount) { Fail "Unexpected image count: expected=$ExpectedImageCount actual=$imageBefore" }

$sourceHashes = @{}
foreach ($move in $Moves) {
    $sourcePath = Join-Path $Root $move.Source
    $destinationPath = Join-Path $Root $move.Destination
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        Fail "Source data file is missing: $($move.Source)"
    }
    if (Test-Path -LiteralPath $destinationPath) {
        Fail "Destination already exists: $($move.Destination)"
    }
    $sourceHashes[$move.Source] = Get-Sha256 -Path $sourcePath
}

$agentsPath = Join-Path $Root "AGENTS.md"
$policyPath = Join-Path $Root "DEVELOPMENT_POLICY.md"
Assert-ExactBlockOnce -Path $agentsPath -OldLines $AgentsOldHold
Assert-ExactBlockOnce -Path $agentsPath -OldLines $AgentsOldScope
Assert-ExactBlockOnce -Path $policyPath -OldLines $PolicyOldData

Write-Host "PHASE=MOVE_DATA_FILES"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "development\data\source") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "development\data\reference") | Out-Null
foreach ($move in $Moves) {
    Invoke-GitNative -GitArgs @("mv", "--", $move.Source, $move.Destination) | Out-Null
}

Write-Host "PHASE=UPDATE_GOVERNANCE_PATHS"
Replace-ExactBlockOnce -Path $agentsPath -OldLines $AgentsOldHold -NewLines $AgentsNewData
Replace-ExactBlockOnce -Path $agentsPath -OldLines $AgentsOldScope -NewLines $AgentsNewScope
Replace-ExactBlockOnce -Path $policyPath -OldLines $PolicyOldData -NewLines $PolicyNewData
Invoke-GitNative -GitArgs @("add", "--", "AGENTS.md", "DEVELOPMENT_POLICY.md") | Out-Null

Write-Host "PHASE=VERIFY_STAGED_CHANGE"
foreach ($move in $Moves) {
    $destinationPath = Join-Path $Root $move.Destination
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Leaf)) {
        Fail "Moved data file is missing: $($move.Destination)"
    }
    if (Test-Path -LiteralPath (Join-Path $Root $move.Source)) {
        Fail "Old source path remains after move: $($move.Source)"
    }
    $afterHash = Get-Sha256 -Path $destinationPath
    if ($afterHash -ne $sourceHashes[$move.Source]) {
        Fail "SHA-256 changed during move: $($move.Source) -> $($move.Destination)"
    }
}

$runtimeDiff = @(Invoke-GitNative -GitArgs @("diff", "--cached", "--name-only", "--", "English_Reading.html", "scripts/", "icon/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($runtimeDiff.Count -ne 0) {
    Fail "Runtime source changed unexpectedly.`n$($runtimeDiff -join "`n")"
}

$expectedChanged = @(
    "AGENTS.md",
    "DEVELOPMENT_POLICY.md",
    "development/data/reference/CEFR.txt",
    "development/data/reference/SVL.txt",
    "development/data/source/LEAP.txt"
) | Sort-Object
$actualChanged = @(Invoke-GitNative -GitArgs @("diff", "--cached", "--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($expectedChanged -join "`n") -ne ($actualChanged -join "`n")) {
    Fail "Unexpected staged path set.`nEXPECTED:`n$($expectedChanged -join "`n")`nACTUAL:`n$($actualChanged -join "`n")"
}

$nameStatus = @(Invoke-GitNative -GitArgs @("diff", "--cached", "--name-status", "--find-renames=100%") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$renameExpectations = @(
    "LEAP.txt`tdevelopment/data/source/LEAP.txt",
    "CEFR.txt`tdevelopment/data/reference/CEFR.txt",
    "SVL.txt`tdevelopment/data/reference/SVL.txt"
)
foreach ($expectation in $renameExpectations) {
    $found = $false
    foreach ($line in $nameStatus) {
        if ($line -eq ("R100`t" + $expectation)) {
            $found = $true
            break
        }
    }
    if (-not $found) {
        Fail "Expected R100 rename not found: $expectation`n$($nameStatus -join "`n")"
    }
}
foreach ($doc in @("AGENTS.md", "DEVELOPMENT_POLICY.md")) {
    if ($nameStatus -notcontains ("M`t" + $doc)) {
        Fail "Expected governance modification not found: $doc`n$($nameStatus -join "`n")"
    }
}
if ($nameStatus.Count -ne 5) {
    Fail "Unexpected staged status count: expected=5 actual=$($nameStatus.Count)`n$($nameStatus -join "`n")"
}

$trackedCurrent = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$oldRootMentionCount = Count-OldRootMentions -TrackedPaths $trackedCurrent
if ($oldRootMentionCount -ne 0) {
    Fail "Old root-path governance mentions remain after update: count=$oldRootMentionCount"
}

$unstaged = @(Invoke-GitNative -GitArgs @("diff", "--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($unstaged.Count -ne 0) {
    Fail "Unexpected unstaged changes before commit.`n$($unstaged -join "`n")"
}

Write-Host "PHASE=REMOTE_RACE_GUARD"
$remoteBeforeCommit = Get-RemoteMainHead
if ($remoteBeforeCommit -ne $ExpectedHead) {
    Fail "Remote main changed before commit: expected=$ExpectedHead actual=$remoteBeforeCommit"
}

Write-Host "PHASE=COMMIT"
Invoke-GitNative -GitArgs @("commit", "-m", "Finalize English Board source and reference data organization") | Out-Null
$newHead = Get-GitText -GitArgs @("rev-parse", "HEAD")

$trackedAfter = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedAfter -ne $ExpectedTrackedCount) {
    Fail "Tracked file count changed unexpectedly: expected=$ExpectedTrackedCount actual=$trackedAfter"
}

$postCommitStatus = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($postCommitStatus.Count -ne 0) {
    Fail "Worktree is not clean after commit.`n$($postCommitStatus -join "`n")"
}

$runtimeCommitDiff = @(Invoke-GitNative -GitArgs @("diff", "--name-only", $ExpectedHead, $newHead, "--", "English_Reading.html", "scripts/", "icon/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($runtimeCommitDiff.Count -ne 0) {
    Fail "Committed runtime source diff detected.`n$($runtimeCommitDiff -join "`n")"
}

$postCommitNameStatus = @(Invoke-GitNative -GitArgs @("diff", "--name-status", "--find-renames=100%", $ExpectedHead, $newHead) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
foreach ($expectation in $renameExpectations) {
    if ($postCommitNameStatus -notcontains ("R100`t" + $expectation)) {
        Fail "Committed R100 rename missing: $expectation`n$($postCommitNameStatus -join "`n")"
    }
}
foreach ($doc in @("AGENTS.md", "DEVELOPMENT_POLICY.md")) {
    if ($postCommitNameStatus -notcontains ("M`t" + $doc)) {
        Fail "Committed governance modification missing: $doc`n$($postCommitNameStatus -join "`n")"
    }
}
if ($postCommitNameStatus.Count -ne 5) {
    Fail "Unexpected committed status count: expected=5 actual=$($postCommitNameStatus.Count)`n$($postCommitNameStatus -join "`n")"
}

foreach ($move in $Moves) {
    $destinationPath = Join-Path $Root $move.Destination
    if ((Get-Sha256 -Path $destinationPath) -ne $sourceHashes[$move.Source]) {
        Fail "Post-commit SHA-256 mismatch: $($move.Destination)"
    }
}

$audioAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageAfter = @(Invoke-GitNative -GitArgs @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioAfter -ne $ExpectedAudioCount) { Fail "Audio count changed: expected=$ExpectedAudioCount actual=$audioAfter" }
if ($imageAfter -ne $ExpectedImageCount) { Fail "Image count changed: expected=$ExpectedImageCount actual=$imageAfter" }

Write-Host "PHASE=PUSH"
$remoteBeforePush = Get-RemoteMainHead
if ($remoteBeforePush -ne $ExpectedHead) {
    Fail "Remote main changed before push: expected=$ExpectedHead actual=$remoteBeforePush"
}
Invoke-GitNative -GitArgs @("push", "--quiet", "origin", "main") | Out-Null
$remoteAfterPush = Get-RemoteMainHead
if ($remoteAfterPush -ne $newHead) {
    Fail "Remote verification failed after push: local=$newHead remote=$remoteAfterPush"
}

$finalStatus = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($finalStatus.Count -ne 0) {
    Fail "Final worktree is not clean.`n$($finalStatus -join "`n")"
}

$reportLines = @(
    "STEP`t12_FINALIZE_DATA_ORGANIZATION",
    "CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "PREVIOUS_HEAD`t$ExpectedHead",
    "NEW_HEAD`t$newHead",
    "TRACKED_COUNT`t$trackedAfter",
    "DATA_MOVE_COUNT`t$($Moves.Count)",
    "DATA_RENAMES_R100`tYES",
    "RUNTIME_SOURCE_DIFF`tNO",
    "FILENAMES_CHANGED`tNO",
    "FILE_CONTENT_CHANGED`tNO",
    "LEAP_SHA256`t$($sourceHashes['LEAP.txt'])",
    "CEFR_SHA256`t$($sourceHashes['CEFR.txt'])",
    "SVL_SHA256`t$($sourceHashes['SVL.txt'])",
    "IGNORED_AUDIO_COUNT`t$audioAfter",
    "IGNORED_IMAGE_COUNT`t$imageAfter",
    "WORKTREE_CLEAN`tYES",
    "REMOTE_PUSH`tYES",
    "REMOTE_HEAD_MATCH`tYES"
)
$reportLines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=12_FINALIZE_DATA_ORGANIZATION"
Write-Host "NEW_HEAD=$newHead"
Write-Host "DATA_MOVE_COUNT=$($Moves.Count)"
Write-Host "DATA_RENAMES_R100=YES"
Write-Host "RUNTIME_SOURCE_DIFF=NO"
Write-Host "FILENAMES_CHANGED=NO"
Write-Host "FILE_CONTENT_CHANGED=NO"
Write-Host "IGNORED_AUDIO_COUNT=$audioAfter"
Write-Host "IGNORED_IMAGE_COUNT=$imageAfter"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "REMOTE_PUSH=YES"
Write-Host "REMOTE_HEAD_MATCH=YES"
Write-Host "OUTPUT=$ReportPath"
