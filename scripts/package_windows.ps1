param(
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root $OutputDir

New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item -Recurse -Force -Path (Join-Path $root "valorant_coach") -Destination $out
Copy-Item -Recurse -Force -Path (Join-Path $root "static") -Destination $out
Copy-Item -Recurse -Force -Path (Join-Path $root "scripts") -Destination $out
Copy-Item -Force -Path (Join-Path $root "run.py") -Destination $out
Copy-Item -Force -Path (Join-Path $root "valorant_coach_app.py") -Destination $out
Copy-Item -Force -Path (Join-Path $root "launch.bat") -Destination $out
Copy-Item -Force -Path (Join-Path $root "launch_desktop.bat") -Destination $out
Copy-Item -Force -Path (Join-Path $root "README.md") -Destination $out
Copy-Item -Force -Path (Join-Path $root "INSTALL.md") -Destination $out
Copy-Item -Force -Path (Join-Path $root "PACKAGING.md") -Destination $out
Copy-Item -Force -Path (Join-Path $root "requirements.txt") -Destination $out

New-Item -ItemType Directory -Force -Path (Join-Path $out "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "clips") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "reports") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out "tools") | Out-Null

Write-Host "Packaged to $out"
Write-Host "Optional: place ffmpeg at $out\tools\ffmpeg\bin\ffmpeg.exe"
Write-Host "Run with launch.bat"
