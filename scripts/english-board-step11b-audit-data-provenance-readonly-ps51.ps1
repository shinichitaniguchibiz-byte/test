$ErrorActionPreference = "Stop"

$SourceUrl = "https://raw.githubusercontent.com/shinichitaniguchibiz-byte/test/987c2a7cf4a3cfe6394013adb87646b0dc876495/scripts/english-board-step11-audit-data-provenance-readonly.ps1"
$SourcePath = Join-Path $env:TEMP "english-board-step11-source.ps1"
$PatchedPath = Join-Path $env:TEMP "english-board-step11b-patched.ps1"

$Old = @'
        $lines = Normalize-Lines -Text $text
        for ($lineNo = 0; $lineNo -lt $lines.Count; $lineNo++) {
            foreach ($needle in $Needles) {
                if ($lines[$lineNo].IndexOf($needle, [System.StringComparison]::Ordinal) -ge 0) {
                    $snippet = $lines[$lineNo].Trim()
                    if ($snippet.Length -gt 260) { $snippet = $snippet.Substring(0, 260) + "..." }
'@

$New = @'
        $lines = @(Normalize-Lines -Text $text)
        for ($lineNo = 0; $lineNo -lt $lines.Count; $lineNo++) {
            $lineText = [string]$lines[$lineNo]
            foreach ($needle in $Needles) {
                if ($lineText.IndexOf($needle, [System.StringComparison]::Ordinal) -ge 0) {
                    $snippet = $lineText.Trim()
                    if ($snippet.Length -gt 260) { $snippet = $snippet.Substring(0, 260) + "..." }
'@

try {
    Invoke-WebRequest -UseBasicParsing -Uri $SourceUrl -OutFile $SourcePath -ErrorAction Stop
    $Text = [System.IO.File]::ReadAllText($SourcePath, [System.Text.Encoding]::UTF8)
    if (-not $Text.Contains($Old)) {
        throw "Expected Step 11 scan block was not found; refusing to patch a different source."
    }
    $Patched = $Text.Replace($Old, $New)
    if ($Patched -eq $Text) {
        throw "Step 11 patch produced no change."
    }
    [System.IO.File]::WriteAllText($PatchedPath, $Patched, (New-Object System.Text.UTF8Encoding($false)))
    & powershell.exe -NoProfile -File $PatchedPath
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "patched Step 11 audit failed: exit=$ExitCode"
    }
} finally {
    Remove-Item -LiteralPath $SourcePath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PatchedPath -Force -ErrorAction SilentlyContinue
}
