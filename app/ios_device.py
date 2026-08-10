"""Thin, error-hardened wrapper around WebDriverAgent for a single connected iPhone.

Mirrors `adb_device.AdbDevice` method for method, so everything above the device layer —
`agent/device_tools.py`, `agent/screen.py`, `extractor.py`, `run_agent.py` — works
unchanged. Pick one with `device.create_device()`.

Two decisions are load-bearing and worth reading before changing anything here.

**The dump is synthesised Android XML.** WebDriverAgent returns a JSON element tree, not
`uiautomator`'s XML. Rather than fork `screen.py` and `extractor.py` into iOS variants,
`dump_xml()` renders WDA's tree into the same `<node ...>` shape those readers already
parse, mapping XCUI types onto the Android attributes they key off (`clickable`,
`content-desc`, `bounds`, …). The true type is preserved in an extra `ios-type` attribute
that the Android readers ignore. This is a translation, so it is lossy — see
`_XCUI_TO_ANDROID_CLASS` for exactly how, and `IOS_TREE_CAVEAT` for what it cannot express.

**Everything is in points, not pixels.** WDA works in points (390x844 on an iPhone 12)
while its screenshots come back in pixels (1170x2532) — a 3x factor that puts a tap in the
wrong third of the screen if the two are ever mixed. `window_size`, element bounds and taps
are all points, and `screenshot_b64()` downscales to match, so a coordinate read off a
screenshot is a coordinate you can tap. That mirrors Android, where the two already agree.

Requires a running tunnel and WDA. See `ensure_ready()` for the preflight and the exact
commands.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests

import config
import system_memory as sysmem
# Shared deliberately: callers already catch adb_device.DeviceError, and raising a
# different-but-identical class here would silently slip past every one of those handlers.
from adb_device import DeviceError

logger = logging.getLogger("ios_device")


IOS_TREE_CAVEAT = (
    "iOS accessibility trees can omit controls entirely: a custom-rendered surface "
    "(video player, game, canvas/WebGL view) publishes one element for the whole area and "
    "nothing for the buttons drawn inside it. Measured on YouTube 21.30.5, whose player "
    "exposes only 'Video Player', a fullscreen toggle and a scrubber — no play, pause, next "
    "or previous. A clickable-control count of zero therefore does NOT imply a floating "
    "overlay is eating taps, the way it does on Android; on iOS it more often means the "
    "surface is drawn rather than composed. Screenshot before concluding anything."
)


# WDA element types that a tester can actually act on. `screen_elements` keys off the
# synthesised `clickable`/`focusable`/`checkable` attributes, and WDA publishes no such
# flags, so the type is the only signal available.
_INTERACTIVE = {
    "Button", "Cell", "Link", "MenuItem", "Tab", "TabBar", "SegmentedControl",
    "TextField", "SecureTextField", "SearchField", "TextView",
    "Switch", "Slider", "Stepper", "PickerWheel", "DatePicker",
}

_EDITABLE = {"TextField", "SecureTextField", "SearchField", "TextView"}

# Mapped onto Android-shaped class names because the readers above already branch on them:
# `screen.screen_elements` decides "is this a text field" with `class.endswith("EditText")`,
# and shows `class.split(".")[-1]` to the agent as the element kind. Emitting the raw
# XCUIElementType names would break the first and read badly in the second.
_XCUI_TO_ANDROID_CLASS = {
    "Button": "ios.widget.Button",
    "Cell": "ios.widget.Button",
    "Link": "ios.widget.Button",
    "MenuItem": "ios.widget.Button",
    "Tab": "ios.widget.Button",
    "SegmentedControl": "ios.widget.Button",
    "TextField": "ios.widget.EditText",
    "SecureTextField": "ios.widget.EditText",
    "SearchField": "ios.widget.EditText",
    "TextView": "ios.widget.EditText",
    "Switch": "ios.widget.Switch",
    "Slider": "ios.widget.SeekBar",
    "StaticText": "ios.widget.TextView",
    "Image": "ios.widget.ImageView",
    "NavigationBar": "ios.view.ViewGroup",
    "Keyboard": "ios.inputmethod.Keyboard",
}

# Keyboard nodes are attributed to a synthetic package containing "keyboard" so that
# `screen._is_ime` filters them exactly as it filters Gboard. Without this the iOS keyboard's
# ~40 key nodes land in the element list and the ownership ranking on every typing flow.
_IME_PACKAGE = "com.apple.keyboard"

_UDID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$|^[0-9A-Fa-f]{40}$")


def looks_like_udid(serial: str) -> bool:
    """Whether a serial has the shape of an iOS UDID rather than an adb serial."""
    return bool(_UDID_RE.match((serial or "").strip()))


def _pmd(*args: str, timeout: float = 30.0) -> str:
    """Run a `pymobiledevice3` CLI command and return stdout.

    The CLI is used rather than the Python API for the handful of non-interactive
    operations (device listing, installed apps, crash reports) because pymobiledevice3's
    in-process service classes went async and change shape between minor releases, while
    this adapter and everything calling it are synchronous. The cost is process startup on
    calls that are rare; anything on the hot path goes through WDA over HTTP instead.
    """
    try:
        result = subprocess.run(
            [config.PYMOBILEDEVICE3_PATH, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeviceError(
            f"Could not run pymobiledevice3 at '{config.PYMOBILEDEVICE3_PATH}': {exc}") from exc
    return result.stdout or ""


def _pmd_json(*args: str, timeout: float = 30.0) -> Any:
    """`_pmd` plus a tolerant JSON parse.

    pymobiledevice3 writes progress logging to stdout alongside the payload, so the text is
    trimmed to the first bracket rather than parsed whole.
    """
    out = _pmd(*args, timeout=timeout)
    start = min([i for i in (out.find("["), out.find("{")) if i != -1], default=-1)
    if start == -1:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def list_serials() -> list[str]:
    """UDIDs of iPhones reachable over usbmux (the iOS analogue of `adb devices`)."""
    devices = _pmd_json("usbmux", "list")
    if not isinstance(devices, list):
        return []
    return [d.get("Identifier", "") for d in devices if d.get("Identifier")]


def describe_serial(serial: str) -> dict[str, str]:
    """Model and iOS version for a UDID.

    Deliberately does not touch WebDriverAgent: naming a device in the title bar should not
    start a test runner, and it would fight a session already in flight — the same reasoning
    that keeps `adb_device.describe_serial` on `getprop` instead of `u2.connect()`.
    """
    out: dict[str, str] = {"serial": serial}
    devices = _pmd_json("usbmux", "list") or []
    for entry in devices:
        if entry.get("Identifier") != serial:
            continue
        product = entry.get("ProductType", "")
        out["brand"] = "Apple"
        out["model"] = _MODEL_NAMES.get(product, product or "iPhone")
        out["ios_version"] = entry.get("ProductVersion", "")
        break
    model = out.get("model", "")
    version = out.get("ios_version", "")
    out["label"] = f"{model} (iOS {version})" if model and version else (model or serial)
    return out


# Enough to name the common devices; anything absent falls back to the raw ProductType
# ("iPhone13,2"), which is ugly in the title bar but never wrong.
_MODEL_NAMES = {
    "iPhone12,1": "iPhone 11", "iPhone12,3": "iPhone 11 Pro", "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd gen)",
    "iPhone13,1": "iPhone 12 mini", "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro", "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13",
    "iPhone14,2": "iPhone 13 Pro", "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15", "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max",
    "iPad12,1": "iPad (9th gen)", "iPad12,2": "iPad (9th gen)",
}


def detect_toolkit(xml: str) -> str:
    """Classify the iOS UI toolkit from a synthesised dump.

    Returns `ios-*` names on purpose. System memory keys learned wait times by toolkit, and
    an iPhone's cold-start numbers filed under bare "native" would be averaged together with
    Android's — teaching the harness a launch budget that is wrong for both platforms.
    """
    if not xml:
        return "unknown"
    if "XCUIElementTypeWebView" in xml:
        return "ios-webview"
    nodes = xml.count("<node")
    named = len(re.findall(r'resource-id="[^"]+"', xml))
    # Flutter and React Native draw into a canvas and publish a thin semantics tree: plenty
    # of nodes, almost none carrying an accessibility identifier. UIKit and SwiftUI apps
    # expose identifiers on most controls.
    if nodes > 8 and named <= max(1, nodes // 12):
        return "ios-hybrid"
    return "ios-native"


class IOSDevice:
    """Adapter around a WebDriverAgent session for one iPhone.

    Construction is cheap and does not talk to the phone; the WDA session is opened on first
    use so that listing devices in the dashboard cannot disturb a run in flight.
    """

    def __init__(self, serial: Optional[str] = None, wda_url: Optional[str] = None):
        self.serial = serial or (list_serials() or [None])[0]
        if not self.serial:
            raise DeviceError(
                "No iPhone found over usbmux. Check the cable (a rear-panel USB port is "
                "more reliable than a front header or hub) and that the device is unlocked "
                "and trusted.")
        self.wda = (wda_url or config.WDA_URL).rstrip("/")
        self._sid: Optional[str] = None
        self._size: Optional[tuple[int, int]] = None
        self._apps_cache: tuple[float, dict[str, dict]] | None = None
        self._seen_crashes: set[str] = set()
        self._lock = threading.Lock()

    # -- transport ---------------------------------------------------------------
    def _call(self, method: str, path: str, payload: Optional[dict] = None,
              timeout: float = 60.0, _retry: bool = True) -> Any:
        """One WDA request, with a single retry for a dead session or a transient 500.

        Both retries are earned. WDA invalidates a session when the app under test is
        terminated, and every later call 404s with 'invalid session id' until a new one is
        opened. Separately, `POST /session` has been observed returning a 500 that succeeds
        immediately on retry — rare, but an unattended run makes enough calls to meet it.
        """
        url = f"{self.wda}{path}"
        try:
            response = requests.request(method, url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise DeviceError(
                f"WebDriverAgent unreachable at {self.wda}: {exc}. Is the port forward "
                f"running? See ensure_ready().") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}

        value = body.get("value")
        error = value.get("error") if isinstance(value, dict) else None

        if error in ("invalid session id", "no such session") and _retry:
            self._sid = None
            return self._call(method, path, payload, timeout, _retry=False)
        if response.status_code >= 500 and _retry:
            time.sleep(0.6)
            return self._call(method, path, payload, timeout, _retry=False)
        if error:
            raise DeviceError(f"WDA {method} {path}: {error} — "
                              f"{str(value.get('message', ''))[:200]}")
        if response.status_code >= 400:
            raise DeviceError(f"WDA {method} {path} returned HTTP {response.status_code}")
        return value

    def _session(self) -> str:
        """Open (once) a WDA session, reusing it across calls."""
        with self._lock:
            if self._sid:
                return self._sid
            value = self._call("POST", "/session", {
                "capabilities": {"alwaysMatch": {"shouldWaitForQuiescence": False}}})
            sid = (value or {}).get("sessionId")
            if not sid:
                raise DeviceError("WDA did not return a session id.")
            self._sid = sid
            return sid

    def _s(self, path: str) -> str:
        return f"/session/{self._session()}{path}"

    # -- preflight ---------------------------------------------------------------
    def ensure_ready(self) -> None:
        """Fail early, with the command that fixes it, rather than mid-test.

        Android needs none of this: adb is either there or the device is not. An iPhone
        needs a tunnel, a running test runner and an unexpired certificate, and each failure
        looks like a different bug from inside a test.
        """
        try:
            status = self._call("GET", "/status", timeout=15) or {}
        except DeviceError as exc:
            raise DeviceError(
                f"{exc}\n\nStart the stack (each in its own terminal, device unlocked):\n"
                f"  1. pymobiledevice3 remote tunneld            (as Administrator)\n"
                f"  2. pymobiledevice3 developer dvt xcuitest {config.WDA_BUNDLE_ID} "
                f"--tunnel {self.serial}\n"
                f"  3. pymobiledevice3 usbmux forward 8100 8100 --udid {self.serial}"
            ) from exc
        if not status.get("ready", True):
            raise DeviceError(f"WebDriverAgent is up but not ready: {status.get('message')}")

    # -- screen state ------------------------------------------------------------
    def is_screen_on(self) -> bool:
        """Approximated by the lock state — WDA exposes no display-power query.

        Good enough for its one caller (waking before a run) and honest about the gap: a
        device that is on but locked reads as off, which errs toward waking something that
        was already awake rather than testing against a black screen.
        """
        return not self.is_locked()

    def wake_screen(self) -> None:
        """Raise the home screen, which wakes the display.

        A passcode-locked iPhone cannot be unlocked from the host — iOS has no equivalent of
        `input keyevent 82`. Set Auto-Lock to Never on a test device; a locked screen
        produces black screenshots and fails every WDA action underneath.
        """
        try:
            self._call("POST", "/wda/homescreen", timeout=30)
        except DeviceError as exc:
            raise DeviceError(f"wake_screen() failed: {exc}") from exc

    def is_locked(self) -> bool:
        try:
            return bool(self._call("GET", self._s("/wda/locked"), timeout=20))
        except DeviceError:
            return False

    @property
    def window_size(self) -> tuple[int, int]:
        """Screen size in **points**. Cached: it cannot change without a rotation."""
        if self._size is None:
            value = self._call("GET", self._s("/window/size"), timeout=30) or {}
            try:
                self._size = int(value["width"]), int(value["height"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DeviceError(f"window_size() returned {value!r}") from exc
        return self._size

    def dump_xml(self) -> str:
        """The current screen as Android-shaped XML (see the module docstring)."""
        # `format=json` is not the default: a bare /source returns WDA's own XML rendering,
        # which is a different shape from uiautomator's and would defeat the whole point of
        # translating. The session route is used because the session-less one ignores the
        # parameter on some builds.
        #
        # The tree and the owning package come from two independent WDA endpoints, so they
        # are fetched concurrently rather than one after the other — this is the single
        # hottest call in the adapter (every read_screen, every wait_for_ui poll), and the
        # two requests have no ordering dependency worth paying twice the round-trip for.
        with ThreadPoolExecutor(max_workers=2) as pool:
            tree_future = pool.submit(self._call, "GET", self._s("/source?format=json"),
                                      None, 60)
            app_future = pool.submit(self.current_app)
            try:
                tree = tree_future.result()
            except DeviceError as exc:
                raise DeviceError(f"dump_xml() failed: {exc}") from exc
            try:
                package = app_future.result().get("package", "unknown")
            except DeviceError as exc:
                raise DeviceError(f"dump_xml() failed: {exc}") from exc
        if isinstance(tree, str):
            raise DeviceError("WDA returned an XML source tree; expected JSON. "
                              "Check the WebDriverAgent build.")
        return self._render_xml(tree or {}, package)

    def screenshot_b64(self) -> str:
        """Base64 JPEG of the current screen, downscaled to point coordinates.

        The downscale is not only for size. WDA screenshots are pixels and WDA taps are
        points; leaving them at native resolution means a coordinate measured off a
        screenshot is 3x off when tapped. Resizing to `window_size` makes the two agree, as
        they already do on Android. It also cuts a playing-video frame from ~5 MB to well
        under one, which matters because those encode ~3x slower than static UI.
        """
        try:
            raw = self._call("GET", "/screenshot", timeout=60)
            data = base64.b64decode(raw)
        except (DeviceError, ValueError, TypeError) as exc:
            raise DeviceError(f"screenshot capture failed: {exc}") from exc

        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            width, height = self.window_size
            if img.size != (width, height):
                img = img.resize((width, height), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=config.SCREENSHOT_QUALITY, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - any decode/resize failure is fatal here
            raise DeviceError(f"screenshot encode failed: {exc}") from exc

    def current_app(self) -> dict:
        """Foreground app. `activity` is always empty — iOS has no such concept."""
        try:
            info = self._call("GET", "/wda/activeAppInfo", timeout=30) or {}
        except DeviceError as exc:
            raise DeviceError(f"current_app() failed: {exc}") from exc
        return {"package": info.get("bundleId", ""), "activity": ""}

    # -- actions -----------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        try:
            self._call("POST", self._s("/wda/tap"), {"x": int(x), "y": int(y)})
        except DeviceError as exc:
            raise DeviceError(f"click({x},{y}) failed: {exc}") from exc

    def long_click(self, x: int, y: int, duration: float = 0.8) -> None:
        try:
            self._call("POST", self._s("/wda/touchAndHold"),
                       {"x": int(x), "y": int(y), "duration": float(duration)})
        except DeviceError as exc:
            raise DeviceError(f"long_click({x},{y}) failed: {exc}") from exc

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.2) -> None:
        try:
            self._call("POST", self._s("/wda/dragfromtoforduration"), {
                "fromX": int(fx), "fromY": int(fy), "toX": int(tx), "toY": int(ty),
                "duration": float(duration)}, timeout=90)
        except DeviceError as exc:
            raise DeviceError(f"swipe({fx},{fy}->{tx},{ty}) failed: {exc}") from exc

    def scroll(self, direction: str = "down", scale: float = 0.6) -> None:
        """Swipe within the safe middle band — same contract as the Android adapter.

        The edges are hazardous here too, for different reasons: a drag from the very top
        pulls Notification Centre or Control Centre down, and one from the bottom edge is
        the app-switcher gesture.
        """
        w, h = self.window_size
        mid_x = w // 2
        span = int(h * max(0.1, min(scale, 0.7)) / 2)
        centre = h // 2
        if direction == "down":
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
        """Type into whatever currently has focus.

        `clear` needs a focused element to clear, so it is a no-op when nothing is focused —
        the same situation in which `d.clear_text()` is a no-op on Android.
        """
        try:
            if clear:
                try:
                    active = self._call("GET", self._s("/element/active"), timeout=30) or {}
                    eid = active.get("ELEMENT") or active.get(
                        "element-6066-11e4-a52e-4f735466cecf")
                    if eid:
                        self._call("POST", self._s(f"/element/{eid}/clear"), timeout=60)
                except DeviceError:
                    pass
            self._call("POST", self._s("/wda/keys"), {"value": list(text)}, timeout=90)
        except DeviceError as exc:
            raise DeviceError(f"send_keys({text!r}) failed: {exc}") from exc

    def press(self, key: str) -> None:
        """key: 'back' | 'home' | 'enter' | 'volume_up' | 'volume_down'.

        'back' has no hardware equivalent on iOS; it is emulated with the system
        edge-swipe gesture, which most navigation stacks honour but a modal sheet or a
        custom gesture handler will not. 'recent' has no equivalent at all and raises rather
        than quietly doing nothing.
        """
        key = (key or "").lower()
        try:
            if key == "home":
                self._call("POST", "/wda/homescreen", timeout=30)
            elif key == "back":
                w, h = self.window_size
                self.swipe(2, h // 2, int(w * 0.7), h // 2, duration=0.25)
            elif key in ("enter", "search", "go"):
                self._call("POST", self._s("/wda/keys"), {"value": ["\n"]}, timeout=60)
            elif key in ("volume_up", "volumeup"):
                self._call("POST", self._s("/wda/pressButton"), {"name": "volumeup"})
            elif key in ("volume_down", "volumedown"):
                self._call("POST", self._s("/wda/pressButton"), {"name": "volumedown"})
            elif key == "recent":
                raise DeviceError(
                    "press('recent') has no iOS equivalent — the app switcher is not "
                    "reachable through XCUITest.")
            else:
                self._call("POST", self._s("/wda/pressButton"), {"name": key})
        except DeviceError as exc:
            raise DeviceError(f"press({key!r}) failed: {exc}") from exc

    def start_app(self, package: str) -> None:
        """Cold-launch a bundle id.

        Terminate-then-launch rather than activate, to match the Android adapter's
        stop-then-monkey: both give every run the same starting point instead of resuming
        wherever the previous one left the app.

        Note there is no iOS equivalent of launching straight to a screen: the launch API
        takes a bundle id only, and a URL scheme (`youtube://results?...`) is rejected as a
        malformed bundle id. Deep-linked entry points must be reached by tapping.
        """
        try:
            self._call("POST", self._s("/wda/apps/terminate"), {"bundleId": package}, timeout=60)
        except DeviceError:
            pass  # best-effort reset, exactly as app_stop is on Android
        try:
            self._call("POST", self._s("/wda/apps/launch"), {"bundleId": package}, timeout=120)
        except DeviceError as exc:
            raise DeviceError(f"start_app({package!r}) failed: {exc}") from exc

    def stop_app(self, package: str) -> None:
        try:
            self._call("POST", self._s("/wda/apps/terminate"), {"bundleId": package}, timeout=60)
        except DeviceError as exc:
            raise DeviceError(f"stop_app({package!r}) failed: {exc}") from exc

    def _installed_apps(self) -> dict[str, dict]:
        """Installed user apps by bundle id, cached — the CLI call costs seconds."""
        now = time.monotonic()
        if self._apps_cache and now - self._apps_cache[0] < config.IOS_APP_LIST_TTL_SECONDS:
            return self._apps_cache[1]
        data = _pmd_json("apps", "list", "--udid", self.serial, timeout=90) or {}
        apps = {k: v for k, v in data.items()
                if isinstance(v, dict) and v.get("ApplicationType") == "User"}
        self._apps_cache = (now, apps)
        return apps

    def is_installed(self, package: str) -> bool:
        """Whether `package` is installed — worth checking before blaming a failed launch."""
        try:
            return package in self._installed_apps()
        except DeviceError as exc:
            raise DeviceError(f"is_installed({package!r}) failed: {exc}") from exc

    def similar_packages(self, hint: str) -> list[str]:
        """Installed bundle ids containing `hint`, so a wrong id can be corrected."""
        token = hint.split(".")[-1] or hint
        try:
            names = list(self._installed_apps())
        except DeviceError:
            return []
        return [n for n in names if token.lower() in n.lower()][:8]

    def clear_app_data(self, package: str) -> bool:
        """Always False: iOS has no `pm clear`.

        Not an unimplemented stub — the capability does not exist. Short of uninstalling and
        reinstalling (which destroys the signing state a sideloaded test device depends on),
        an app's persisted session cannot be dropped from the host. Suites that assume a
        clean slate per run need to reset through the app's own UI (sign out) or a backend
        fixture, and any test relying on `clear_app_data` returning True will be weaker here.
        """
        sysmem.learn(
            "ios-no-app-data-clear",
            "iOS cannot clear an app's data from the host: there is no `pm clear` "
            "equivalent, so a persisted login survives between runs. Reset through the "
            "app's own sign-out flow instead of assuming a clean start.",
            evidence=f"clear_app_data({package}) is unavailable on the iOS adapter",
        )
        return False

    def wait_for_ui(self, package: str, timeout: float | None = None,
                    poll: float = 0.5,
                    cancelled: "threading.Event | None" = None) -> tuple[str, float]:
        """Block until `package` owns the screen with rendered content.

        `poll` is tighter than the Android adapter's 1.0s default: SYSTEM_MEMORY.md's own
        learned data shows iOS native screens typically settle in ~0.3-0.8s, so a 1s poll
        was routinely overshooting the actual settle time by up to a second. 0.5s catches
        the common case in one extra tick instead of two, at the cost of one extra dump on
        the slower tail — cheap now that dump_xml() fetches concurrently (see above).

        Same contract and the same reasoning as the Android adapter: an empty or foreign
        dump right after a launch means "not ready", and treating it as "the app is broken"
        invents defects. Text, not node count, is the readiness test — an iOS splash screen
        publishes a view hierarchy with nothing readable in it.

        Worth knowing on this platform: the launch call returns as soon as the process is
        spawned, well before the first frame. Measured on an iPhone 12, a launch that
        returned in 120 ms still showed the home screen 678 ms later and was only drawn by
        1.5 s. The returned pid proves nothing about readiness.
        """
        toolkit_guess = sysmem.environment("last_toolkit", "ios-native")
        budget = timeout if timeout is not None else max(
            8.0, sysmem.suggest_launch_settle(toolkit_guess, default=8.0) * 1.5)

        started = time.monotonic()
        deadline = started + budget
        last_xml = ""
        while True:
            if cancelled is not None and cancelled.is_set():
                return last_xml, round(time.monotonic() - started, 2)
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
            if cancelled is not None:
                if cancelled.wait(poll):
                    return last_xml, round(time.monotonic() - started, 2)
            else:
                time.sleep(poll)

    # -- crash detection ---------------------------------------------------------
    def clear_logs(self) -> None:
        """Snapshot the existing crash reports so `read_new_crashes` only sees later ones.

        iOS has no `logcat -c`: reports are files on the device and cannot be truncated from
        the host, so "new since last call" is tracked here instead of on the phone.
        """
        try:
            self._seen_crashes = set(self._crash_files())
        except DeviceError as exc:
            logger.warning("clear_logs() failed: %s", exc)

    def _crash_files(self) -> list[str]:
        data = _pmd_json("crash", "ls", "--udid", self.serial, timeout=60) or {}
        files = data.get("files") if isinstance(data, dict) else data
        return list(files or [])

    def read_new_crashes(self, package: str) -> str | None:
        """Crash reports naming `package` that appeared since the last call.

        Returns the report filenames, not their contents — pulling a full `.ips` would cost
        another CLI round trip per crash. Fetch one with
        `pymobiledevice3 crash cp <name> <dest>` when a report needs the stack.

        Timing caveat with no Android equivalent: `crashreporterd` writes reports
        asynchronously, several seconds after the process dies. A crash caused by the action
        you just took will often not be listed until one or two steps later, so an empty
        result immediately after a suspicious action is weak evidence of "no crash".
        """
        try:
            current = self._crash_files()
        except DeviceError as exc:
            logger.warning("read_new_crashes() failed: %s", exc)
            return None

        new = [f for f in current if f not in self._seen_crashes]
        self._seen_crashes.update(current)
        if not new:
            return None

        # Reports are named after the executable, not the bundle id, so the last dotted
        # component is the best available match ("com.google.ios.youtube" -> "youtube").
        token = (package.split(".")[-1] or package).lower()
        hits = [f for f in new if token in f.lower()]
        if not hits:
            return None
        return "Crash report(s) on device: " + ", ".join(hits[:5])

    # -- WDA JSON -> Android XML -------------------------------------------------
    def _render_xml(self, root: dict, package: str) -> str:
        return render_dump(root, package, self.serial or "")


def render_dump(root: dict, package: str, udid: str = "") -> str:
    """Render a WDA element tree as the `<node>` XML that `screen.py` already parses.

    A module-level pure function, not a method, so the translation can be tested from a
    captured tree with no device and no WebDriverAgent — the same way `screen.py`'s readers
    are tested from captured dumps in `tests/conftest.py`.
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<hierarchy rotation="0" platform="ios" udid="{_attr(udid)}">',
    ]
    _render_node(root, package, parts, in_keyboard=False)
    parts.append("</hierarchy>")
    return "".join(parts)


def _render_node(node: dict, package: str, out: list[str], in_keyboard: bool) -> None:
    if not isinstance(node, dict):
        return

    raw_type = str(node.get("type", ""))
    kind = raw_type.replace("XCUIElementType", "") or "Other"
    # Inherited down the subtree: only the Keyboard container carries the type, while the
    # individual keys under it are ordinary Buttons that would otherwise be attributed to
    # the app and land in the element list.
    in_keyboard = in_keyboard or kind == "Keyboard"

    rect = node.get("rect") or {}
    try:
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("width", 0)), int(rect.get("height", 0))
    except (TypeError, ValueError):
        x = y = w = h = 0

    enabled = _truthy(node, "isEnabled", "enabled", default=True)
    interactive = kind in _INTERACTIVE
    editable = kind in _EDITABLE
    checkable = kind == "Switch"

    # WDA overloads `value`: the typed contents for a field, "1"/"0" for a switch, and a
    # number for a slider. Only the string form is text.
    value = node.get("value")
    text = value if isinstance(value, str) else ""
    checked = ""
    if checkable:
        checked = "true" if str(value) in ("1", "true", "True") else "false"
        text = ""

    attrs = {
        "class": _XCUI_TO_ANDROID_CLASS.get(kind, "ios.view.View"),
        "ios-type": raw_type,
        "package": _IME_PACKAGE if in_keyboard else package,
        "text": text,
        "content-desc": str(node.get("label") or ""),
        "resource-id": str(node.get("name") or ""),
        "bounds": f"[{x},{y}][{x + w},{y + h}]",
        "enabled": "true" if enabled else "false",
        "clickable": "true" if interactive else "false",
        "focusable": "true" if editable else "false",
        "checkable": "true" if checkable else "false",
    }
    if checked:
        attrs["checked"] = checked

    rendered = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items())
    children = node.get("children") or []
    if children:
        out.append(f"<node {rendered}>")
        for child in children:
            _render_node(child, package, out, in_keyboard)
        out.append("</node>")
    else:
        out.append(f"<node {rendered} />")


def _truthy(node: dict, *keys: str, default: bool = False) -> bool:
    """Read a WDA boolean, which arrives as a bool or the strings 'true'/'1' by build."""
    for key in keys:
        if key in node:
            raw = node[key]
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes")
    return default


def _attr(value: Any) -> str:
    """Escape a value for an XML attribute."""
    return (str(value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\n", " ").replace("\r", " "))
