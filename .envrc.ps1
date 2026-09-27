# Переменные окружения для локального запуска. Значений здесь нет — только имена секретов в Bitwarden.
# Если в сессии нет своей функции Get-Secret — берём из Bitwarden CLI (хранилище должно быть разблокировано:
# bw unlock -> $env:BW_SESSION). Поле: пароль записи, если пусто — заметка.
if (-not (Get-Command Get-Secret -ErrorAction SilentlyContinue)) {
    function Get-Secret([string]$Name) {
        $v = bw get password $Name --nointeraction 2>$null
        if (-not $v) { $v = bw get notes $Name --nointeraction 2>$null }
        if (-not $v) { throw "Bitwarden: секрет «$Name» не найден или хранилище заблокировано (bw unlock)" }
        $v
    }
}

$env:SUPABASE_KEY = Get-Secret wohnung-monitor-supabase-key
