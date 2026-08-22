$ErrorActionPreference = "Stop"

$ExpectedRoot = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$Root = (Get-Location).Path
$ReportRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board\reports"

if ($Root -ne $ExpectedRoot) {
    throw "Unexpected working directory. Expected: $ExpectedRoot ; Actual: $Root"
}

New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null

$ReportPatterns = @(
    "filelists_*.txt",
    "gitinfo_*.txt",
    "english_board_inventory_*.txt"
)

$Moved = @()
foreach ($Pattern in $ReportPatterns) {
    Get-ChildItem -LiteralPath $Root -Filter $Pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
        $Destination = Join-Path $ReportRoot $_.Name
        if (Test-Path -LiteralPath $Destination) {
            $Base = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
            $Ext = [System.IO.Path]::GetExtension($_.Name)
            $Destination = Join-Path $ReportRoot ("{0}_moved_{1}{2}" -f $Base, (Get-Date -Format "yyyyMMdd_HHmmss"), $Ext)
        }
        Move-Item -LiteralPath $_.FullName -Destination $Destination
        $Moved += $Destination
    }
}

$GitIgnore = @'
# ============================================================
# English Board - local runtime media
# Required by the current static application, but not stored in Git.
# ============================================================
/audio/
/image/

# ============================================================
# Generated reports / local investigation output
# ============================================================
/filelists_*.txt
/gitinfo_*.txt
/english_board_inventory_*.txt

# ============================================================
# Generated archives / local data
# ============================================================
*.zip
*.xlsx
~$*.xlsx

# ============================================================
# Local credentials / secrets
# ============================================================
credentials.json
token.json
client_secret*.json
.env
.env.*
*.key
*.pem
scripts/chatgpt-api.local.js

# ============================================================
# Logs / caches / temporary files
# ============================================================
logs/
*.log
__pycache__/
*.py[cod]
*$py.class
.venv/
venv/
env/
*.tmp
*.bak

# ============================================================
# OS / editor files
# ============================================================
.DS_Store
Thumbs.db
.vscode/
.idea/
'@

$GitIgnorePath = Join-Path $Root ".gitignore"
[System.IO.File]::WriteAllText($GitIgnorePath, $GitIgnore, (New-Object System.Text.UTF8Encoding($false)))

$Hash = (Get-FileHash -LiteralPath $GitIgnorePath -Algorithm SHA256).Hash

Write-Host "RESULT=PASS"
Write-Host "STEP=1_PREPARE_GITIGNORE"
Write-Host "ROOT=$Root"
Write-Host "MOVED_REPORT_COUNT=$($Moved.Count)"
foreach ($Item in $Moved) {
    Write-Host "MOVED_REPORT=$Item"
}
Write-Host "GITIGNORE=$GitIgnorePath"
Write-Host "GITIGNORE_SHA256=$Hash"
Write-Host "GIT_INIT=NO"
Write-Host "APPLICATION_SOURCE_MOVED=NO"
Write-Host "MEDIA_MOVED=NO"
