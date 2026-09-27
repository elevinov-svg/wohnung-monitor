# Один запуск сбора Kleinanzeigen — его вызывает Планировщик Windows раз в 30 минут.
# Секреты — из Bitwarden через .envrc.ps1 (Get-Secret), лог — data\kleinanzeigen\collect.log.
# Пауза «браузер закрыт -> через 2 часа» живёт в самом Python-скрипте (state.json).

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo
$logDir = Join-Path $repo "data\kleinanzeigen"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "collect.log"

try {
    . (Join-Path $repo ".envrc.ps1")
} catch {
    Add-Content $log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [error] .envrc.ps1: $($_.Exception.Message)"
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"
python kleinanzeigen_collect.py *>> $log
exit $LASTEXITCODE
