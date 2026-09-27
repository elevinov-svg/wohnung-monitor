# Запуск Chrome для сбора Kleinanzeigen: копия профиля + порт отладки 9222.
# Это обычное окно Chrome — им можно пользоваться. Пока оно открыто, сбор работает;
# закрыто — сбор откладывается на 2 часа и пробует снова.
#
#   pwsh -File scripts\start_kleinanzeigen_chrome.ps1                  запустить
#   pwsh -File scripts\start_kleinanzeigen_chrome.ps1 -CreateShortcut  + ярлык на рабочем столе

param(
    [string]$ProfileDir = "C:\dev\chrome-kleinanzeigen",
    [int]$Port = 9222,
    [switch]$CreateShortcut
)
$ErrorActionPreference = "Stop"

# ProgramW6432 — настоящая «Program Files» даже в 32-битном PowerShell (там ProgramFiles = «Program Files (x86)»)
$appPath = @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
             "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") |
    ForEach-Object { (Get-ItemProperty $_ -ErrorAction SilentlyContinue).'(default)' }
$chrome = @("$env:ProgramW6432\Google\Chrome\Application\chrome.exe",
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") + $appPath |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $chrome) { throw "chrome.exe не найден" }
if (-not (Test-Path $ProfileDir)) { throw "Нет папки профиля $ProfileDir — сначала scripts\copy_chrome_profile.ps1" }

$chromeArgs = @("--remote-debugging-port=$Port", "--user-data-dir=`"$ProfileDir`"", "--profile-directory=Default",
                "https://www.kleinanzeigen.de/")

if ($CreateShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Chrome Kleinanzeigen.lnk"
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sh.TargetPath = $chrome
    $sh.Arguments = $chromeArgs -join " "
    $sh.Save()
    Write-Host "Ярлык: $lnk"
}

try {
    Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 3 | Out-Null
    Write-Host "Chrome с портом $Port уже запущен"
} catch {
    Start-Process $chrome -ArgumentList $chromeArgs
    Write-Host "Chrome запущен (порт $Port, профиль $ProfileDir)"
}
