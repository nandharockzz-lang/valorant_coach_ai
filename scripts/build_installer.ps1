$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root "dist"
$package = Join-Path $dist "valorant-coach-agent"
$zip = Join-Path $dist "valorant-coach-agent-windows.zip"

if (Test-Path $package) {
    Remove-Item -Recurse -Force $package
}
New-Item -ItemType Directory -Force -Path $package | Out-Null

$items = @(
    "valorant_coach",
    "static",
    "scripts",
    "README.md",
    "INSTALL.md",
    "PACKAGING.md",
    "requirements.txt",
    "run.py",
    "valorant_coach_app.py",
    "launch.bat",
    "launch_desktop.bat"
)

foreach ($item in $items) {
    $source = Join-Path $root $item
    $target = Join-Path $package $item
    if (Test-Path $source -PathType Container) {
        Copy-Item -Recurse -Force $source $target
    } elseif (Test-Path $source) {
        Copy-Item -Force $source $target
    }
}

$installScript = Join-Path $package "install_desktop_shortcut.ps1"
@'
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "VALORANT Coach Agent.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = Join-Path $root "launch_desktop.bat"
$link.WorkingDirectory = $root
$link.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
$link.Save()
Write-Host "Desktop shortcut created: $shortcut"
'@ | Set-Content -Path $installScript -Encoding ASCII

if (Test-Path $zip) {
    Remove-Item -Force $zip
}
Compress-Archive -Path $package -DestinationPath $zip
Write-Host "Installer package written: $zip"
