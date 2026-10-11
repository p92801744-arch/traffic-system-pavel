# One-time logon task for the leads inbox. Current user, no admin.
$ErrorActionPreference = "Stop"
$TaskName = "PavelLeadsInbox"

$LeadsRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $LeadsRoot ".venv\Scripts\python.exe"
$Vbs = Join-Path $PSScriptRoot "start-hidden.vbs"
$Wscript = Join-Path $env:SystemRoot "System32\wscript.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Нет интерпретатора: $Python. Сначала создай apps\leads\.venv и поставь зависимости."
}
if (-not (Test-Path -LiteralPath $Vbs)) {
    Write-Error "Нет запускалки: $Vbs"
}
if (-not (Test-Path -LiteralPath $Wscript)) {
    Write-Error "Нет wscript.exe: $Wscript"
}

Set-Location -LiteralPath $LeadsRoot
Write-Output "Каталог: $LeadsRoot"
Write-Output "Останавливаю прежний запуск, если он был."
& $Python -m app.supervisor stop
if ($LASTEXITCODE -ne 0) {
    Write-Error "Ручной python -m app ещё работает. Закрой это окно и запусти установку ещё раз."
}

$action = New-ScheduledTaskAction -Execute $Wscript -Argument "//B //Nologo `"$Vbs`"" -WorkingDirectory $LeadsRoot
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
try { $settings.Hidden = $true } catch { Write-Output "Окно задачи не скрылось флагом Hidden. Консоли всё равно не будет: запуск через wscript." }
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Ящик лидов: сайт, Telegram, биржи, встречи и бот. Перезапуск, если процесс упал." `
    -Force | Out-Null

Write-Output "Задача $TaskName записана на вход пользователя $identity. Запускаю сейчас."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
& $Python -m app.supervisor status
exit $LASTEXITCODE
