$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReviewRoot = Join-Path $DevRoot "reviews"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReviewDir = Join-Path $ReviewRoot ("english_board_reorganization_review_{0}" -f $Timestamp)
$SnapshotRoot = Join-Path $ReviewDir "repo-snapshot"
$MigrationDir = Join-Path $ReviewDir "migration-scripts"
$ExternalizedDir = Join-Path $ReviewDir "externalized-artifacts"
$EvidenceDir = Join-Path $ReviewDir "evidence"
$PackagePath = Join-Path $ReviewRoot ("english_board_reorganization_review_{0}.zip" -f $Timestamp)
$PromptPath = Join-Path $ReviewDir "REVIEW_REQUEST.md"
$AiReviewPath = Join-Path $ReviewDir "AI_REVIEW.md"
$CodexStdoutPath = Join-Path $EvidenceDir "codex_stdout.txt"
$CodexStderrPath = Join-Path $EvidenceDir "codex_stderr.txt"
$ExpectedHead = "aae2864bb83ab9b23c8960f2f90fc9918e0017f6"
$ExpectedTrackedCount = 46
$ExpectedAudioCount = 3747
$ExpectedImageCount = 6480
$ExpectedRemote = "https://github.com/shinichitaniguchibiz-byte/english-board.git"

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Text)
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Get-RelativePathSimple {
    param([Parameter(Mandatory = $true)][string]$Base, [Parameter(Mandatory = $true)][string]$Full)
    $value = $Full.Substring($Base.Length)
    return $value.TrimStart([char[]]@('\','/')).Replace('\','/')
}

function Read-TextBestEffort {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        return [System.IO.File]::ReadAllText($Path, $utf8)
    }
    catch {
        return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::Default)
    }
}

function New-SnapshotManifest {
    param([Parameter(Mandatory = $true)][string]$SnapshotPath, [Parameter(Mandatory = $true)][string]$OutputPath)
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("PATH`tBYTES`tSHA256")
    foreach ($file in @(Get-ChildItem -LiteralPath $SnapshotPath -Recurse -File | Sort-Object FullName)) {
        $rel = Get-RelativePathSimple -Base $SnapshotPath -Full $file.FullName
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        $lines.Add("$rel`t$($file.Length)`t$hash")
    }
    Write-Utf8NoBom -Path $OutputPath -Text (($lines -join "`r`n") + "`r`n")
}

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    throw "English Board Git repository is missing: $Root"
}

$Branch = (& git -C $Root symbolic-ref --short HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

$Head = (& git -C $Root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Head -ne $ExpectedHead) {
    throw "Unexpected HEAD: expected=$ExpectedHead actual=$Head"
}

$StatusBefore = @(& git -C $Root status --porcelain)
if ($LASTEXITCODE -ne 0 -or $StatusBefore.Count -ne 0) {
    throw "English Board worktree must be clean before review"
}

$TrackedCount = @(& git -C $Root ls-files).Count
if ($TrackedCount -ne $ExpectedTrackedCount) {
    throw "Unexpected tracked count: expected=$ExpectedTrackedCount actual=$TrackedCount"
}

$IgnoredAudioCount = @(& git -C $Root ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCount = @(& git -C $Root ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCount -ne $ExpectedAudioCount -or $IgnoredImageCount -ne $ExpectedImageCount) {
    throw "Unexpected ignored media count: audio=$IgnoredAudioCount image=$IgnoredImageCount"
}

$Origin = (& git -C $Root remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $Origin -ne $ExpectedRemote) {
    throw "Unexpected origin: $Origin"
}

New-Item -ItemType Directory -Force -Path $ReviewRoot, $ReviewDir, $SnapshotRoot, $MigrationDir, $ExternalizedDir, $EvidenceDir | Out-Null

$SnapshotZip = Join-Path $ReviewDir "repo-snapshot.zip"
& git -C $Root archive --format=zip "--output=$SnapshotZip" HEAD
if ($LASTEXITCODE -ne 0) {
    throw "git archive failed"
}
Expand-Archive -LiteralPath $SnapshotZip -DestinationPath $SnapshotRoot -Force
Remove-Item -LiteralPath $SnapshotZip -Force

$ManifestBefore = Join-Path $EvidenceDir "snapshot_manifest_before.tsv"
New-SnapshotManifest -SnapshotPath $SnapshotRoot -OutputPath $ManifestBefore
$ManifestBeforeHash = (Get-FileHash -LiteralPath $ManifestBefore -Algorithm SHA256).Hash

$GitEvidence = New-Object System.Collections.Generic.List[string]
$GitEvidence.Add("ROOT`t$Root")
$GitEvidence.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$GitEvidence.Add("BRANCH`t$Branch")
$GitEvidence.Add("HEAD`t$Head")
$GitEvidence.Add("TRACKED_COUNT`t$TrackedCount")
$GitEvidence.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCount")
$GitEvidence.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCount")
$GitEvidence.Add("ORIGIN`t$Origin")
$GitEvidence.Add("")
$GitEvidence.Add("=== GIT LOG ===")
foreach ($line in @(& git -C $Root -c core.quotepath=false log --oneline --decorate --all -n 20)) { $GitEvidence.Add($line) }
$GitEvidence.Add("")
$GitEvidence.Add("=== REMOTES ===")
foreach ($line in @(& git -C $Root remote -v)) { $GitEvidence.Add($line) }
$GitEvidence.Add("")
$GitEvidence.Add("=== STATUS ===")
foreach ($line in @(& git -C $Root -c core.quotepath=false status --short --ignored)) { $GitEvidence.Add($line) }
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "git_state.txt") -Text (($GitEvidence -join "`r`n") + "`r`n")

$TextExtensions = @(".html", ".htm", ".js", ".css", ".txt", ".md", ".conf", ".ps1")
$SnapshotTextFiles = @(Get-ChildItem -LiteralPath $SnapshotRoot -Recurse -File | Where-Object {
    ($TextExtensions -contains $_.Extension.ToLowerInvariant()) -or $_.Name -in @(".gitignore", ".gitattributes")
})
$RootDocLike = @(Get-ChildItem -LiteralPath $SnapshotRoot -File | Where-Object { $_.Extension.ToLowerInvariant() -in @(".txt", ".md") })

$ReferenceLines = New-Object System.Collections.Generic.List[string]
$ReferenceLines.Add("ROOT DOCUMENT/REFERENCE NAME SCAN")
$ReferenceLines.Add("The scan searches current tracked text-like files for literal references to each root-level TXT/MD filename.")
$ReferenceLines.Add("")
foreach ($candidate in $RootDocLike) {
    $hits = New-Object System.Collections.Generic.List[string]
    foreach ($file in $SnapshotTextFiles) {
        if ($file.FullName -eq $candidate.FullName) { continue }
        $content = Read-TextBestEffort -Path $file.FullName
        if ($content.IndexOf($candidate.Name, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $hits.Add((Get-RelativePathSimple -Base $SnapshotRoot -Full $file.FullName))
        }
    }
    $ReferenceLines.Add("FILE`t$($candidate.Name)`tREFERENCE_COUNT`t$($hits.Count)")
    foreach ($hit in $hits) { $ReferenceLines.Add("  REF`t$hit") }
}
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "root_document_reference_scan.txt") -Text (($ReferenceLines -join "`r`n") + "`r`n")

$RuntimeEvidence = New-Object System.Collections.Generic.List[string]
$RuntimeEvidence.Add("RUNTIME ENTRY EVIDENCE FROM English_Reading.html")
$RuntimeEvidence.Add("")
$EntryPath = Join-Path $SnapshotRoot "English_Reading.html"
$EntryText = Read-TextBestEffort -Path $EntryPath
foreach ($line in ($EntryText -split "`r?`n")) {
    if ($line -match "<script|<link|audio/|image/|icon/") { $RuntimeEvidence.Add($line.Trim()) }
}
$RuntimeEvidence.Add("")
$RuntimeEvidence.Add("RUNTIME STRING EVIDENCE IN TRACKED TEXT-LIKE FILES")
$Needles = @("./audio/", "./image/", "window.teppekiWordData", "window.leapWordData", "words-teppeki.js", "words-leap.js")
foreach ($needle in $Needles) {
    $RuntimeEvidence.Add("NEEDLE`t$needle")
    foreach ($file in $SnapshotTextFiles) {
        $content = Read-TextBestEffort -Path $file.FullName
        if ($content.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $RuntimeEvidence.Add("  HIT`t$(Get-RelativePathSimple -Base $SnapshotRoot -Full $file.FullName)")
        }
    }
}
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "runtime_dependency_evidence.txt") -Text (($RuntimeEvidence -join "`r`n") + "`r`n")

$ExternalMoves = @(
    @{ Source = (Join-Path $DevRoot "samples\20260517095055.txt"); Name = "20260517095055.txt"; Sha256 = "D85D86D6FCC86DB601BD6400F5744410CADCAB6EF6C94A5D710BF65257BD2D79" },
    @{ Source = (Join-Path $DevRoot "design\icon-check.html"); Name = "icon-check.html"; Sha256 = "6DEC2A194FD06C14C07D5E99FB1A210D54B0C3A646A847325E2996C9C08B6C3A" },
    @{ Source = (Join-Path $DevRoot "design\icon-concepts.html"); Name = "icon-concepts.html"; Sha256 = "DD7F45A8272587382D6C9F16D91BAD77793E5C27B60A3658BF38E0B4D494A939" },
    @{ Source = (Join-Path $DevRoot "legacy-tools\make_zip.ps1"); Name = "make_zip.ps1"; Sha256 = "E7439BEB87718A5A581BC73B31A9A7BA20AC3FB47DCF303C1DBA49E9F48A0D53" },
    @{ Source = (Join-Path $DevRoot "legacy-tools\make_zip_all.ps1"); Name = "make_zip_all.ps1"; Sha256 = "F65D6D4C5549E424393F09DEB97061BBED0AB1D3612B7FDA32C37D10570CD356" }
)
$ExternalEvidence = New-Object System.Collections.Generic.List[string]
$ExternalEvidence.Add("EXTERNALIZED ARTIFACTS")
foreach ($item in $ExternalMoves) {
    if (-not (Test-Path -LiteralPath $item.Source -PathType Leaf)) {
        throw "Previously externalized artifact is missing: $($item.Name)"
    }
    $hash = (Get-FileHash -LiteralPath $item.Source -Algorithm SHA256).Hash
    if ($hash -ne $item.Sha256) {
        throw "Previously externalized artifact hash mismatch: $($item.Name)"
    }
    Copy-Item -LiteralPath $item.Source -Destination (Join-Path $ExternalizedDir $item.Name)
    $ExternalEvidence.Add("$($item.Name)`t$($item.Source)`t$hash")
}
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "externalized_artifacts.txt") -Text (($ExternalEvidence -join "`r`n") + "`r`n")

$MigrationScripts = @(
    @{ Name = "collect_gitinfo.ps1"; Commit = "409156f8d8b6339f89f72d6e3b0db55034d87e7d" },
    @{ Name = "analyze_english_board_files.ps1"; Commit = "c3d60c307831a6ea0f516d8f884034b906820126" },
    @{ Name = "english-board-step1-prepare-gitignore.ps1"; Commit = "c05eac4c03fb5fddf406c6cb1d1e5560c4ea9960" },
    @{ Name = "english-board-step2-init-verify-git.ps1"; Commit = "d12c386a10db862071c688c6fef3b6970d0dfee4" },
    @{ Name = "english-board-step3-inspect-hold-files.ps1"; Commit = "35897aad3f972b2f81c67dbb5c4b0a17138e8bac" },
    @{ Name = "english-board-step4-move-hold-and-stage.ps1"; Commit = "a82a23e0218eef213a2a1b4e3d1e167d7c22496f" },
    @{ Name = "english-board-step4b-recover-and-stage.ps1"; Commit = "6f40565750890208576325c1b47fc684be99d95f" },
    @{ Name = "english-board-step4c-stage-encoding-safe.ps1"; Commit = "470b0c221e61c5b9ceb9798eb71b4b3d207ec631" },
    @{ Name = "english-board-step5-preserve-baseline.ps1"; Commit = "28186352a819907ebf59c12ec16c63fd0715869e" },
    @{ Name = "english-board-step6-baseline-commit.ps1"; Commit = "dd8dc609458c43903218816b372a16c37ffd833c" },
    @{ Name = "english-board-step6b-baseline-commit-safe.ps1"; Commit = "f6c2b7380b6155329d9731faa4929a710cac4a4f" },
    @{ Name = "english-board-step7-create-private-remote-and-push.ps1"; Commit = "34fce305b0c031cefd93316c9d2d8e0c165f6802" },
    @{ Name = "english-board-step7b-create-private-remote-and-push-safe.ps1"; Commit = "1cbab9e82679faa675589391bb8114b9af1cef77" }
)
$MigrationIndex = New-Object System.Collections.Generic.List[string]
$MigrationIndex.Add("NAME`tCOMMIT`tSHA256")
foreach ($item in $MigrationScripts) {
    $url = "https://raw.githubusercontent.com/shinichitaniguchibiz-byte/test/$($item.Commit)/scripts/$($item.Name)"
    $dest = Join-Path $MigrationDir $item.Name
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest -ErrorAction Stop
    $hash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
    $MigrationIndex.Add("$($item.Name)`t$($item.Commit)`t$hash")
}
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "migration_script_index.tsv") -Text (($MigrationIndex -join "`r`n") + "`r`n")

$History = @"
# English Board Git migration history for independent review

This package is read-only evidence for an independent AI review. No source move is authorized by this package.

## Baseline facts

- Local root: C:\OneDrive2\OneDrive\lib\codex\English Board
- Private GitHub repository: shinichitaniguchibiz-byte/english-board
- Branch: main
- Baseline HEAD: $ExpectedHead
- Tracked files: 46
- Ignored local runtime media: audio=$ExpectedAudioCount files, image=$ExpectedImageCount files
- The local version remains a permanent fast development/runtime environment.
- The current initial Web migration is Local-to-Web one-way alignment. Future Local/Web synchronization policy is not yet finalized.

## Completed setup sequence

1. Inventory classified 10,278 files. Large local learning media were identified separately from source/reference text.
2. .gitignore was normalized. audio/ and image/ remain physically in the local runtime but are excluded from Git.
3. Git was initialized on main without a remote.
4. Five ambiguous non-runtime artifacts were inspected before any move.
5. Those five artifacts were moved outside the application root into the external development area. They are copied into this review package and MUST be re-evaluated; previous externalization is not assumed to be final.
6. The remaining application/reference set was staged using encoding-safe validation.
7. .gitattributes and DEVELOPMENT_POLICY.md were added. Local core.autocrlf was set false so the local baseline is not silently line-ending normalized.
8. Baseline root commit was created: $ExpectedHead.
9. A private GitHub repository was created, origin was set, main was pushed, and local/remote HEAD equality was verified.

## Important historical failures

- An early move/stage script embedded non-ASCII filenames and failed under Windows PowerShell 5.1 decoding. Recovery scripts switched to ASCII-only source literals and discovery/hash-based checks.
- The first baseline-commit script treated an unborn HEAD as an error. The safe replacement handled the initial commit state correctly.
- The first GitHub remote setup script had quoting/empty-argument problems around gh --jq. The safe replacement removed --jq and verified JSON in PowerShell.

All historical scripts are included under migration-scripts/ at their exact pinned commits.
"@
Write-Utf8NoBom -Path (Join-Path $ReviewDir "MIGRATION_HISTORY.md") -Text $History

$Context = @"
# English Board reorganization review context

## Goal

Before moving any more files, independently determine the safest physical organization of the local English Board repository. The review must preserve every important file and must not confuse Git management with runtime necessity.

## Current conceptual proposal (NOT approved yet)

Possible structure:

English Board/
  English_Reading.html
  scripts/
  icon/
  audio/       (local runtime, ignored by Git)
  image/       (local runtime, ignored by Git)
  data/
    source/
      root vocabulary/reference source TXT files if proven safe to move
  docs/
    development policy and developer-facing specification/technical/setup documents
  .gitignore
  .gitattributes

This is only a proposal. The independent reviewer must change or reject it if evidence indicates a better structure.

## Review boundaries

- The local application must continue to run independently and quickly.
- The Web application does not load files from this local directory.
- The current task is local repository organization only; it does not implement the Learning Platform Web port.
- Do not delete files.
- Do not move or rename source files during this review.
- If a dependency is uncertain, classify the candidate as HOLD rather than assuming it is safe.
- The five previously externalized artifacts must be reviewed again for whether they should remain external or be restored to Git under an appropriate archive/docs/samples/tools location.
"@
Write-Utf8NoBom -Path (Join-Path $ReviewDir "REVIEW_CONTEXT.md") -Text $Context

$Prompt = @"
You are an independent software-repository auditor. Your job is analysis only. Do not modify, rename, delete, or create application files. The workspace is a disposable review copy, but still do not edit it.

Read REVIEW_CONTEXT.md and MIGRATION_HISTORY.md first.
Then inspect the evidence/ directory, the complete repo-snapshot/ directory, migration-scripts/, and externalized-artifacts/.

Mandatory content review:
1. Read DEVELOPMENT_POLICY.md in full.
2. Read every root-level TXT and MD file in repo-snapshot/ in full and explain what each contains and whether it is runtime, source/reference data, or developer documentation.
3. Read English_Reading.html in full and inspect every directly loaded JS/CSS dependency.
4. Inspect scripts/*.js, scripts/*.css, and scripts/*.conf sufficiently to identify actual runtime dependencies and generated/source-data relationships. Do not decide by filename extension alone.
5. Inspect all five externalized artifacts in full and decide whether each should remain external, return to Git, or be stored under an archive/docs/samples/tools location.
6. Inspect the migration scripts and Git evidence sufficiently to verify that the current baseline and prior moves are understood.

Key questions:
- Which files are required for the current local runtime?
- Which files are developer-facing documents only?
- Which files are source/reference data that may generate or support runtime files even if the browser does not load them directly?
- Is moving the root TXT/MD files into docs/ or data/source/ safe without code changes? If not proven, mark HOLD.
- Should DEVELOPMENT_POLICY.md remain at repository root or move under docs/? Explain the tradeoff for future AI/developer discoverability.
- Are any of the five previously externalized artifacts important enough to restore to Git? If yes, recommend the exact logical destination.
- Does the proposed directory structure create ambiguity between runtime source, generated data, source data, and documentation?
- What is the safest move order with validation gates so no important content is lost?

Required output language: Japanese.
Required output sections:
A. Overall verdict: SAFE_NOW, SAFE_AFTER_FIXES, or HOLD.
B. Repository model: explain the actual runtime/source-data/documentation/repository-control layers.
C. Document content review: one entry for every root-level TXT/MD file with a substantive content summary and classification.
D. Full tracked-file classification: account for all 46 tracked files, grouping repetitive icon files if and only if every file is still explicitly accounted for by name.
E. Externalized-artifact review: all five files, with KEEP_EXTERNAL / RESTORE_TO_GIT / HOLD and exact recommended destination.
F. Dependency and loss risks: cite concrete file paths and evidence.
G. Recommended final directory tree.
H. Exact move-now list and exact HOLD list. Do not recommend deletion.
I. Validation gates required before and after any future move.
J. Independent opinion on whether today's proposed docs/ and data/source/ consolidation is correct or should be changed.

Rules for uncertainty:
- Do not infer safety from absence of a browser <script> reference alone.
- Distinguish browser runtime dependency from build/generation/manual-maintenance dependency.
- If the relationship between a root data file and words-*.js cannot be proven from the supplied evidence, explicitly say UNKNOWN and HOLD that move.
- Preserve important historical and operational knowledge even if it is not runtime code.
- The goal is a defensible repository organization, not the smallest possible root directory.
"@
Write-Utf8NoBom -Path $PromptPath -Text $Prompt

$CodexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $CodexCommand) {
    Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $PackagePath -Force
    Write-Host "RESULT=NEEDS_CODEX_CLI"
    Write-Host "STEP=8_AI_REORGANIZATION_REVIEW"
    Write-Host "SOURCE_HEAD=$Head"
    Write-Host "SOURCE_MUTATION=NO"
    Write-Host "REVIEW_DIR=$ReviewDir"
    Write-Host "PACKAGE=$PackagePath"
    exit 2
}

$CodexPath = $CodexCommand.Source
$CodexVersion = (& $CodexPath --version 2>&1 | Out-String).Trim()
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "codex_version.txt") -Text ($CodexVersion + "`r`n")

$PromptText = Read-TextBestEffort -Path $PromptPath
$OldLocation = Get-Location
$CodexExit = 999
try {
    Set-Location $ReviewDir
    $PromptText | & $CodexPath exec --ephemeral --skip-git-repo-check --sandbox workspace-write -c "sandbox_workspace_write.network_access=false" -C $ReviewDir -o $AiReviewPath - 1> $CodexStdoutPath 2> $CodexStderrPath
    $CodexExit = $LASTEXITCODE
}
finally {
    Set-Location $OldLocation
}

$HeadAfter = (& git -C $Root rev-parse HEAD).Trim()
$StatusAfter = @(& git -C $Root status --porcelain)
$SourceMutation = ($HeadAfter -ne $ExpectedHead) -or ($StatusAfter.Count -ne 0)

$ManifestAfter = Join-Path $EvidenceDir "snapshot_manifest_after.tsv"
New-SnapshotManifest -SnapshotPath $SnapshotRoot -OutputPath $ManifestAfter
$ManifestAfterHash = (Get-FileHash -LiteralPath $ManifestAfter -Algorithm SHA256).Hash
$SnapshotCopyMutated = $ManifestBeforeHash -ne $ManifestAfterHash

$FinalEvidence = @"
RESULT_EVIDENCE
SOURCE_HEAD_BEFORE=$Head
SOURCE_HEAD_AFTER=$HeadAfter
SOURCE_STATUS_AFTER_COUNT=$($StatusAfter.Count)
SOURCE_MUTATION=$SourceMutation
SNAPSHOT_MANIFEST_BEFORE_SHA256=$ManifestBeforeHash
SNAPSHOT_MANIFEST_AFTER_SHA256=$ManifestAfterHash
SNAPSHOT_COPY_MUTATED_BY_AI=$SnapshotCopyMutated
CODEX_EXIT=$CodexExit
CODEX_VERSION=$CodexVersion
"@
Write-Utf8NoBom -Path (Join-Path $EvidenceDir "final_safety_evidence.txt") -Text $FinalEvidence

Compress-Archive -Path (Join-Path $ReviewDir "*") -DestinationPath $PackagePath -Force

if ($SourceMutation) {
    Write-Host "RESULT=FAIL_SOURCE_MUTATION_DETECTED"
    Write-Host "STEP=8_AI_REORGANIZATION_REVIEW"
    Write-Host "SOURCE_HEAD=$HeadAfter"
    Write-Host "REVIEW_DIR=$ReviewDir"
    Write-Host "PACKAGE=$PackagePath"
    exit 5
}

if ($SnapshotCopyMutated) {
    Write-Host "RESULT=FAIL_REVIEW_COPY_MUTATED"
    Write-Host "STEP=8_AI_REORGANIZATION_REVIEW"
    Write-Host "SOURCE_MUTATION=NO"
    Write-Host "REVIEW_DIR=$ReviewDir"
    Write-Host "PACKAGE=$PackagePath"
    exit 6
}

if ($CodexExit -ne 0 -or -not (Test-Path -LiteralPath $AiReviewPath -PathType Leaf) -or (Get-Item -LiteralPath $AiReviewPath).Length -lt 200) {
    Write-Host "RESULT=AI_REVIEW_FAILED"
    Write-Host "STEP=8_AI_REORGANIZATION_REVIEW"
    Write-Host "SOURCE_HEAD=$HeadAfter"
    Write-Host "SOURCE_MUTATION=NO"
    Write-Host "CODEX_EXIT=$CodexExit"
    Write-Host "REVIEW_DIR=$ReviewDir"
    Write-Host "PACKAGE=$PackagePath"
    exit 4
}

Write-Host "RESULT=PASS"
Write-Host "STEP=8_AI_REORGANIZATION_REVIEW"
Write-Host "SOURCE_HEAD=$HeadAfter"
Write-Host "SOURCE_MUTATION=NO"
Write-Host "TRACKED_COUNT=$TrackedCount"
Write-Host "EXTERNALIZED_ARTIFACT_COUNT=$($ExternalMoves.Count)"
Write-Host "MIGRATION_SCRIPT_COUNT=$($MigrationScripts.Count)"
Write-Host "CODEX_EXIT=$CodexExit"
Write-Host "AI_REVIEW=$AiReviewPath"
Write-Host "REVIEW_DIR=$ReviewDir"
Write-Host "PACKAGE=$PackagePath"
