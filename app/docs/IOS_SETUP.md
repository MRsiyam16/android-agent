# Driving an iPhone

How this harness controls a real iPhone from Windows, why the stack looks the way it does,
and every dead end that was tried first so nobody spends a morning re-discovering them.

Everything below was established on an **iPhone 12 (`iPhone13,2`), iOS 18.7.8**, driven from
Windows 11 with no Mac involved at any point.

---

## What you get, and what you do not

Working: launch and terminate apps, tap, long-press, swipe, type, hardware volume buttons,
screenshots, the full accessibility tree, crash reports.

Not available, ever, on this platform — these are missing capabilities, not unfinished work,
and `device.CAPABILITIES` declares them so callers can branch instead of failing mid-test:

| Capability | Android | iPhone |
|---|---|---|
| Deep-link launch to a screen | `am start -d` | **No.** The launch API takes a bundle id; a URL scheme is rejected as one |
| Clear app data | `pm clear` | **No.** A login persists between runs |
| Live log stream | `logcat` | **No.** Crash reports only, written seconds late |
| App switcher | yes | **No.** Not reachable through XCUITest |

---

## Why this stack

An iPhone has no ADB. There are two layers, and both are required:

* **pymobiledevice3** — the transport. Pairing, the iOS 17+ RemoteXPC tunnel, mounting the
  Developer Disk Image, launching the XCTest runner, screenshots over the DVT channel.
* **WebDriverAgent** — the hands. An XCUITest runner that must be *installed and code-signed
  onto the device*; nothing can synthesise a touch without it. This is the part Apple makes
  awkward, and it is where the whole setup either works or does not.

go-ios also appears, for exactly one job: **signing**. It is the only piece here that signs
an app bundle's nested code correctly. See [The signing trap](#the-signing-trap).

---

## One-time setup

### 1. Apple device drivers

Install **Apple Mobile Device Support** (this is the standalone driver package, not iTunes):

```bash
winget install --id Apple.AppleMobileDeviceSupport
```

Do **not** rely on the Microsoft Store "Apple Devices" app. It is sandboxed, ships no usable
transport for third-party tooling, and installing it does nothing for this setup.

If the installer fails with exit code **1603**, see
[AMDS never reports RUNNING](#amds-never-reports-running).

### 2. pymobiledevice3

```bash
pip install pymobiledevice3
```

On Windows this fails to build `lzfse`, and later `pylzss`, because neither ships a wheel and
there is no MSVC toolchain. Both are firmware/kernelcache decompression codecs that the
device-control path never touches. Two options:

* **Install MSVC Build Tools** (`Microsoft.VisualStudio.2022.BuildTools`, C++ workload) and
  let them build — ~3-4 GB, fully native.
* **Stub them.** Create `lzss.py` and `lzfse.py` in `site-packages` whose functions raise
  `NotImplementedError` with a message saying how to install the real thing. The Developer
  Disk Image mount performs IMG4 personalization and completes without them; if anything
  ever does need them, it fails loudly rather than silently returning wrong bytes.

Install the deps around the failure with `pip install --no-deps pyimg4 ipsw_parser`, then
`pip install --no-deps pymobiledevice3`, then `pip install pycryptodome`.

### 3. go-ios

Download `go-ios-win.zip` from the [go-ios releases page][go-ios] and put `ios.exe` on your
PATH. Then place **wintun.dll** (amd64, from [wintun.net][wintun], signed by WireGuard LLC)
next to `ios.exe` — without it go-ios can only use its userspace tunnel, which drops the
connection partway through transferring a test bundle.

[go-ios]: https://github.com/danielpaulus/go-ios/releases
[wintun]: https://www.wintun.net/

### 4. Plug the phone in and trust it

Use a **rear-panel USB port** on a desktop, not a front header or a hub. Unlock the phone and
tap **Trust**, then confirm the pairing record appeared:

```bash
pymobiledevice3 usbmux list
```

An empty `[]` here means nothing else will work. `C:\ProgramData\Apple\Lockdown\<udid>.plist`
should exist afterwards; if only `SystemConfiguration.plist` is there, the pairing did not
complete.

### 5. Developer Mode

```bash
pymobiledevice3 mounter auto-mount
```

This fails the first time with *"Developer Mode is disabled"* — that failure is the point, as
it is what makes the menu appear on the phone. Then, **on the device**:

**Settings → Privacy & Security → Developer Mode** → on → Restart → after reboot confirm and
enter the passcode. Also enable **Enable UI Automation** in the same screen; without it WDA
attaches but every tap and query fails.

This cannot be done from the host. `pymobiledevice3 amfi enable-developer-mode` refuses with
*"Cannot enable developer-mode when passcode is set"* — deliberate on Apple's part, so a
device cannot be opened up remotely. Do not remove the passcode to work around it.

Re-run `pymobiledevice3 mounter auto-mount` afterwards; it should report
`DeveloperDiskImage mounted successfully`.

### 6. Get a signing certificate

Install [Sideloadly][sl], load `WebDriverAgentRunner.ipa`, sign it with any Apple ID. Free
accounts work; the certificate lasts **7 days** (a paid developer account lasts a year).

[sl]: https://sideloadly.io/

Then trust the certificate on the device: **Settings → General → VPN & Device Management** →
your Apple ID → **Trust**. Until you do, launching the runner fails with
`Error code: 2, Domain: com.apple.dt.deviceprocesscontrolservice`.

**Sideloadly's install will not work on its own.** It is used here only to obtain a
certificate and a provisioning profile. Continue to the next step.

### 7. Re-sign properly — the signing trap

> **Sideloadly signs the outer `.app` but not the nested `.xctest` binary inside it.**

The test bundle lands on the device unsigned, and XCTest refuses to load it. Symptom: the
runner launches, reports `authorized: true`, then the device closes the connection at
`_IDE_startExecutingTestPlanWithProtocolVersion`. go-ios reports only a dropped socket. The
real error is only visible under `pymobiledevice3 -v`:

```
PlugIns/WebDriverAgentRunner.xctest/WebDriverAgentRunner
  → "mapped file has no cdhash, completely unsigned?
     Code has to be at least ad-hoc signed."
```

Both WebDriverAgent and DeviceKit fail identically, which is the clue that it is the signer
and not the app. Fix it by re-signing with go-ios, reusing the certificate Sideloadly already
obtained and the provisioning profile it installed on the device:

```bash
openssl pkcs12 -export -inkey key.pem -in "cert-<appleid>.pem" -out signing.p12 -passout pass:CHANGEME -legacy
```

The cert and key are in `%APPDATA%\sideloadly\`. Pull the profile off the device:

```bash
pymobiledevice3 provision dump ./profiles
```

Pick the one whose `Name` mentions `WebDriverAgentRunner`, then sign and install:

```bash
ios ui install wda --p12file=signing.p12 --p12password=CHANGEME --profile=./profiles/<uuid>.mobileprovision --bundleid=com.facebook.WebDriverAgentRunner.xctrunner.<TEAMID>
```

The bundle id carries a team-id suffix because a free Apple ID cannot claim Facebook's. Read
the exact value from `pymobiledevice3 apps list` and put it in `WDA_BUNDLE_ID`.

---

## Starting a session

Four processes, each in its own terminal, **phone unlocked**. Set
**Settings → Display & Brightness → Auto-Lock → Never** on a test device — a locked screen
produces black screenshots and fails every action underneath, which reads exactly like a
broken app.

```bash
pymobiledevice3 remote tunneld
```

Run that one **as Administrator**; it serves on `127.0.0.1:49151`.

```bash
pymobiledevice3 developer dvt xcuitest com.facebook.WebDriverAgentRunner.xctrunner.<TEAMID> --tunnel <udid>
```

Wait for `didBeginExecutingTestPlan` and `testCaseDidStart: UITestingUITests/testRunner` —
that is WDA's server loop, and it is the only proof the runner is actually up.

```bash
pymobiledevice3 usbmux forward 8100 8100 --udid <udid>
```

```bash
curl http://127.0.0.1:8100/status
```

Expect `"ready": true` and `"message": "WebDriverAgent is ready to accept commands"`.
`IOSDevice.ensure_ready()` performs this check and prints these commands on failure.

---

## Every week: the certificate expires

A free Apple ID certificate lasts **7 days**. On expiry the runner stops launching with the
same `Error code: 2` as an untrusted certificate. To refresh:

1. Re-sideload through Sideloadly (obtains a fresh certificate and profile).
2. **Re-run step 7.** Sideloadly alone still leaves the `.xctest` unsigned.
3. Re-trust under VPN & Device Management if prompted.

A paid Apple Developer account makes this yearly, and `ios sign provision appstoreconnect`
makes it scriptable with an App Store Connect API key. For an unattended rig that is the
whole argument for the $99.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `usbmux list` returns `[]` | Partial driver stack, or a front-panel USB port | Rear port, replug, confirm AMDS is alive |
| Apple MSI fails with **1603** | Installer polls AMDS for `RUNNING`, which never happens here | Install right after a reboot |
| `Developer Mode is disabled` | Expected on first mount | It is what makes the menu appear; enable on device |
| `Cannot enable developer-mode when passcode is set` | Apple blocks host-initiated enabling | Do it on the phone |
| Launch fails, `Error code: 2` | Certificate untrusted **or expired** | Trust under VPN & Device Management; re-sign if expired |
| Runner dies at `startExecutingTestPlan` | Nested `.xctest` unsigned | [The signing trap](#7-re-sign-properly--the-signing-trap) |
| `Error loading wintun.dll` | Kernel tunnel unavailable | Drop `wintun.dll` beside `ios.exe` |
| Tunnel drops mid-transfer | Userspace tunnel fallback in use | Same fix — use the kernel tunnel |
| All screenshots are black | Phone is locked | Unlock; set Auto-Lock to Never |
| `WDA returned an XML source tree` | `/source` defaults to XML | Request `/source?format=json` on the **session** route |
| `POST /session` returns 500 | Transient; observed once | Retry — `IOSDevice._call` already retries once |

### AMDS never reports RUNNING

On some machines `AppleMobileDeviceService` launches, works correctly, and never signals
`RUNNING` to the Windows service manager — it sits in `START_PENDING` forever. usbmux, the
tunnel and screenshots are all fine; the only casualty is Apple's own MSI installers, which
poll for `RUNNING` and abort with 1603. Install Apple packages immediately after a reboot,
during the window where it does report correctly.

---

## Dead ends — do not retry these

* **TrollStore** — permanent signing with no Apple ID, but the CoreTrust bug it uses was
  patched after iOS 17.0. Useless on 18.x.
* **Jailbreak** — no public jailbreak exists for A14 on iOS 17/18. checkra1n is bootrom-based
  and stops at A11.
* **URL-scheme launch** — `youtube://results?search_query=...` is rejected: *"Failed to launch
  process with bundle identifier 'youtube://…'"*. There is no URL-opening service in
  pymobiledevice3 10.3.0's `apps` or `developer` groups either.
* **Apple Devices (Microsoft Store)** — sandboxed, and contributes nothing to this stack.
* **DeviceKit instead of WebDriverAgent** — go-ios supports it, but it fails identically under
  Sideloadly signing. Once you can sign nested code correctly, WDA is the better-supported
  choice.

---

## Measured cost

From an iPhone 12 over USB. Useful for setting timeouts and for knowing when something is
genuinely stuck rather than merely slow.

| Action | Typical |
|---|---|
| Screenshot, static UI | 135–215 ms |
| Screenshot, video playing | 500–535 ms |
| Find element (identifier or predicate) | 80–850 ms |
| Tap, in place | 680–970 ms |
| Tap causing navigation | 2,600–2,900 ms |
| Type, 14 characters | ~540 ms |
| Hardware volume button | ~255 ms |
| Scroll / drag | ~3,300 ms |
| Read the full UI tree | 850–1,870 ms |
| Session create + cold app launch | ~2,565 ms, once |

Two things that catch people out:

**Screenshot cost tracks visual complexity, not resolution.** Every frame is 1170×2532, but a
dark static UI encodes in ~150 ms while a playing video takes ~500 ms — photographic frames
resist PNG compression. `IOSDevice.screenshot_b64` downscales to point coordinates, which
takes a playing-video frame from ~5 MB to ~44 KB.

**A launch returns long before the app draws.** Measured: returned in 120 ms, still showing
the home screen at 678 ms, drawn by 1.5 s. Never gate on the launch call — `wait_for_ui`
polls for readable text instead.

---

## How the adapter fits the harness

`IOSDevice` ([`ios_device.py`](../ios_device.py)) mirrors `AdbDevice` method for method, and
`device.create_device()` picks between them. Nothing above the device layer knows which
platform it is driving.

Two decisions are load-bearing:

**The dump is synthesised Android XML.** WDA returns a JSON element tree. Rather than fork
`agent/screen.py` and `extractor.py`, `dump_xml()` renders that tree into the same `<node>`
shape those readers already parse, mapping XCUI types onto the attributes they key off. The
true type is kept in an extra `ios-type` attribute they ignore, and the root carries
`platform="ios"` so any reader holding only the string can still tell the platforms apart.

**Everything is in points.** WDA taps in points (390×844) but screenshots in pixels
(1170×2532). Mixing them puts a tap a third of the way across the screen from its target, so
`window_size`, element bounds, taps and the downscaled screenshot all agree — as they already
do on Android.

### One rule that inverts

On Android, **zero touchable controls** means something is covering the screen. On iOS it
routinely means a custom-drawn surface: a video player, game or canvas publishes one element
for the whole area and nothing for the controls painted inside it. YouTube's player exposes
only `Video Player`, a fullscreen toggle and a scrubber — no play, pause, next or previous,
though a screenshot shows them plainly.

Telling an iOS agent to "deal with the overlay" there sends it hunting for a problem that does
not exist, which is this harness's definition of a false defect. `agent/prompts.py` ships a
separate iOS trap section, and `_render_screen` branches on the platform. Selector strategy
should cascade: **identifier → predicate on label → coordinate inside a named container**,
with coordinates a legitimate last resort rather than a bug.

`tests/test_ios_device.py` covers the translation without a device — `render_dump` is a pure
function over a captured tree, the same way the Android dump readers are.
