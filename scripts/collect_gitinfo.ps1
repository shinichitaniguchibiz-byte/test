$ErrorActionPreference = "Continue"

$Root = (Get-Location).Path
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$OutputFile = Join-Path $Root ("gitinfo_{0}.txt" -f $Timestamp)

$Lines = New-Object System.Collections.Generic.List[string]

function Add-Section {
    param(
        [string]$Title,
        [scriptblock]$Command
    )

    $Lines.Add("")
    $Lines.Add("============================================================")
    $Lines.Add($Title)
    $Lines.Add("============================================================")

    try {
        $Result = & $Command 2>&1
        if ($null -eq $Result) {
            $Lines.Add("(no output)")
        }
        else {
            foreach ($Line in $Result) {
                $Lines.Add([string]$Line)
            }
        }
    }
    catch {
        $Lines.Add("ERROR: $($_.Exception.Message)")
    }
}

$Lines.Add("ROOT`t$Root")
$Lines.Add("CREATED`t$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$Lines.Add("MODE`tREAD_ONLY_GIT_INSPECTION")
$Lines.Add("NETWORK_MUTATION`tNONE")
$Lines.Add("WORKTREE_MUTATION`tNONE")

Add-Section "01. GIT VERSION" {
    git --version
}

Add-Section "02. .GIT EXISTENCE" {
    $GitPath = Join-Path $Root ".git"
    if (Test-Path $GitPath) {
        $Item = Get-Item $GitPath -Force
        "EXISTS=YES"
        "PSISCONTAINER=$($Item.PSIsContainer)"
        "ATTRIBUTES=$($Item.Attributes)"
        "FULLNAME=$($Item.FullName)"
        if (-not $Item.PSIsContainer) {
            "CONTENT:"
            Get-Content $Item.FullName
        }
    }
    else {
        "EXISTS=NO"
    }
}

Add-Section "03. REPOSITORY DETECTION" {
    git rev-parse --is-inside-work-tree
    git rev-parse --is-bare-repository
    git rev-parse --show-toplevel
    git rev-parse --git-dir
    git rev-parse --git-common-dir
}

Add-Section "04. CURRENT HEAD" {
    git rev-parse HEAD
    git symbolic-ref --short -q HEAD
    git log -1 --date=iso-strict --format="COMMIT=%H%nPARENTS=%P%nAUTHOR=%an <%ae>%nAUTHOR_DATE=%aI%nCOMMITTER=%cn <%ce>%nCOMMIT_DATE=%cI%nSUBJECT=%s"
}

Add-Section "05. STATUS PORCELAIN V2" {
    git status --porcelain=v2 --branch
}

Add-Section "06. STATUS HUMAN READABLE" {
    git status
}

Add-Section "07. LOCAL BRANCHES" {
    git branch -vv
}

Add-Section "08. ALL BRANCH REFS" {
    git for-each-ref refs/heads refs/remotes --sort=refname --format="%(refname) | %(objectname) | %(upstream) | %(upstream:track) | %(committerdate:iso8601) | %(subject)"
}

Add-Section "09. REMOTES" {
    git remote -v
}

Add-Section "10. REMOTE CONFIG WITHOUT NETWORK ACCESS" {
    foreach ($Remote in @(git remote)) {
        "REMOTE=$Remote"
        git remote get-url --all $Remote
        git config --get-all "remote.$Remote.fetch"
        ""
    }
}

Add-Section "11. LOCAL GIT CONFIG" {
    git config --local --list --show-origin
}

Add-Section "12. RECENT HISTORY CURRENT BRANCH" {
    git log -50 --date=iso-strict --decorate --graph --pretty=format:"%h | %aI | %d | %an | %s"
}

Add-Section "13. RECENT HISTORY ALL REFS" {
    git log --all -100 --date=iso-strict --decorate --pretty=format:"%H | %aI | %d | %an | %s"
}

Add-Section "14. TAGS" {
    git tag --list --sort=-creatordate --format="%(refname:short) | %(objectname) | %(creatordate:iso8601) | %(subject)"
}

Add-Section "15. WORKTREES" {
    git worktree list --porcelain
}

Add-Section "16. SUBMODULE CONFIG" {
    $Gitmodules = Join-Path $Root ".gitmodules"
    if (Test-Path $Gitmodules) {
        Get-Content $Gitmodules
        ""
        git submodule status
    }
    else {
        "NO .gitmodules"
    }
}

Add-Section "17. ROOT .GITIGNORE" {
    $Gitignore = Join-Path $Root ".gitignore"
    if (Test-Path $Gitignore) {
        Get-Content $Gitignore
    }
    else {
        "NO .gitignore"
    }
}

Add-Section "18. TRACKED FILE COUNT" {
    $Tracked = @(git ls-files)
    "TRACKED_FILE_COUNT=$($Tracked.Count)"
}

Add-Section "19. TRACKED ROOT-LEVEL FILES" {
    git ls-files | Where-Object { $_ -notmatch "/" }
}

Add-Section "20. TRACKED FILES BY TOP DIRECTORY" {
    $Tracked = @(git ls-files)
    $Tracked |
        ForEach-Object {
            if ($_ -match "/") { ($_ -split "/")[0] } else { "(ROOT)" }
        } |
        Group-Object |
        Sort-Object Count -Descending |
        ForEach-Object { "{0}`t{1}" -f $_.Count, $_.Name }
}

Add-Section "21. OBJECT / REPOSITORY SIZE" {
    git count-objects -vH
}

Add-Section "22. REFLOG CURRENT HEAD" {
    git reflog -30 --date=iso-strict --pretty=format:"%h | %gD | %aI | %gs"
}

Add-Section "23. MERGE BASE / UPSTREAM INFORMATION" {
    $Branch = git symbolic-ref --short -q HEAD
    if ($Branch) {
        "CURRENT_BRANCH=$Branch"
        $Upstream = git for-each-ref --format="%(upstream:short)" "refs/heads/$Branch"
        "UPSTREAM=$Upstream"
        if ($Upstream) {
            git rev-list --left-right --count "$Upstream...HEAD"
        }
    }
    else {
        "DETACHED_HEAD=YES"
    }
}

Add-Section "24. IMPORTANT GIT FILES" {
    $Candidates = @(
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".github",
        ".gitlab-ci.yml"
    )
    foreach ($Candidate in $Candidates) {
        $Path = Join-Path $Root $Candidate
        if (Test-Path $Path) {
            $Item = Get-Item $Path -Force
            "{0}`tEXISTS`t{1}" -f $Candidate, $Item.FullName
        }
        else {
            "{0}`tMISSING" -f $Candidate
        }
    }
}

Add-Section "25. ROOT-LEVEL FILE HASHES FOR KEY SOURCE FILES" {
    $Candidates = @(
        "英文読解.html",
        "english-reading.js",
        "english-reading.css",
        "words.js"
    )
    foreach ($Candidate in $Candidates) {
        $Path = Join-Path $Root $Candidate
        if (Test-Path $Path -PathType Leaf) {
            $Item = Get-Item $Path
            $Hash = Get-FileHash -Algorithm SHA256 -Path $Path
            "FILE=$Candidate"
            "SIZE_BYTES=$($Item.Length)"
            "LAST_WRITE_TIME=$($Item.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
            "SHA256=$($Hash.Hash)"
            ""
        }
        else {
            "FILE=$Candidate"
            "STATUS=MISSING_AT_ROOT"
            ""
        }
    }
}

$Lines | Set-Content -Path $OutputFile -Encoding UTF8

Write-Host "RESULT=PASS"
Write-Host "MODE=READ_ONLY_GIT_INSPECTION"
Write-Host "ROOT=$Root"
Write-Host "OUTPUT=$OutputFile"
