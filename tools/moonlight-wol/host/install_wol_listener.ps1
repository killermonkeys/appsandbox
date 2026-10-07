<#
.SYNOPSIS
    Install the Wake-on-LAN listener on the HOST (run elevated).

.DESCRIPTION
    Registers a scheduled task that, at logon, runs wol_listener.py with highest
    privileges. The listener launches `appsandbox.exe --headless` if it is not
    already running (the headless daemon needs elevation) and starts the VM when
    Moonlight's magic packet arrives. Also opens the WoL UDP ports in the Windows
    firewall for Private networks.

    The AppSandbox GUI and the headless daemon cannot run at the same time; with
    this installed, close the GUI and manage the VM through the daemon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_wol_listener.ps1 -VmName Gaming `
        -AppSandboxExe "C:\Program Files\AppSandbox\appsandbox.exe"
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_wol_listener.ps1 -Uninstall
#>
param(
    [string]$VmName,
    [string]$AppSandboxExe,
    [string]$Python,
    [string]$Ports = '7,9,47998,47999,48000,48002,48010',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'AppSandbox WoL Listener'
$RuleName = 'AppSandbox WoL Listener (UDP)'
$Dir      = Join-Path $env:ProgramData 'AppSandbox\wol'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Write-Host "Removed '$TaskName' and its firewall rule."
    exit 0
}

if (-not $VmName) { throw '-VmName is required.' }
if (-not $AppSandboxExe) {
    foreach ($c in @("$env:ProgramFiles\AppSandbox\appsandbox.exe",
                     "${env:ProgramFiles(x86)}\AppSandbox\appsandbox.exe")) {
        if (Test-Path $c) { $AppSandboxExe = $c; break }
    }
    if (-not $AppSandboxExe) { throw 'Could not find appsandbox.exe; pass -AppSandboxExe.' }
}
if (-not (Test-Path $AppSandboxExe)) { throw "Not found: $AppSandboxExe" }

if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $cmd -or $cmd.Source -like '*WindowsApps*') {
        throw 'Python not found (or only the Microsoft Store stub). Install Python 3 for all users, or pass -Python.'
    }
    $Python = $cmd.Source
}

# Copy the listener + SDK somewhere stable (the repo checkout may move).
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# The listener runs elevated and Python imports from its own folder first, so
# only admins may write here (ProgramData's inherited ACL lets Users add files).
& icacls.exe $Dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot 'wol_listener.py') $Dir
Copy-Item -Force (Join-Path $PSScriptRoot '..\..\headless-api\asb.py') $Dir
$Listener = Join-Path $Dir 'wol_listener.py'
$Log      = Join-Path $Dir 'wol_listener.log'

# Fail now, not at logon, if the VM or its MAC can't be resolved.
& $Python.Replace('pythonw.exe', 'python.exe') -c "import sys; sys.path.insert(0, r'$Dir'); import wol_listener as w; print(w.resolve_targets([r'$VmName']))"
if ($LASTEXITCODE -ne 0) { throw 'Listener self-check failed (see above).' }

$argList = "`"$Listener`" --vm `"$VmName`" --appsandbox-exe `"$AppSandboxExe`" --ports $Ports --log `"$Log`""
$action   = New-ScheduledTaskAction -Execute $Python -Argument $argList -WorkingDirectory $Dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
                -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$runAs    = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $runAs -Force | Out-Null

Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol UDP `
    -LocalPort ($Ports -split ',') -Action Allow -Profile Private | Out-Null

Write-Host "Installed '$TaskName' (starts at logon) and firewall rule '$RuleName'."
Write-Host "  Log: $Log"
Write-Host 'Close the AppSandbox window, then start it now with:'
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
