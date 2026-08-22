$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = "C:\OneDrive2\OneDrive\lib\codex\learning-platform"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform"
$LogDir = Join-Path $DevRoot "logs"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $LogDir ("lp_root_residue_audit_{0}.txt" -f $Timestamp)
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/learning-platform.git"
$PreviewRef = "refs/heads/feature/test-results-ui"
$ExpectedCandidateCount = 12

function Fail([string]$Message) { throw $Message }

$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -eq $GitCommand) { $GitCommand = Get-Command git -ErrorAction Stop }
$GitExe = $GitCommand.Source
if ([string]::IsNullOrWhiteSpace($GitExe)) { Fail "Unable to resolve native Git executable." }

function Invoke-Git([string[]]$GitArgs, [bool]$AllowFailure = $false) {
    $old = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $out = & $script:GitExe -C $script:RepoRoot -c core.quotepath=false @GitArgs 2>&1
        $code = [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    $lines = @($out | ForEach-Object { [string]$_ })
    if ($code -ne 0 -and -not $AllowFailure) {
        Fail ("git failed ({0}): git {1}`n{2}" -f $code,($GitArgs -join ' '),($lines -join "`n"))
    }
    [pscustomobject]@{ ExitCode=$code; Lines=$lines; Text=(($lines -join "`n").Trim()) }
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Get-IgnoreState([string]$RelativePath) {
    $r = Invoke-Git -GitArgs @("check-ignore","-v","--",$RelativePath) -AllowFailure $true
    if ($r.ExitCode -eq 0) { return "IGNORED`t$($r.Text)" }
    return "NOT_IGNORED"
}

function Test-LikelyText([string]$Path) {
    $ext = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    if ($ext -in @('.txt','.md','.json','.js','.mjs','.cjs','.html','.htm','.css','.ps1','.psm1','.sql','.csv','.tsv','.xml','.yml','.yaml','.ini','.conf','.log','.gitignore','.gitattributes')) { return $true }
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            $count = [Math]::Min(4096, [int]$stream.Length)
            $buffer = New-Object byte[] $count
            [void]$stream.Read($buffer,0,$count)
            foreach ($b in $buffer) { if ($b -eq 0) { return $false } }
            return $true
        } finally { $stream.Dispose() }
    } catch { return $false }
}

function Test-SensitiveLine([string]$Line) {
    if ($Line -match '(?i)(service[_ -]?role|client[_ -]?secret|secret[_ -]?key|access[_ -]?token|refresh[_ -]?token|password\s*[:=]|api[_ -]?key\s*[:=])') { return $true }
    if ($Line -match '(?i)(sb_secret_[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.)') { return $true }
    return $false
}

function Protect-EvidenceLine([string]$Line) {
    if (Test-SensitiveLine -Line $Line) { return '[REDACTED_SENSITIVE_LINE]' }
    return ($Line -replace "`t",'    ')
}

function Read-TextEvidence([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $enc = New-Object Text.UTF8Encoding($false,$false)
    $text = $enc.GetString($bytes)
    if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) { $text = $text.Substring(1) }
    $normalized = $text.Replace("`r`n","`n").Replace("`r","`n")
    $lines = @($normalized -split "`n")
    $conversationSignals = 0
    $scratchSignals = 0
    $secretSignals = 0
    $backupSignals = 0
    $buildSignals = 0
    $toolSignals = 0
    foreach ($line in $lines) {
        if ($line -match '(?i)思考時間|ChatGPT|Codex|引き継ぎ|次のチャット|ユーザー|assistant|user:|assistant:') { $conversationSignals++ }
        if ($line -match '(?i)clipboard|scratch|temporary|temp|貼り付け|会話|履歴') { $scratchSignals++ }
        if (Test-SensitiveLine -Line $line) { $secretSignals++ }
        if ($line -match '(?i)backup|restore|recovery|snapshot|archive|rollback|復旧|バックアップ') { $backupSignals++ }
        if ($line -match '(?i)generate|generator|build|compile|bundle|compress-archive|expand-archive|生成|ビルド') { $buildSignals++ }
        if ($line -match '(?i)powershell|script|installer|utility|tool|diagnostic|audit|検査|監査|ツール') { $toolSignals++ }
    }
    [pscustomobject]@{
        LineCount=$lines.Count
        CharCount=$text.Length
        ConversationSignals=$conversationSignals
        ScratchSignals=$scratchSignals
        SecretSignals=$secretSignals
        BackupSignals=$backupSignals
        BuildSignals=$buildSignals
        ToolSignals=$toolSignals
        FirstLines=@($lines | Select-Object -First 12)
        LastLines=@($lines | Select-Object -Last 12)
    }
}

function Get-ZipEvidence([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entries = @($zip.Entries)
        $total = [long]0
        $names = New-Object System.Collections.ArrayList
        foreach ($e in $entries) {
            $total += [long]$e.Length
            [void]$names.Add([string]$e.FullName)
        }
        $coreSignals = @('AGENTS.md','PROJECT_RULES.md','CHATGPT.md','DB.md','index.html','app.js','functions/','assets/','scripts/')
        $coreMatches = 0
        foreach ($signal in $coreSignals) {
            if (@($names | Where-Object { $_ -eq $signal -or $_.StartsWith($signal,[StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) { $coreMatches++ }
        }
        [pscustomobject]@{
            EntryCount=$entries.Count
            UncompressedBytes=$total
            CoreRepoSignals=$coreMatches
            Sample=@($entries | Select-Object -First 80 | ForEach-Object { "{0}`t{1}" -f $_.FullName,$_.Length })
        }
    } finally { $zip.Dispose() }
}

function Get-TrackedReferences([string]$Needle, [string[]]$TrackedFiles) {
    $hits = New-Object System.Collections.ArrayList
    foreach ($rel in $TrackedFiles) {
        $full = Join-Path $script:RepoRoot $rel
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        if (-not (Test-LikelyText -Path $full)) { continue }
        try { $text = [IO.File]::ReadAllText($full) } catch { continue }
        if ($text.IndexOf($Needle,[StringComparison]::OrdinalIgnoreCase) -ge 0) { [void]$hits.Add($rel) }
    }
    @($hits)
}

function Get-PreviewPresence([string]$PreviewSha, [string]$RelativePath) {
    $obj = Invoke-Git -GitArgs @("cat-file","-e",("{0}^{{commit}}" -f $PreviewSha)) -AllowFailure $true
    if ($obj.ExitCode -ne 0) { return "UNKNOWN_OBJECT_NOT_LOCAL" }
    $r = Invoke-Git -GitArgs @("cat-file","-e",("{0}:{1}" -f $PreviewSha,$RelativePath)) -AllowFailure $true
    if ($r.ExitCode -eq 0) { return "YES" }
    return "NO"
}

function Get-SuggestedClassification([string]$ContentKind, [int]$ReferenceCount, $TextEvidence, $ZipEvidence, [int]$HistoryCount) {
    if ($ReferenceCount -gt 0) { return 'HOLD' }
    if ($ContentKind -eq 'ZIP_ARCHIVE' -and $null -ne $ZipEvidence -and $ZipEvidence.CoreRepoSignals -ge 3) { return 'backup' }
    if ($ContentKind -eq 'TEXT' -and $null -ne $TextEvidence) {
        if ($TextEvidence.ConversationSignals -ge 3 -and $TextEvidence.ToolSignals -lt 3 -and $HistoryCount -eq 0) { return 'transient-obsolete' }
        if ($TextEvidence.BuildSignals -ge 3) { return 'build-generation' }
        if ($TextEvidence.ToolSignals -ge 3) { return 'developer-tool' }
        if ($TextEvidence.BackupSignals -ge 3) { return 'legacy-recovery' }
    }
    return 'HOLD'
}

if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) { Fail "LP repository root missing: $RepoRoot" }
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) { Fail "LP .git missing: $RepoRoot" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$origin = (Invoke-Git -GitArgs @("remote","get-url","origin")).Text
if ($origin -ne $ExpectedOrigin) { Fail "Unexpected LP origin: $origin" }

$branch = (Invoke-Git -GitArgs @("branch","--show-current")).Text
$localHeadBefore = (Invoke-Git -GitArgs @("rev-parse","HEAD")).Text.ToLowerInvariant()
$statusBefore = @((Invoke-Git -GitArgs @("status","--porcelain=v1","--untracked-files=all")).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$trackedDirty = @($statusBefore | Where-Object { -not $_.StartsWith('?? ') })
if ($trackedDirty.Count -ne 0) {
    Fail ("Tracked/staged worktree changes make residue dependency evidence unstable. Audit stopped.`n{0}" -f ($trackedDirty -join "`n"))
}

$remoteLine = (& $GitExe -C $RepoRoot ls-remote origin $PreviewRef 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteLine)) { Fail "Unable to read current Preview ref." }
$previewSha = ($remoteLine -split "\s+")[0].ToLowerInvariant()
if ($previewSha -notmatch '^[0-9a-f]{40}$') { Fail "Invalid Preview SHA: $previewSha" }

$trackedCurrent = @((Invoke-Git -GitArgs @("ls-files")).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$trackedSet = @{}
foreach ($p in $trackedCurrent) { $trackedSet[$p.Replace('\','/')] = $true }

$rootFiles = @(Get-ChildItem -LiteralPath $RepoRoot -File -Force)
$candidates = New-Object System.Collections.ArrayList
foreach ($f in $rootFiles) {
    $rel = $f.Name.Replace('\','/')
    if (-not $trackedSet.ContainsKey($rel)) { [void]$candidates.Add($f) }
}

if ($candidates.Count -ne $ExpectedCandidateCount) {
    $names = @($candidates | ForEach-Object { $_.Name })
    Fail ("Expected exactly {0} untracked/ignored LP root files, found {1}. No classification report written.`nCandidates:`n{2}" -f $ExpectedCandidateCount,$candidates.Count,($names -join "`n"))
}

$report = New-Object System.Collections.Generic.List[string]
$report.Add("AUDIT`tLP_ROOT_RESIDUE_READ_ONLY")
$report.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
$report.Add("REPOSITORY`t$RepoRoot")
$report.Add("LOCAL_BRANCH`t$branch")
$report.Add("LOCAL_HEAD_BEFORE`t$localHeadBefore")
$report.Add("PREVIEW_REF`t$PreviewRef")
$report.Add("PREVIEW_REMOTE_HEAD`t$previewSha")
$report.Add("LOCAL_STATUS_COUNT_BEFORE`t$($statusBefore.Count)")
$report.Add("ROOT_FILE_COUNT`t$($rootFiles.Count)")
$report.Add("ROOT_RESIDUE_CANDIDATE_COUNT`t$($candidates.Count)")
$report.Add("READ_ONLY`tYES")
$report.Add("REPOSITORY_MUTATION`tNO")
$report.Add("TARGET_FILE_MUTATION`tNO")
$report.Add("COMMIT_PUSH`tNO")
$report.Add("")

foreach ($f in @($candidates | Sort-Object Name)) {
    $name = $f.Name
    $rel = $name.Replace('\','/')
    $sha = Get-Sha256 -Path $f.FullName
    $ignore = Get-IgnoreState -RelativePath $rel
    $previewPresence = Get-PreviewPresence -PreviewSha $previewSha -RelativePath $rel
    $history = @((Invoke-Git -GitArgs @("log","--all","--follow","--date=iso-strict","--format=%H`t%ad`t%s","--",$rel) -AllowFailure $true).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $refs = @(Get-TrackedReferences -Needle $name -TrackedFiles $trackedCurrent)

    $contentKind = 'BINARY_OR_UNKNOWN'
    $textEvidence = $null
    $zipEvidence = $null
    $ext = [IO.Path]::GetExtension($name).ToLowerInvariant()
    if ($ext -eq '.zip') {
        try {
            $zipEvidence = Get-ZipEvidence -Path $f.FullName
            $contentKind = 'ZIP_ARCHIVE'
        } catch {
            $contentKind = 'ZIP_ARCHIVE_UNREADABLE'
        }
    } elseif (Test-LikelyText -Path $f.FullName) {
        try {
            $textEvidence = Read-TextEvidence -Path $f.FullName
            $contentKind = 'TEXT'
        } catch {
            $contentKind = 'TEXT_UNREADABLE'
        }
    }
    $suggested = Get-SuggestedClassification -ContentKind $contentKind -ReferenceCount $refs.Count -TextEvidence $textEvidence -ZipEvidence $zipEvidence -HistoryCount $history.Count

    $report.Add("============================================================================")
    $report.Add("FILE`t$name")
    $report.Add("FULL_PATH`t$($f.FullName)")
    $report.Add("SIZE_BYTES`t$($f.Length)")
    $report.Add("LAST_WRITE`t$($f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz'))")
    $report.Add("SHA256`t$sha")
    $report.Add("TRACKED_CURRENT_WORKTREE_INDEX`tNO")
    $report.Add("IGNORE_STATE`t$ignore")
    $report.Add("CURRENT_PREVIEW_PATH_PRESENT`t$previewPresence")
    $report.Add("GIT_HISTORY_COUNT`t$($history.Count)")
    foreach ($h in @($history | Select-Object -First 40)) { $report.Add("GIT_HISTORY`t$h") }
    $report.Add("TRACKED_LITERAL_REFERENCE_COUNT`t$($refs.Count)")
    foreach ($r in $refs) { $report.Add("TRACKED_LITERAL_REFERENCE`t$r") }
    $report.Add("CONTENT_KIND`t$contentKind")

    if ($contentKind -eq 'ZIP_ARCHIVE' -and $null -ne $zipEvidence) {
        $report.Add("ZIP_ENTRY_COUNT`t$($zipEvidence.EntryCount)")
        $report.Add("ZIP_UNCOMPRESSED_BYTES`t$($zipEvidence.UncompressedBytes)")
        $report.Add("ZIP_LP_CORE_REPO_SIGNAL_COUNT`t$($zipEvidence.CoreRepoSignals)")
        foreach ($s in $zipEvidence.Sample) { $report.Add("ZIP_ENTRY_SAMPLE`t$s") }
    }
    if ($contentKind -eq 'TEXT' -and $null -ne $textEvidence) {
        $report.Add("TEXT_LINE_COUNT`t$($textEvidence.LineCount)")
        $report.Add("TEXT_CHAR_COUNT`t$($textEvidence.CharCount)")
        $report.Add("CONVERSATION_SIGNAL_LINES`t$($textEvidence.ConversationSignals)")
        $report.Add("SCRATCH_SIGNAL_LINES`t$($textEvidence.ScratchSignals)")
        $report.Add("SECRET_PATTERN_SIGNAL_LINES`t$($textEvidence.SecretSignals)")
        $report.Add("BACKUP_RECOVERY_SIGNAL_LINES`t$($textEvidence.BackupSignals)")
        $report.Add("BUILD_GENERATION_SIGNAL_LINES`t$($textEvidence.BuildSignals)")
        $report.Add("DEVELOPER_TOOL_SIGNAL_LINES`t$($textEvidence.ToolSignals)")
        foreach ($l in $textEvidence.FirstLines) { $report.Add("TEXT_FIRST`t$(Protect-EvidenceLine -Line $l)") }
        foreach ($l in $textEvidence.LastLines) { $report.Add("TEXT_LAST`t$(Protect-EvidenceLine -Line $l)") }
    }
    $report.Add("SUGGESTED_CLASSIFICATION`t$suggested")
    $report.Add("FINAL_CLASSIFICATION`tHOLD_PENDING_ORCHESTRATION_REVIEW")
    $report.Add("")
}

$localHeadAfter = (Invoke-Git -GitArgs @("rev-parse","HEAD")).Text.ToLowerInvariant()
$statusAfter = @((Invoke-Git -GitArgs @("status","--porcelain=v1","--untracked-files=all")).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($localHeadAfter -ne $localHeadBefore) { Fail "Repository HEAD changed during read-only audit." }
if (($statusAfter -join "`n") -cne ($statusBefore -join "`n")) { Fail "Repository status changed during read-only audit." }

$report.Add("[AUDIT_BOUNDARY]")
$report.Add("NOTE`tCandidate selection is physical LP repository root files not tracked by the current local Git index. Exactly 12 are required; otherwise the utility stops without writing the classification report.")
$report.Add("NOTE`tCurrent Preview ref is read with git ls-remote without fetch or ref mutation.")
$report.Add("NOTE`tNo filename alone authorizes deletion or movement. Suggested classification is evidence-based and remains HOLD until orchestration review.")
$report.Add("NOTE`tZIP contents are inventoried without extraction. Text files are fully read for signal analysis; emitted first/last evidence is bounded and sensitive-looking lines are redacted.")
$report.Add("NOTE`tGit history and literal references are evidence only; zero references does not by itself prove obsolescence.")
$report.Add("LOCAL_HEAD_AFTER`t$localHeadAfter")
$report.Add("LOCAL_STATUS_COUNT_AFTER`t$($statusAfter.Count)")
$report.Add("READ_ONLY`tYES")
$report.Add("REPOSITORY_MUTATION`tNO")
$report.Add("TARGET_FILE_MUTATION`tNO")
$report.Add("COMMIT_PUSH`tNO")

$report | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "AUDIT=LP_ROOT_RESIDUE_READ_ONLY"
Write-Host "ROOT_RESIDUE_CANDIDATE_COUNT=$($candidates.Count)"
Write-Host "LOCAL_BRANCH=$branch"
Write-Host "LOCAL_HEAD=$localHeadBefore"
Write-Host "PREVIEW_REMOTE_HEAD=$previewSha"
Write-Host "READ_ONLY=YES"
Write-Host "REPOSITORY_MUTATION=NO"
Write-Host "TARGET_FILE_MUTATION=NO"
Write-Host "COMMIT_PUSH=NO"
Write-Host "OUTPUT=$ReportPath"
