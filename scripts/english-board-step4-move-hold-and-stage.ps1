$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_stage_{0}.txt" -f $Timestamp)

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "Git repository is not initialized: $Root"
}

$Branch = (git symbolic-ref --short HEAD 2>$null).Trim()
if ($Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

# Never overwrite existing external-development files silently.
$Moves = @(
    @{ Source = "20260517095055.txt"; Sha256 = "D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79"; DestDir = (Join-Path $DevRoot "samples") },
    @{ Source = "icon-check.html"; Sha256 = "6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A"; DestDir = (Join-Path $DevRoot "design") },
    @{ Source = "icon-concepts.html"; Sha256 = "DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939"; DestDir = (Join-Path $DevRoot "design") },
    @{ Source = "make_zip.ps1"; Sha256 = "E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53"; DestDir = (Join-Path $DevRoot "legacy-tools") },
    @{ Source = "make_zip_all.ps1"; Sha256 = "F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356"; DestDir = (Join-Path $DevRoot "legacy-tools") }
)

foreach ($Move in $Moves) {
    $SourcePath = Join-Path $Root $Move.Source
    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "Expected hold file is missing: $($Move.Source)"
    }
    $ActualHash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash
    if ($ActualHash -ne $Move.Sha256) {
        throw "Hold file changed since inspection: $($Move.Source) expected=$($Move.Sha256) actual=$ActualHash"
    }

    New-Item -ItemType Directory -Force -Path $Move.DestDir | Out-Null
    $DestPath = Join-Path $Move.DestDir $Move.Source
    if (Test-Path -LiteralPath $DestPath) {
        throw "Destination already exists; refusing overwrite: $DestPath"
    }
}

foreach ($Move in $Moves) {
    $SourcePath = Join-Path $Root $Move.Source
    $DestPath = Join-Path $Move.DestDir $Move.Source
    Move-Item -LiteralPath $SourcePath -Destination $DestPath
}

$ExpectedTracked = @(
    ".gitignore",
    "CEFR.txt",
    "English_Reading.html",
    "LEAP.txt",
    "SVL.txt",
    "技術情報.txt",
    "仕様書_20260111.txt",
    "設定手順.txt",
    "icon\add.svg",
    "icon\auto_play.svg",
    "icon\autoplay_24dp_1F1F1F_FILL0_wght400_GRAD0_opsz24.svg",
    "icon\autoplay_off.svg",
    "icon\autoplay_on.svg",
    "icon\content_paste.svg",
    "icon\download.svg",
    "icon\folder_open.svg",
    "icon\play.svg",
    "icon\play_2.svg",
    "icon\play_arrow.svg",
    "icon\read_aloud.svg",
    "icon\remove.svg",
    "icon\replay.svg",
    "icon\search.svg",
    "icon\search_off.svg",
    "icon\search_on.svg",
    "icon\settings.svg",
    "icon\settings_off.svg",
    "icon\settings_on.svg",
    "icon\stop_circle.svg",
    "icon\text_to_speech.svg",
    "icon\toggle_off.svg",
    "icon\toggle_on.svg",
    "icon\tts_off.svg",
    "icon\tts_on.svg",
    "icon\word_play.svg",
    "scripts\app-config.js",
    "scripts\dragdrop-guard.js",
    "scripts\english-reading.css",
    "scripts\english-reading.js",
    "scripts\english-tts.js",
    "scripts\tts-config.js",
    "scripts\voices.conf",
    "scripts\words-leap.js",
    "scripts\words-teppeki.js"
)

foreach ($RelativePath in $ExpectedTracked) {
    $FullPath = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $FullPath -PathType Leaf)) {
        throw "Expected Git-managed file is missing: $RelativePath"
    }
    $Ignored = git check-ignore -- "$RelativePath" 2>$null
    if ($LASTEXITCODE -eq 0) {
        throw "Expected Git-managed file is ignored: $RelativePath"
    }
}

# Detect unexpected non-ignored files before staging anything.
$Untracked = @(git ls-files --others --exclude-standard)
$ExpectedGitPaths = @($ExpectedTracked | ForEach-Object { $_ -replace '\\','/' } | Sort-Object)
$ActualGitPaths = @($Untracked | Sort-Object)
$Unexpected = @(Compare-Object -ReferenceObject $ExpectedGitPaths -DifferenceObject $ActualGitPaths)
if ($Unexpected.Count -ne 0) {
    $Details = ($Unexpected | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }) -join "; "
    throw "Unexpected Git candidate set after move. $Details"
}

# Stage only the validated whitelist. No commit and no remote mutation.
git add -- $ExpectedTracked
if ($LASTEXITCODE -ne 0) {
    throw "git add failed"
}

$Staged = @(git diff --cached --name-only)
if ($Staged.Count -ne $ExpectedTracked.Count) {
    throw "Unexpected staged count: expected=$($ExpectedTracked.Count) actual=$($Staged.Count)"
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

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t4_MOVE_HOLD_AND_STAGE")
$Lines.Add("BRANCH`t$Branch")
$Lines.Add("MOVED_HOLD_COUNT`t$($Moves.Count)")
foreach ($Move in $Moves) {
    $Lines.Add("MOVED`t$($Move.Source)`t$(Join-Path $Move.DestDir $Move.Source)")
}
$Lines.Add("STAGED_COUNT`t$($Staged.Count)")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCount")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCount")
$Lines.Add("COMMIT`tNO")
$Lines.Add("REMOTE`tNO")
$Lines.Add("")
$Lines.Add("=== STAGED FILES ===")
foreach ($Path in $Staged) { $Lines.Add($Path) }
$Lines.Add("")
$Lines.Add("=== GIT STATUS ===")
foreach ($Line in @(git status --short --ignored)) { $Lines.Add($Line) }
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=4_MOVE_HOLD_AND_STAGE"
Write-Host "ROOT=$Root"
Write-Host "MOVED_HOLD_COUNT=$($Moves.Count)"
Write-Host "STAGED_COUNT=$($Staged.Count)"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioCount"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageCount"
Write-Host "COMMIT=NO"
Write-Host "REMOTE=NO"
Write-Host "MEDIA_MOVED=NO"
Write-Host "OUTPUT=$ReportPath"
