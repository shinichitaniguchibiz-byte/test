$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step9e_{0}.txt" -f $Timestamp)
$ClipboardHash = "9FF7079F2EF4EB5DEF1083684249A98B924146D7C05E0F3632822473DF956F57"
$ExpectedRemoteHead = "4b5091a912af4cbf2828fb8d180d9fb2ddc383b6"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedTrackedBefore = 47
$ExpectedTrackedAfter = 52
$ExpectedAudioCount = 3747
$ExpectedImageCount = 6480

function Fail([string]$Message) { throw $Message }
function Sha([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }

$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $GitCommand) { $GitCommand = Get-Command git -ErrorAction Stop }
$GitExe = $GitCommand.Source
if ([string]::IsNullOrWhiteSpace($GitExe)) { Fail "Unable to resolve native Git executable." }

function Git([string[]]$Args) {
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $out = & $script:GitExe -C $script:Root @Args 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    $lines = @($out | ForEach-Object { $_.ToString() })
    if ($code -ne 0) { Fail "git failed ($code): git $($Args -join ' ')`n$($lines -join "`n")" }
    return $lines
}
function GitText([string[]]$Args) { ((Git $Args) -join "`n").Trim() }

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) { Fail "Git repository missing." }
if ((GitText @("symbolic-ref","--short","HEAD")) -ne "main") { Fail "Unexpected branch." }

$trackedDirty = @(Git @("status","--porcelain","--untracked-files=no") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($trackedDirty.Count -ne 0) { Fail "Tracked or staged changes exist. Refusing organization." }

Write-Host "PHASE=REMOVE_TRANSIENT_CLIPBOARD_TEXT"
$removedTransient = 0
$rootClipboard = Join-Path $Root "Clipboard Text.txt"
if (Test-Path -LiteralPath $rootClipboard -PathType Leaf) {
    if ((Sha $rootClipboard) -ne $ClipboardHash) { Fail "Unexpected Clipboard Text.txt content in repository root." }
    Remove-Item -LiteralPath $rootClipboard -Force
    $removedTransient++
}
$archiveRoot = Join-Path $DevRoot "clipboard-archive"
if (Test-Path -LiteralPath $archiveRoot -PathType Container) {
    $copies = @(Get-ChildItem -LiteralPath $archiveRoot -Filter "Clipboard Text.txt" -File -Recurse -ErrorAction SilentlyContinue)
    foreach ($copy in $copies) {
        if ((Sha $copy.FullName) -eq $ClipboardHash) {
            Remove-Item -LiteralPath $copy.FullName -Force
            $removedTransient++
        }
    }
    $dirs = @(Get-ChildItem -LiteralPath $archiveRoot -Directory -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)
    foreach ($d in $dirs) {
        if (-not (Get-ChildItem -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
            Remove-Item -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue
        }
    }
    if ((Test-Path -LiteralPath $archiveRoot) -and -not (Get-ChildItem -LiteralPath $archiveRoot -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Remove-Item -LiteralPath $archiveRoot -Force -ErrorAction SilentlyContinue
    }
}
$untracked = @(Git @("ls-files","--others","--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($untracked.Count -ne 0) { Fail "Unexpected untracked files remain: $($untracked -join ', ')" }

$origin = GitText @("remote","get-url","origin")
if ($origin -ne $ExpectedOrigin) { Fail "Unexpected origin: $origin" }

Write-Host "PHASE=SYNC_REMOTE"
Git @("fetch","--quiet","origin","main") | Out-Null
$remoteHead = GitText @("rev-parse","origin/main")
if ($remoteHead -ne $ExpectedRemoteHead) { Fail "Unexpected remote main HEAD: expected=$ExpectedRemoteHead actual=$remoteHead" }
$localHead = GitText @("rev-parse","HEAD")
$mergeBase = GitText @("merge-base","HEAD","origin/main")
if ($mergeBase -ne $localHead) { Fail "Local HEAD is not an ancestor of origin/main." }
Git @("merge","--ff-only","origin/main") | Out-Null
if ((GitText @("rev-parse","HEAD")) -ne $ExpectedRemoteHead) { Fail "Local sync failed." }

$trackedBefore = @(Git @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedBefore -ne $ExpectedTrackedBefore) { Fail "Unexpected tracked count before move: $trackedBefore" }
$audioBefore = @(Git @("ls-files","--others","--ignored","--exclude-standard","--","audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageBefore = @(Git @("ls-files","--others","--ignored","--exclude-standard","--","image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioBefore -ne $ExpectedAudioCount) { Fail "Unexpected audio count: $audioBefore" }
if ($imageBefore -ne $ExpectedImageCount) { Fail "Unexpected image count: $imageBefore" }

$Items = @(
    [pscustomobject]@{ Source=(Join-Path $DevRoot "samples\20260517095055.txt"); Destination=(Join-Path $Root "samples\reading\20260517095055.txt"); Relative="samples/reading/20260517095055.txt"; Hash="D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79" },
    [pscustomobject]@{ Source=(Join-Path $DevRoot "design\icon-check.html"); Destination=(Join-Path $Root "tools\icon\icon-check.html"); Relative="tools/icon/icon-check.html"; Hash="6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A" },
    [pscustomobject]@{ Source=(Join-Path $DevRoot "design\icon-concepts.html"); Destination=(Join-Path $Root "docs\design\icon-concepts.html"); Relative="docs/design/icon-concepts.html"; Hash="DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939" },
    [pscustomobject]@{ Source=(Join-Path $DevRoot "legacy-tools\make_zip.ps1"); Destination=(Join-Path $Root "tools\legacy\make_zip.ps1"); Relative="tools/legacy/make_zip.ps1"; Hash="E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53" },
    [pscustomobject]@{ Source=(Join-Path $DevRoot "legacy-tools\make_zip_all.ps1"); Destination=(Join-Path $Root "tools\legacy\make_zip_all.ps1"); Relative="tools/legacy/make_zip_all.ps1"; Hash="F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356" }
)

Write-Host "PHASE=VERIFY_APPROVED_FILES"
foreach ($i in $Items) {
    if (-not (Test-Path -LiteralPath $i.Source -PathType Leaf)) { Fail "Missing source: $($i.Source)" }
    if ((Sha $i.Source) -ne $i.Hash) { Fail "Source hash mismatch: $($i.Source)" }
    if (Test-Path -LiteralPath $i.Destination) { Fail "Destination exists: $($i.Destination)" }
}

Write-Host "PHASE=COPY_VERIFY_STAGE_COMMIT"
foreach ($i in $Items) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $i.Destination) | Out-Null
    Copy-Item -LiteralPath $i.Source -Destination $i.Destination
    if ((Sha $i.Destination) -ne $i.Hash) { Fail "Destination hash mismatch: $($i.Destination)" }
}
$expected = @($Items.Relative | Sort-Object)
$actual = @(Git @("ls-files","--others","--exclude-standard") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($expected -join "`n") -ne ($actual -join "`n")) { Fail "Unexpected untracked set after copy." }
foreach ($i in $Items) { Git @("add","--",$i.Relative) | Out-Null }
$staged = @(Git @("diff","--cached","--name-only") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
if (($expected -join "`n") -ne ($staged -join "`n")) { Fail "Unexpected staged set." }
Git @("commit","-m","Organize English Board non-runtime artifacts") | Out-Null
$newHead = GitText @("rev-parse","HEAD")
$trackedAfter = @(Git @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($trackedAfter -ne $ExpectedTrackedAfter) { Fail "Unexpected tracked count after commit: $trackedAfter" }
if (@(Git @("status","--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) { Fail "Worktree not clean after commit." }

Write-Host "PHASE=PUSH"
Git @("fetch","--quiet","origin","main") | Out-Null
if ((GitText @("rev-parse","origin/main")) -ne $ExpectedRemoteHead) { Fail "Remote changed during operation." }
Git @("push","--quiet","origin","main") | Out-Null
Git @("fetch","--quiet","origin","main") | Out-Null
if ((GitText @("rev-parse","origin/main")) -ne $newHead) { Fail "Remote verification failed." }

Write-Host "PHASE=REMOVE_OLD_EXTERNAL_COPIES"
foreach ($i in $Items) {
    if ((Sha $i.Destination) -ne $i.Hash) { Fail "Destination changed before cleanup: $($i.Destination)" }
    Remove-Item -LiteralPath $i.Source -Force
}
foreach ($i in $Items) {
    if (Test-Path -LiteralPath $i.Source) { Fail "Old external copy remains: $($i.Source)" }
    if ((Sha $i.Destination) -ne $i.Hash) { Fail "Final hash mismatch: $($i.Destination)" }
}
$audioAfter = @(Git @("ls-files","--others","--ignored","--exclude-standard","--","audio/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
$imageAfter = @(Git @("ls-files","--others","--ignored","--exclude-standard","--","image/") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($audioAfter -ne $ExpectedAudioCount -or $imageAfter -ne $ExpectedImageCount) { Fail "Runtime media counts changed." }
if (@(Git @("status","--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne 0) { Fail "Final worktree not clean." }

$Lines = @(
    "STEP`t9E_DELETE_TRANSIENT_AND_FINISH_ORGANIZATION",
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
Write-Host "STEP=9E_DELETE_TRANSIENT_AND_FINISH_ORGANIZATION"
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
