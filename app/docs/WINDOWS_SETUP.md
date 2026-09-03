# Driving a Windows desktop app

How this harness controls a Windows app inside a headless VirtualBox VM, why the stack looks
the way it does, and what to check first when it doesn't answer.

Unlike the Android and iOS adapters, this one exists specifically so a test run never touches
your physical desktop, mouse or keyboard — every click and keystroke happens inside the VM's
own isolated framebuffer, so you can keep using your PC while a run is in progress.

---

## Quick fix: "the agent can't reach the Windows VM"

```bash
"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" list runningvms
```

**The VM isn't listed** → it never booted. `WindowsDevice._ensure_ready()` boots it
automatically on first use, so this usually means the boot itself failed or is still in
progress — check `VBoxManage list runningvms` again in ~30s, and check the VM's own log at
`VBoxManage.exe showvminfo <name> --log 0` if it still isn't up.

**The VM is listed as running, but the agent times out anyway** → almost always one of:

1. **Guest Additions aren't installed or aren't running.** `vbox.guest_ip()` reads a guest
   property Guest Additions publishes; with no Guest Additions, that property never appears
   and the adapter times out waiting for an IP that will never come. Confirm with:
   ```bash
   VBoxManage guestproperty get <name> /VirtualBox/GuestInfo/Net/0/V4/IP
   ```
   `No value set!` here means Guest Additions either aren't installed or haven't started yet.

2. **`windows_agent.py` isn't running inside the guest.** The VM can be fully booted and
   reachable while nobody is listening on port 9100 — most often because auto-logon isn't
   configured (a headless boot parks at the lock screen, and the Scheduled Task that starts
   the agent is set to run "at logon", which never fires) or the Scheduled Task itself was
   never registered. There's no way to see this from the host directly; if the guest IP
   resolves but `http://<ip>:9100/status` never answers, this is the likely cause.

---

## What you get, and what you don't

Working: launch/stop an exe, click, long-press, drag, scroll, type, the foreground window's
full UIA control tree, screenshots, Win+Tab (Task View) as a genuine app-switcher equivalent.

Not available, ever, on this platform — `device.CAPABILITIES` declares them so callers branch
instead of failing mid-test:

| Capability | Android | Windows |
|---|---|---|
| Deep-link launch to a screen | `am start -d` | **No.** No generic desktop-app URI-scheme convention |
| Clear app data | `pm clear` | **No, not the fast kind.** The only real reset is a full VM snapshot restore — a poweroff+restore+reboot that takes tens of seconds to minutes, not the sub-second in-place reset every other platform's `clear_app_data` provides. Deliberately kept out of that method; see [Resetting to a clean state](#resetting-to-a-clean-state) |
| Live log stream | `logcat` | **No, not yet.** No Windows Event Log crash reading in v1 |
| App switcher | yes | **Yes** — Win+Tab genuinely works, unlike iOS/Web |

---

## Why this stack

VirtualBox, not Hyper-V: this project's host is Windows 11 **Home**, which does not support
Hyper-V at all. VirtualBox needs no Windows edition upgrade and was already installed on this
machine. Two layers:

* **`vbox.py`** — the transport for VM *lifecycle*: boot, poweroff, snapshot restore, reading
  the guest's IP via a Guest-Additions-published property. All via the `VBoxManage` CLI,
  wrapped in `subprocess.run`, exactly the way `ios_device.py` shells out to `pymobiledevice3`
  for its own non-hot-path operations.
* **`windows_agent.py`** — the hands. A small FastAPI server that must be *running inside the
  guest*; nothing can read the UI tree or synthesize input without it. This is installed once
  during setup and started automatically by a Scheduled Task at logon.

`pywinauto`'s UIA backend is the one piece doing real work inside the guest — it walks the
Windows accessibility tree and drives clicks/keys through the same APIs a screen reader uses.

---

## One-time setup

### 1. Create the VM

New VM in VirtualBox, type **Windows 10 (64-bit)**. Give it enough RAM/disk for the target
app — 4GB RAM / 60GB disk is a reasonable floor for a modern desktop app.

### 2. Install Windows 10

From a plain Microsoft ISO (Media Creation Tool, or the direct ISO download from
microsoft.com/software-download/windows10). **You do not need a product key or activation to
use this for UI testing** — an unactivated Windows 10 install runs indefinitely with a
desktop watermark and a restricted personalization menu; neither affects UIA or input
automation. Activate later if you want the watermark gone, but nothing here requires it.

### 3. Install Guest Additions

**Devices → Insert Guest Additions CD image** inside the VM window, run the installer, reboot.
Required for `vbox.guest_ip()` to resolve anything at all — without it the adapter has no way
to find the VM on the network.

### 4. Configure auto-logon

A headless boot (`VBoxManage startvm <name> --type headless`) has nobody at the console to
type a password. Use [Sysinternals Autologon][autologon] (simplest — handles the registry
correctly) or set the `Winlogon` registry keys (`DefaultUserName`, `DefaultPassword`,
`AutoAdminLogon=1`) by hand. Without this, every boot parks at the lock screen and
`windows_agent.py`'s Scheduled Task never fires.

[autologon]: https://learn.microsoft.com/en-us/sysinternals/downloads/autologon

### 5. Disable idle lock / screensaver / sleep

**Settings → Accounts → Sign-in options → "If you've been away, when should Windows require
you to sign in again?" → Never**, and set Screen/Sleep timeouts to Never in Power options. A
VM has no real "display power" concept, but an idle lock still blocks UIA the same way a
locked phone blocks WDA.

### 6. Install Python + dependencies

Inside the guest:
```bash
pip install fastapi uvicorn[standard]
pip install -r requirements-windows-guest.txt
```
(`requirements-windows-guest.txt`, from this repo, pins `pywinauto`/`pywin32`/`mss` — kept out
of the main `requirements.txt` since those packages make no sense on a checkout that isn't
itself the automation target.)

Copy `windows_agent.py` into the guest (a shared folder is easiest: **Devices → Shared
Folders**, then copy from there — or just paste it in via clipboard sharing).

### 7. Register the control agent as a Scheduled Task

**Run at logon**, not "at startup" — UIA needs the interactive desktop session that only
exists after logon, and combined with step 4's auto-logon this makes the agent alive within
moments of `startvm --type headless` returning:

```bash
schtasks /create /tn "WindowsAgent" /tr "python C:\path\to\windows_agent.py" ^
  /sc onlogon /rl highest /f
```

Confirm it's alive after a reboot: `curl http://localhost:9100/status` from inside the guest.

### 8. Install the target app under test

Whatever you're testing. Note its exe path — that's what `package` holds for this project
(see [How the adapter fits the harness](#how-the-adapter-fits-the-harness)).

### 9. Take a clean snapshot

```bash
VBoxManage snapshot <name> take clean
```

Do this once everything above is in a known-good state — logged in, agent running, target app
installed. This is the snapshot `WindowsDevice.restore_snapshot()` targets by default
(`config.WINDOWS_DEFAULT_SNAPSHOT`, or a project-specific `snapshot_name`).

---

## Starting a session

No separate "start the stack" step the way iOS needs three terminals — `WindowsDevice` boots
the VM itself, lazily, on first use. Create the project with `platform=windows` and
`device_serial=<VM name>` (the dashboard's project-creation form, or
`POST /projects {"package": "<exe path>", "platform": "windows", "device_serial": "<vm
name>"}`), then open a module and run it. The first device call will:

1. Boot the VM headless if it isn't already running.
2. Wait for Guest Additions to publish an IP.
3. Wait for `windows_agent.py`'s `/status` to answer.

Bounded by `config.WINDOWS_VM_BOOT_TIMEOUT_SECONDS` (180s default) — a `DeviceError` after
that points at the two causes in [Quick fix](#quick-fix-the-agent-cant-reach-the-windows-vm).

### Resetting to a clean state

Not through the agent's `reset_app_data` tool — a VM snapshot restore reboots the whole
desktop and routinely takes minutes, far past the 90s ceiling every other platform's
`clear_app_data` respects. Use the dashboard's "Reset VM to clean snapshot" button next to the
module instead (`POST /device/windows/restore-snapshot`), which has no such timeout.

### Stage-1 local development, no VM at all

`WindowsDevice(serial="localhost")` bypasses the whole VM-lifecycle path and talks straight to
`http://127.0.0.1:9100` — useful for developing/debugging `windows_agent.py` itself directly
on a Windows host, since `pywinauto` works locally too. **Not a supported end-user mode** — a
real project always names an actual VM.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| VM never appears in `list runningvms` | Boot failed, or still in progress | Check `VBoxManage showvminfo <name> --log 0`; wait ~30s and retry |
| `guestproperty get .../V4/IP` returns "No value set!" | Guest Additions missing or not started | Install/reinstall Guest Additions, reboot the guest |
| VM running, IP resolves, agent never answers `/status` | `windows_agent.py`'s Scheduled Task didn't fire — usually missing auto-logon | Configure auto-logon (step 4); confirm the task's trigger is "At log on" not "At startup" |
| Every run starts at the Windows lock screen | Idle lock still enabled despite step 5 | Recheck sign-in/sleep settings; the guest agent's `/is_locked` can confirm |
| `clear_app_data` always returns False | Not a bug — deliberate, see the capability table above | Use the "Reset VM" button / `restore_snapshot()` instead |
| `restore_snapshot` fails with "machine is locked" | Issued immediately after `poweroff` before VirtualBox released the lock | `vbox.poweroff()` already waits up to 20s for this; a much slower disk may need more |

---

## Dead ends — do not retry these

None discovered yet. This section exists for the next real failure — add to it rather than
re-discovering the same dead end twice, the way `docs/IOS_SETUP.md` does.

---

## How the adapter fits the harness

`WindowsDevice` (`windows_device.py`) mirrors `IOSDevice` method for method, and
`device.create_device()` picks between them. Nothing above the device layer knows which
platform it is driving.

**The dump is synthesised Android XML**, the same trick every non-Android adapter uses.
`windows_agent.py`'s `/dump` endpoint returns a real nested UIA control tree as JSON;
`render_dump()` walks it into the same `<node>` shape `agent/screen.py` already parses,
mapping UIA control types onto the attributes it keys off via `_UIA_TO_ANDROID_CLASS`. The
root carries `platform="windows"` so any reader holding only the string can tell the
platforms apart.

**Bounds need no rescale**, unlike iOS's points-vs-pixels split — UIA rectangles are already
absolute screen coordinates, the same units `click()` accepts.

**`serial` is a VM name; `package` is the target executable's path** — the same field
overloading `web_device.py` already uses for a URL. There is no "plug in and detect" step the
way Android/iOS have one: a Windows project's VM is always named explicitly, never guessed.

`tests/test_windows_device.py` covers the translation without a VM — `render_dump` is a pure
function over a captured tree, the same way the Android dump readers and
`tests/test_ios_device.py` are.

### A risk worth watching, not yet measured

Custom-drawn UI frameworks (a game engine, a canvas/Skia surface, sometimes Electron apps
before accessibility support is force-enabled) can publish a UIA tree as sparse as iOS's
custom-drawn-surface trap — one element for a whole region, nothing for the controls painted
inside it. Nothing in this adapter guesses at that yet; if a target app's dump comes back
suspiciously flat, screenshot before concluding anything, the same rule `IOS_TREE_CAVEAT`
teaches for iOS. Update this section with whatever's actually measured once a real target
app has been driven end to end — no numbers are invented here ahead of that.
