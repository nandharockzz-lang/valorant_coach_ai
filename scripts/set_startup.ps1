param(
    [string]$Launcher = "launch_app.bat",
    [switch]$Disable
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startup "VALORANT Coach Agent.lnk"

if ($Disable) {
    if (Test-Path $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
    }
    Write-Host "Start-at-login disabled."
    exit 0
}

$target = Join-Path $root $Launcher
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.Description = "Launch VALORANT Coach Agent"
$shortcut.Save()
Write-Host "Start-at-login enabled: $shortcutPath"
