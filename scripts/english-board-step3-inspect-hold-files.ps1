$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$ReportDir = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board\reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Output = Join-Path $ReportDir ("english_board_hold_files_{0}.txt" -f $Timestamp)

if ((Get-Location).Path -ne $Root) {
    Set-Location $Root
}

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "Git repository is not initialized: $Root"
}

New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null

$HoldFiles = @(
    "20260517095055.txt",
    "icon-check.html",
    "icon-concepts.html",
    "make_zip.ps1",
    "make_zip_all.ps1"
)

$Lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$Text = "") { $Lines.Add($Text) }

Add-Line "ROOT`t$Root"
Add-Line "CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Line "MODE`tREAD_ONLY_HOLD_FILE_INSPECTION"
Add-Line "SOURCE_MUTATION`tNO"
Add-Line ""

Add-Line "=== GIT STATUS BEFORE ==="
$StatusBefore = @(git status --short --ignored)
foreach ($line in $StatusBefore) { Add-Line $line }
Add-Line ""

# Build a list of non-ignored files that could reference the hold files.
$CandidateFiles = @(Get-ChildItem -Path $Root -Recurse -File -Force | Where-Object {
    $_.FullName -notlike "$Root\.git\*" -and
    $_.FullName -notlike "$Root\audio\*" -and
    $_.FullName -notlike "$Root\image\*" -and
    $_.Name -notlike "filelists_*.txt" -and
    $_.Name -notlike "gitinfo_*.txt" -and
    $_.Name -notlike "english_board_*.txt"
})

foreach ($Relative in $HoldFiles) {
    $Path = Join-Path $Root $Relative
    Add-Line "============================================================"
    Add-Line "FILE`t$Relative"
    Add-Line "============================================================"

    if (-not (Test-Path $Path -PathType Leaf)) {
        Add-Line "STATUS`tMISSING"
        Add-Line ""
        continue
    }

    $Item = Get-Item $Path
    $Hash = (Get-FileHash -Path $Path -Algorithm SHA256).Hash
    Add-Line "STATUS`tEXISTS"
    Add-Line "SIZE_BYTES`t$($Item.Length)"
    Add-Line "LAST_WRITE_TIME`t$($Item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
    Add-Line "SHA256`t$Hash"
    Add-Line ""

    Add-Line "--- REFERENCES FROM OTHER NON-MEDIA FILES ---"
    $BaseName = [System.IO.Path]::GetFileName($Relative)
    $ReferenceHits = New-Object System.Collections.Generic.List[string]
    foreach ($Candidate in $CandidateFiles) {
        if ($Candidate.FullName -eq $Path) { continue }
        try {
            $Matches = Select-String -Path $Candidate.FullName -SimpleMatch -Pattern $BaseName -ErrorAction Stop
            foreach ($Match in $Matches) {
                $RelCandidate = $Candidate.FullName.Substring($Root.Length).TrimStart('\')
                $ReferenceHits.Add(("{0}:{1}: {2}" -f $RelCandidate, $Match.LineNumber, $Match.Line.Trim()))
            }
        } catch {
            # Ignore unreadable/binary files; media is already excluded.
        }
    }
    if ($ReferenceHits.Count -eq 0) {
        Add-Line "NONE"
    } else {
        foreach ($Hit in $ReferenceHits) { Add-Line $Hit }
    }
    Add-Line ""

    Add-Line "--- FULL CONTENT ---"
    try {
        $Content = Get-Content -Path $Path -Raw -Encoding UTF8
    } catch {
        $Content = Get-Content -Path $Path -Raw
    }
    Add-Line $Content
    Add-Line ""
}

Add-Line "=== GIT STATUS AFTER ==="
$StatusAfter = @(git status --short --ignored)
foreach ($line in $StatusAfter) { Add-Line $line }
Add-Line ""

$Lines | Set-Content -Path $Output -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=3_INSPECT_HOLD_FILES"
Write-Host "ROOT=$Root"
Write-Host "HOLD_FILE_COUNT=$($HoldFiles.Count)"
Write-Host "SOURCE_MUTATION=NO"
Write-Host "GIT_ADD=NO"
Write-Host "COMMIT=NO"
Write-Host "REMOTE=NO"
Write-Host "OUTPUT=$Output"
