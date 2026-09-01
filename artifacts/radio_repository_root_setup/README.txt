Radio Recording repository root setup

Target:
C:\OneDrive2\OneDrive\lib\codex\radio-recording

Run:
powershell.exe -ExecutionPolicy Bypass -File .\SETUP_RADIO_REPOSITORY.ps1

The setup performs the following:
- initializes Git at radio-recording root;
- migrates files from the former main directory to the repository root;
- backs up the former main directory before migration;
- removes the former main directory after successful migration;
- excludes audio, database, logs, temporary files, runtime worktrees, and secrets from Git;
- creates repository rules, database-design documentation, Codex task templates, and development scripts;
- stages source-controlled files without committing them;
- runs repository checks.

The setup stops before moving files when a destination conflict exists.
Existing audio and database data are not deleted or moved.

After a successful setup:
cd C:\OneDrive2\OneDrive\lib\codex\radio-recording
git status --short
git commit -m "Initialize radio recording repository"

Environment check:
.\dev\scripts\run_checks.ps1

Create a Codex task:
.\dev\scripts\new_codex_task.ps1 -Slug database-table-design

Create an isolated worktree after the initial commit:
.\dev\scripts\create_worktree.ps1 -Slug database-table-design
