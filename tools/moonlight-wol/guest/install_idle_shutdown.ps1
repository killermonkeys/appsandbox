<#
.SYNOPSIS
    Install the idle-shutdown watchdog inside the Windows VM (run elevated, in the guest).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_idle_shutdown.ps1 -IdleMinutes 20
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_idle_shutdown.ps1 -Uninstall
#>
param(
    [int]$IdleMinutes = 20,
    [int]$ThresholdKbps = 1500,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AppSandbox Idle Shutdown'
$Dir      = Join-Path $env:ProgramData 'AppSandboxIdle'
$Script   = Join-Path $Dir 'idle_shutdown.ps1'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    cmd.exe /c "shutdown /a >nul 2>&1"   # cancel a pending idle shutdown, if any
    Write-Host "Removed scheduled task '$TaskName'. Logs/state remain in $Dir."
    exit 0
}

$UserDir = Join-Path $Dir 'user'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# The task runs the script as SYSTEM, so only admins may change it: drop the
# inherited ProgramData ACL (which lets Users create files) for this folder.
& icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'idle_shutdown.ps1') $Script
# Sunshine prep commands run unelevated; let Users write the -Touch / keepawake files.
New-Item -ItemType Directory -Force -Path $UserDir | Out-Null
& icacls.exe $UserDir /grant '*S-1-5-32-545:(OI)(CI)M' | Out-Null

$taskArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass " +
        "-File `"$Script`" -IdleMinutes $IdleMinutes -ThresholdKbps $ThresholdKbps"
$action   = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $taskArgs
# Every minute, forever. A time trigger keeps repeating across reboots.
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -StartWhenAvailable
$task     = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $task -Force | Out-Null

Write-Host "Installed '$TaskName': shuts the VM down after $IdleMinutes idle minutes."
Write-Host "  Log:        $Dir\idle_shutdown.log"
Write-Host "  Keep awake: create $UserDir\keepawake (delete it to re-enable)."
Write-Host ''
Write-Host 'Optional - make stream start/end count as activity. In Sunshine (Configuration >'
Write-Host 'General > Command Preparations) add a command with both Do and Undo set to:'
Write-Host "  powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`" -Touch"
