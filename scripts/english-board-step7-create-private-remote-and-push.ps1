$ErrorActionPreference = "Stop"

$Root = "C:\OneDrive2\OneDrive\lib\codex\English Board"
$DevRoot = "C:\OneDrive2\OneDrive\lib\codex\dev\learning-platform\english-board"
$ReportDir = Join-Path $DevRoot "reports"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$ReportPath = Join-Path $ReportDir ("english_board_remote_setup_{0}.txt" -f $Timestamp)
$Repo = "shinichitaniguchibiz-byte/english-board"
$RemoteUrl = "https://github.com/shinichitaniguchibiz-byte/english-board.git"
$ExpectedHead = "aae2864bb83ab9b23c8960f2f90fc9918e0017f6"

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$Arguments
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()

    [pscustomobject]@{
        ExitCode = $p.ExitCode
        StdOut = $stdout.Trim()
        StdErr = $stderr.Trim()
    }
}

Set-Location $Root
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) {
    throw "Git repository is not initialized: $Root"
}

$Branch = (git symbolic-ref --short HEAD).Trim()
if ($Branch -ne "main") {
    throw "Unexpected branch: $Branch"
}

$Head = (git rev-parse HEAD).Trim()
if ($Head -ne $ExpectedHead) {
    throw "Unexpected local HEAD: expected=$ExpectedHead actual=$Head"
}

$TrackedCount = @(git ls-files).Count
if ($TrackedCount -ne 46) {
    throw "Unexpected tracked count: expected=46 actual=$TrackedCount"
}

$Status = @(git status --porcelain)
if ($Status.Count -ne 0) {
    throw "Working tree is not clean before remote setup"
}

$IgnoredAudioCount = @(git ls-files --others --ignored --exclude-standard -- "audio/").Count
$IgnoredImageCount = @(git ls-files --others --ignored --exclude-standard -- "image/").Count
if ($IgnoredAudioCount -ne 3747) {
    throw "Unexpected ignored audio count: $IgnoredAudioCount"
}
if ($IgnoredImageCount -ne 6480) {
    throw "Unexpected ignored image count: $IgnoredImageCount"
}

$GhCommand = Get-Command gh -ErrorAction SilentlyContinue
if ($null -eq $GhCommand) {
    Write-Host "RESULT=NEEDS_GITHUB_CLI"
    Write-Host "STEP=7_CREATE_PRIVATE_REMOTE_AND_PUSH"
    Write-Host "LOCAL_HEAD=$Head"
    Write-Host "REMOTE=NO"
    Write-Host "PUSH=NO"
    exit 2
}

$GhPath = $GhCommand.Source
$Auth = Invoke-NativeCapture -FilePath $GhPath -Arguments "auth status --hostname github.com"
if ($Auth.ExitCode -ne 0) {
    Write-Host "RESULT=NEEDS_GITHUB_AUTH"
    Write-Host "STEP=7_CREATE_PRIVATE_REMOTE_AND_PUSH"
    Write-Host "LOCAL_HEAD=$Head"
    Write-Host "REMOTE=NO"
    Write-Host "PUSH=NO"
    if ($Auth.StdErr) { Write-Host "DETAIL=$($Auth.StdErr -replace '[\r\n]+',' | ')" }
    exit 3
}

$RepoCreatedNow = $false
$View = Invoke-NativeCapture -FilePath $GhPath -Arguments "repo view $Repo --json nameWithOwner,visibility --jq \".nameWithOwner + \\"|\\" + .visibility\""
if ($View.ExitCode -ne 0) {
    $Create = Invoke-NativeCapture -FilePath $GhPath -Arguments "repo create $Repo --private --description \"English Board local development source and Local-to-Web migration baseline\""
    if ($Create.ExitCode -ne 0) {
        throw "GitHub repository creation failed: $($Create.StdErr)"
    }
    $RepoCreatedNow = $true
    $View = Invoke-NativeCapture -FilePath $GhPath -Arguments "repo view $Repo --json nameWithOwner,visibility --jq \".nameWithOwner + \\"|\\" + .visibility\""
    if ($View.ExitCode -ne 0) {
        throw "Created repository cannot be verified: $($View.StdErr)"
    }
}

$RepoView = $View.StdOut.Trim()
if ($RepoView -notmatch '^shinichitaniguchibiz-byte/english-board\|PRIVATE$') {
    throw "Unexpected repository identity or visibility: $RepoView"
}

$Remotes = @(git remote)
if ($Remotes.Count -eq 0) {
    git remote add origin $RemoteUrl
    if ($LASTEXITCODE -ne 0) {
        throw "git remote add origin failed"
    }
}
elseif ($Remotes.Count -eq 1 -and $Remotes[0] -eq "origin") {
    $ExistingUrl = (git remote get-url origin).Trim()
    if ($ExistingUrl -ne $RemoteUrl) {
        throw "Unexpected origin URL: $ExistingUrl"
    }
}
else {
    throw "Unexpected remote configuration: $($Remotes -join ',')"
}

$LsBefore = Invoke-NativeCapture -FilePath "git.exe" -Arguments "ls-remote origin refs/heads/main"
if ($LsBefore.ExitCode -ne 0) {
    throw "git ls-remote failed before push: $($LsBefore.StdErr)"
}
if (-not [string]::IsNullOrWhiteSpace($LsBefore.StdOut)) {
    $RemoteBeforeSha = ($LsBefore.StdOut -split '\s+')[0]
    if ($RemoteBeforeSha -ne $ExpectedHead) {
        throw "Remote main already exists with different SHA: $RemoteBeforeSha"
    }
}

if ([string]::IsNullOrWhiteSpace($LsBefore.StdOut)) {
    $Push = Invoke-NativeCapture -FilePath "git.exe" -Arguments "push -u origin main"
    if ($Push.ExitCode -ne 0) {
        throw "git push failed: $($Push.StdErr)"
    }
}
else {
    git branch --set-upstream-to=origin/main main | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set upstream to origin/main"
    }
}

$LsAfter = Invoke-NativeCapture -FilePath "git.exe" -Arguments "ls-remote origin refs/heads/main"
if ($LsAfter.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($LsAfter.StdOut)) {
    throw "Remote main cannot be verified after push"
}
$RemoteHead = ($LsAfter.StdOut -split '\s+')[0]
if ($RemoteHead -ne $ExpectedHead) {
    throw "Remote HEAD mismatch: local=$ExpectedHead remote=$RemoteHead"
}

$RemoteName = (git config --get branch.main.remote).Trim()
$RemoteMerge = (git config --get branch.main.merge).Trim()
if ($RemoteName -ne "origin" -or $RemoteMerge -ne "refs/heads/main") {
    throw "Unexpected upstream config: remote=$RemoteName merge=$RemoteMerge"
}

$StatusAfter = @(git status --porcelain)
if ($StatusAfter.Count -ne 0) {
    throw "Working tree is not clean after remote setup"
}

$Lines = New-Object System.Collections.Generic.List[string]
$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("STEP`t7_CREATE_PRIVATE_REMOTE_AND_PUSH")
$Lines.Add("REPOSITORY`t$Repo")
$Lines.Add("VISIBILITY`tPRIVATE")
$Lines.Add("REPO_CREATED_NOW`t$RepoCreatedNow")
$Lines.Add("LOCAL_HEAD`t$Head")
$Lines.Add("REMOTE_HEAD`t$RemoteHead")
$Lines.Add("TRACKED_COUNT`t$TrackedCount")
$Lines.Add("IGNORED_AUDIO_COUNT`t$IgnoredAudioCount")
$Lines.Add("IGNORED_IMAGE_COUNT`t$IgnoredImageCount")
$Lines.Add("ORIGIN`t$RemoteUrl")
$Lines.Add("UPSTREAM`tmain -> origin/main")
$Lines.Add("WORKTREE_CLEAN`tYES")
$Lines | Set-Content -LiteralPath $ReportPath -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "STEP=7_CREATE_PRIVATE_REMOTE_AND_PUSH"
Write-Host "REPOSITORY=$Repo"
Write-Host "VISIBILITY=PRIVATE"
Write-Host "REPO_CREATED_NOW=$RepoCreatedNow"
Write-Host "LOCAL_HEAD=$Head"
Write-Host "REMOTE_HEAD=$RemoteHead"
Write-Host "TRACKED_COUNT=$TrackedCount"
Write-Host "IGNORED_AUDIO_COUNT=$IgnoredAudioCount"
Write-Host "IGNORED_IMAGE_COUNT=$IgnoredImageCount"
Write-Host "ORIGIN=$RemoteUrl"
Write-Host "UPSTREAM=main->origin/main"
Write-Host "WORKTREE_CLEAN=YES"
Write-Host "OUTPUT=$ReportPath"
