# Firecrawl queries for Agent 2 (Pains)
# Usage: .\scripts\run-pains.ps1 [-Date "2026-08-06"]

param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [string]$OutputDir = "data/raw"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

$queries = @(
    @{ Name = "vc-ii"; Query = '"внедрение ии" site:vc.ru' },
    @{ Name = "automation-problems"; Query = '"автоматизация бизнеса" проблемы' },
    @{ Name = "habr-ii"; Query = '"ии для бизнеса" site:habr.com' },
    @{ Name = "vc-automation"; Query = '"автоматизация" предприниматель site:vc.ru' },
    @{ Name = "chatgpt-business"; Query = '"chatgpt для бизнеса" опыт' },
    @{ Name = "ecommerce-automation"; Query = '"интернет магазин" автоматизация проблемы' },
    @{ Name = "habr-n8n"; Query = '"n8n" внедрение site:habr.com' }
)

Write-Host "Agent 2 - Pains collection ($Date)"
Write-Host "Project: $ProjectRoot"
Write-Host ""

foreach ($q in $queries) {
    $outFile = Join-Path $OutputDir "pains-$($q.Name)-$Date.json"
    Write-Host "Searching: $($q.Query)"
    Write-Host "  -> $outFile"

    firecrawl search $($q.Query) --scrape -o $outFile --json

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Failed: $($q.Name). Check firecrawl auth: firecrawl login --browser"
    }
}

Write-Host ""
Write-Host "Done. Merge with: node scripts/merge-results.js"
