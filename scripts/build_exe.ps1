param(
    [switch]$OneFile,
    [string]$Name = "ValorantCoachAgent"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$check = Start-Process -FilePath "python" -ArgumentList @("-c", "import PyInstaller") -Wait -PassThru -WindowStyle Hidden
if ($check.ExitCode -ne 0) {
    Write-Host "PyInstaller is not installed."
    Write-Host "Install it with:"
    Write-Host "  python -m pip install pyinstaller"
    exit 1
}

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--noconsole",
    $mode,
    "--name", $Name,
    "--add-data", "static;static",
    "valorant_coach_app.py"
)

python @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$appDir = if ($OneFile) { Join-Path $root "dist" } else { Join-Path (Join-Path $root "dist") $Name }
New-Item -ItemType Directory -Force -Path (Join-Path $appDir "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $appDir "clips") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $appDir "reports") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $appDir "tools") | Out-Null

Copy-Item -Force -Path (Join-Path $root "README.md") -Destination $appDir
Copy-Item -Force -Path (Join-Path $root "INSTALL.md") -Destination $appDir
Copy-Item -Force -Path (Join-Path $root "PACKAGING.md") -Destination $appDir

Write-Host "Built $Name under $appDir"
Write-Host "Optional OCR: install Tesseract or place it on PATH."
Write-Host "Optional video analysis: place ffmpeg at $appDir\tools\ffmpeg\bin\ffmpeg.exe or on PATH."
