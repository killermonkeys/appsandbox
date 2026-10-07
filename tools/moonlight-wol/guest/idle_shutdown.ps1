<#
.SYNOPSIS
    Shut this VM down after it has been idle (not streaming) for a while.

.DESCRIPTION
    Runs inside the Windows guest, once a minute, from the scheduled task that
    install_idle_shutdown.ps1 registers. "Active" means any of:

      * the VM sent more than -ThresholdKbps of network traffic since the last
        run (a Moonlight stream is several Mbit/s of outbound video, even on a
        static screen, so this is the main signal);
      * something called this script with -Touch recently (wire it to
        Sunshine's prep commands so the start/end of a stream counts);
      * the keep-awake file exists (C:\ProgramData\AppSandboxIdle\user\keepawake).

    The idle clock starts at boot, so a VM woken by Wake-on-LAN gets the full
    -IdleMinutes for the Moonlight client to connect.

    When the VM has been idle for -IdleMinutes it runs `shutdown /s`. The VM
    powers off, AppSandbox sees it stop, and the host is free to sleep.

.PARAMETER Touch
    Just mark "active now" and exit. Safe to call as a normal user.
#>
param(
    [int]$IdleMinutes = 20,
    [int]$ThresholdKbps = 1500,
    [int]$WarningSeconds = 60,
    [switch]$Touch
)

$ErrorActionPreference = 'Stop'
$Dir       = Join-Path $env:ProgramData 'AppSandboxIdle'
$StateFile = Join-Path $Dir 'state.json'
# Writable by normal users (Sunshine runs prep commands unelevated); the
# script and state stay admin-only because the task runs this as SYSTEM.
$UserDir   = Join-Path $Dir 'user'
$TouchFile = Join-Path $UserDir 'last_touch'
$KeepAwake = Join-Path $UserDir 'keepawake'
$LogFile   = Join-Path $Dir 'idle_shutdown.log'

function Now { [DateTime]::UtcNow }

# ISO-8601 round-trip strings -> UTC. PowerShell 7's ConvertFrom-Json already
# yields DateTime for these, 5.1 yields strings; accept both.
function To-Utc($v) {
    if ($v -is [DateTime]) { return $v.ToUniversalTime() }
    return [DateTime]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture,
                             [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
}

function Write-Log([string]$msg) {
    $line = '{0:u} {1}' -f (Get-Date), $msg
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
        Move-Item -Force $LogFile "$LogFile.old" -ErrorAction SilentlyContinue
    }
}

if ($Touch) {
    Set-Content -Path $TouchFile -Value (Now).ToString('o')
    exit 0
}

if (-not (Test-Path $Dir)) { New-Item -ItemType Directory -Path $Dir | Out-Null }

# ---- previous state ---------------------------------------------------------
$state = $null
if (Test-Path $StateFile) {
    try { $state = Get-Content -Raw $StateFile | ConvertFrom-Json } catch { $state = $null }
}

$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
$now  = Now

# Total bytes sent on all connected adapters. Get-NetAdapterStatistics is not
# localized (unlike performance counter names), so it works on any guest language.
$sent = [UInt64]0
foreach ($s in Get-NetAdapterStatistics -ErrorAction SilentlyContinue) {
    $sent += [UInt64]$s.SentBytes
}

$lastActive = $boot
$scheduled  = $false
if ($state) {
    $prevActive = To-Utc $state.lastActive
    if ($prevActive -gt $lastActive) { $lastActive = $prevActive }
    # A shutdown scheduled before the last boot is long gone.
    $scheduled = [bool]$state.shutdownScheduled -and (To-Utc $state.sampleTime) -gt $boot
}

# ---- activity signals -------------------------------------------------------
$active  = $false
$reasons = @()

if ($state -and $state.sampleTime) {
    $prevTime = To-Utc $state.sampleTime
    $elapsed  = ($now - $prevTime).TotalSeconds
    # Ignore samples across a reboot (counters reset) or a stalled scheduler.
    if ($elapsed -gt 0 -and $prevTime -gt $boot -and $sent -ge [UInt64]$state.sentBytes) {
        $kbps = (($sent - [UInt64]$state.sentBytes) * 8 / 1000) / $elapsed
        if ($kbps -ge $ThresholdKbps) { $active = $true; $reasons += ('network {0:N0} kbps' -f $kbps) }
    }
}

if (Test-Path $TouchFile) {
    try {
        $touched = To-Utc (Get-Content -Raw $TouchFile).Trim()
        if (-not $state -or $touched -gt (To-Utc $state.sampleTime)) { $active = $true; $reasons += 'touch' }
    } catch { }
}

if (Test-Path $KeepAwake) { $active = $true; $reasons += 'keepawake' }

if ($active) { $lastActive = $now }

# ---- act ----------------------------------------------------------------------
$idleMin = ($now - $lastActive).TotalMinutes

if ($active -and $scheduled) {
    & shutdown.exe /a | Out-Null
    Write-Log ('activity ({0}); cancelled pending shutdown' -f ($reasons -join ', '))
    $scheduled = $false
}

if (-not $scheduled -and $idleMin -ge $IdleMinutes) {
    Write-Log ('idle for {0:N1} min (limit {1}); shutting down in {2}s' -f $idleMin, $IdleMinutes, $WarningSeconds)
    & shutdown.exe /s /t $WarningSeconds /d p:0:0 /c "No game stream for $IdleMinutes minutes - shutting down to save power." | Out-Null
    $scheduled = ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 1190)  # 1190 = already scheduled
}

@{
    lastActive        = $lastActive.ToString('o')
    sampleTime        = $now.ToString('o')
    sentBytes         = $sent
    shutdownScheduled = $scheduled
} | ConvertTo-Json | Set-Content -Path $StateFile
