[CmdletBinding()]
param(
    [string]$RootPath = "C:\OneDrive2\OneDrive\lib\codex\radio-recording",
    [switch]$Commit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootPath = [System.IO.Path]::GetFullPath($RootPath)
$CoreScript = Join-Path $PSScriptRoot "SETUP_RADIO_REPOSITORY_CORE.ps1"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path -LiteralPath $CoreScript)) {
    throw "Required setup core was not found: $CoreScript"
}

# The core performs safe migration, repository creation, managed-file generation,
# staging, and its first validation pass. Commit is intentionally handled here.
& $CoreScript -RootPath $RootPath
if ($LASTEXITCODE -ne 0) {
    throw "Core repository setup failed."
}

$RunChecks = @'
[CmdletBinding()]
param(
    [string]$RootPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RootPath = [System.IO.Path]::GetFullPath($RootPath)

Push-Location $RootPath
try {
    if (-not (Test-Path -LiteralPath (Join-Path $RootPath ".git"))) {
        throw "CHECK FAILED: repository root does not contain .git"
    }

    if (Test-Path -LiteralPath (Join-Path $RootPath "main")) {
        throw "CHECK FAILED: the former main directory must not exist"
    }

    $ProbePaths = @(
        "audio\__ignore_probe__.m4a",
        "database\__ignore_probe__.db",
        "dev\logs\__ignore_probe__.log",
        "dev\tmp\__ignore_probe__.tmp",
        "dev\output\__ignore_probe__.zip",
        "dev\worktrees\__ignore_probe__.txt",
        ".env"
    )

    try {
        foreach ($RelativePath in $ProbePaths) {
            $FullPath = Join-Path $RootPath $RelativePath
            $Parent = Split-Path -Parent $FullPath
            if (-not (Test-Path -LiteralPath $Parent)) {
                New-Item -ItemType Directory -Path $Parent -Force | Out-Null
            }
            Set-Content -LiteralPath $FullPath -Value "probe" -Encoding UTF8

            & git check-ignore -q -- $RelativePath
            if ($LASTEXITCODE -ne 0) {
                throw "CHECK FAILED: expected Git ignore did not match $RelativePath"
            }
        }
    }
    finally {
        foreach ($RelativePath in $ProbePaths) {
            Remove-Item -LiteralPath (Join-Path $RootPath $RelativePath) -Force -ErrorAction SilentlyContinue
        }
    }

    $TrackedChecks = @(
        "AGENTS.md",
        "README.md",
        "docs/DEVELOPMENT_RULES.md",
        "dev/scripts/run_checks.ps1",
        "src/radio_recording/__init__.py"
    )
    foreach ($RelativePath in $TrackedChecks) {
        & git check-ignore -q -- $RelativePath
        if ($LASTEXITCODE -eq 0) {
            throw "CHECK FAILED: source file is incorrectly ignored: $RelativePath"
        }
    }

    & git diff --check
    if ($LASTEXITCODE -ne 0) {
        throw "CHECK FAILED: git diff --check"
    }

    & git diff --cached --check
    if ($LASTEXITCODE -ne 0) {
        throw "CHECK FAILED: git diff --cached --check"
    }

    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }

    if ($PythonCommand) {
        $TrackedPythonFiles = @(& git ls-files "*.py")
        foreach ($PythonFile in $TrackedPythonFiles) {
            if ([string]::IsNullOrWhiteSpace($PythonFile)) {
                continue
            }
            & $PythonCommand.Source -m py_compile $PythonFile
            if ($LASTEXITCODE -ne 0) {
                throw "CHECK FAILED: Python compile failed: $PythonFile"
            }
        }
    }

    Write-Host "CHECK_RESULT=PASS"
    Write-Host "REPOSITORY_ROOT=$RootPath"
    Write-Host "MAIN_DIRECTORY_PRESENT=False"
    Write-Host "AUDIO_IGNORED=True"
    Write-Host "DATABASE_IGNORED=True"
    Write-Host "DEV_RUNTIME_OUTPUT_IGNORED=True"
}
finally {
    Pop-Location
}
'@

$CreateWorktree = @'
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Slug,
    [string]$BaseRef = "main"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Slug -notmatch "^[a-z0-9][a-z0-9-]*$") {
    throw "Slug must contain lowercase letters, digits, and hyphens only."
}

$RootPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WorktreePath = Join-Path $RootPath "dev\worktrees\$Slug"
$BranchName = "codex/$Slug"

Push-Location $RootPath
try {
    & git rev-parse --verify HEAD *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Create the initial commit before creating an isolated worktree."
    }

    if (Test-Path -LiteralPath $WorktreePath) {
        throw "Worktree path already exists: $WorktreePath"
    }

    & git show-ref --verify --quiet "refs/heads/$BranchName"
    if ($LASTEXITCODE -eq 0) {
        & git worktree add $WorktreePath $BranchName
    }
    else {
        & git worktree add -b $BranchName $WorktreePath $BaseRef
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create worktree."
    }

    Write-Host "WORKTREE_RESULT=PASS"
    Write-Host "BRANCH=$BranchName"
    Write-Host "WORKTREE=$WorktreePath"
}
finally {
    Pop-Location
}
'@

$RunChecksPath = Join-Path $RootPath "dev\scripts\run_checks.ps1"
$CreateWorktreePath = Join-Path $RootPath "dev\scripts\create_worktree.ps1"
[System.IO.File]::WriteAllText($RunChecksPath, $RunChecks.Replace("`r`n", "`n").TrimEnd() + "`n", $Utf8NoBom)
[System.IO.File]::WriteAllText($CreateWorktreePath, $CreateWorktree.Replace("`r`n", "`n").TrimEnd() + "`n", $Utf8NoBom)

& git -C $RootPath add -A
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stage final repository setup files."
}

& $RunChecksPath -RootPath $RootPath
if ($LASTEXITCODE -ne 0) {
    throw "Final repository validation failed."
}

if ($Commit) {
    & git -C $RootPath diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        & git -C $RootPath commit -m "Initialize radio recording repository"
        if ($LASTEXITCODE -ne 0) {
            throw "Initial commit failed. Configure git user.name and user.email, then run the commit manually."
        }
    }
}

$Branch = (& git -C $RootPath branch --show-current).Trim()
Write-Host "FINAL_SETUP_RESULT=PASS"
Write-Host "REPOSITORY_ROOT=$RootPath"
Write-Host "GIT_BRANCH=$Branch"
Write-Host "PROGRAM_LOCATION=repository-root"
Write-Host "FORMER_MAIN_PRESENT=False"
Write-Host "LOCATION_SAFE_SCRIPTS=True"
Write-Host "AUTO_COMMIT=$([bool]$Commit)"
