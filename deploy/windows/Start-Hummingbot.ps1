#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step($Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

Write-Step "Disable sleep / hibernate so the bot stays up"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 30
powercfg /hibernate off
try {
    powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
    powercfg /setactive SCHEME_CURRENT
} catch {}

Write-Step "Keep Tailscale connected while locked"
try { tailscale up --unattended --reset=false } catch {}

Write-Step "Ensure Docker is installed"
$docker = Get-Command docker -ErrorAction SilentlyContinue
$dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (-not $docker -and -not (Test-Path $dockerDesktop)) {
    $installer = Join-Path $env:TEMP "DockerDesktopInstaller.exe"
    Write-Step "Downloading Docker Desktop installer (winget source often fails in CN)"
    $urls = @(
        "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe",
        "https://mirrors.cloud.tencent.com/docker-toolbox/windows/docker-desktop/Docker%20Desktop%20Installer.exe"
    )
    $ok = $false
    foreach ($url in $urls) {
        try {
            curl.exe --noproxy '*' -L --retry 3 -o $installer $url
            if ((Get-Item $installer).Length -gt 10MB) { $ok = $true; break }
        } catch {}
    }
    if (-not $ok) {
        throw "Could not download Docker Desktop. Install it manually, then re-run this script."
    }
    Write-Step "Installing Docker Desktop (quiet). A reboot may be required."
    Start-Process -Wait -FilePath $installer -ArgumentList "install","--quiet","--accept-license"
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker -and -not (Test-Path $dockerDesktop)) {
        throw "Docker Desktop installed or needs a reboot. Reboot, open Docker Desktop once, then re-run this script."
    }
}

$dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
if (Test-Path $dockerDesktop) {
    $running = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
    if (-not $running) {
        Write-Step "Starting Docker Desktop"
        Start-Process $dockerDesktop
    }
}

Write-Step "Wait for Docker engine"
$deadline = (Get-Date).AddMinutes(5)
do {
    try {
        docker info | Out-Null
        $ready = $true
    } catch {
        $ready = $false
        Start-Sleep -Seconds 3
    }
} while (-not $ready -and (Get-Date) -lt $deadline)
if (-not $ready) {
    throw "Docker engine did not become ready. Open Docker Desktop, wait until it says Running, then re-run."
}

New-Item -ItemType Directory -Force -Path "$Root\logs", "$Root\data", "$Root\certs" | Out-Null
if (-not (Test-Path "$Root\docker-compose.yml")) {
    throw "docker-compose.yml missing in $Root"
}

Write-Step "Pull image and start Hummingbot"
docker compose pull hummingbot
docker compose up -d --remove-orphans hummingbot

Write-Step "Register logon task so it comes back after reboot"
$taskName = "HummingbotOKX"
$action = New-ScheduledTaskAction -Execute "docker" -Argument "compose up -d" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host ""
Write-Host "Hummingbot is starting on this Windows machine." -ForegroundColor Green
Write-Host "Check: docker compose ps"
Write-Host "Logs:  docker compose logs -f --tail 80"
Write-Host "Status: docker exec hummingbot hbot status"
Write-Host ""
Write-Host "Leave this Windows session logged in. Do not run the Mac bot at the same time."
