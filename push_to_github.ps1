# Push this repo to GitHub.
#
# Deliberately does NOT embed a credential. Authentication goes through
# Windows Credential Manager (already signed in as sharathtableau); if the
# cached token has expired you get a browser sign-in prompt.
#
# NEVER put a token in this file. A previous version of this script in
# another project hardcoded a ghp_ PAT into the remote URL, and because the
# script was itself committed, the token was pushed to GitHub and had to be
# revoked. Credentials belong in the credential manager, never in a file.

$ErrorActionPreference = "Stop"
$folder = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $folder
Write-Host "Working in: $(Get-Location)" -ForegroundColor Cyan

# Long paths (this repo has deeply nested report/evidence directories)
git config core.longpaths true

# Safety: refuse to run outside a git repo rather than silently init one.
# (The old script deleted .git and re-init'd every run, destroying history.)
if (-not (Test-Path ".git")) {
    Write-Host "ERROR: no .git here. Run 'git init' first -- this script will not create one." -ForegroundColor Red
    exit 1
}

# Show what is about to be committed, and stop if nothing changed.
$changed = git status --porcelain
if (-not $changed) {
    Write-Host "Nothing to commit. Pushing any unpushed commits..." -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "Changes to be committed:" -ForegroundColor Cyan
    git status --short
    Write-Host ""

    # Guard: never commit a file containing a GitHub token.
    $leak = git diff --cached --name-only
    git add -A
    $staged = git diff --cached --name-only
    foreach ($f in $staged) {
        if (Test-Path $f -PathType Leaf) {
            if (Select-String -Path $f -Pattern 'ghp_[A-Za-z0-9]{20,}' -Quiet -ErrorAction SilentlyContinue) {
                Write-Host "ABORT: '$f' looks like it contains a GitHub token. Remove it before pushing." -ForegroundColor Red
                git reset -q
                exit 1
            }
        }
    }

    $msg = Read-Host "Commit message (blank = 'Update <timestamp>')"
    if ([string]::IsNullOrWhiteSpace($msg)) {
        $msg = "Update $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    }
    git commit -m $msg
}

# Normal push -- NOT --force. If this is rejected, the remote has commits you
# do not have locally; pull and merge rather than overwriting someone's work.
Write-Host "Pushing..." -ForegroundColor Cyan
git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "SUCCESS" -ForegroundColor Green
    Write-Host "https://github.com/sharathtableau/tableau-to-sis-accelerator"
} else {
    Write-Host ""
    Write-Host "FAILED - exit code $LASTEXITCODE" -ForegroundColor Red
    Write-Host "If rejected as non-fast-forward, run 'git pull --rebase origin main' and retry."
    Write-Host "Do NOT use --force unless you are certain you want to discard the remote's commits."
}
