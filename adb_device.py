"""Thin, error-hardened wrapper around uiautomator2 for a single connected device."""
from __future__ import annotations

import logging
import subprocess

import re
import time

import uiautomator2 as u2

import config
import system_memory as sysmem

logger = logging.getLogger("adb_device")


def detect_toolkit(xml: str) -> str:
    """Classify the UI toolkit from a dump, so learned waits generalise across apps.

    Timings are keyed by toolkit rather than by package on purpose: "Flutter's first frame
    takes ~12s on this machine" is knowledge that transfers to the next Flutter app;
    "com.example takes 12s" is a fact about one test target and belongs nowhere near
    system memory.
    """
    if not xml:
        return "unknown"
    ids = len(re.findall(r'resource-id="[^"]+"', xml))
    descs = len(re.findall(r'content-desc="[^"]+"', xml))
    webviews = len(re.findall(r'class="android\.webkit\.WebView"', xml))
    nodes = xml.count("<node")
    if webviews:
        return "webview"
    # Flutter publishes a semantics tree: plenty of labelled ViewGroups, almost no
    # resource-ids (native Android layouts are the other way round).
    if nodes > 5 and descs > 2 and ids <= max(1, nodes // 20):
        return "flutter"
    return "native"


class DeviceError(RuntimeError):
    """Raised when the device adapter cannot complete an operation."""


def list_serials() -> list[str]:
    """Return serials reported by `adb devices` for state == 'device' (ready, not offline)."""
    try:
        result = subprocess.run(
            [config.ADB_PATH, "devices"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(f"Could not run adb at '{config.ADB_PATH}': {exc}") from exc

    serials: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, state = line.split("\t", 1)
        if state.strip() == "device":
            serials.append(serial)
    return serials


class AdbDevice:
    """Adapter around a `uiautomator2` session for one Android device/emulator."""

    def __init__(self, serial: str | None = None):
        self.serial = serial
        try:
            self.d: u2.Device = u2.connect(serial) if serial else u2.connect()
        except Exception as exc:  # noqa: BLE001 - surface any connect failure uniformly
            raise DeviceError(f"Failed to connect to device '{serial or 'default'}': {exc}") from exc

        try:
            info = self.d.device_info
            self.serial = self.serial or info.get("serial", "unknown")
        except Exception:  # noqa: BLE001
            pass

    # -- screen state -----------------------------------------------------------
    def is_screen_on(self) -> bool:
        try:
            return bool(self.d.info.get("screenOn", True))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"is_screen_on() failed: {exc}") from exc

    def wake_screen(self) -> None:
        try:
            self.d.shell(["input", "keyevent", "KEYCODE_WAKEUP"])
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"wake_screen() failed: {exc}") from exc

    def is_locked(self) -> bool:
        """Best-effort keyguard/lockscreen detection via dumpsys window."""
        try:
            output = self.d.shell(["dumpsys", "window"]).output
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"is_locked() failed: {exc}") from exc

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("mCurrentFocus") or line.startswith("mFocusedApp"):
                if any(marker in line for marker in ("Keyguard", "Bouncer", "StatusBar")):
                    return True
            if "mDreamingLockscreen=true" in line or "isStatusBarKeyguard=true" in line:
                return True
        return False

    @property
    def window_size(self) -> tuple[int, int]:
        try:
            w, h = self.d.window_size()
            return int(w), int(h)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"window_size() failed: {exc}") from exc

    def dump_xml(self) -> str:
        try:
            return self.d.dump_hierarchy()
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"dump_hierarchy() failed: {exc}") from exc

    def screenshot_b64(self) -> str:
        """Base64 JPEG of the current screen."""
        try:
            import base64
            import io
            img = self.d.screenshot(format="pillow")
            buf = io.BytesIO()
            img.convert("RGB").save(
                buf, format="JPEG", quality=config.SCREENSHOT_QUALITY, optimize=True
            )
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"screenshot capture failed: {exc}") from exc

    def current_app(self) -> dict:
        try:
            info = self.d.app_current()
            return {
                "package": info.get("package", ""),
                "activity": info.get("activity", ""),
            }
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"app_current() failed: {exc}") from exc

    # -- actions -----------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        try:
            self.d.click(x, y)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"click({x},{y}) failed: {exc}") from exc

    def long_click(self, x: int, y: int, duration: float = 0.8) -> None:
        try:
            self.d.long_click(x, y, duration)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"long_click({x},{y}) failed: {exc}") from exc

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.2) -> None:
        try:
            self.d.swipe(fx, fy, tx, ty, duration)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"swipe({fx},{fy}->{tx},{ty}) failed: {exc}") from exc

    def scroll(self, direction: str = "down", scale: float = 0.6) -> None:
        """Swipe within the safe middle band of the screen.

        Starting a swipe near the very top or bottom edge gets intercepted by the status bar
        pull-down or the gesture nav bar, which reads afterwards as "the app navigated
        somewhere unexpected". `scale` is the fraction of screen height travelled.
        """
        w, h = self.window_size
        mid_x = w // 2
        span = int(h * max(0.1, min(scale, 0.7)) / 2)
        centre = h // 2
        if direction == "down":      # reveal content further down: drag upward
            self.swipe(mid_x, centre + span, mid_x, centre - span)
        elif direction == "up":
            self.swipe(mid_x, centre - span, mid_x, centre + span)
        elif direction == "left":
            self.swipe(int(w * 0.8), centre, int(w * 0.2), centre)
        elif direction == "right":
            self.swipe(int(w * 0.2), centre, int(w * 0.8), centre)
        else:
            raise DeviceError(f"scroll(direction={direction!r}) — expected up/down/left/right")

    def send_keys(self, text: str, clear: bool = False) -> None:
        try:
            if clear:
                self.d.clear_text()
            self.d.send_keys(text)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"send_keys({text!r}) failed: {exc}") from exc

    def press(self, key: str) -> None:
        """key: 'back' | 'home' | 'enter' | 'recent' | volume/power etc (uiautomator2 keys)."""
        try:
            self.d.press(key)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"press({key!r}) failed: {exc}") from exc

    def start_app(self, package: str) -> None:
        """Launch via `monkey`, not `am start`/`app_start` — some OEM ROMs (observed on a
        Transsion-family device) silently block background-initiated `am start` calls and
        redirect to a Settings screen instead. `monkey`'s instrumentation-based launch is
        not subject to that restriction and reliably foregrounds the app."""
        try:
            self.d.app_stop(package)
        except Exception:  # noqa: BLE001 - best-effort reset, not fatal if it fails
            pass
        try:
            self.d.shell(["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"start_app({package!r}) via monkey failed: {exc}") from exc

    def is_installed(self, package: str) -> bool:
        """Whether `package` is installed at all.

        Worth checking before blaming a failed launch on the app: `monkey -p` on a package
        that isn't there fails quietly and leaves you on the launcher, which reads exactly
        like "the app refused to start".
        """
        try:
            out = (self.d.shell(["pm", "list", "packages", package]).output or "")
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"is_installed({package!r}) failed: {exc}") from exc
        return any(line.strip() == f"package:{package}" for line in out.splitlines())

    def similar_packages(self, hint: str) -> list[str]:
        """Installed packages whose name contains `hint` — so a wrong id can be corrected
        rather than merely reported (com.google.android.calculator vs the Samsung one)."""
        token = hint.split(".")[-1] or hint
        try:
            out = (self.d.shell(["pm", "list", "packages"]).output or "")
        except Exception:  # noqa: BLE001
            return []
        names = [l.strip()[len("package:"):] for l in out.splitlines()
                 if l.strip().startswith("package:")]
        return [n for n in names if token.lower() in n.lower()][:8]

    def stop_app(self, package: str) -> None:
        try:
            self.d.app_stop(package)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"app_stop({package!r}) failed: {exc}") from exc

    def clear_app_data(self, package: str) -> bool:
        """Wipe app data, dropping any persisted session.

        Learns which `pm clear` form this ROM needs. On multi-user ROMs the bare form
        reports success while doing nothing, so the plain result is verified rather than
        trusted, and the working variant is remembered for next time.
        """
        preferred = sysmem.environment("pm_clear_variant")
        variants = [["pm", "clear", "--user", "0", package], ["pm", "clear", package]]
        if preferred == "plain":
            variants.reverse()

        for argv in variants:
            try:
                out = (self.d.shell(argv).output or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("clear_app_data via %s failed: %s", " ".join(argv), exc)
                continue
            if "Success" in out:
                variant = "user0" if "--user" in argv else "plain"
                sysmem.observe_environment(
                    "pm_clear_variant", variant,
                    evidence=f"`{' '.join(argv)}` returned Success on this ROM",
                )
                return True
        sysmem.learn(
            "pm-clear-unavailable",
            "`pm clear` did not succeed in any form; a persisted session cannot be dropped "
            "automatically on this device.",
            evidence="both the --user 0 and bare forms failed",
        )
        return False

    def wait_for_ui(self, package: str, timeout: float | None = None,
                    poll: float = 1.0) -> tuple[str, float]:
        """Block until `package` actually owns the screen with rendered content.

        Returns (xml, seconds_waited). This exists because an empty or foreign UI dump is
        the single most misleading signal the harness produces: right after a launch it
        means "not ready yet", and code that treats it as "the app is broken" invents
        defects. A splash screen publishes nodes with no text, so *text* is the readiness
        test, not node count.

        The wait budget and the observed duration both feed system memory, so the timeout
        converges on what this machine really needs instead of a hardcoded guess.
        """
        toolkit_guess = sysmem.environment("last_toolkit", "unknown")
        budget = timeout if timeout is not None else max(
            8.0, sysmem.suggest_launch_settle(toolkit_guess, default=8.0) * 1.5)

        started = time.monotonic()
        deadline = started + budget
        last_xml = ""
        while True:
            try:
                last_xml = self.dump_xml()
            except DeviceError:
                last_xml = ""
            if last_xml:
                owns = f'package="{package}"' in last_xml
                has_text = bool(re.search(r'\stext="[^"]+"', last_xml)
                                or re.search(r'content-desc="[^"]+"', last_xml))
                if owns and has_text:
                    elapsed = time.monotonic() - started
                    toolkit = detect_toolkit(last_xml)
                    sysmem.observe_launch(toolkit, elapsed)
                    sysmem.observe_environment("last_toolkit", toolkit)
                    if elapsed > 6.0:
                        sysmem.learn(
                            "slow-first-frame",
                            f"A {toolkit} UI can take {elapsed:.0f}s to publish its first "
                            "usable dump; treat an empty dump after launch as 'not ready', "
                            "not as a broken app.",
                            evidence=f"measured {elapsed:.1f}s waiting for first content",
                        )
                    return last_xml, round(elapsed, 2)
            if time.monotonic() >= deadline:
                elapsed = time.monotonic() - started
                sysmem.learn(
                    "ui-never-settled",
                    "The UI did not publish readable content within the learned budget — "
                    "screenshot the device before concluding anything about the app.",
                    evidence=f"waited {elapsed:.0f}s for {package} with no readable dump",
                )
                return last_xml, round(elapsed, 2)
            time.sleep(poll)

    # -- crash / ANR detection -----------------------------------------------------------
    def clear_logs(self) -> None:
        """Clear the device's log buffers so a later read_new_crashes() call only sees
        what happens from this point forward, not stale crashes from before the run."""
        try:
            self.d.shell(["logcat", "-c"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("clear_logs() failed: %s", exc)

    def read_new_crashes(self, package: str) -> str | None:
        """Check the main/system/crash log buffers for a crash or ANR involving `package`
        since the last call — buffers are cleared after every read, so each call only
        covers what happened since the previous one (call once per step, right after an
        action). Returns a short excerpt if something looks like a crash/ANR, else None."""
        try:
            output = self.d.shell(["logcat", "-d", "-b", "main", "-b", "system", "-b", "crash"]).output
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_new_crashes() failed: %s", exc)
            return None
        finally:
            try:
                self.d.shell(["logcat", "-c"])
            except Exception:  # noqa: BLE001
                pass

        lines = output.splitlines()
        for i, line in enumerate(lines):
            if "FATAL EXCEPTION" in line:
                window = lines[i : i + 12]
                if any(package in w for w in window):
                    return "\n".join(window)
            elif "ANR in" in line and package in line:
                return line
        return None
