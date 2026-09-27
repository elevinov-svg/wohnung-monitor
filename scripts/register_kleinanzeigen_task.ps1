# Разово: задача в Планировщике Windows — сбор Kleinanzeigen каждые 30 минут,
# пока пользователь вошёл в систему (браузер всё равно нужен открытый).
#
#   pwsh -File scripts\register_kleinanzeigen_task.ps1            создать / обновить
#   pwsh -File scripts\register_kleinanzeigen_task.ps1 -Remove    удалить
#
# Проверка: Планировщик заданий -> «Wohnung-Monitor Kleinanzeigen» -> Выполнить.

param([switch]$Remove)
$ErrorActionPreference = "Stop"
$name = "Wohnung-Monitor Kleinanzeigen"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Задача удалена"
    return
}

$pwsh = (Get-Command pwsh).Source
$script = Join-Path $PSScriptRoot "kleinanzeigen_task.ps1"
$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "wohnung-monitor: сбор Kleinanzeigen в Supabase (kleinanzeigen_collect.py)" -Force | Out-Null
Write-Host "Задача «$name» создана: каждые 30 минут, первый запуск через минуту"
