param(
    [string]$ShortcutName = "VALORANT Coach Agent",
    [string]$Launcher = "launch_app.bat",
    [switch]$StartMenu
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root $Launcher
$shell = New-Object -ComObject WScript.Shell
$folder = if ($StartMenu) {
    [Environment]::GetFolderPath("Programs")
} else {
    [Environment]::GetFolderPath("Desktop")
}
$shortcutPath = Join-Path $folder "$ShortcutName.lnk"
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.Description = "Launch VALORANT Coach Agent"
$shortcut.Save()
Write-Host "Created shortcut: $shortcutPath"
