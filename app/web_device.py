"""Thin, error-hardened wrapper around a real Chromium tab for one browser-based target.

Mirrors `adb_device.AdbDevice` / `ios_device.IOSDevice` method for method, so everything above
the device layer — `agent/device_tools.py`, `agent/screen.py`, `extractor.py`, `run_agent.py` —
works unchanged. Pick one with `device.create_device()`.

Two decisions are load-bearing and worth reading before changing anything here.

**The dump is synthesised Android XML, exactly like the iOS adapter.** A DOM is not
`uiautomator`'s XML, so rather than fork `screen.py` into a web variant, `dump_xml()` walks the
live DOM (including open shadow roots and same/cross-origin iframes) and renders it into the
same `<node ...>` shape those readers already parse — see `render_dom()` for exactly how, and the
module docstring's "known limitations" section below for what it cannot express.

**Playwright's sync API is not thread-safe across threads.** Every device call in
`agent/device_tools.py` runs off the event loop via `asyncio.to_thread`, which hands off to a
*pool* of worker threads, not one fixed thread. A Playwright `Page` created on one thread raises
if driven from another. `WebDevice` therefore owns a dedicated single-worker executor and
marshals every Playwright call onto that one thread internally — `_call()` is the one door in.
The outer `asyncio.to_thread` just blocks on that call, same as today; the actual browser work
never leaves its home thread.

**Known v1 limitations**, stated rather than engineered around:
- Closed shadow DOM is invisible (the same shape of honesty as iOS's `IOS_TREE_CAVEAT` — a
  custom-drawn surface publishes nothing).
- Iframe recursion is capped at a few levels, to bound pathological nesting.
- "Clickable" is a best-effort heuristic (native interactive tags, ARIA widget roles, a
  pointer cursor) — expect it to need tuning once it meets real, framework-heavy sites.
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlparse

import config
import system_memory as sysmem
# Shared deliberately, exactly as ios_device does: callers already catch adb_device.DeviceError,
# and raising a different-but-identical class here would silently slip past every handler.
from adb_device import DeviceError

logger = logging.getLogger("web_device")


# Native tags and ARIA roles that a tester can act on directly. Anything else is judged
# interactive by computed style (`cursor: pointer`) inside the browser-side collector, since a
# framework-built "button" is very often a bare <div> with a click handler.
_NATIVE_INTERACTIVE_TAGS = {
    "a", "button", "select", "textarea", "summary", "option", "input",
}
_EDITABLE_TYPES = {
    "text", "email", "password", "search", "tel", "url", "number", "date", "datetime-local",
    "month", "time", "week",
}
_CHECKABLE_TYPES = {"checkbox", "radio"}

_ARIA_WIDGET_ROLES = {
    "button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "textbox", "searchbox", "slider", "spinbutton", "combobox",
}

# Mapped onto Android-shaped class names because the readers above already branch on them:
# `screen.screen_elements` decides "is this a text field" with `class.endswith("EditText")`.
_TAG_TO_WEB_CLASS = {
    "a": "web.widget.Button",
    "button": "web.widget.Button",
    "summary": "web.widget.Button",
    "option": "web.widget.Button",
    "select": "web.widget.Spinner",
    "textarea": "web.widget.EditText",
    "img": "web.widget.ImageView",
}

_ROLE_TO_WEB_CLASS = {
    "button": "web.widget.Button",
    "link": "web.widget.Button",
    "tab": "web.widget.Button",
    "menuitem": "web.widget.Button",
    "checkbox": "web.widget.CheckBox",
    "radio": "web.widget.CheckBox",
    "switch": "web.widget.Switch",
    "textbox": "web.widget.EditText",
    "searchbox": "web.widget.EditText",
    "combobox": "web.widget.EditText",
    "slider": "web.widget.SeekBar",
}

# Cap on iframe recursion, so a pathological ad-nesting chain can't hang the collector.
_MAX_FRAME_DEPTH = 3

# JS injected once per `dump_xml()` call, in the context of one frame at a time (the top page,
# then each of `page.frames()`), never crossing a frame boundary itself — cross-frame reads are
# done by having Playwright evaluate this same script per-frame instead.
_COLLECTOR_JS = r"""
() => {
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== "hidden" && style.display !== "none";
  }

  function looksClickable(el, tag) {
    if (el.hasAttribute("onclick")) return true;
    const tabindex = el.getAttribute("tabindex");
    if (tabindex !== null && tabindex !== "-1") return true;
    try {
      if (window.getComputedStyle(el).cursor === "pointer") return true;
    } catch (e) { /* detached or foreign element */ }
    return false;
  }

  const SKIP_TAGS = new Set(["script", "style", "meta", "link", "head", "noscript", "template"]);

  function walk(node, depth) {
    if (!node || node.nodeType !== 1) return null;
    const tag = node.tagName.toLowerCase();
    if (SKIP_TAGS.has(tag)) return null;
    if (!isVisible(node)) return null;

    const rect = node.getBoundingClientRect();
    const role = (node.getAttribute("role") || "").toLowerCase();
    const type = (node.getAttribute("type") || "").toLowerCase();
    const ownText = node.matches("input, textarea, select") ? "" :
      Array.from(node.childNodes)
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim())
        .filter(Boolean)
        .join(" ");

    const children = [];
    if (depth < 40) {
      for (const child of node.children) {
        const rendered = walk(child, depth + 1);
        if (rendered) children.push(rendered);
      }
      // Open shadow roots render as if their children were inline in this same tree — a
      // shadow-hosting element with no light-DOM children still needs its shadow content.
      if (node.shadowRoot) {
        for (const child of node.shadowRoot.children) {
          const rendered = walk(child, depth + 1);
          if (rendered) children.push(rendered);
        }
      }
    }

    return {
      tag, role, type,
      id: node.id || "",
      name: node.getAttribute("name") || "",
      href: node.getAttribute("href") || "",
      ariaLabel: node.getAttribute("aria-label") || "",
      alt: node.getAttribute("alt") || "",
      placeholder: node.getAttribute("placeholder") || "",
      value: (tag === "input" || tag === "textarea" || tag === "select")
        ? String(node.value || "") : "",
      checked: !!node.checked,
      disabled: !!node.disabled,
      text: ownText,
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      clickable: looksClickable(node, tag),
      isFrame: tag === "iframe" || tag === "frame",
      children,
    };
  }

  return walk(document.documentElement, 0);
}
"""

# Run once per breakpoint by `check_responsive` — deliberately a separate, cheaper pass rather
# than reusing `_COLLECTOR_JS`'s full tree: this only needs interactive-element rects and one
# scrollWidth read, not the whole synthetic-XML tree, and runs three-plus times per sweep.
_RESPONSIVE_ISSUES_JS = r"""
() => {
  const vw = window.innerWidth;
  const issues = [];

  const sw = document.documentElement.scrollWidth;
  if (sw > vw + 1) {
    issues.push(`horizontal overflow: page content is ${sw}px wide but the viewport is `
      + `${vw}px (${sw - vw}px over) — likely a fixed-width element or missing responsive CSS`);
  }

  const rects = [];
  const nodes = document.querySelectorAll(
    'a,button,input,select,textarea,summary,[role],[onclick],[tabindex]');
  for (const el of nodes) {
    if (rects.length >= 150) break;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const label = (el.getAttribute('aria-label') || el.textContent
      || el.getAttribute('placeholder') || el.tagName || '').trim().slice(0, 40) || '(unlabeled)';
    rects.push({ label, x: r.x, y: r.y, w: r.width, h: r.height });
  }

  // Horizontal only: a bottom edge past the viewport is just "below the fold", which is
  // normal on every page and not a responsive bug — only sideways overflow/clipping is.
  for (const r of rects) {
    if (r.x < -1 || r.x + r.w > vw + 1) {
      issues.push(`'${r.label}' runs off the horizontal edge of the viewport `
        + `(left=${Math.round(r.x)}, right=${Math.round(r.x + r.w)}, viewport width=${vw})`);
    }
  }

  for (let i = 0; i < rects.length && issues.length < 20; i++) {
    for (let j = i + 1; j < rects.length && issues.length < 20; j++) {
      const a = rects[i], b = rects[j];
      const ox = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
      const oy = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
      const minArea = Math.min(a.w * a.h, b.w * b.h);
      if (minArea > 0 && (ox * oy) / minArea > 0.5) {
        issues.push(`'${a.label}' and '${b.label}' interactive elements overlap by more than `
          + `half of the smaller one's area`);
      }
    }
  }

  return issues.slice(0, 20);
}
"""


def render_dom(tree: Optional[dict], origin: str) -> str:
    """Render a collected DOM tree as the `<node>` XML that `screen.py` already parses.

    A module-level pure function, not a method — like `ios_device.render_dump` — so the
    translation is testable from a captured tree with no browser at all.
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<hierarchy rotation="0" platform="web">',
    ]
    if tree:
        _render_node(tree, origin, parts)
    parts.append("</hierarchy>")
    return "".join(parts)


def _render_node(node: dict, origin: str, out: list[str]) -> None:
    if not isinstance(node, dict):
        return

    # A subtree spliced in from a same/cross-origin iframe carries its own frame's origin —
    # propagated to it and everything under it, which is what makes `screen.package_ranking`'s
    # "another package owns the screen" guard fire correctly on a payment/ad iframe for free.
    origin = node.get("_frame_origin") or origin

    tag = str(node.get("tag", ""))
    role = str(node.get("role", ""))
    itype = str(node.get("type", ""))
    rect = node.get("rect") or {}
    try:
        x, y = int(rect.get("x", 0)), int(rect.get("y", 0))
        w, h = int(rect.get("width", 0)), int(rect.get("height", 0))
    except (TypeError, ValueError):
        x = y = w = h = 0

    editable = tag == "textarea" or (tag == "input" and itype in _EDITABLE_TYPES) \
        or role in ("textbox", "searchbox", "combobox")
    checkable = (tag == "input" and itype in _CHECKABLE_TYPES) or role in ("checkbox", "radio")
    native_interactive = tag in _NATIVE_INTERACTIVE_TAGS and not (
        tag == "input" and itype in ("hidden",))
    aria_interactive = role in _ARIA_WIDGET_ROLES
    clickable = bool(node.get("clickable")) or native_interactive or aria_interactive

    css_class = _ROLE_TO_WEB_CLASS.get(role) or _TAG_TO_WEB_CLASS.get(tag, "web.view.View")
    if editable:
        css_class = "web.widget.EditText"
    elif checkable:
        css_class = "web.widget.Switch" if role == "switch" else "web.widget.CheckBox"

    label = (node.get("ariaLabel") or node.get("alt") or node.get("placeholder")
              or node.get("text") or "").strip()
    text = "" if checkable else (str(node.get("value") or "") or node.get("text") or "")
    resource_id = node.get("id") or node.get("name") or ""

    attrs = {
        "class": css_class,
        "web-tag": tag,
        "package": origin,
        "text": text,
        "content-desc": label,
        "resource-id": resource_id,
        "bounds": f"[{x},{y}][{x + w},{y + h}]",
        "enabled": "false" if node.get("disabled") else "true",
        "clickable": "true" if clickable else "false",
        "focusable": "true" if editable else "false",
        "checkable": "true" if checkable else "false",
    }
    if checkable:
        attrs["checked"] = "true" if node.get("checked") else "false"

    rendered = " ".join(f'{k}="{_attr(v)}"' for k, v in attrs.items())
    children = node.get("children") or []
    if children:
        out.append(f"<node {rendered}>")
        for child in children:
            _render_node(child, origin, out)
        out.append("</node>")
    else:
        out.append(f"<node {rendered} />")


def _attr(value: Any) -> str:
    """Escape a value for an XML attribute."""
    return (str(value)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("\n", " ").replace("\r", " "))


def _origin_of(url: str) -> str:
    parsed = urlparse(url or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else (url or "about:blank")


def _ensure_scheme(url: str) -> str:
    """Prepend `https://` to a bare host/domain — Playwright's `goto` rejects a URL with no
    scheme outright, but a project's `package` field (this session's `serial`, and whatever
    `launch` passes in) is only ever guaranteed to be a URL's *identity*, not a fully-formed
    one; entering a project as a bare domain (e.g. typing "example.com" instead of
    "https://example.com" when creating it) is an easy, common slip to make.
    """
    if not url or url == "about:blank" or "://" in url:
        return url
    return f"https://{url}"


# Playwright raises a bare Error (no distinct subclass) whose *message* says the browser,
# context or page is gone — an OS-level crash, an out-of-memory kill, or the process getting
# reaped out from under this session. `_booted` has no way to know that happened short of
# trying an actual call, so it stays True forever and every call after the first failure
# raises the same "closed" error — until now, that meant the whole server had to be
# restarted by hand to get a fresh browser. Whatever the dead tab had loaded is gone with it
# either way, so relaunching once and retrying the call is strictly better than staying stuck.
_CLOSED_ERROR_MARKERS = (
    "has been closed", "target closed", "browser has disconnected", "connection closed",
)


def _looks_closed(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CLOSED_ERROR_MARKERS)


class WebDevice:
    """Adapter around one Playwright-driven browser tab for one website under test.

    Construction is cheap and does not open a browser; Chromium is launched on first use, off a
    dedicated worker thread (see the module docstring), so listing a project in the dashboard
    cannot spawn a browser by itself.
    """

    def __init__(self, serial: Optional[str] = None):
        # `serial` doubles as the start URL, then tracks whatever page is actually loaded —
        # legitimate for the same reason `package` doubles as bundle id on iOS: the two are the
        # same concept here (what does this session point at), just live rather than fixed.
        self.serial = _ensure_scheme(serial) if serial else "about:blank"
        self._exec = ThreadPoolExecutor(max_workers=1)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._console_errors: list[str] = []
        self._seen_console_count = 0
        self._lock = threading.Lock()
        self._booted = False

    # -- thread marshalling --------------------------------------------------------
    def _call(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run `fn` on this device's single worker thread and block for the result.

        The one door through which every Playwright call passes — see the module docstring.
        """
        return self._exec.submit(self._run_boxed, fn, *args, **kwargs).result()

    def _run_boxed(self, fn, *args: Any, **kwargs: Any) -> Any:
        self._ensure_booted()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - only a dead browser gets a relaunch + retry
            if not _looks_closed(exc):
                raise
            logger.warning(
                "browser session for %r looks closed (%s); relaunching and retrying once",
                self.serial, exc)
            self._discard_dead_session()
            self._ensure_booted()
            return fn(*args, **kwargs)

    def _discard_dead_session(self) -> None:
        """Drop every handle to a browser that is no longer there.

        Only ever called from the worker thread, same as `_ensure_booted` — there is no
        `close()`-style hop through `_exec.submit` here because we are already on it. Best
        effort throughout: a `.stop()` on a connection that is already gone is expected to
        fail too, and is not itself the problem being reported.
        """
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001 - already gone; nothing left to clean up
            pass
        self._pw = self._browser = self._context = self._page = None
        self._booted = False

    def _ensure_booted(self) -> None:
        """Launch Chromium and open one page. Only ever called from the worker thread."""
        if self._booted:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DeviceError(
                "Playwright is not installed. Run `pip install playwright` then "
                "`playwright install chromium`.") from exc
        try:
            self._pw = sync_playwright().start()
            channel = config.WEB_BROWSER_CHANNEL
            browser_type = getattr(self._pw, channel, self._pw.chromium)
            width, height = config.WEB_DEFAULT_VIEWPORT
            launch_args: list[str] = []
            if not config.WEB_HEADLESS and browser_type is self._pw.chromium:
                # Headed Chromium opens its OS window at its own default size, then
                # `new_context(viewport=...)` below applies a *different* size a beat later
                # over CDP (`Emulation.setDeviceMetricsOverride`) — two sizes racing to paint
                # is exactly what "the page flickers the moment it spawns" looks like from
                # outside. `--window-size` makes the window open already the right size (plus
                # headroom for Chromium's own window chrome), so there is nothing left to
                # visibly snap to once the viewport emulation lands. Chromium-only: Firefox
                # and WebKit take different launch flags entirely.
                launch_args = [f"--window-size={width},{height + 90}", "--window-position=0,0"]
            self._browser = browser_type.launch(headless=config.WEB_HEADLESS, args=launch_args)
            self._context = self._browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=config.WEB_SCREENSHOT_SCALE)
            self._page = self._context.new_page()
            self._page.on("console", self._on_console)
            self._page.on("pageerror", self._on_pageerror)
            self._page.set_default_navigation_timeout(config.WEB_NAV_TIMEOUT_SECONDS * 1000)
            if self.serial and self.serial != "about:blank":
                self._page.goto(self.serial, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - surface a clear, single DeviceError
            raise DeviceError(f"Could not start the browser: {exc}") from exc
        self._booted = True

    def _on_console(self, msg: Any) -> None:
        if msg.type == "error":
            self._console_errors.append(f"console.error: {msg.text}"[:400])

    def _on_pageerror(self, exc: Any) -> None:
        self._console_errors.append(f"uncaught exception: {exc}"[:400])

    def close(self) -> None:
        """Idempotent: safe to call more than once (explicitly, then again from `__del__`)."""
        def _do_close() -> None:
            try:
                if self._context is not None:
                    self._context.close()
                if self._browser is not None:
                    self._browser.close()
                if self._pw is not None:
                    self._pw.stop()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                logger.warning("close() failed: %s", exc)
        if self._booted:
            self._booted = False
            try:
                self._exec.submit(_do_close).result(timeout=15)
            except Exception as exc:  # noqa: BLE001
                logger.warning("close() failed to shut the browser down cleanly: %s", exc)
            self._exec.shutdown(wait=False)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - never raise from a destructor
            pass

    # -- screen state ---------------------------------------------------------------
    def is_screen_on(self) -> bool:
        return True

    def wake_screen(self) -> None:
        pass

    def is_locked(self) -> bool:
        return False

    @property
    def window_size(self) -> tuple[int, int]:
        def _get() -> tuple[int, int]:
            size = self._page.viewport_size or {}
            return int(size.get("width", 0)), int(size.get("height", 0))
        return self._call(_get)

    def set_viewport(self, width: int, height: int) -> tuple[int, int]:
        """Web-only capability, deliberately kept off the shared `Device` protocol.

        Callers reach this through `getattr(dev, "set_viewport", None)`, the same pattern
        already used for iOS-only capabilities like `refresh_window_size`.
        """
        def _set() -> tuple[int, int]:
            self._page.set_viewport_size({"width": int(width), "height": int(height)})
            return int(width), int(height)
        return self._call(_set)

    def responsive_issues(self) -> list[str]:
        """Layout-issue candidates at the current viewport size — horizontal overflow, an
        interactive element running off the horizontal edge, or two interactive elements
        overlapping by more than half of the smaller one's area.

        Named "candidates" deliberately: this is a heuristic over rects and computed style,
        not a verdict. `check_responsive` (agent/device_tools.py) tells the agent as much and
        routes any real one through `record_finding` with a screenshot, same as every other
        finding in this harness.
        """
        try:
            return list(self._call(lambda: self._page.evaluate(_RESPONSIVE_ISSUES_JS)) or [])
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"responsive_issues() failed: {exc}") from exc

    def dump_xml(self) -> str:
        """The current page (and its frames) as web-shaped XML — see module docstring."""
        def _dump() -> str:
            top = self._collect_frame(self._page.main_frame, depth=0)
            return render_dom(top, _origin_of(self._page.url))
        try:
            return self._call(_dump)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"dump_xml() failed: {exc}") from exc

    def _collect_frame(self, frame: Any, depth: int) -> Optional[dict]:
        """One frame's tree, with each same/cross-origin child iframe spliced in by URL match.

        Playwright drives the browser over CDP rather than through page-injected script, so it
        can read a cross-origin iframe's content directly — `frame.evaluate` is not blocked by
        the same-origin policy the way a plain `contentDocument` read from JS would be.
        """
        try:
            tree = frame.evaluate(_COLLECTOR_JS)
        except Exception as exc:  # noqa: BLE001 - a detached/navigating frame is not fatal
            logger.debug("frame collect failed: %s", exc)
            return None
        if depth >= _MAX_FRAME_DEPTH:
            return tree
        self._splice_child_frames(tree, frame, depth)
        return tree

    def _splice_child_frames(self, node: Optional[dict], parent_frame: Any, depth: int) -> None:
        """Merge each `<iframe>` node's own frame content into it, in place.

        Only descends into children that are *not* frames: an iframe's subtree comes back
        from `_collect_frame` already fully spliced (recursively, against that frame's own
        `child_frames`), so re-walking it here — against this call's `parent_frame`, which is
        the wrong frame for anything nested inside it — would at best repeat work and at worst
        mis-splice a deeper node against a frame that isn't its actual parent.
        """
        if not isinstance(node, dict):
            return
        for child in node.get("children") or []:
            if not isinstance(child, dict):
                continue
            if child.get("isFrame"):
                child_frame = self._find_child_frame(parent_frame, child)
                if child_frame is not None:
                    subtree = self._collect_frame(child_frame, depth + 1)
                    if subtree:
                        child["_frame_origin"] = _origin_of(child_frame.url)
                        child.setdefault("children", []).append(subtree)
                continue
            self._splice_child_frames(child, parent_frame, depth)

    @staticmethod
    def _find_child_frame(parent_frame: Any, iframe_node: dict) -> Optional[Any]:
        wanted_id = iframe_node.get("id") or ""
        for f in parent_frame.child_frames:
            try:
                if f.parent_frame is not parent_frame:
                    continue
            except Exception:  # noqa: BLE001
                continue
            if not wanted_id:
                return f
            try:
                el = f.frame_element()
                if el.get_attribute("id") == wanted_id:
                    return f
            except Exception:  # noqa: BLE001
                continue
        return parent_frame.child_frames[0] if parent_frame.child_frames else None

    def screenshot_b64(self) -> str:
        def _shot() -> str:
            data = self._page.screenshot(type=config.SCREENSHOT_FORMAT,
                                         quality=config.SCREENSHOT_QUALITY)
            return base64.b64encode(data).decode("ascii")
        try:
            return self._call(_shot)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"screenshot capture failed: {exc}") from exc

    def current_app(self) -> dict:
        def _get() -> dict:
            url = self._page.url
            parsed = urlparse(url)
            return {"package": parsed.netloc or url, "activity": parsed.path or "/"}
        return self._call(_get)

    # -- actions ----------------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        try:
            self._call(lambda: self._page.mouse.click(int(x), int(y)))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"click({x},{y}) failed: {exc}") from exc

    def long_click(self, x: int, y: int, duration: float = 0.8) -> None:
        def _do() -> None:
            self._page.mouse.move(int(x), int(y))
            self._page.mouse.down()
            time.sleep(duration)
            self._page.mouse.up()
        try:
            self._call(_do)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"long_click({x},{y}) failed: {exc}") from exc

    def swipe(self, fx: int, fy: int, tx: int, ty: int, duration: float = 0.2) -> None:
        def _do() -> None:
            self._page.mouse.move(int(fx), int(fy))
            self._page.mouse.down()
            steps = max(1, int(duration / 0.02))
            for i in range(1, steps + 1):
                self._page.mouse.move(
                    int(fx + (tx - fx) * i / steps), int(fy + (ty - fy) * i / steps))
            self._page.mouse.up()
        try:
            self._call(_do)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"swipe({fx},{fy}->{tx},{ty}) failed: {exc}") from exc

    def scroll(self, direction: str = "down", scale: float = 0.6) -> None:
        w, h = self.window_size
        dy = int(h * max(0.1, min(scale, 0.9)))
        dx = int(w * max(0.1, min(scale, 0.9)))
        delta = {"down": (0, dy), "up": (0, -dy), "right": (dx, 0), "left": (-dx, 0)}
        if direction not in delta:
            raise DeviceError(f"scroll(direction={direction!r}) — expected up/down/left/right")
        ddx, ddy = delta[direction]
        try:
            self._call(lambda: self._page.mouse.wheel(ddx, ddy))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"scroll({direction!r}) failed: {exc}") from exc

    def send_keys(self, text: str, clear: bool = False) -> None:
        def _do() -> None:
            if clear:
                try:
                    self._page.keyboard.press("Control+A")
                    self._page.keyboard.press("Delete")
                except Exception:  # noqa: BLE001 - nothing focused is not fatal
                    pass
            self._page.keyboard.type(text)
        try:
            self._call(_do)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"send_keys({text!r}) failed: {exc}") from exc

    def press(self, key: str) -> None:
        """key: 'back' | 'enter' | 'delete' | 'home' | 'recent'.

        'back' is browser history back — the direct web analogue of a hardware back button.
        'home' and 'recent' have no analogue in a single browser tab and raise, the same
        honesty the iOS adapter already uses for 'recent'.
        """
        key = (key or "").lower()
        try:
            if key == "back":
                self._call(lambda: self._page.go_back())
            elif key == "enter":
                self._call(lambda: self._page.keyboard.press("Enter"))
            elif key == "delete":
                self._call(lambda: self._page.keyboard.press("Backspace"))
            elif key == "reload":
                self._call(lambda: self._page.reload())
            elif key in ("home", "recent"):
                raise DeviceError(
                    f"press({key!r}) has no web equivalent — a browser tab has no home "
                    f"screen or app switcher.")
            else:
                self._call(lambda: self._page.keyboard.press(key))
        except DeviceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"press({key!r}) failed: {exc}") from exc

    def start_app(self, package: str) -> None:
        """Navigate to `package` (a URL) — the web analogue of launching an app."""
        url = _ensure_scheme(package)
        try:
            self._call(lambda: self._page.goto(url, wait_until="domcontentloaded"))
            self.serial = self._call(lambda: self._page.url)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"start_app({package!r}) failed: {exc}") from exc

    def stop_app(self, package: str) -> None:
        try:
            self._call(lambda: self._page.goto("about:blank"))
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"stop_app({package!r}) failed: {exc}") from exc

    def is_installed(self, package: str) -> bool:
        """Always True: a website has no install concept — a bad URL fails honestly on

        `start_app`'s navigation instead of being pre-checked here.
        """
        return True

    def similar_packages(self, hint: str) -> list[str]:
        return []

    def clear_app_data(self, package: str) -> bool:
        """Clears cookies and storage — genuinely supported, unlike iOS's `pm clear` gap."""
        def _do() -> bool:
            self._context.clear_cookies()
            try:
                self._page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            except Exception:  # noqa: BLE001 - a page that blocks storage access is not fatal
                pass
            return True
        try:
            return self._call(_do)
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"clear_app_data({package!r}) failed: {exc}") from exc

    def wait_for_ui(self, package: str, timeout: float | None = None, poll: float = 0.5,
                    cancelled: "threading.Event | None" = None) -> tuple[str, float]:
        """Block until the page has rendered readable content.

        Same contract as the other adapters: an empty dump means "not ready", not "broken" —
        judged on text content, not node count, since a loading skeleton can carry plenty of
        empty nodes.
        """
        budget = timeout if timeout is not None else config.WEB_NAV_TIMEOUT_SECONDS
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
            has_text = bool(re.search(r'\stext="[^"]+"', last_xml)
                            or re.search(r'content-desc="[^"]+"', last_xml))
            if last_xml and has_text:
                elapsed = time.monotonic() - started
                sysmem.observe_launch("web", elapsed)
                return last_xml, round(elapsed, 2)
            if time.monotonic() >= deadline:
                elapsed = time.monotonic() - started
                sysmem.learn(
                    "web-ui-never-settled",
                    "The page did not publish readable text within the learned budget — "
                    "screenshot the browser before concluding anything about the site.",
                    evidence=f"waited {elapsed:.0f}s for {package} with no readable dump",
                )
                return last_xml, round(elapsed, 2)
            if cancelled is not None:
                if cancelled.wait(poll):
                    return last_xml, round(time.monotonic() - started, 2)
            else:
                time.sleep(poll)

    # -- crash / error detection --------------------------------------------------------
    def clear_logs(self) -> None:
        def _do() -> None:
            self._console_errors = []
        self._call(_do)

    def read_new_crashes(self, package: str) -> str | None:
        """Console errors and uncaught exceptions since the last `clear_logs()` call.

        The web analogue of a crash report: there is no process to crash, but a JS exception or
        a logged error is the same class of signal, and unlike iOS's crash files these arrive
        synchronously — no seconds-later delay to account for.
        """
        def _get() -> list[str]:
            out = self._console_errors[:]
            return out
        try:
            errors = self._call(_get)
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_new_crashes() failed: %s", exc)
            return None
        if not errors:
            return None
        return "Console error(s): " + " | ".join(errors[:5])
