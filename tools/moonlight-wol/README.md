# Moonlight Wake-on-LAN + idle shutdown for AppSandbox VMs

Use an AppSandbox Windows VM as a Sunshine game-streaming host that powers off when nobody
is streaming and powers back on when a Moonlight client sends Wake-on-LAN.

```
 Moonlight ──WoL magic packet (VM's MAC)──▶ host: wol_listener.py ──headless API──▶ start VM
                                                                                     │
 VM: idle_shutdown.ps1 (every minute) ── no stream for N min ──▶ shutdown /s ◀───────┘
```

| Piece | Runs on | Does |
|---|---|---|
| `host/wol_listener.py` | Host PC | Listens for magic packets addressed to the VM's MAC and starts the VM through the headless daemon (launching the daemon if needed). |
| `guest/idle_shutdown.ps1` | Inside the VM | Once a minute, checks for streaming activity, and shuts the VM down after a period with none. |
| `relay/wol_relay.py` | An always-on LAN device (optional) | Turns a magic packet for the VM into one for the host, so a **sleeping** host wakes up. |

## Why stock WoL doesn't work

Sunshine reports the VM's MAC address, so Moonlight's magic packet carries the VM's MAC.
When the VM is off, AppSandbox removes its network connection, so nothing receives the
packet. And if the host PC is asleep, its network card only wakes for its **own** MAC.
`wol_listener.py` handles the first problem and `wol_relay.py` handles the second.

## Requirements

- **External network mode on the VM**, so it gets its own LAN address and a stable MAC.
  AppSandbox saves the MAC in `%ProgramData%\AppSandbox\vms.cfg`, and the listener reads it
  from there.
- **The headless daemon instead of the AppSandbox window.** The listener drives the
  daemon's API, and only one AppSandbox (window or daemon) can run at a time. Each also
  shuts down the VMs it owns when it exits. To view the VM's screen while the daemon runs,
  open its display through the API (`tools/headless-api`). Moonlight works the same either
  way.
- **Python 3 on the host**, installed for all users (not the Microsoft Store stub).
- **Sunshine running as a service in the VM** (the default install), so it's available
  right after boot without anyone logging in.

## Setup

### 1. Inside the VM (elevated PowerShell)

Copy the `guest` folder into the VM, then run:

```powershell
powershell -ExecutionPolicy Bypass -File install_idle_shutdown.ps1 -IdleMinutes 20
```

This installs `C:\ProgramData\AppSandboxIdle\idle_shutdown.ps1` and a scheduled task that
runs it once a minute as SYSTEM. The VM counts as **active** when any of these is true:

- **Network traffic.** The VM sent at least `-ThresholdKbps` (default 1500) since the
  last check. A Moonlight stream sends several Mbit/s of video even when the screen is
  static, so this is the main signal. Downloads don't count, because only outbound
  traffic is measured.
- **A recent `-Touch` call.** Optionally, so that the start and end of a stream count
  as activity: in Sunshine, go to **Configuration › General › Command Preparations**
  and add a command with both *Do* and *Undo* set to:
  `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\ProgramData\AppSandboxIdle\idle_shutdown.ps1" -Touch`
- **The keep-awake file exists:** `C:\ProgramData\AppSandboxIdle\user\keepawake`. Create
  it before a long unattended download and delete it afterwards.

The idle clock starts at boot. A VM woken by Moonlight therefore gets the full idle period
for the client to connect. If the stream ends without quitting the game (a Moonlight
"disconnect"), the traffic stops and the VM still shuts down.

When the limit is reached the script runs `shutdown /s /t 60`, and logs to
`C:\ProgramData\AppSandboxIdle\idle_shutdown.log`. AppSandbox sees the VM stop and
releases its network connection, and with no VM running the host can sleep again.

To uninstall: `install_idle_shutdown.ps1 -Uninstall`.

### 2. On the host (elevated PowerShell)

Close the AppSandbox window first, then run:

```powershell
powershell -ExecutionPolicy Bypass -File host\install_wol_listener.ps1 -VmName "Gaming" `
    -AppSandboxExe "C:\Program Files\AppSandbox\appsandbox.exe"
Start-ScheduledTask -TaskName 'AppSandbox WoL Listener'
```

The installer does three things:

- **Copies files.** `wol_listener.py` and `asb.py` go to `%ProgramData%\AppSandbox\wol`,
  with that folder locked to admins because the listener runs elevated.
- **Registers a scheduled task.** It starts the listener at your logon, with highest
  privileges because the daemon needs elevation. The listener then starts
  `appsandbox.exe --headless`.
- **Opens the firewall.** It allows inbound UDP 7, 9, 47998, 47999, 48000, 48002 and 48010,
  the ports Moonlight sends WoL to, on **Private** networks. If Windows treats your LAN as
  Public, either switch it to Private or edit the rule.

The listener ignores packets while the VM is running, and if a packet arrives while the VM
is shutting down, it waits for the shutdown to finish and then starts the VM again. It logs
to `%ProgramData%\AppSandbox\wol\wol_listener.log`.

To try it without installing:
`python host\wol_listener.py --vm Gaming --appsandbox-exe "...\appsandbox.exe" -v`.
If the VM has never had a MAC saved, pass it explicitly with `--vm Gaming=02-AB-CD-EF-12-34`.

To uninstall: `install_wol_listener.ps1 -Uninstall`.

### 3. Optional: wake a sleeping host

You only need this if you also let the **host** sleep. Run the relay on any always-on
machine on the same LAN, such as a Raspberry Pi, a NAS or an OpenWrt router with Python:

```sh
sudo python3 wol_relay.py --vm-mac 02-AB-CD-EF-12-34 --host-mac 9C-6B-00-11-22-33 \
    --broadcast 192.168.1.255
```

To find the two MAC addresses:

- **VM MAC:** the `MacAddress=` line in `vms.cfg`, or `getmac` inside the VM.
- **Host MAC:** run `getmac /v` on the host and use the physical adapter's MAC. With the
  External network active, Windows may show it on the `vEthernet` adapter instead; the
  MAC is the same.

Also enable Wake-on-LAN for the host:

- **Firmware:** turn on WoL in the BIOS/UEFI.
- **Windows:** in Device Manager, open the network adapter's properties and enable *Wake on
  Magic Packet* under Advanced. Under Power Management, tick *Allow this device to wake the
  computer*.

The relay can't use ports below 1024 without root, so either run it with `sudo` or drop
ports 7 and 9 with `--ports`. The relay doesn't need AppSandbox or `asb.py`.

Here's how waking from sleep goes:

1. You click the PC in Moonlight, which sends WoL.
2. The relay wakes the host.
3. The listener resumes (the daemon is still running), but it slept through that first
   packet. Click the PC in Moonlight again: the listener catches that packet and starts
   the VM.
4. About 30–60 seconds later, Sunshine is up and the PC shows as online in Moonlight.

## Notes and limitations

- **Pause or save isn't supported.** Pausing or saving a GPU-PV VM isn't supported, so this
  uses a full shutdown and a cold boot. Expect roughly 30–60 seconds from the wake to
  being able to stream.
- **The host network blinks.** Stopping the VM may tear down AppSandbox's External network
  if no other VM uses it, which makes the host's network connection drop briefly as the
  bridge is removed and re-created. The listener listens on all addresses, so it keeps
  working through this.
- **Busy traffic can keep the VM up.** Heavy outbound traffic that isn't streaming
  (seeding, cloud backup uploads) also counts as activity. Raise `-ThresholdKbps` if
  that's a problem.
- **The host must be logged in.** The scheduled task starts at your logon. To also handle
  a host that boots fresh from WoL, enable auto-logon or change the task's trigger.
