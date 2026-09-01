param(
    [string]$TargetRoot = "C:\OneDrive2\OneDrive\lib\codex\radio-recording"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupRoot = Join-Path $TargetRoot "dev\setup_backup_$Timestamp"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-RepositoryFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $Destination = Join-Path $TargetRoot $RelativePath
    $Parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $Parent -Force | Out-Null

    if (Test-Path $Destination) {
        $Current = [System.IO.File]::ReadAllText($Destination)
        if ($Current -ne $Content) {
            $Backup = Join-Path $BackupRoot $RelativePath
            New-Item -ItemType Directory -Path (Split-Path -Parent $Backup) -Force | Out-Null
            Copy-Item $Destination $Backup -Force
        }
    }

    [System.IO.File]::WriteAllText($Destination, $Content, $Utf8NoBom)
}

New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
foreach ($Directory in @(
    "audio",
    "database",
    "main",
    "main\sql",
    "main\config",
    "main\scripts",
    "dev",
    "dev\scripts",
    "dev\tests",
    "dev\fixtures",
    "dev\logs",
    "dev\tmp",
    "dev\output",
    "dev\worktrees",
    "docs",
    "docs\codex_tasks"
)) {
    New-Item -ItemType Directory -Path (Join-Path $TargetRoot $Directory) -Force | Out-Null
}

$GitIgnore = @'
# Local environment and secrets
.env
.env.*
!.env.example
*.local
*.local.*
/main/config/*.local.*

# Python
__pycache__/
*.py[cod]
*.pyd
.pytest_cache/
.mypy_cache/
.pyright/
.ruff_cache/
.coverage
coverage.xml
htmlcov/
.venv/
venv/

# Editors and operating-system files
.vscode/
.idea/
.DS_Store
Thumbs.db
Desktop.ini

# Operational audio: never commit downloaded or generated media
/audio/**
!/audio/.gitkeep
*.m4a
*.mp3
*.aac
*.wav
*.flac
*.part

# Operational databases: SQL source belongs under main/sql
/database/**
!/database/.gitkeep
*.db
*.db-wal
*.db-shm
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.sqlite3

# Legacy/local workbook and downloader runtime state
/radio_recording.xlsx
.nhk_download.lock
.nhk_download_work/
.nhk_download_state/

# Development runtime output
/dev/logs/
/dev/tmp/
/dev/output/
/dev/worktrees/
/dev/coverage/
/dev/setup_backup_*/

# General generated output
*.log
*.zip
*.7z
*.tmp
*.bak
*.pid

# Build and packaging output
dist/
build/
*.egg-info/
'@

$GitAttributes = @'
* text=auto

*.py   text eol=lf
*.md   text eol=lf
*.sql  text eol=lf
*.json text eol=lf
*.toml text eol=lf
*.yml  text eol=lf
*.yaml text eol=lf

*.ps1 text eol=crlf
*.bat text eol=crlf
*.cmd text eol=crlf

*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.ico binary
*.m4a binary
*.mp3 binary
*.aac binary
*.wav binary
*.flac binary
*.db binary
*.sqlite binary
*.sqlite3 binary
'@

$EditorConfig = @'
root = true

[*]
charset = utf-8
insert_final_newline = true
trim_trailing_whitespace = true

[*.py]
indent_style = space
indent_size = 4

[*.{json,yml,yaml}]
indent_style = space
indent_size = 2

[*.{md,sql,toml}]
indent_style = space
indent_size = 2

[*.{ps1,bat,cmd}]
end_of_line = crlf
'@

$Readme = @'
# Radio Recording

This repository contains the production source, SQLite schema, tests, and development contracts for the local radio-recording system.

## Directory contract

- `main/`: production source code, SQL schema, configuration templates, and launch scripts. Tracked by Git.
- `dev/`: tests, debug tools, validation tools, sanitized fixtures, and Codex support scripts. Source files are tracked; generated logs, temporary files, outputs, and worktrees are ignored.
- `audio/`: downloaded and processed audio. Runtime data only; ignored by Git.
- `database/`: local SQLite database files. Runtime data only; ignored by Git. SQL source belongs under `main/sql/`.
- `docs/`: architecture, database design, development rules, and Codex task definitions. Tracked by Git.

The Git repository root is `radio-recording/`, not `main/`. This keeps `main/` and the source portions of `dev/` version-controlled while the sibling `audio/` and `database/` directories remain local runtime areas.

## Validation

Run from the repository root:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\dev\scripts\run_checks.ps1
```
'@

$Agents = @'
# AGENTS.md

This file is the authoritative Codex and automated-development contract for this repository.

## 1. Repository boundaries

- The repository root is `radio-recording/`.
- Production code and tracked operational assets belong under `main/`.
- Tests, debug tools, validation tools, sanitized fixtures, and Codex helpers belong under `dev/`.
- Runtime audio under `audio/` is never committed.
- Runtime SQLite databases under `database/` are never committed.
- Database schema and migration source files belong under `main/sql/`.
- Generated logs, temporary files, output packages, coverage files, and isolated worktrees belong under ignored paths in `dev/`.

## 2. Protected download behavior

The stable download core is a behavioral contract. It is not a requirement to preserve the current Python file byte-for-byte.

The implementation may be reorganized and Excel access may be replaced with SQLite access. Unless a task explicitly authorizes a behavioral change, preserve:

- NHK metadata retrieval and episode identification;
- authoritative persisted-status checks before download;
- bounded parallel execution;
- bounded whole-episode retries;
- HLS manifest validation;
- FFmpeg `http_seekable=0` input behavior;
- temporary-file download and atomic finalization;
- expected/actual duration validation;
- strict full-file decode validation;
- SHA-256 calculation;
- deterministic file-name collision handling;
- process-tree termination on cancellation;
- single-instance protection;
- detailed run logging.

## 3. Database safety

- Tests use a temporary SQLite database under `dev/tmp/` or the operating-system temporary directory.
- Do not modify an operational file under `database/` unless the task explicitly authorizes it.
- Enable SQLite foreign keys for every connection.
- Schema changes require a versioned SQL source file under `main/sql/` and repeatable validation.
- Do not create SQLite views unless the approved database specification explicitly requires one.

## 4. Task execution

- One implementation cycle uses one Markdown task file under `docs/codex_tasks/`.
- Use an isolated worktree under `dev/worktrees/` when parallel work is possible.
- Preserve unrelated behavior and files.
- Do not commit audio, database files, logs, archives, credentials, or local configuration.
- Specifications are authoritative. Code must not invent table columns, relations, status meanings, or fallback behavior not present in the approved specification.

## 5. Required checks

At minimum run:

1. `python -m compileall -q main dev/tests dev/scripts`
2. `powershell.exe -ExecutionPolicy Bypass -File dev/scripts/check_environment.ps1`
3. task-specific unit and database-contract tests
4. `git diff --check`

Real NHK downloads and writes to the operational SQLite database require explicit authorization.

## 6. Completion report

Report:

- `RESULT`: `PASS`, `BLOCKED`, or `FAIL`;
- task file;
- base commit and new commit;
- changed files;
- checks and results;
- whether real NHK network access was used;
- whether an operational database under `database/` was modified;
- unresolved risks and next action.
'@

$DevelopmentRules = @'
# Development Rules

## Definition

This document defines how source code, operational data, tests, debug materials, specifications, and Codex work are separated and managed in the radio-recording repository.

## Production area: `main/`

`main/` contains files intended to participate in normal operation, including production Python programs, shared modules, SQL schema and migrations, configuration templates, and launch scripts.

The stable download behavior may be refactored to support SQLite and a clearer internal structure. The proven network, FFmpeg, validation, retry, cancellation, and file-finalization behavior remains protected by `AGENTS.md`.

## Development area: `dev/`

Tracked contents include tests, database validators, contract checks, sanitized fixtures, debug utilities, and Codex helper scripts.

Ignored contents include logs, temporary databases, generated output, validation packages, coverage output, and isolated worktrees.

## Runtime areas

`audio/` contains source and processed audio. `database/` contains operational SQLite files. Both remain outside Git even though they are sibling directories under the repository root.

The repository tracks the SQL and programs that create and use the database, not the operational database file itself.

## Specification management

Specifications are explicit Markdown contracts. A table specification defines the table purpose, one-row meaning, columns, constraints, relations, ownership, insert/update rules, and migration mapping before application implementation begins.
'@

$DatabaseDesign = @'
# Database Design

Status: design in progress.

This file will become the authoritative SQLite table and relation specification before implementation begins.

Each table definition is written in this order:

1. table purpose;
2. meaning of one row;
3. data ownership and source;
4. column definitions;
5. primary, unique, foreign-key, default, `NOT NULL`, and `CHECK` constraints;
6. relations;
7. insert and update rules;
8. migration mapping from the current Excel workbook;
9. operational queries required by Python programs.

No SQLite view is part of the currently approved design.
'@

$TaskReadme = @'
# Codex Tasks

Each implementation cycle uses one task Markdown file in this directory.

File name:

```text
YYYY-MM-DD_<short-task-name>.md
```

Each task defines the objective, approved specifications, allowed files, prohibited files, protected behavior, database safety requirements, required checks, acceptance criteria, and completion-report fields.
'@

$TaskTemplate = @'
# Task: <title>

## Objective

<single implementation objective>

## Approved specifications

- `AGENTS.md`
- `docs/DEVELOPMENT_RULES.md`
- `docs/DATABASE_DESIGN.md`

## Allowed files

- <paths>

## Prohibited files

- `audio/**`
- `database/**`
- unrelated accepted specifications

## Required behavior to preserve

- <contracts>

## Required checks

- `python -m compileall -q main dev/tests dev/scripts`
- `powershell.exe -ExecutionPolicy Bypass -File dev/scripts/check_environment.ps1`
- `git diff --check`
- <task-specific checks>

## Acceptance criteria

- <measurable results>

## Completion report

Report `RESULT`, task file, base/new commit, changed files, checks, network usage, operational-database modification, remaining risks, and next action.
'@

$MainReadme = @'
# Main

This directory contains production code and tracked operational assets.

Planned contents:

```text
main/
  nhk_download.py
  nhk_trim.py
  nhk_transcribe.py
  radio_core/
  sql/
  config/
  scripts/
```

The SQLite downloader will be implemented here. The protected behavioral download contract is defined in the repository-root `AGENTS.md`.
'@

$DevReadme = @'
# Dev

This directory contains development and validation material.

Tracked areas:

- `scripts/`: environment, validation, task, and worktree helpers;
- `tests/`: automated tests and contracts;
- `fixtures/`: sanitized test data suitable for Git.

Ignored areas:

- `logs/`;
- `tmp/`;
- `output/`;
- `worktrees/`;
- `coverage/`;
- `setup_backup_*`.
'@

$CheckEnvironment = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

foreach ($Required in @("main", "dev", "audio", "database", "docs", ".gitignore", "AGENTS.md")) {
    if (-not (Test-Path (Join-Path $Root $Required))) {
        throw "Missing required path: $Required"
    }
}

if (-not (Test-Path (Join-Path $Root ".git"))) {
    throw "Git is not initialized at the repository root."
}

$Probes = @(
    "audio\ignore_probe.m4a",
    "database\ignore_probe.db",
    "dev\logs\ignore_probe.log",
    "dev\tmp\ignore_probe.tmp",
    "dev\worktrees\ignore_probe.txt"
)

try {
    foreach ($Probe in $Probes) {
        $FullPath = Join-Path $Root $Probe
        New-Item -ItemType Directory -Path (Split-Path -Parent $FullPath) -Force | Out-Null
        New-Item -ItemType File -Path $FullPath -Force | Out-Null
        git check-ignore -q -- $Probe
        if ($LASTEXITCODE -ne 0) {
            throw "Path is not ignored: $Probe"
        }
    }
}
finally {
    foreach ($Probe in $Probes) {
        Remove-Item (Join-Path $Root $Probe) -Force -ErrorAction SilentlyContinue
    }
}

$TrackedRuntime = git ls-files audio database dev/logs dev/tmp dev/output dev/worktrees
$Illegal = @($TrackedRuntime | Where-Object { $_ -notin @("audio/.gitkeep", "database/.gitkeep") })
if ($Illegal.Count -gt 0) {
    throw "Runtime files are tracked: $($Illegal -join ', ')"
}

Write-Host "ENVIRONMENT_RESULT=PASS"
Write-Host "REPOSITORY_ROOT=$Root"
Write-Host "TRACKED=main,dev-source,docs"
Write-Host "IGNORED=audio,database,dev-runtime"
'@

$RunChecks = @'
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

python.exe -m compileall -q main dev\tests dev\scripts
if ($LASTEXITCODE -ne 0) { throw "Python compile check failed." }

powershell.exe -ExecutionPolicy Bypass -File dev\scripts\check_environment.ps1
if ($LASTEXITCODE -ne 0) { throw "Environment contract check failed." }

git diff --check
if ($LASTEXITCODE -ne 0) { throw "git diff --check failed." }

Write-Host "CHECK_RESULT=PASS"
'@

$NewTask = @'
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Slug
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Date = Get-Date -Format "yyyy-MM-dd"
$Target = Join-Path $Root "docs\codex_tasks\${Date}_${Slug}.md"
$Template = Join-Path $Root "docs\codex_tasks\TEMPLATE.md"

if (Test-Path $Target) {
    throw "Task file already exists: $Target"
}

Copy-Item $Template $Target
Write-Host "TASK_FILE=$Target"
'@

$CreateWorktree = @'
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Slug,
    [string]$BaseRef = "main"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Branch = "codex/$Slug"
$WorktreeRoot = Join-Path $Root "dev\worktrees"
$WorktreePath = Join-Path $WorktreeRoot $Slug

New-Item -ItemType Directory -Path $WorktreeRoot -Force | Out-Null
if (Test-Path $WorktreePath) {
    throw "Worktree path already exists: $WorktreePath"
}

git -C $Root worktree add -b $Branch $WorktreePath $BaseRef
if ($LASTEXITCODE -ne 0) {
    throw "git worktree add failed."
}

Write-Host "WORKTREE=$WorktreePath"
Write-Host "BRANCH=$Branch"
'@

Write-RepositoryFile ".gitignore" $GitIgnore
Write-RepositoryFile ".gitattributes" $GitAttributes
Write-RepositoryFile ".editorconfig" $EditorConfig
Write-RepositoryFile "README.md" $Readme
Write-RepositoryFile "AGENTS.md" $Agents
Write-RepositoryFile "docs\DEVELOPMENT_RULES.md" $DevelopmentRules
Write-RepositoryFile "docs\DATABASE_DESIGN.md" $DatabaseDesign
Write-RepositoryFile "docs\codex_tasks\README.md" $TaskReadme
Write-RepositoryFile "docs\codex_tasks\TEMPLATE.md" $TaskTemplate
Write-RepositoryFile "main\README.md" $MainReadme
Write-RepositoryFile "dev\README.md" $DevReadme
Write-RepositoryFile "dev\scripts\check_environment.ps1" $CheckEnvironment
Write-RepositoryFile "dev\scripts\run_checks.ps1" $RunChecks
Write-RepositoryFile "dev\scripts\new_codex_task.ps1" $NewTask
Write-RepositoryFile "dev\scripts\create_worktree.ps1" $CreateWorktree
Write-RepositoryFile "audio\.gitkeep" ""
Write-RepositoryFile "database\.gitkeep" ""
Write-RepositoryFile "main\sql\.gitkeep" ""
Write-RepositoryFile "main\config\.gitkeep" ""
Write-RepositoryFile "main\scripts\.gitkeep" ""
Write-RepositoryFile "dev\tests\.gitkeep" ""
Write-RepositoryFile "dev\fixtures\.gitkeep" ""

if (-not (Test-Path (Join-Path $TargetRoot ".git"))) {
    git -C $TargetRoot init -b main
    if ($LASTEXITCODE -ne 0) {
        throw "git init failed."
    }
}

git -C $TargetRoot config core.autocrlf false
git -C $TargetRoot config core.filemode false
git -C $TargetRoot config core.longpaths true

git -C $TargetRoot add --all
if ($LASTEXITCODE -ne 0) {
    throw "git add failed."
}

git -C $TargetRoot diff --cached --check
if ($LASTEXITCODE -ne 0) {
    throw "git staged diff check failed."
}

powershell.exe -ExecutionPolicy Bypass -File (Join-Path $TargetRoot "dev\scripts\check_environment.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Environment validation failed."
}

Write-Host ""
Write-Host "SETUP_RESULT=PASS"
Write-Host "REPOSITORY_ROOT=$TargetRoot"
Write-Host "GIT_BRANCH=main"
Write-Host "FILES_STAGED=True"
Write-Host "AUTO_COMMIT=False"
if (Test-Path $BackupRoot) {
    Write-Host "BACKUP=$BackupRoot"
}
Write-Host ""
Write-Host "Review: git -C `"$TargetRoot`" status --short"
Write-Host "Commit: git -C `"$TargetRoot`" commit -m `"Initialize radio recording repository`""
