$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_step11_data_provenance_audit_{0}.txt" -f $Timestamp)

$ExpectedHead = "c5814f1fc1f0783d5d196755c4dd7f14b7cc557c"
$ExpectedOrigin = "https://github.com/shinichitaniguchibiz-byte/english-board.git"

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

function Read-Utf8Text([string]$Path) {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) {
        $text = $text.Substring(1)
    }
    return $text
}

function Normalize-Lines([string]$Text) {
    $n = $Text.Replace("`r`n", "`n").Replace("`r", "`n")
    $n = $n.Trim([char]10)
    if ([string]::IsNullOrEmpty($n)) {
        return @()
    }
    return @($n -split "`n")
}

function Extract-BacktickPayloadLines([string]$Path) {
    $text = Read-Utf8Text -Path $Path
    $first = $text.IndexOf([char]96)
    $last = $text.LastIndexOf([char]96)
    if ($first -lt 0 -or $last -le $first) {
        Fail "Backtick payload not found: $Path"
    }
    $payload = $text.Substring($first + 1, $last - $first - 1)
    return @(Normalize-Lines -Text $payload | Where-Object { $_ -ne "" })
}

function New-OrdinalHashtable([bool]$IgnoreCase) {
    if ($IgnoreCase) {
        return New-Object System.Collections.Hashtable ([System.StringComparer]::OrdinalIgnoreCase)
    }
    return New-Object System.Collections.Hashtable ([System.StringComparer]::Ordinal)
}

function Get-Multiset([string[]]$Lines) {
    $map = New-OrdinalHashtable -IgnoreCase $false
    foreach ($line in $Lines) {
        if ($map.ContainsKey($line)) {
            $map[$line] = [int]$map[$line] + 1
        } else {
            $map[$line] = 1
        }
    }
    return $map
}

function Get-MultisetDiff([System.Collections.Hashtable]$Left, [System.Collections.Hashtable]$Right) {
    $result = New-Object System.Collections.ArrayList
    foreach ($key in $Left.Keys) {
        $leftCount = [int]$Left[$key]
        $rightCount = 0
        if ($Right.ContainsKey($key)) {
            $rightCount = [int]$Right[$key]
        }
        if ($leftCount -gt $rightCount) {
            for ($i = 0; $i -lt ($leftCount - $rightCount); $i++) {
                [void]$result.Add([string]$key)
            }
        }
    }
    return @($result)
}

function Build-LevelMap([string[]]$Lines, [int]$WordIndex, [int]$LevelIndex) {
    $map = New-OrdinalHashtable -IgnoreCase $true
    foreach ($line in $Lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split "`t"
        if ($parts.Count -le [Math]::Max($WordIndex, $LevelIndex)) { continue }
        $word = $parts[$WordIndex].Trim()
        $level = $parts[$LevelIndex].Trim()
        if ([string]::IsNullOrWhiteSpace($word) -or [string]::IsNullOrWhiteSpace($level)) { continue }
        if (-not $map.ContainsKey($word)) {
            $map[$word] = New-OrdinalHashtable -IgnoreCase $true
        }
        $map[$word][$level] = $true
    }
    return $map
}

function Measure-ReferenceCoverage(
    [string[]]$Rows,
    [int]$WordIndex,
    [int]$LevelIndex,
    [System.Collections.Hashtable]$ReferenceMap
) {
    $stats = [ordered]@{
        NONBLANK = 0
        MATCH = 0
        CONFLICT = 0
        NO_WORD = 0
        BLANK = 0
    }
    foreach ($line in $Rows) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split "`t"
        if ($parts.Count -le [Math]::Max($WordIndex, $LevelIndex)) { continue }
        $word = $parts[$WordIndex].Trim()
        $level = $parts[$LevelIndex].Trim()
        if ([string]::IsNullOrWhiteSpace($level)) {
            $stats.BLANK++
            continue
        }
        $stats.NONBLANK++
        if (-not $ReferenceMap.ContainsKey($word)) {
            $stats.NO_WORD++
        } elseif ($ReferenceMap[$word].ContainsKey($level)) {
            $stats.MATCH++
        } else {
            $stats.CONFLICT++
        }
    }
    return [pscustomobject]$stats
}

function Find-LiteralReferences([string[]]$TrackedPaths, [string[]]$Needles) {
    $hits = New-Object System.Collections.ArrayList
    foreach ($relative in $TrackedPaths) {
        if ($relative -in @("CEFR.txt", "LEAP.txt", "SVL.txt")) { continue }
        $full = Join-Path $script:Root $relative
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        try {
            $text = Read-Utf8Text -Path $full
        } catch {
            continue
        }
        $lines = Normalize-Lines -Text $text
        for ($lineNo = 0; $lineNo -lt $lines.Count; $lineNo++) {
            foreach ($needle in $Needles) {
                if ($lines[$lineNo].IndexOf($needle, [System.StringComparison]::Ordinal) -ge 0) {
                    $snippet = $lines[$lineNo].Trim()
                    if ($snippet.Length -gt 260) { $snippet = $snippet.Substring(0, 260) + "..." }
                    [void]$hits.Add([pscustomobject]@{
                        PATH = $relative
                        LINE = $lineNo + 1
                        NEEDLE = $needle
                        TEXT = $snippet
                    })
                }
            }
        }
    }
    return @($hits)
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    Fail "Git repository is missing: $Root"
}

$branch = Get-GitText -GitArgs @("symbolic-ref", "--short", "HEAD")
if ($branch -ne "main") { Fail "Unexpected branch: $branch" }

$head = Get-GitText -GitArgs @("rev-parse", "HEAD")
if ($head -ne $ExpectedHead) { Fail "Unexpected local HEAD: expected=$ExpectedHead actual=$head" }

$origin = Get-GitText -GitArgs @("remote", "get-url", "origin")
if ($origin -ne $ExpectedOrigin) { Fail "Unexpected origin: $origin" }

$remoteLine = (& $GitExe -C $Root ls-remote origin refs/heads/main 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($remoteLine)) { Fail "Unable to read remote main." }
$remoteHead = ($remoteLine -split "\s+")[0]
if ($remoteHead -ne $ExpectedHead) { Fail "Unexpected remote HEAD: expected=$ExpectedHead actual=$remoteHead" }

$status = @(Invoke-GitNative -GitArgs @("status", "--porcelain") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($status.Count -ne 0) { Fail "Worktree must be clean for provenance audit.`n$($status -join "`n")" }

$required = @("CEFR.txt", "LEAP.txt", "SVL.txt", "scripts/words-leap.js", "scripts/words-teppeki.js")
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $relative) -PathType Leaf)) {
        Fail "Required file is missing: $relative"
    }
}

Write-Host "PHASE=PATH_REFERENCE_AUDIT"
$trackedPaths = @(Invoke-GitNative -GitArgs @("ls-files") | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$literalNeedles = @("CEFR.txt", "LEAP.txt", "SVL.txt")
$literalRefs = @(Find-LiteralReferences -TrackedPaths $trackedPaths -Needles $literalNeedles)
$runtimeLiteralRefs = @($literalRefs | Where-Object { $_.PATH -eq "English_Reading.html" -or $_.PATH.StartsWith("scripts/") -or $_.PATH.StartsWith("icon/") })

Write-Host "PHASE=GIT_HISTORY_AUDIT"
$histories = [ordered]@{}
foreach ($name in @("CEFR.txt", "LEAP.txt", "SVL.txt")) {
    $histories[$name] = @(Invoke-GitNative -GitArgs @("log", "--follow", "--date=iso-strict", "--format=%H`t%ad`t%s", "--", $name))
}

Write-Host "PHASE=LEAP_FULL_DATASET_COMPARE"
$leapTextLines = @(Normalize-Lines -Text (Read-Utf8Text -Path (Join-Path $Root "LEAP.txt")) | Where-Object { $_ -ne "" })
if ($leapTextLines.Count -lt 2) { Fail "LEAP.txt is unexpectedly short." }
$leapHeader = $leapTextLines[0]
$leapRows = @($leapTextLines | Select-Object -Skip 1)
$leapRuntimeRows = @(Extract-BacktickPayloadLines -Path (Join-Path $Root "scripts\words-leap.js"))

$sourceBadColumns = @($leapRows | Where-Object { ($_ -split "`t").Count -ne 7 })
$runtimeBadColumns = @($leapRuntimeRows | Where-Object { ($_ -split "`t").Count -ne 7 })
$sourceMap = Get-Multiset -Lines $leapRows
$runtimeMap = Get-Multiset -Lines $leapRuntimeRows
$sourceOnly = @(Get-MultisetDiff -Left $sourceMap -Right $runtimeMap)
$runtimeOnly = @(Get-MultisetDiff -Left $runtimeMap -Right $sourceMap)
$multisetMatch = ($sourceOnly.Count -eq 0 -and $runtimeOnly.Count -eq 0)
$orderMatch = (($leapRows -join "`n") -ceq ($leapRuntimeRows -join "`n"))

Write-Host "PHASE=REFERENCE_ROLE_AUDIT"
$cefrLines = @(Normalize-Lines -Text (Read-Utf8Text -Path (Join-Path $Root "CEFR.txt")) | Where-Object { $_ -ne "" })
$svlLines = @(Normalize-Lines -Text (Read-Utf8Text -Path (Join-Path $Root "SVL.txt")) | Where-Object { $_ -ne "" })
$teppekiRows = @(Extract-BacktickPayloadLines -Path (Join-Path $Root "scripts\words-teppeki.js"))

$cefrMap = Build-LevelMap -Lines $cefrLines -WordIndex 0 -LevelIndex 2
$svlMap = Build-LevelMap -Lines $svlLines -WordIndex 0 -LevelIndex 2

$leapCefr = Measure-ReferenceCoverage -Rows $leapRows -WordIndex 0 -LevelIndex 3 -ReferenceMap $cefrMap
$leapSvl = Measure-ReferenceCoverage -Rows $leapRows -WordIndex 0 -LevelIndex 4 -ReferenceMap $svlMap
$teppekiCefr = Measure-ReferenceCoverage -Rows $teppekiRows -WordIndex 0 -LevelIndex 4 -ReferenceMap $cefrMap
$teppekiSvl = Measure-ReferenceCoverage -Rows $teppekiRows -WordIndex 0 -LevelIndex 5 -ReferenceMap $svlMap

$semanticNeedles = @("words-leap.js", "words-teppeki.js", "CEFR", "SVL")
$semanticRefs = @(Find-LiteralReferences -TrackedPaths $trackedPaths -Needles $semanticNeedles | Where-Object { $_.PATH -notin @("CEFR.txt", "LEAP.txt", "SVL.txt", "scripts/words-leap.js", "scripts/words-teppeki.js") })

$report = New-Object System.Collections.Generic.List[string]
$report.Add("STEP`t11_DATA_PROVENANCE_AUDIT_READONLY")
$report.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$report.Add("HEAD`t$head")
$report.Add("REMOTE_HEAD`t$remoteHead")
$report.Add("WORKTREE_CLEAN`tYES")
$report.Add("")
$report.Add("[PATH_REFERENCE_AUDIT]")
$report.Add("LITERAL_REFERENCE_COUNT`t$($literalRefs.Count)")
$report.Add("RUNTIME_LITERAL_REFERENCE_COUNT`t$($runtimeLiteralRefs.Count)")
foreach ($hit in $literalRefs) {
    $report.Add("REF`t$($hit.NEEDLE)`t$($hit.PATH):$($hit.LINE)`t$($hit.TEXT)")
}
$report.Add("")
$report.Add("[GIT_HISTORY_AUDIT]")
foreach ($name in @("CEFR.txt", "LEAP.txt", "SVL.txt")) {
    $entries = @($histories[$name] | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $report.Add("HISTORY_COUNT`t$name`t$($entries.Count)")
    foreach ($entry in $entries) { $report.Add("HISTORY`t$name`t$entry") }
}
$report.Add("")
$report.Add("[LEAP_FULL_DATASET_COMPARE]")
$report.Add("LEAP_HEADER`t$leapHeader")
$report.Add("LEAP_SOURCE_ROWS`t$($leapRows.Count)")
$report.Add("LEAP_RUNTIME_ROWS`t$($leapRuntimeRows.Count)")
$report.Add("LEAP_SOURCE_UNIQUE_ROWS`t$($sourceMap.Count)")
$report.Add("LEAP_RUNTIME_UNIQUE_ROWS`t$($runtimeMap.Count)")
$report.Add("LEAP_SOURCE_BAD_COLUMN_ROWS`t$($sourceBadColumns.Count)")
$report.Add("LEAP_RUNTIME_BAD_COLUMN_ROWS`t$($runtimeBadColumns.Count)")
$report.Add("LEAP_MULTISET_MATCH`t$(if ($multisetMatch) { 'YES' } else { 'NO' })")
$report.Add("LEAP_ORDER_MATCH`t$(if ($orderMatch) { 'YES' } else { 'NO' })")
$report.Add("LEAP_SOURCE_ONLY_ROWS`t$($sourceOnly.Count)")
$report.Add("LEAP_RUNTIME_ONLY_ROWS`t$($runtimeOnly.Count)")
foreach ($line in @($sourceOnly | Select-Object -First 20)) { $report.Add("SOURCE_ONLY`t$line") }
foreach ($line in @($runtimeOnly | Select-Object -First 20)) { $report.Add("RUNTIME_ONLY`t$line") }
$report.Add("")
$report.Add("[REFERENCE_ROLE_AUDIT]")
$report.Add("CEFR_REFERENCE_ROWS`t$($cefrLines.Count)")
$report.Add("SVL_REFERENCE_ROWS`t$($svlLines.Count)")
$report.Add("TEPPEKI_RUNTIME_ROWS`t$($teppekiRows.Count)")
$report.Add("LEAP_CEFR_NONBLANK`t$($leapCefr.NONBLANK)")
$report.Add("LEAP_CEFR_MATCH`t$($leapCefr.MATCH)")
$report.Add("LEAP_CEFR_CONFLICT`t$($leapCefr.CONFLICT)")
$report.Add("LEAP_CEFR_NO_WORD`t$($leapCefr.NO_WORD)")
$report.Add("LEAP_CEFR_BLANK`t$($leapCefr.BLANK)")
$report.Add("LEAP_SVL_NONBLANK`t$($leapSvl.NONBLANK)")
$report.Add("LEAP_SVL_MATCH`t$($leapSvl.MATCH)")
$report.Add("LEAP_SVL_CONFLICT`t$($leapSvl.CONFLICT)")
$report.Add("LEAP_SVL_NO_WORD`t$($leapSvl.NO_WORD)")
$report.Add("LEAP_SVL_BLANK`t$($leapSvl.BLANK)")
$report.Add("TEPPEKI_CEFR_NONBLANK`t$($teppekiCefr.NONBLANK)")
$report.Add("TEPPEKI_CEFR_MATCH`t$($teppekiCefr.MATCH)")
$report.Add("TEPPEKI_CEFR_CONFLICT`t$($teppekiCefr.CONFLICT)")
$report.Add("TEPPEKI_CEFR_NO_WORD`t$($teppekiCefr.NO_WORD)")
$report.Add("TEPPEKI_CEFR_BLANK`t$($teppekiCefr.BLANK)")
$report.Add("TEPPEKI_SVL_NONBLANK`t$($teppekiSvl.NONBLANK)")
$report.Add("TEPPEKI_SVL_MATCH`t$($teppekiSvl.MATCH)")
$report.Add("TEPPEKI_SVL_CONFLICT`t$($teppekiSvl.CONFLICT)")
$report.Add("TEPPEKI_SVL_NO_WORD`t$($teppekiSvl.NO_WORD)")
$report.Add("TEPPEKI_SVL_BLANK`t$($teppekiSvl.BLANK)")
$report.Add("")
$report.Add("[SEMANTIC_REFERENCE_SCAN]")
$report.Add("SEMANTIC_REFERENCE_COUNT`t$($semanticRefs.Count)")
foreach ($hit in @($semanticRefs | Select-Object -First 200)) {
    $report.Add("SEMANTIC_REF`t$($hit.NEEDLE)`t$($hit.PATH):$($hit.LINE)`t$($hit.TEXT)")
}
$report.Add("")
$report.Add("[INTERPRETATION_BOUNDARY]")
$report.Add("NOTE`tMultiset equality proves dataset equality ignoring row order; it does not by itself prove which file generated the other.")
$report.Add("NOTE`tReference coverage shows consistency of CEFR/SVL values by word; it does not by itself prove causal generation history.")
$report.Add("NOTE`tLiteral path references are scanned across all currently tracked files except the three target data files themselves.")
$report.Add("READ_ONLY`tYES")

$report | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=11_DATA_PROVENANCE_AUDIT_READONLY"
Write-Host "HEAD=$head"
Write-Host "RUNTIME_LITERAL_REFERENCE_COUNT=$($runtimeLiteralRefs.Count)"
Write-Host "ALL_LITERAL_REFERENCE_COUNT=$($literalRefs.Count)"
Write-Host "LEAP_SOURCE_ROWS=$($leapRows.Count)"
Write-Host "LEAP_RUNTIME_ROWS=$($leapRuntimeRows.Count)"
Write-Host "LEAP_MULTISET_MATCH=$(if ($multisetMatch) { 'YES' } else { 'NO' })"
Write-Host "LEAP_ORDER_MATCH=$(if ($orderMatch) { 'YES' } else { 'NO' })"
Write-Host "LEAP_SOURCE_ONLY_ROWS=$($sourceOnly.Count)"
Write-Host "LEAP_RUNTIME_ONLY_ROWS=$($runtimeOnly.Count)"
Write-Host "LEAP_CEFR_MATCH=$($leapCefr.MATCH)/$($leapCefr.NONBLANK)"
Write-Host "LEAP_SVL_MATCH=$($leapSvl.MATCH)/$($leapSvl.NONBLANK)"
Write-Host "TEPPEKI_CEFR_MATCH=$($teppekiCefr.MATCH)/$($teppekiCefr.NONBLANK)"
Write-Host "TEPPEKI_SVL_MATCH=$($teppekiSvl.MATCH)/$($teppekiSvl.NONBLANK)"
Write-Host "READ_ONLY=YES"
Write-Host "OUTPUT=$ReportPath"
