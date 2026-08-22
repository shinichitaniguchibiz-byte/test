$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = "C:\OneDrive2\OneDrive\lib\codex\learning-platform"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("lp_root_residue_audit_{0}.txt" -f $Timestamp)
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
    foreach ($line in $lines) {
        if ($line -match '(?i)思考時間|ChatGPT|Codex|引き継ぎ|次のチャット|ユーザー|assistant|user:|assistant:') { $conversationSignals++ }
        if ($line -match '(?i)clipboard|scratch|temporary|temp|貼り付け|会話|履歴') { $scratchSignals++ }
        if ($line -match '(?i)(service[_ -]?role|client[_ -]?secret|secret[_ -]?key|access[_ -]?token|refresh[_ -]?token|password\s*[:=]|api[_ -]?key\s*[:=])') { $secretSignals++ }
    }
    $first = @($lines | Select-Object -First 12)
    $last = @($lines | Select-Object -Last 12)
    [pscustomobject]@{
        LineCount=$lines.Count
        CharCount=$text.Length
        ConversationSignals=$conversationSignals
        ScratchSignals=$scratchSignals
        SecretSignals=$secretSignals
        FirstLines=$first
        LastLines=$last
    }
}

function Get-ZipEvidence([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entries = @($zip.Entries)
        $total = [long]0
        foreach ($e in $entries) { $total += [long]$e.Length }
        $sample = @($entries | Select-Object -First 80 | ForEach-Object { "{0}`t{1}" -f $_.FullName,$_.Length })
        [pscustomobject]@{ EntryCount=$entries.Count; UncompressedBytes=$total; Sample=$sample }
    } finally { $zip.Dispose() }
}

function Get-TrackedReferences([string]$Needle, [string[]]$TrackedFiles) {
    $hits = New-Object System.Collections.ArrayList
    foreach ($rel in $TrackedFiles) {
        $full = Join-Path $script:RepoRoot $rel
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        if (-not (Test-LikelyText -Path $full)) { continue }
        try {
            $text = [IO.File]::ReadAllText($full)
        } catch { continue }
        if ($text.IndexOf($Needle,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
            [void]$hits.Add($rel)
        }
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

if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) { Fail "LP repository root missing: $RepoRoot" }
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git"))) { Fail "LP .git missing: $RepoRoot" }
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$origin = (Invoke-Git -GitArgs @("remote","get-url","origin")).Text
if ($origin -ne $ExpectedOrigin) { Fail "Unexpected LP origin: $origin" }

$branch = (Invoke-Git -GitArgs @("branch","--show-current")).Text
$localHead = (Invoke-Git -GitArgs @("rev-parse","HEAD")).Text.ToLowerInvariant()
$status = @(Invoke-Git -GitArgs @("status","--porcelain=v1","--untracked-files=all")).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

$remoteLine = (& $GitExe -C $RepoRoot ls-remote origin $PreviewRef 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteLine)) { Fail "Unable to read current Preview ref." }
$previewSha = ($remoteLine -split "\s+")[0].ToLowerInvariant()
if ($previewSha -notmatch '^[0-9a-f]{40}$') { Fail "Invalid Preview SHA: $previewSha" }

$trackedCurrent = @((Invoke-Git -GitArgs @("ls-files")).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$trackedSet = @{}
foreach ($p in $trackedCurrent) { $trackedSet[$p.Replace('\','/')] = $true }

$rootFiles = @(Get-ChildItem -LiteralPath $RepoRoot -File -Force | Where-Object { $_.Name -ne '.git' })
$candidates = New-Object System.Collections.ArrayList
foreach ($f in $rootFiles) {
    $rel = $f.Name.Replace('\','/')
    if (-not $trackedSet.ContainsKey($rel)) { [void]$candidates.Add($f) }
}

if ($candidates.Count -ne $ExpectedCandidateCount) {
    $names = @($candidates | ForEach-Object { $_.Name })
    Fail ("Expected exactly {0} untracked/ignored LP root files, found {1}. No audit classification performed.`nCandidates:`n{2}" -f $ExpectedCandidateCount,$candidates.Count,($names -join "`n"))
}

$report = New-Object System.Collections.Generic.List[string]
$report.Add("AUDIT`tLP_ROOT_RESIDUE_READ_ONLY")
$report.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')")
$report.Add("REPOSITORY`t$RepoRoot")
$report.Add("LOCAL_BRANCH`t$branch")
$report.Add("LOCAL_HEAD`t$localHead")
$report.Add("PREVIEW_REF`t$PreviewRef")
$report.Add("PREVIEW_REMOTE_HEAD`t$previewSha")
$report.Add("LOCAL_STATUS_COUNT`t$($status.Count)")
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
    $trackedNow = if ($trackedSet.ContainsKey($rel)) { 'YES' } else { 'NO' }
    $previewPresence = Get-PreviewPresence -PreviewSha $previewSha -RelativePath $rel
    $history = @(Invoke-Git -GitArgs @("log","--all","--follow","--date=iso-strict","--format=%H`t%ad`t%s","--",$rel) -AllowFailure $true).Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $refs = @(Get-TrackedReferences -Needle $name -TrackedFiles $trackedCurrent)

    $report.Add("============================================================================")
    $report.Add("FILE`t$name")
    $report.Add("FULL_PATH`t$($f.FullName)")
    $report.Add("SIZE_BYTES`t$($f.Length)")
    $report.Add("LAST_WRITE`t$($f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss zzz'))")
    $report.Add("SHA256`t$sha")
    $report.Add("TRACKED_CURRENT_WORKTREE_INDEX`t$trackedNow")
    $report.Add("IGNORE_STATE`t$ignore")
    $report.Add("CURRENT_PREVIEW_PATH_PRESENT`t$previewPresence")
    $report.Add("GIT_HISTORY_COUNT`t$($history.Count)")
    foreach ($h in @($history | Select-Object -First 40)) { $report.Add("GIT_HISTORY`t$h") }
    $report.Add("TRACKED_LITERAL_REFERENCE_COUNT`t$($refs.Count)")
    foreach ($r in $refs) { $report.Add("TRACKED_LITERAL_REFERENCE`t$r") }

    $ext = [IO.Path]::GetExtension($name).ToLowerInvariant()
    if ($ext -eq '.zip') {
        try {
            $z = Get-ZipEvidence -Path $f.FullName
            $report.Add("CONTENT_KIND`tZIP_ARCHIVE")
            $report.Add("ZIP_ENTRY_COUNT`t$($z.EntryCount)")
            $report.Add("ZIP_UNCOMPRESSED_BYTES`t$($z.UncompressedBytes)")
            foreach ($s in $z.Sample) { $report.Add("ZIP_ENTRY_SAMPLE`t$s") }
        } catch {
            $report.Add("CONTENT_KIND`tZIP_ARCHIVE_UNREADABLE")
            $report.Add("CONTENT_ERROR`t$($_.Exception.Message)")
        }
    } elseif (Test-LikelyText -Path $f.FullName) {
        try {
            $t = Read-TextEvidence -Path $f.FullName
            $report.Add("CONTENT_KIND`tTEXT")
            $report.Add("TEXT_LINE_COUNT`t$($t.LineCount)")
            $report.Add("TEXT_CHAR_COUNT`t$($t.CharCount)")
            $report.Add("CONVERSATION_SIGNAL_LINES`t$($t.ConversationSignals)")
            $report.Add("SCRATCH_SIGNAL_LINES`t$($t.ScratchSignals)")
            $report.Add("SECRET_PATTERN_SIGNAL_LINES`t$($t.SecretSignals)")
            foreach ($l in $t.FirstLines) { $report.Add("TEXT_FIRST`t$($l -replace "`t",'    ')") }
            foreach ($l in $t.LastLines) { $report.Add("TEXT_LAST`t$($l -replace "`t",'    ')") }
        } catch {
            $report.Add("CONTENT_KIND`tTEXT_UNREADABLE")
            $report.Add("CONTENT_ERROR`t$($_.Exception.Message)")
        }
    } else {
        $report.Add("CONTENT_KIND`tBINARY_OR_UNKNOWN")
    }

    $report.Add("CLASSIFICATION`tHOLD")
    $report.Add("CLASSIFICATION_NOTE`tEvidence only. Orchestration must classify as runtime/build-generation/source-data/developer-document/developer-tool/legacy-recovery/backup/transient-obsolete/HOLD after review.")
    $report.Add("")
}

$report.Add("[AUDIT_BOUNDARY]")
$report.Add("NOTE`tCandidate selection is physical root-level files not tracked by the current local Git index; exactly 12 are required or the utility stops.")
$report.Add("NOTE`tCurrent Preview presence is checked against the remote Preview SHA when that commit object exists locally; otherwise it is reported UNKNOWN_OBJECT_NOT_LOCAL.")
$report.Add("NOTE`tNo filename alone authorizes deletion or movement.")
$report.Add("NOTE`tZIP contents are inventoried without extraction. Text content is fully read for signal analysis but only bounded first/last evidence is emitted to avoid reproducing secrets or huge chat histories.")
$report.Add("NOTE`tGit history and literal references are evidence only; zero references does not by itself prove obsolescence.")
$report.Add("READ_ONLY`tYES")

$report | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "AUDIT=LP_ROOT_RESIDUE_READ_ONLY"
Write-Host "ROOT_RESIDUE_CANDIDATE_COUNT=$($candidates.Count)"
Write-Host "LOCAL_BRANCH=$branch"
Write-Host "LOCAL_HEAD=$localHead"
Write-Host "PREVIEW_REMOTE_HEAD=$previewSha"
Write-Host "READ_ONLY=YES"
Write-Host "REPOSITORY_MUTATION=NO"
Write-Host "TARGET_FILE_MUTATION=NO"
Write-Host "COMMIT_PUSH=NO"
Write-Host "OUTPUT=$ReportPath"
