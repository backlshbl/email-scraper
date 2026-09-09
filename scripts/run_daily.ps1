[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$scraperPath = Join-Path $repoRoot ".venv\Scripts\email-lead-scraper.exe"
$mergePath = Join-Path $repoRoot ".venv\Scripts\email-lead-merge.exe"

if (-not (Test-Path -LiteralPath $scraperPath)) {
    throw "Missing .venv. Run .\scripts\bootstrap.ps1 first."
}

$scraperArgs = @(
    "--config", (Join-Path $repoRoot "config\scraper.json"),
    "--fresh-output",
    "--output", (Join-Path $repoRoot "output\leads_today.csv")
)

Push-Location $repoRoot
try {
    & $scraperPath @scraperArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    & $mergePath (Join-Path $repoRoot "output\leads_today.csv") `
        (Join-Path $repoRoot "output\leads.csv")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
