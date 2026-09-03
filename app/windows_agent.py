"""In-guest control server for a Windows desktop app under test.

Runs INSIDE the Windows VM (or, for stage-1 development, directly on a Windows host with no
VM at all — see docs/WINDOWS_SETUP.md and `windows_device.py`'s `serial="localhost"` bypass).
`windows_device.py` on the orchestrator side is the only client; this file has no knowledge of
projects, modules or the dashboard, mirroring how WebDriverAgent knows nothing about this
codebase either.

**Everything UIA-related runs on one dedicated worker thread.** `pywinauto`'s UIA backend goes
through `comtypes`, which requires `CoInitialize()` per OS thread and does not tolerate being
driven from whichever thread a request happens to land on. `uvicorn`/Starlette hands each sync
request to an arbitrary thread-pool thread, so every UIA touch is marshalled onto one dedicated
thread via `_UIA` — the same shape `web_device.py` uses for Playwright's sync API, for the same
reason: a COM/library handle that is only safe from the thread that created it.

**Binding 0.0.0.0 is intentional here**, unlike `config.SERVER_HOST`'s loopback-only default on
the real host machine — that default guards a physical machine against LAN exposure, which does
not apply inside a VM reachable only over its own isolated NAT/host-only adapter. Do not "fix"
this to match `config.py`'s reasoning; it is a different threat model.

**The dump is a real nested tree**, not the flattened list WDA/DOM readers produce — closer to
Android's actual nested `<node>` XML. `windows_device.py` renders it into that shape client-side.

Run directly: `python windows_agent.py` (installs its own deps from
`requirements-windows-guest.txt` — not part of the main project's `requirements.txt`, since
`pywinauto`/`pywin32`/`mss` only make sense inside the Windows target, not on every checkout).
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import mss
import pywinauto
import win32gui
import win32process
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("windows_agent")
logging.basicConfig(level=logging.INFO)

PORT = int(os.environ.get("WINDOWS_AGENT_PORT", 9100))

# Node/depth caps mirror web_device.py's iframe-recursion cap: bound pathological trees rather
# than trusting an arbitrary target app's UI complexity.
MAX_DEPTH = 40
MAX_NODES = 6000

# UIA control types that carry a togglable on/off state worth reading.
_TOGGLE_TYPES = {"CheckBox", "RadioButton"}
# UIA control types worth reading a text *value* off of, distinct from their `Name` label.
_VALUE_TYPES = {"Edit", "Document"}

app = FastAPI()

# See module docstring: every pywinauto/comtypes call is marshalled onto this one thread.
_UIA = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")


def _run_on_uia_thread(fn, *args, **kwargs):
    return _UIA.submit(fn, *args, **kwargs).result()


# --- foreground window / process -------------------------------------------------------------

def _foreground_hwnd() -> int:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("no foreground window")
    return hwnd


def _process_exe(pid: int) -> str:
    try:
        import win32api
        import win32con

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            return win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001 - best-effort identification, not fatal
        logger.debug("could not resolve exe for pid %s: %s", pid, exc)
        return ""


def _current_app_impl() -> dict[str, Any]:
    hwnd = _foreground_hwnd()
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    exe_path = _process_exe(pid)
    exe_name = os.path.basename(exe_path) if exe_path else ""
    return {"exe": exe_name, "exe_path": exe_path, "title": title, "pid": pid, "hwnd": hwnd}


# --- tree dump ---------------------------------------------------------------------------------

def _rect_of(elem_info) -> dict[str, int]:
    r = elem_info.rectangle
    return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom}


def _toggle_state(wrapper, control_type: str) -> Optional[int]:
    if control_type not in _TOGGLE_TYPES:
        return None
    try:
        return int(wrapper.get_toggle_state())
    except Exception:  # noqa: BLE001 - element doesn't implement TogglePattern
        return None


def _value_of(wrapper, control_type: str) -> str:
    if control_type not in _VALUE_TYPES:
        return ""
    try:
        return wrapper.get_value()
    except Exception:  # noqa: BLE001 - no ValuePattern; fall back to window text
        try:
            return wrapper.window_text()
        except Exception:  # noqa: BLE001
            return ""


def _walk(wrapper, depth: int, budget: list[int]) -> Optional[dict[str, Any]]:
    if depth > MAX_DEPTH or budget[0] <= 0:
        return None
    budget[0] -= 1

    info = wrapper.element_info
    control_type = info.control_type or ""
    node = {
        "control_type": control_type,
        "name": info.name or "",
        "automation_id": info.automation_id or "",
        "class_name": info.class_name or "",
        "value": _value_of(wrapper, control_type),
        "toggle_state": _toggle_state(wrapper, control_type),
        "enabled": bool(getattr(info, "enabled", True)),
        "rect": _rect_of(info),
        "children": [],
    }
    try:
        for child in wrapper.children():
            if budget[0] <= 0:
                break
            child_node = _walk(child, depth + 1, budget)
            if child_node is not None:
                node["children"].append(child_node)
    except Exception as exc:  # noqa: BLE001 - one bad child must not lose the whole subtree
        logger.debug("child walk failed at depth %s: %s", depth, exc)
    return node


def _dump_impl() -> dict[str, Any]:
    hwnd = _foreground_hwnd()
    app_conn = pywinauto.Application(backend="uia").connect(handle=hwnd)
    window = app_conn.window(handle=hwnd)
    budget = [MAX_NODES]
    tree = _walk(window, 0, budget)
    if tree is None:
        raise RuntimeError("foreground window produced an empty tree")
    return tree


# --- input ---------------------------------------------------------------------------------

def _click_impl(x: int, y: int) -> None:
    pywinauto.mouse.click(coords=(x, y))


def _long_click_impl(x: int, y: int, duration: float) -> None:
    pywinauto.mouse.press(coords=(x, y))
    time.sleep(duration)
    pywinauto.mouse.release(coords=(x, y))


def _drag_impl(fx: int, fy: int, tx: int, ty: int, duration: float) -> None:
    pywinauto.mouse.press(coords=(fx, fy))
    steps = max(1, int(duration / 0.02))
    for i in range(1, steps + 1):
        ix = fx + (tx - fx) * i // steps
        iy = fy + (ty - fy) * i // steps
        pywinauto.mouse.move(coords=(ix, iy))
        time.sleep(duration / steps)
    pywinauto.mouse.release(coords=(tx, ty))


def _scroll_impl(x: int, y: int, direction: str, amount: int) -> None:
    wheel = amount if direction == "up" else -amount
    pywinauto.mouse.scroll(coords=(x, y), wheel_dist=wheel)


def _send_keys_impl(text: str, clear: bool) -> None:
    if clear:
        pywinauto.keyboard.send_keys("^a{DELETE}")
    # with_spaces/with_tabs so literal whitespace in `text` types as-is rather than being
    # interpreted as pywinauto's own key-sequence syntax.
    pywinauto.keyboard.send_keys(text, with_spaces=True, with_tabs=True, with_newlines=True)


# Best-effort only — see windows_device.py's press() docstring for which of these are
# universal (enter/delete) versus app-dependent conventions (back/home/recent).
_PRESS_KEYS = {
    "back": "%{LEFT}",
    "home": "#d",
    "enter": "{ENTER}",
    "delete": "{DELETE}",
    "recent": "#{TAB}",
}


def _press_impl(key: str) -> None:
    sequence = _PRESS_KEYS.get(key)
    if not sequence:
        raise ValueError(f"unsupported key: {key!r}")
    pywinauto.keyboard.send_keys(sequence)


# --- process lifecycle -----------------------------------------------------------------------

def _launch_impl(path: str, args: list[str]) -> int:
    proc = subprocess.Popen([path, *args])
    return proc.pid


def _stop_impl(pid: Optional[int], exe: Optional[str]) -> None:
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    elif exe:
        subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True, check=False)
    else:
        raise ValueError("stop requires pid or exe")


def _is_locked_impl() -> bool:
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) if hwnd else ""
        class_name = win32gui.GetClassName(hwnd) if hwnd else ""
        return class_name == "Windows.UI.Core.CoreWindow" and "LogonUI" in (title or "") \
            or class_name == "LogonUIUserSwitchWindow"
    except Exception:  # noqa: BLE001 - honest "don't know" beats a crash
        return False


# --- HTTP surface ------------------------------------------------------------------------------

class ClickBody(BaseModel):
    x: int
    y: int


class LongClickBody(BaseModel):
    x: int
    y: int
    duration: float = 0.8


class DragBody(BaseModel):
    fx: int
    fy: int
    tx: int
    ty: int
    duration: float = 0.2


class ScrollBody(BaseModel):
    x: int
    y: int
    direction: str = "down"
    amount: int = 3


class SendKeysBody(BaseModel):
    text: str
    clear: bool = False


class PressBody(BaseModel):
    key: str


class LaunchBody(BaseModel):
    path: str
    args: list[str] = []


class StopBody(BaseModel):
    pid: Optional[int] = None
    exe: Optional[str] = None


def _wrap(fn, *args, **kwargs):
    try:
        return _run_on_uia_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - surface as a clean HTTP error, not a stack trace
        logger.exception("agent call failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/status")
def status():
    return {"ready": True, "pywinauto": pywinauto.__version__}


@app.get("/dump")
def dump():
    return _wrap(_dump_impl)


@app.get("/screenshot")
def screenshot():
    def _grab() -> str:
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
            png_bytes = mss.tools.to_png(shot.rgb, shot.size)
            return base64.b64encode(png_bytes).decode("ascii")
    return {"image": _wrap(_grab)}


@app.get("/window_size")
def window_size():
    def _size() -> dict[str, int]:
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return {"width": mon["width"], "height": mon["height"]}
    return _wrap(_size)


@app.get("/current_app")
def current_app():
    return _wrap(_current_app_impl)


@app.get("/is_locked")
def is_locked():
    return {"locked": _wrap(_is_locked_impl)}


@app.post("/wake")
def wake():
    def _wake() -> None:
        pywinauto.mouse.move(coords=(1, 1))
        pywinauto.mouse.move(coords=(0, 0))
    _wrap(_wake)
    return {"ok": True}


@app.post("/click")
def click(body: ClickBody):
    _wrap(_click_impl, body.x, body.y)
    return {"ok": True}


@app.post("/long_click")
def long_click(body: LongClickBody):
    _wrap(_long_click_impl, body.x, body.y, body.duration)
    return {"ok": True}


@app.post("/drag")
def drag(body: DragBody):
    _wrap(_drag_impl, body.fx, body.fy, body.tx, body.ty, body.duration)
    return {"ok": True}


@app.post("/scroll")
def scroll(body: ScrollBody):
    _wrap(_scroll_impl, body.x, body.y, body.direction, body.amount)
    return {"ok": True}


@app.post("/send_keys")
def send_keys(body: SendKeysBody):
    _wrap(_send_keys_impl, body.text, body.clear)
    return {"ok": True}


@app.post("/press")
def press(body: PressBody):
    _wrap(_press_impl, body.key)
    return {"ok": True}


@app.post("/launch")
def launch(body: LaunchBody):
    pid = _wrap(_launch_impl, body.path, body.args)
    return {"pid": pid}


@app.post("/stop")
def stop(body: StopBody):
    _wrap(_stop_impl, body.pid, body.exe)
    return {"ok": True}


@app.get("/is_installed")
def is_installed(path: str):
    return {"installed": os.path.isfile(path)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
