# Разово: копия основного профиля Chrome (с cookies и сессией Kleinanzeigen) в отдельную папку.
# Зачем: начиная с Chrome 136 порт отладки (9222) не открывается для профиля по умолчанию —
# защита от кражи cookies. С копией профиля в своей папке порт работает.
#
# ПЕРЕД ЗАПУСКОМ ЗАКРОЙ CHROME полностью (иначе файлы cookies заблокированы).
#
#   pwsh -File scripts\copy_chrome_profile.ps1
#   pwsh -File scripts\copy_chrome_profile.ps1 -Profile "Profile 1" -Target C:\dev\chrome-kleinanzeigen
#
# Если после копирования в новом окне Kleinanzeigen просит войти — войди один раз вручную,
# дальше сессия хранится в копии профиля (cookies могут не расшифроваться после копирования).

param(
    [string]$Profile = "Default",
    [string]$Target = "C:\dev\chrome-kleinanzeigen"
)
$ErrorActionPreference = "Stop"

$src = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
if (-not (Test-Path (Join-Path $src $Profile))) { throw "Профиль не найден: $src\$Profile" }
if (Get-Process chrome -ErrorAction SilentlyContinue) {
    throw "Chrome запущен. Закрой все окна Chrome (и значок в трее) и запусти скрипт снова."
}
if (Test-Path $Target) { throw "Папка уже есть: $Target. Удали её сама, если нужно скопировать заново." }

New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item (Join-Path $src "Local State") $Target
# кэши не нужны: без них копия в разы меньше
robocopy (Join-Path $src $Profile) (Join-Path $Target "Default") /E /R:1 /W:1 /NFL /NDL /NJH /NP `
    /XD Cache "Code Cache" GPUCache "Service Worker" DawnCache DawnGraphiteCache DawnWebGPUCache "blob_storage" | Out-Host
if ($LASTEXITCODE -ge 8) { throw "robocopy: ошибка копирования (код $LASTEXITCODE)" }

Write-Host "Готово: $Target. Дальше: pwsh -File scripts\start_kleinanzeigen_chrome.ps1 -CreateShortcut"
