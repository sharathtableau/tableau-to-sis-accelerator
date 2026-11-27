# One-time setup: put the V3 Cortex lab under git and push it to GitHub.
#
# Before running: create an EMPTY PRIVATE repository on GitHub named
#   tableau-to-sis-v3-cortex
# under your account (no README, no .gitignore, no licence).
#
# No credential is stored here. The push signs in through Windows Credential
# Manager (your existing GitHub sign-in) or opens a browser sign-in.

$ErrorActionPreference = "Continue"
$Repo = "https://github.com/sharathtableau/tableau-to-sis-v3-cortex.git"
$Lab  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Lab

# 1. Keep the original accelerator's repository from swallowing the V3/V2
#    folders as embedded repositories (its push script runs "git add -A").
$rootIgnore = Join-Path $Root ".gitignore"
$guard = "`n# V3 and V2 live in their own repositories (added 2026-10-30)`ntableau_accelerator_v3_cortex/`ntableau_accelerator_v3/`ntableau_sis_accelerator_v2/`n"
if (-not (Select-String -Path $rootIgnore -Pattern "^tableau_accelerator_v3_cortex/$" -Quiet)) {
    Add-Content -Path $rootIgnore -Value $guard
    Write-Host "Root .gitignore: V3/V2 folders excluded from the original repository." -ForegroundColor Cyan
}

# 2. Create the lab repository.
Set-Location -LiteralPath $Lab
Write-Host "Working in: $(Get-Location)" -ForegroundColor Cyan
if (Test-Path ".git") {
    git rev-parse --verify HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "This folder already has commits - stopping so nothing is overwritten." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "Empty repository from an earlier attempt found - continuing." -ForegroundColor Cyan
} else {
    git init
}
git symbolic-ref HEAD refs/heads/main
git config core.longpaths true
git add -A

# 3. Safety: refuse to commit anything that looks like a credential.
$staged = git diff --cached --name-only
foreach ($f in $staged) {
    if ($f -eq "git_setup_v3.ps1") { continue }   # this script holds the patterns themselves
    if (Test-Path -LiteralPath $f -PathType Leaf) {
        if (Select-String -LiteralPath $f -Pattern 'ghp_[A-Za-z0-9]{20,}|github_pat_|-----BEGIN [A-Z ]*PRIVATE KEY' -Quiet -ErrorAction SilentlyContinue) {
            Write-Host "ABORT: '$f' looks like it contains a credential. Nothing was committed." -ForegroundColor Red
            git reset -q
            exit 1
        }
    }
}
$count = ($staged | Measure-Object).Count
Write-Host "Files to commit: $count" -ForegroundColor Cyan

# 4. Commit and push.
git commit -q -m "V3 Cortex lab: initial import (release 19 live, plan 6.0)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Commit FAILED - nothing pushed. If git asks for your name/email, run:" -ForegroundColor Red
    Write-Host '  git config --global user.name "Your Name"; git config --global user.email "you@example.com"' -ForegroundColor Red
    exit 1
}
git remote remove origin 2>$null
git remote add origin $Repo
git push -u origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "SUCCESS - https://github.com/sharathtableau/tableau-to-sis-v3-cortex" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Push FAILED (exit $LASTEXITCODE). The local commit is kept; fix the cause and run:  git push -u origin main" -ForegroundColor Red
}
