$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_stage_encoding_safe_{0}.txt" -f $Timestamp)

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "Git repository is not initialized: $Root"
}

$Branch = (git symbolic-ref --short HEAD 2>$null).Trim()
if ($Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

$PreStaged = @(git diff --cached --name-only)
if ($PreStaged.Count -ne 0) {
    throw "Unexpected staged files already exist: $($PreStaged.Count)"
}

$Moves = @(
    @{ Source = "20260517095055.txt"; Sha256 = "D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79"; DestDir = (Join-Path $DevRoot "samples") },
    @{ Source = "icon-check.html"; Sha256 = "6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A"; DestDir = (Join-Path $DevRoot "design") },
    @{ Source = "icon-concepts.html"; Sha256 = "DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939"; DestDir = (Join-Path $DevRoot "design") },
    @{ Source = "make_zip.ps1"; Sha256 = "E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53"; DestDir = (Join-Path $DevRoot "legacy-tools") },
    @{ Source = "make_zip_all.ps1"; Sha256 = "F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356"; DestDir = (Join-Path $DevRoot "legacy-tools") }
)

$MoveStates = New-Object System.Collections.Generic.List[string]
foreach ($Move in $Moves) {
    $SourcePath = Join-Path $Root $Move.Source
    $DestPath = Join-Path $Move.DestDir $Move.Source
    $SourceExists = Test-Path -LiteralPath $SourcePath -PathType Leaf
    $DestExists = Test-Path -LiteralPath $DestPath -PathType Leaf

    if ($SourceExists -and $DestExists) {
        throw "Hold file exists in both source and destination: $($Move.Source)"
    }
    if (-not $SourceExists -and -not $DestExists) {
        throw "Hold file missing from both source and destination: $($Move.Source)"
    }

    if ($SourceExists) {
        $Hash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash
        if ($Hash -ne $Move.Sha256) {
            throw "Hold source hash mismatch: $($Move.Source)"
        }
        New-Item -ItemType Directory -Force -Path $Move.DestDir | Out-Null
        Move-Item -LiteralPath $SourcePath -Destination $DestPath
        $MoveStates.Add("MOVED_NOW`t$($Move.Source)`t$DestPath")
    }
    else {
        $Hash = (Get-FileHash -LiteralPath $DestPath -Algorithm SHA256).Hash
        if ($Hash -ne $Move.Sha256) {
            throw "Hold destination hash mismatch: $($Move.Source)"
        }
        $MoveStates.Add("ALREADY_MOVED`t$($Move.Source)`t$DestPath")
    }
}

$ExpectedAscii = @(
    ".gitignore",
    "CEFR.txt",
    "English_Reading.html",
    "LEAP.txt",
    "SVL.txt",
    "icon/add.svg",
    "icon/auto_play.svg",
    "icon/autoplay_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.svg",
    "icon/autoplay_off.svg",
    "icon/autoplay_on.svg",
    "icon/content_paste.svg",
    "icon/download.svg",
    "icon/folder_open.svg",
    "icon/play.svg",
    "icon/play_2.svg",
    "icon/play_arrow.svg",
    "icon/read_aloud.svg",
    "icon/remove.svg",
    "icon/replay.svg",
    "icon/search.svg",
    "icon/search_off.svg",
    "icon/search_on.svg",
    "icon/settings.svg",
    "icon/settings_off.svg",
    "icon/settings_on.svg",
    "icon/stop_circle.svg",
    "icon/text_to_speech.svg",
    "icon/toggle_off.svg",
    "icon/toggle_on.svg",
    "icon/tts_off.svg",
    "icon/tts_on.svg",
    "icon/word_play.svg",
    "scripts/app-config.js",
    "scripts/dragdrop-guard.js",
    "scripts/english-reading.css",
    "scripts/english-reading.js",
    "scripts/english-tts.js",
    "scripts/tts-config.js",
    "scripts/voices.conf",
    "scripts/words-leap.js",
    "scripts/words-teppeki.js"
)

$Actual = @(git -c core.quotepath=false ls-files --others --exclude-standard)
if ($Actual.Count -ne 44) {
    throw "Unexpected Git candidate count after hold-file move: expected=44 actual=$($Actual.Count)"
}

$MissingAscii = @($ExpectedAscii | Where-Object { $Actual -notcontains $_ })
if ($MissingAscii.Count -ne 0) {
    throw "Missing expected ASCII Git candidates: $($MissingAscii -join ', ')"
}

$Residual = @($Actual | Where-Object { $_ -notin $ExpectedAscii })
if ($Residual.Count -ne 3) {
    throw "Unexpected residual candidate count: expected=3 actual=$($Residual.Count)"
}

$IgnoredAudioCount = @(git ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCount = @(git ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCount -ne 3747) {
    throw "Unexpected ignored audio count: $IgnoredAudioCount"
}
if ($IgnoredImageCount -ne 6480) {
    throw "Unexpected ignored image count: $IgnoredImageCount"
}

$RemoteCount = @(git remote).Count
if ($RemoteCount -ne 0) {
    throw "Unexpected remote exists before baseline setup. REMOTE_COUNT=$RemoteCount"
}

# Stage exactly the already-validated non-ignored candidate set.
git add -A
if ($LASTEXITCODE -ne 0) {
    throw "git add -A failed"
}

$Staged = @(git -c core.quotepath=false diff --cached --name-only)
if ($Staged.Count -ne 44) {
    throw "Unexpected staged count: expected=44 actual=$($Staged.Count)"
}

$RemainingUntracked = @(git -c core.quotepath=false ls-files --others --exclude-standard)
if ($RemainingUntracked.Count -ne 0) {
    throw "Unexpected non-ignored untracked files remain: $($RemainingUntracked.Count)"
}

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t4C_STAGE_ENCODING_SAFE")
$Lines.Add("BRANCH`t$Branch")
foreach ($State in $MoveStates) { $Lines.Add($State) }
$Lines.Add("GIT_CANDIDATE_COUNT_BEFORE_STAGE`t$($Actual.Count)")
$Lines.Add("KNOWN_ASCII_CANDIDATE_COUNT`t$($ExpectedAscii.Count)")
$Lines.Add("RESIDUAL_CANDIDATE_COUNT`t$($Residual.Count)")
foreach ($Path in $Residual) { $Lines.Add("RESIDUAL`t$Path") }
$Lines.Add("STAGED_COUNT`t$($Staged.Count)")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCount")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCount")
$Lines.Add("REMAINING_UNTRACKED_COUNT`t$($RemainingUntracked.Count)")
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
Write-Host "STEP=4C_STAGE_ENCODING_SAFE"
Write-Host "ROOT=$Root"
Write-Host "HOLD_FILE_COUNT=$($Moves.Count)"
Write-Host "GIT_CANDIDATE_COUNT=44"
Write-Host "KNOWN_ASCII_CANDIDATE_COUNT=$($ExpectedAscii.Count)"
Write-Host "RESIDUAL_CANDIDATE_COUNT=$($Residual.Count)"
Write-Host "STAGED_COUNT=$($Staged.Count)"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioCount"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageCount"
Write-Host "REMAINING_UNTRACKED_COUNT=$($RemainingUntracked.Count)"
Write-Host "COMMIT=NO"
Write-Host "REMOTE=NO"
Write-Host "MEDIA_MOVED=NO"
Write-Host "OUTPUT=$ReportPath"