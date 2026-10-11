# Print whether the leads inbox is up. Exit 0 when every process is alive and the page answers.
$ErrorActionPreference = "Stop"
$LeadsRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $LeadsRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Нет интерпретатора: $Python"
}
Set-Location -LiteralPath $LeadsRoot
& $Python -m app.supervisor status
exit $LASTEXITCODE
