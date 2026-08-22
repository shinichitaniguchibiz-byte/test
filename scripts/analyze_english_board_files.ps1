$ErrorActionPreference = "Stop"

$Root = (Get-Location).Path
$ExpectedRoot = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$ReportRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board\reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutputFile = Join-Path $ReportRoot ("english_board_inventory_{0}.txt" -f $Timestamp)

if ($Root -ne $ExpectedRoot) {
    throw "Run this script from $ExpectedRoot. Current=$Root"
}

New-Item -ItemType Directory -Force -Path $ReportRoot | Out-Null

$Files = Get-ChildItem -Path $Root -Recurse -File -Force | Where-Object {
    $_.FullName -notlike "$Root\.git\*"
}

function Get-RelativePath([string]$FullName) {
    return $FullName.Substring($Root.Length).TrimStart('\')
}

function Get-TopName([string]$RelativePath) {
    if ($RelativePath -match '\\') { return ($RelativePath -split '\\')[0] }
    return "(ROOT)"
}

function Get-Category([string]$RelativePath, [string]$Extension, [string]$Name) {
    $p = $RelativePath.Replace('/', '\')
    $e = $Extension.ToLowerInvariant()

    if ($p -like "audio\*") { return "IGNORE_MEDIA_AUDIO" }
    if ($p -like "image\*") { return "IGNORE_MEDIA_IMAGE" }
    if ($Name -like "filelists_*.txt" -or $Name -like "gitinfo_*.txt" -or $Name -like "english_board_inventory_*.txt") { return "IGNORE_GENERATED_REPORT" }
    if ($Name -match '^~\$' -or $e -in @('.tmp','.bak','.log','.zip','.xlsx','.xlsm','.xls','.pyc','.pyo','.key','.pem')) { return "IGNORE_LOCAL_OR_GENERATED" }
    if ($Name -in @('credentials.json','token.json') -or $Name -like 'client_secret*.json' -or $Name -like '.env*') { return "IGNORE_SECRET_OR_LOCAL_CONFIG" }
    if ($p -like ".venv\*" -or $p -like "venv\*" -or $p -like "env\*" -or $p -like "__pycache__\*" -or $p -like ".vscode\*" -or $p -like ".idea\*") { return "IGNORE_TOOLING_LOCAL" }
    if ($p -ieq "scripts\chatgpt-api.local.js") { return "IGNORE_SECRET_OR_LOCAL_CONFIG" }
    return "TRACK_CANDIDATE"
}

$Rows = foreach ($File in $Files) {
    $Relative = Get-RelativePath $File.FullName
    [pscustomobject]@{
        RelativePath = $Relative
        Top = Get-TopName $Relative
        Extension = if ([string]::IsNullOrWhiteSpace($File.Extension)) { "(none)" } else { $File.Extension.ToLowerInvariant() }
        SizeBytes = [int64]$File.Length
        SizeMiB = [math]::Round($File.Length / 1MB, 3)
        LastWrite = $File.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')
        Category = Get-Category $Relative $File.Extension $File.Name
    }
}

$TotalBytes = ($Rows | Measure-Object SizeBytes -Sum).Sum
$TrackRows = @($Rows | Where-Object Category -eq 'TRACK_CANDIDATE')
$IgnoreRows = @($Rows | Where-Object Category -ne 'TRACK_CANDIDATE')
$TrackBytes = ($TrackRows | Measure-Object SizeBytes -Sum).Sum
$IgnoreBytes = ($IgnoreRows | Measure-Object SizeBytes -Sum).Sum

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("MODE`tREAD_ONLY_CLASSIFICATION")
$Lines.Add("FILE_COUNT`t$($Rows.Count)")
$Lines.Add("TOTAL_BYTES`t$TotalBytes")
$Lines.Add("TOTAL_GIB`t$([math]::Round($TotalBytes / 1GB, 3))")
$Lines.Add("TRACK_CANDIDATE_COUNT`t$($TrackRows.Count)")
$Lines.Add("TRACK_CANDIDATE_MIB`t$([math]::Round($TrackBytes / 1MB, 3))")
$Lines.Add("IGNORE_CANDIDATE_COUNT`t$($IgnoreRows.Count)")
$Lines.Add("IGNORE_CANDIDATE_GIB`t$([math]::Round($IgnoreBytes / 1GB, 3))")

$Lines.Add("")
$Lines.Add("=== TOP DIRECTORY SUMMARY ===")
$Rows | Group-Object Top | ForEach-Object {
    $sum = ($_.Group | Measure-Object SizeBytes -Sum).Sum
    $max = ($_.Group | Measure-Object SizeBytes -Maximum).Maximum
    [pscustomobject]@{ Name=$_.Name; Count=$_.Count; Sum=$sum; Max=$max }
} | Sort-Object Sum -Descending | ForEach-Object {
    $Lines.Add(("{0}`tCOUNT={1}`tMIB={2}`tMAX_MIB={3}" -f $_.Name,$_.Count,[math]::Round($_.Sum/1MB,3),[math]::Round($_.Max/1MB,3)))
}

$Lines.Add("")
$Lines.Add("=== EXTENSION SUMMARY ===")
$Rows | Group-Object Extension | ForEach-Object {
    $sum = ($_.Group | Measure-Object SizeBytes -Sum).Sum
    [pscustomobject]@{ Ext=$_.Name; Count=$_.Count; Sum=$sum }
} | Sort-Object Sum -Descending | ForEach-Object {
    $Lines.Add(("{0}`tCOUNT={1}`tMIB={2}" -f $_.Ext,$_.Count,[math]::Round($_.Sum/1MB,3)))
}

$Lines.Add("")
$Lines.Add("=== CLASSIFICATION SUMMARY ===")
$Rows | Group-Object Category | ForEach-Object {
    $sum = ($_.Group | Measure-Object SizeBytes -Sum).Sum
    [pscustomobject]@{ Category=$_.Name; Count=$_.Count; Sum=$sum }
} | Sort-Object Category | ForEach-Object {
    $Lines.Add(("{0}`tCOUNT={1}`tMIB={2}" -f $_.Category,$_.Count,[math]::Round($_.Sum/1MB,3)))
}

$Lines.Add("")
$Lines.Add("=== LARGEST 100 FILES OVERALL ===")
$Rows | Sort-Object SizeBytes -Descending | Select-Object -First 100 | ForEach-Object {
    $Lines.Add(("{0}`t{1} bytes`t{2} MiB`t{3}" -f $_.RelativePath,$_.SizeBytes,$_.SizeMiB,$_.Category))
}

$Lines.Add("")
$Lines.Add("=== LARGEST TRACK CANDIDATES ===")
$TrackRows | Sort-Object SizeBytes -Descending | Select-Object -First 100 | ForEach-Object {
    $Lines.Add(("{0}`t{1} bytes`t{2} MiB" -f $_.RelativePath,$_.SizeBytes,$_.SizeMiB))
}

$Lines.Add("")
$Lines.Add("=== TRACK CANDIDATES >= 5 MiB ===")
$LargeTrack = @($TrackRows | Where-Object SizeBytes -ge 5MB | Sort-Object SizeBytes -Descending)
if ($LargeTrack.Count -eq 0) {
    $Lines.Add("NONE")
} else {
    $LargeTrack | ForEach-Object { $Lines.Add(("{0}`t{1} MiB" -f $_.RelativePath,$_.SizeMiB)) }
}

$Lines.Add("")
$Lines.Add("=== ROOT LEVEL FILES ===")
$Rows | Where-Object Top -eq '(ROOT)' | Sort-Object RelativePath | ForEach-Object {
    $Lines.Add(("{0}`t{1} bytes`t{2}" -f $_.RelativePath,$_.SizeBytes,$_.Category))
}

$Lines.Add("")
$Lines.Add("=== TRACK CANDIDATE FILE LIST ===")
$TrackRows | Sort-Object RelativePath | ForEach-Object {
    $Lines.Add(("{0}`t{1} bytes" -f $_.RelativePath,$_.SizeBytes))
}

$Lines.Add("")
$Lines.Add("=== IGNORE CANDIDATE FILE LIST ===")
$IgnoreRows | Sort-Object Category, RelativePath | ForEach-Object {
    $Lines.Add(("{0}`t{1}`t{2} bytes" -f $_.Category,$_.RelativePath,$_.SizeBytes))
}

$Lines | Set-Content -Path $OutputFile -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "SOURCE_MUTATION=NO"
Write-Host "FILE_COUNT=$($Rows.Count)"
Write-Host "TRACK_CANDIDATE_COUNT=$($TrackRows.Count)"
Write-Host "TRACK_CANDIDATE_MIB=$([math]::Round($TrackBytes / 1MB, 3))"
Write-Host "IGNORE_CANDIDATE_COUNT=$($IgnoreRows.Count)"
Write-Host "IGNORE_CANDIDATE_GIB=$([math]::Round($IgnoreBytes / 1GB, 3))"
Write-Host "OUTPUT=$OutputFile"
