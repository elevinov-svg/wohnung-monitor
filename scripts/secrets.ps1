# Get-Secret <имя> для локальных запусков: Bitwarden — главный источник, зашифрованная копия — запасной.
#
#   1. своя функция / cmdlet Get-Secret, если уже есть в сессии;
#   2. Bitwarden CLI, если хранилище разблокировано ($env:BW_SESSION, после  bw unlock --raw);
#   3. иначе — копия, сохранённая при последнем успешном чтении из Bitwarden.
#
# Копия: %LOCALAPPDATA%\wohnung-monitor\secrets\<имя>.xml — SecureString, зашифрованная Windows (DPAPI):
# расшифровать может только твоя учётная запись Windows на этом компьютере. Мастер-пароль нигде не хранится.
# Зачем: фоновая задача Планировщика не может разблокировать Bitwarden, а копия ей доступна.
# Ключ поменяли в Bitwarden -> один ручной запуск с разблокированным хранилищем обновит копию.

# своя Get-Secret (если была до нас) — остаётся первым источником
$existing = Get-Command Get-Secret -ErrorAction SilentlyContinue
if ($existing -and -not ($existing.CommandType -eq 'Function' -and $existing.ScriptBlock.ToString().Contains('WM-SECRETS'))) {
    # у функции сохраняем само тело: имя Get-Secret сейчас будет переопределено (иначе вызов пойдёт по кругу)
    $global:WmOrigGetSecret = if ($existing.CommandType -eq 'Function') { $existing.ScriptBlock } else { $existing }
}
$global:WmSecretCache = Join-Path $env:LOCALAPPDATA "wohnung-monitor\secrets"

function global:Get-Secret([Parameter(Mandatory)][string]$Name) {
    # WM-SECRETS
    $value = $null
    $source = $null
    if ($global:WmOrigGetSecret -and -not $global:WmInGetSecret) {
        $global:WmInGetSecret = $true
        try { $value = & $global:WmOrigGetSecret $Name; $source = "Get-Secret" } catch { $value = $null }
        finally { $global:WmInGetSecret = $false }
    }
    if (-not $value -and $env:BW_SESSION) {
        $value = bw get password $Name --nointeraction 2>$null
        if (-not $value) { $value = bw get notes $Name --nointeraction 2>$null }
        if ($value) { $source = "Bitwarden" }
    }
    $file = Join-Path $global:WmSecretCache "$Name.xml"
    if ($value) {
        if ($value -is [securestring]) {
            $secure = $value
            $value = [Net.NetworkCredential]::new("", $secure).Password
        } else {
            $secure = ConvertTo-SecureString ([string]$value) -AsPlainText -Force
        }
        New-Item -ItemType Directory -Force $global:WmSecretCache | Out-Null
        $secure | Export-Clixml -Path $file -Force        # обновить зашифрованную копию
        $global:WmSecretSource = $source
        return $value
    }
    if (Test-Path $file) {
        $global:WmSecretSource = "копия (DPAPI) от $((Get-Item $file).LastWriteTime.ToString('yyyy-MM-dd HH:mm'))"
        return [Net.NetworkCredential]::new("", (Import-Clixml $file)).Password
    }
    throw "Секрет «$Name»: Bitwarden заблокирован и сохранённой копии нет. Один раз: `$env:BW_SESSION = bw unlock --raw; . .\.envrc.ps1"
}
