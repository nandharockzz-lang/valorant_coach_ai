$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$port = if ($env:VALORANT_COACH_PORT) { $env:VALORANT_COACH_PORT } else { "8766" }
$url = "http://127.0.0.1:$port"
$script:serverProcess = $null
$script:userStopped = $false
$script:healthFailures = 0
$logDir = Join-Path $root "logs"
$logPath = Join-Path $logDir "desktop-shell.log"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Write-CoachLog([string]$message) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$stamp] $message"
}

function Start-CoachServer {
    if ($script:serverProcess -and -not $script:serverProcess.HasExited) {
        return
    }
    $script:userStopped = $false
    $script:serverProcess = Start-Process -FilePath "python" -ArgumentList "run.py" -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Write-CoachLog "Started server process $($script:serverProcess.Id)."
    Start-Sleep -Milliseconds 900
}

function Stop-CoachServer {
    $script:userStopped = $true
    if ($script:serverProcess -and -not $script:serverProcess.HasExited) {
        Stop-Process -Id $script:serverProcess.Id -Force
        Write-CoachLog "Stopped server process $($script:serverProcess.Id)."
    }
}

function Restart-CoachServer {
    if ($script:serverProcess -and -not $script:serverProcess.HasExited) {
        Stop-Process -Id $script:serverProcess.Id -Force
        Write-CoachLog "Restarted server process $($script:serverProcess.Id)."
    }
    $script:serverProcess = $null
    $script:userStopped = $false
    $script:healthFailures = 0
    Start-CoachServer
}

function Test-CoachHealth {
    if ($script:userStopped) {
        return
    }
    if (-not $script:serverProcess -or $script:serverProcess.HasExited) {
        Write-CoachLog "Server process exited unexpectedly. Restarting."
        Start-CoachServer
        return
    }
    try {
        Invoke-WebRequest -Uri "$url/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
        $script:healthFailures = 0
    } catch {
        $script:healthFailures += 1
        Write-CoachLog "Health check failed ($script:healthFailures): $($_.Exception.Message)"
        if ($script:healthFailures -ge 3) {
            Restart-CoachServer
        }
    }
}

function Open-Folder([string]$name) {
    $path = Join-Path $root $name
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    Start-Process $path
}

Start-CoachServer
Start-Process $url

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$open = $menu.Items.Add("Open Dashboard")
$data = $menu.Items.Add("Open Data Folder")
$clips = $menu.Items.Add("Open Clips Folder")
$reports = $menu.Items.Add("Open Reports Folder")
$restart = $menu.Items.Add("Restart Server")
$stop = $menu.Items.Add("Stop Server")
$quit = $menu.Items.Add("Quit")

$open.Add_Click({ Start-Process $url })
$data.Add_Click({ Open-Folder "data" })
$clips.Add_Click({ Open-Folder "clips" })
$reports.Add_Click({ Open-Folder "reports" })
$restart.Add_Click({ Restart-CoachServer; Start-Process $url })
$stop.Add_Click({ Stop-CoachServer })
$quit.Add_Click({
    Stop-CoachServer
    $notify.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Application
$notify.Text = "VALORANT Coach Agent"
$notify.ContextMenuStrip = $menu
$notify.Visible = $true
$notify.Add_DoubleClick({ Start-Process $url })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 10000
$timer.Add_Tick({ Test-CoachHealth })
$timer.Start()

[System.Windows.Forms.Application]::Run()
