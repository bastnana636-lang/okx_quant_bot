#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA53K+FQ8g72VlmgvyBN3hvYAHkOd5bNEA1elH38djQe mac-visatb-4060"

Write-Host "Installing OpenSSH Server..."
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

$userKeyDir = Join-Path $env:USERPROFILE ".ssh"
New-Item -ItemType Directory -Force -Path $userKeyDir | Out-Null
$userKeys = Join-Path $userKeyDir "authorized_keys"
if (-not (Test-Path $userKeys) -or -not (Select-String -Path $userKeys -Pattern "mac-visatb-4060" -Quiet)) {
    Add-Content -Path $userKeys -Value $pub
}

$adminKeys = Join-Path $env:ProgramData "ssh\administrators_authorized_keys"
New-Item -ItemType Directory -Force -Path (Split-Path $adminKeys) | Out-Null
if (-not (Test-Path $adminKeys) -or -not (Select-String -Path $adminKeys -Pattern "mac-visatb-4060" -Quiet)) {
    Add-Content -Path $adminKeys -Value $pub
}
icacls $adminKeys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null

$sshdConfig = Join-Path $env:ProgramData "ssh\sshd_config"
if (Test-Path $sshdConfig) {
    $text = Get-Content $sshdConfig
    $text = $text -replace '^Match Group administrators', '# Match Group administrators'
    $text = $text -replace '^\s*AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys', '# AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'
    Set-Content -Path $sshdConfig -Value $text
}

Restart-Service sshd

Write-Host ""
Write-Host ("SSH ready. user={0}" -f $env:USERNAME) -ForegroundColor Green
Get-Service sshd | Format-Table Name, Status, StartType
