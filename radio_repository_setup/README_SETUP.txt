RADIO RECORDING REPOSITORY SETUP

Target directory:
C:\OneDrive2\OneDrive\lib\codex\radio-recording

Procedure:

1. Extract the ZIP to a temporary directory.
2. Open PowerShell in the extracted directory.
3. Run:

powershell.exe -ExecutionPolicy Bypass -File .\SETUP_RADIO_REPOSITORY.ps1

The script performs the following operations:

- keeps the Git repository root at radio-recording;
- tracks source under main, development source under dev, and specifications under docs;
- excludes audio and operational SQLite databases from Git;
- creates .gitignore, .gitattributes, .editorconfig, AGENTS.md, and development contracts;
- creates Codex task and isolated-worktree helper scripts;
- initializes the local Git repository with branch main;
- stages the intended initial repository contents;
- validates that runtime audio, databases, logs, temporary output, and worktrees are ignored;
- does not create a commit automatically.

Existing files are not deleted. When the setup replaces one of its managed files, the prior file is copied to:

dev\setup_backup_YYYYMMDD_HHMMSS

After setup:

git status --short

git commit -m "Initialize radio recording repository"
