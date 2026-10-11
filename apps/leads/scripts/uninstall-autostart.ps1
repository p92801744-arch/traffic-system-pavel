# Remove the logon task and stop the supervisor.
$ErrorActionPreference = "Continue"
$TaskName = "PavelLeadsInbox"
$LeadsRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $LeadsRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $Python) {
    Set-Location -LiteralPath $LeadsRoot
    & $Python -m app.supervisor stop
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Output "Автозапуск снят. Задача $TaskName удалена. После следующего входа в Windows ящик сам не поднимется."
