[CmdletBinding()]
param(
    [string]$RootPath = "C:\OneDrive2\OneDrive\lib\codex\radio-recording",
    [switch]$Commit
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootPath = [System.IO.Path]::GetFullPath($RootPath)
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupRoot = $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Ensure-Directory {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-BackupRoot {
    if ($script:BackupRoot) {
        return $script:BackupRoot
    }

    $script:BackupRoot = Join-Path $RootPath "dev\setup_backup_$Timestamp"
    Ensure-Directory -Path $script:BackupRoot
    return $script:BackupRoot
}

function Write-ManagedFile {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][string]$Content
    )

    $TargetPath = Join-Path $RootPath $RelativePath
    $ParentPath = Split-Path -Parent $TargetPath
    Ensure-Directory -Path $ParentPath

    $NormalizedContent = $Content.Replace("`r`n", "`n").TrimEnd() + "`n"

    if (Test-Path -LiteralPath $TargetPath) {
        $CurrentContent = [System.IO.File]::ReadAllText($TargetPath)
        $CurrentNormalized = $CurrentContent.Replace("`r`n", "`n")

        if ($CurrentNormalized -eq $NormalizedContent) {
            return
        }

        $BackupPath = Join-Path (Get-BackupRoot) $RelativePath
        Ensure-Directory -Path (Split-Path -Parent $BackupPath)
        Copy-Item -LiteralPath $TargetPath -Destination $BackupPath -Force
    }

    [System.IO.File]::WriteAllText($TargetPath, $NormalizedContent, $Utf8NoBom)
}

function Invoke-Git {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
}

function Test-FilesEqual {
    param(
        [Parameter(Mandatory)][string]$First,
        [Parameter(Mandatory)][string]$Second
    )

    if ((Get-Item -LiteralPath $First).Length -ne (Get-Item -LiteralPath $Second).Length) {
        return $false
    }

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $First).Hash -eq
        (Get-FileHash -Algorithm SHA256 -LiteralPath $Second).Hash
}

Ensure-Directory -Path $RootPath

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not installed or is not available in PATH."
}

# Migrate the former main directory before creating the new root structure.
$FormerMain = Join-Path $RootPath "main"
if (Test-Path -LiteralPath $FormerMain) {
    $MainItems = @(Get-ChildItem -LiteralPath $FormerMain -Force)

    foreach ($Item in $MainItems) {
        $Destination = Join-Path $RootPath $Item.Name
        if (-not (Test-Path -LiteralPath $Destination)) {
            continue
        }

        if (-not $Item.PSIsContainer -and -not (Get-Item -LiteralPath $Destination).PSIsContainer) {
            if (Test-FilesEqual -First $Item.FullName -Second $Destination) {
                continue
            }
        }

        throw "Migration conflict: '$($Item.FullName)' cannot be moved because '$Destination' already exists. No files were moved."
    }

    if ($MainItems.Count -gt 0) {
        $MainBackup = Join-Path (Get-BackupRoot) "main_original"
        Copy-Item -LiteralPath $FormerMain -Destination $MainBackup -Recurse -Force
    }

    foreach ($Item in $MainItems) {
        $Destination = Join-Path $RootPath $Item.Name
        if (Test-Path -LiteralPath $Destination) {
            Remove-Item -LiteralPath $Item.FullName -Force
        }
        else {
            Move-Item -LiteralPath $Item.FullName -Destination $Destination
        }
    }

    if ((Get-ChildItem -LiteralPath $FormerMain -Force | Measure-Object).Count -eq 0) {
        Remove-Item -LiteralPath $FormerMain -Force
    }
}

$RequiredDirectories = @(
    "audio",
    "database",
    "dev\scripts",
    "dev\logs",
    "dev\tmp",
    "dev\output",
    "dev\worktrees",
    "docs\codex_tasks",
    "src\radio_recording",
    "tests"
)
foreach ($RelativeDirectory in $RequiredDirectories) {
    Ensure-Directory -Path (Join-Path $RootPath $RelativeDirectory)
}

$Files = [ordered]@{}

$Files[".gitignore"] = @'
# Local configuration and secrets
.env
.env.*
!.env.example
*.local

# Python
__pycache__/
*.py[cod]
*.pyd
.venv/
venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
coverage.xml
htmlcov/
build/
dist/
*.egg-info/

# Runtime audio and database data
/audio/
/database/
*.m4a
*.mp3
*.aac
*.wav
*.flac
*.db
*.sqlite
*.sqlite3
*.db-wal
*.db-shm
*.db-journal

# Development runtime output; development source under dev remains tracked
/dev/logs/
/dev/tmp/
/dev/output/
/dev/worktrees/
/dev/setup_backup_*/

# Application runtime output
/log/
/logs/
*.log
*.part
*.tmp
*.temp
*.zip
.nhk_download.lock

# Codex and editor local state
.codex/
.idea/
.vscode/

# Operating system and Office temporary files
.DS_Store
Thumbs.db
desktop.ini
~$*
'@

$Files[".gitattributes"] = @'
* text=auto

*.py text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.yml text eol=lf
*.yaml text eol=lf
*.json text eol=lf
*.sql text eol=lf
*.txt text eol=lf
*.ps1 text eol=crlf
*.bat text eol=crlf
*.cmd text eol=crlf

*.db binary
*.sqlite binary
*.sqlite3 binary
*.m4a binary
*.mp3 binary
*.wav binary
*.zip binary
'@

$Files[".editorconfig"] = @'
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true

[*.py]
indent_style = space
indent_size = 4
max_line_length = 100

[*.{md,yml,yaml,json,toml,sql}]
indent_style = space
indent_size = 2

[*.ps1]
end_of_line = crlf
indent_style = space
indent_size = 4

[*.{bat,cmd}]
end_of_line = crlf
'@

$Files["README.md"] = @'
# Radio Recording

Local NHK radio download, audio trimming, and transcription project.

## Repository layout

```text
radio-recording/
├─ download entry programs and project configuration
├─ src/radio_recording/    reusable application modules
├─ tests/                  automated tests
├─ docs/                   authoritative specifications and Codex tasks
├─ dev/                    development scripts and local diagnostics
├─ audio/                  runtime audio data; excluded from Git
└─ database/               runtime SQLite data; excluded from Git
```

Executable entry programs are placed at the repository root. Reusable implementation is placed under `src/radio_recording`.

The former `main` directory is not used. The Git repository root is also the execution-path reference.

## Runtime data

The following are intentionally excluded from Git:

- downloaded and processed audio under `audio`;
- SQLite databases and journal files under `database`;
- logs, temporary files, generated output, and isolated worktrees under `dev`;
- local secrets and environment files.

Database definitions and migrations are source files and belong under the repository, not in `database`. Their final directory will be fixed when the database design is approved.

## Development workflow

1. Define the task in `docs/codex_tasks`.
2. Create an isolated task branch and worktree.
3. Change only the files authorized by the task.
4. Run `dev/scripts/run_checks.ps1`.
5. Review the diff and commit on the task branch.

See `AGENTS.md` and `docs/DEVELOPMENT_RULES.md`.
'@

$Files["AGENTS.md"] = @'
# AGENTS.md

This file defines the repository-wide operating contract for Codex and other implementation agents.

## 1. Authoritative sources

Read these files before changing source code or schema:

1. the active task file under `docs/codex_tasks`;
2. `docs/DATABASE_DESIGN.md` for the approved SQLite contract;
3. `docs/DEVELOPMENT_RULES.md` for branch, worktree, validation, and delivery rules;
4. `README.md` for the repository layout.

A task-specific requirement may override a general rule only when it states the exception explicitly.

## 2. Repository boundaries

- Executable entry programs belong at the repository root.
- Reusable Python implementation belongs under `src/radio_recording`.
- Tests belong under `tests`.
- Development scripts and diagnostics belong under `dev`.
- Runtime audio under `audio` and runtime SQLite files under `database` must never be committed.
- The former `main` directory must not be recreated.

## 3. Stable download behavior

The current download core was stabilized through repeated defect correction. A task may reorganize files or replace Excel persistence with SQLite persistence, but it must not change the proven download behavior unless the task expressly authorizes that change.

Protected behavior includes planning, concurrency, retry count, HLS handling, FFmpeg execution, temporary-file handling, duration validation, full decode validation, SHA-256 generation, filename collision handling, process cancellation, locking, and logging semantics.

Refactoring is not proof of equivalence. Tests and runtime evidence must demonstrate behavior retention.

## 4. Database work

- Do not create or change tables before `docs/DATABASE_DESIGN.md` marks the relevant contract Approved.
- SQLite runtime files are data, not source.
- Schema definitions and migrations are reviewed source files.
- Foreign keys, constraints, defaults, NULL meaning, status transitions, and migration rules must be explicit.
- Do not create views unless the approved design explicitly requires them.

## 5. Task execution

- Use one branch and one isolated worktree per task.
- Do not modify files outside the task's allowed-file list.
- Do not edit the main branch directly.
- Preserve unrelated local work.
- Do not claim a test passed unless it was executed successfully.
- Do not commit generated audio, databases, logs, credentials, archives, or temporary files.

## 6. Required completion report

Return at least:

```text
RESULT=
TASK_REF=
BASE_SHA=
NEW_SHA=
CHANGED_FILES=
CHECKS=
RUNTIME_VALIDATION=
REMAINING_RISKS=
```
'@

$Files["docs\DEVELOPMENT_RULES.md"] = @'
# Development Rules

## 1. Repository root

`radio-recording` is the Git root and the execution-path reference. Entry programs are placed at the root. The former `main` directory is not used.

## 2. Data separation

`audio` and `database` are runtime data locations outside Git tracking. Their contents must not be copied into source directories for convenience.

Source-controlled schema files, migrations, tests, and documentation remain in the repository.

## 3. Branch and worktree model

Each implementation task uses:

- one task file under `docs/codex_tasks`;
- one branch named `codex/<task-slug>`;
- one isolated worktree under `dev/worktrees/<task-slug>`;
- one reviewable commit sequence.

The main branch remains the accepted baseline.

## 4. Markdown contracts

Specifications are written before implementation. The database design document is authoritative for table purpose, columns, keys, constraints, relations, status transitions, and migration rules.

Implementation must not silently reinterpret an approved Markdown contract.

## 5. Change control

A task identifies:

- purpose;
- allowed files;
- prohibited changes;
- acceptance criteria;
- required checks;
- required runtime evidence.

The stable download core is changed only when an explicit requirement identifies the behavior to change.

## 6. Validation

Run:

```powershell
.\dev\scripts\run_checks.ps1
```

Additional task-specific tests remain mandatory. Static checks alone do not establish Windows Excel, FFmpeg, NHK network, or SQLite runtime correctness.

## 7. Delivery

A completion report records result, branch, base and new SHA, changed files, checks, runtime validation, and remaining risks. Deliverable archives use a leading `YYYYMMDD_` date when an archive is required.
'@

$Files["docs\DATABASE_DESIGN.md"] = @'
# Database Design

Status: Draft

This document will become the authoritative SQLite table and relation contract before database implementation begins.

For each table, define in this order:

1. table purpose;
2. one-row meaning;
3. data ownership and update responsibility;
4. columns, type, NULL meaning, and default;
5. primary key and unique constraints;
6. foreign keys and delete/update behavior;
7. CHECK constraints;
8. relation to other tables;
9. state transitions;
10. source data and migration rules.

Current fixed decisions:

- SQLite is the new system of record after migration acceptance.
- Runtime database files are stored under `database` and excluded from Git.
- No database view is created in the initial design.
- Program-level default trim seconds and Monday-through-Sunday overrides are columns of the program table.
- A recording-level trim override takes priority over the program setting.
- Download, trim, and transcription states are independent.
- The stable download core behavior is preserved while persistence changes from Excel to SQLite.

Table names and column names remain under review until this document is explicitly marked Approved.
'@

$Files["docs\codex_tasks\TEMPLATE.md"] = @'
# {{SLUG}}

DATE={{DATE}}
STATUS=DRAFT

## Purpose

Describe the single task outcome.

## Baseline

Record the accepted base branch and SHA before implementation.

## Allowed files

List every file or directory that may change.

## Prohibited changes

State protected behavior, especially stable download-core behavior and runtime data.

## Requirements

Define the complete behavior and data contract.

## Acceptance criteria

Define objective pass conditions.

## Required checks

List static, unit, integration, and Windows runtime checks.

## Completion report

```text
RESULT=
TASK_REF=
BASE_SHA=
NEW_SHA=
CHANGED_FILES=
CHECKS=
RUNTIME_VALIDATION=
REMAINING_RISKS=
```
'@

$Files["pyproject.toml"] = @'
[build-system]
requires = ["setuptools>=70"]
build-backend = "setuptools.build_meta"

[project]
name = "radio-recording"
version = "0.0.0"
description = "Local NHK radio download, audio processing, and transcription"
requires-python = ">=3.12"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
'@

$Files["requirements-dev.txt"] = @'
pytest>=8,<9
ruff>=0.6,<1
'@

$Files["src\radio_recording\__init__.py"] = @'
"""Shared implementation for the radio-recording project."""
'@

$Files["dev\scripts\run_checks.ps1"] = @'
[CmdletBinding()]
param(
    [string]$RootPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RootPath = [System.IO.Path]::GetFullPath($RootPath)
Set-Location $RootPath

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
'@

$Files["dev\scripts\new_codex_task.ps1"] = @'
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Slug
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Slug -notmatch "^[a-z0-9][a-z0-9-]*$") {
    throw "Slug must contain lowercase letters, digits, and hyphens only."
}

$RootPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$TemplatePath = Join-Path $RootPath "docs\codex_tasks\TEMPLATE.md"
$Date = Get-Date -Format "yyyy-MM-dd"
$TaskPath = Join-Path $RootPath "docs\codex_tasks\${Date}_${Slug}.md"

if (Test-Path -LiteralPath $TaskPath) {
    throw "Task file already exists: $TaskPath"
}

$Content = [System.IO.File]::ReadAllText($TemplatePath)
$Content = $Content.Replace("{{DATE}}", $Date).Replace("{{SLUG}}", $Slug)
[System.IO.File]::WriteAllText($TaskPath, $Content, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "TASK_RESULT=PASS"
Write-Host "TASK_FILE=$TaskPath"
'@

$Files["dev\scripts\create_worktree.ps1"] = @'
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

Set-Location $RootPath

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
'@

foreach ($Entry in $Files.GetEnumerator()) {
    Write-ManagedFile -RelativePath $Entry.Key -Content $Entry.Value
}

if (-not (Test-Path -LiteralPath (Join-Path $RootPath ".git"))) {
    Push-Location $RootPath
    try {
        Invoke-Git -Arguments @("init")
        Invoke-Git -Arguments @("branch", "-M", "main")
    }
    finally {
        Pop-Location
    }
}

Push-Location $RootPath
try {
    Invoke-Git -Arguments @("config", "core.autocrlf", "false")
    Invoke-Git -Arguments @("add", "-A")

    & (Join-Path $RootPath "dev\scripts\run_checks.ps1") -RootPath $RootPath

    if ($Commit) {
        & git diff --cached --quiet
        if ($LASTEXITCODE -ne 0) {
            Invoke-Git -Arguments @("commit", "-m", "Initialize radio recording repository")
        }
    }

    $Branch = (& git branch --show-current).Trim()
    $Status = @(& git status --short)
}
finally {
    Pop-Location
}

Write-Host "SETUP_RESULT=PASS"
Write-Host "REPOSITORY_ROOT=$RootPath"
Write-Host "GIT_BRANCH=$Branch"
Write-Host "PROGRAM_LOCATION=repository-root"
Write-Host "FORMER_MAIN_PRESENT=False"
Write-Host "FILES_STAGED=True"
Write-Host "AUTO_COMMIT=$([bool]$Commit)"
if ($BackupRoot) {
    Write-Host "BACKUP_ROOT=$BackupRoot"
}
Write-Host "GIT_STATUS_BEGIN"
$Status | ForEach-Object { Write-Host $_ }
Write-Host "GIT_STATUS_END"
