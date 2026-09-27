# Переменные окружения для локального запуска. Значений здесь нет — только имена секретов в Bitwarden.
# Откуда Get-Secret берёт значение (Bitwarden или зашифрованная копия для Планировщика) — scripts\secrets.ps1
. (Join-Path $PSScriptRoot "scripts\secrets.ps1")

$env:SUPABASE_KEY = Get-Secret wohnung-monitor-supabase-key
