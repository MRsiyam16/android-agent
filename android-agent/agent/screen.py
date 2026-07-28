"""Reading an Android UI dump: who owns the screen, what it says, what can be tapped.

Pure functions over the XML string - no device, no session, no I/O - which is why the
tests can drive them from captured dumps in tests/conftest.py.

The package ranking is the load-bearing part. `dump_hierarchy()` returns only the topmost
window and the first `package` attribute in it belongs to the status bar, so "which app am
I looking at" has to be answered by counting nodes per package. Soft keyboards are excluded
from that count: Gboard contributes ~180 nodes whenever a text field has focus and would
otherwise trip the wrong-app guard on every search flow.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from xml.etree import ElementTree as ET

import config


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Soft keyboards contribute ~180 nodes whenever a text field has focus. Counting them in the
# "who owns the screen" ranking would trip the wrong-app guard on every single search flow.
_IME_HINTS = ("inputmethod", ".ime", "keyboard", "latin")

def _parse_bounds(raw: str) -> Optional[tuple[int, int, int, int]]:
    m = _BOUNDS_RE.match(raw or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def _is_ime(package: str) -> bool:
    p = (package or "").lower()
    return any(h in p for h in _IME_HINTS)


# A single UI dump is read three times per `read_screen` — once for the ownership ranking,
# once for the visible text, once for the touchable elements — and a full-screen dump is
# hundreds of KB of XML, so parsing it three times is the most expensive thing in the tool.
# One entry is all that is needed: the three readers are handed the same string in immediate
# succession. Keyed on the string itself (identity first, since it is normally the very same
# object) so a stale dump can never be served for a new one.
#
# The parsed tree is only ever iterated and read here, never mutated, so sharing it is safe.
# A racing caller costs a cache miss and a redundant parse — never a wrong answer.
_parse_cache: tuple[str, Optional[ET.Element]] | None = None


def _parsed(xml: str) -> Optional[ET.Element]:
    """Parse a dump, reusing the immediately-preceding parse of the same string."""
    global _parse_cache
    cached = _parse_cache
    if cached is not None and (cached[0] is xml or cached[0] == xml):
        return cached[1]
    try:
        root: Optional[ET.Element] = ET.fromstring(xml)
    except ET.ParseError:
        root = None
    _parse_cache = (xml, root)
    return root


def package_ranking(xml: str) -> list[tuple[str, int]]:
    """Packages present in a dump, by node count, most first, excluding soft keyboards.

    The first `package=` attribute in a dump is usually `com.android.systemui` (the status
    bar), so reading position 0 to answer "which app is on screen" is wrong. Volume decides.
    """
    counts: dict[str, int] = {}
    root = _parsed(xml)
    if root is None:
        return []
    for node in root.iter():
        pkg = node.attrib.get("package", "")
        if not pkg or _is_ime(pkg):
            continue
        counts[pkg] = counts.get(pkg, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)


def screen_texts(xml: str) -> list[str]:
    """Every visible text and content-desc, in document order, de-duplicated.

    Deliberately includes EditText values, which `compute_state_hash` strips. The hash asks
    "is this the same screen?"; a test asks "what does it say?", and a validation message or
    a typed value is precisely the answer.
    """
    out: list[str] = []
    seen: set[str] = set()
    root = _parsed(xml)
    if root is None:
        return []
    for node in root.iter():
        if _is_ime(node.attrib.get("package", "")):
            continue
        for key in ("text", "content-desc"):
            val = (node.attrib.get(key) or "").strip()
            if val and val not in seen:
                seen.add(val)
                out.append(val)
    return out


def screen_elements(xml: str, width: int, height: int) -> list[dict[str, Any]]:
    """Touchable elements for the agent.

    Intentionally *not* `extractor.extract_actions`, for two reasons:

    * That one drops anything matching BLOCKED_ACTION_KEYWORDS (camera, share, export…),
      because autonomous exploration should not wander out of the app. A tester often needs to
      tap exactly those, so the keyword filter is dropped here.
    * It also excludes the bottom 8% of the screen to avoid the gesture nav bar. On a 2400px
      phone that is 192px — enough to hide a calculator's whole bottom keypad row. Measured:
      the agent could not find the `=` key and had to locate it by eye from a screenshot. The
      agent therefore uses its own, much tighter band (config.AGENT_EXCLUDE_*).
    """
    if not xml or width <= 0 or height <= 0:
        return []
    root = _parsed(xml)
    if root is None:
        return []

    top_bound = height * config.AGENT_EXCLUDE_TOP_PCT
    bottom_bound = height * (1 - config.AGENT_EXCLUDE_BOTTOM_PCT)

    elements: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for node in root.iter():
        a = node.attrib
        if a.get("enabled") == "false":
            continue
        clickable = a.get("clickable") == "true"
        focusable = a.get("focusable") == "true"
        editable = a.get("class", "").endswith("EditText")
        checkable = a.get("checkable") == "true"
        if not (clickable or focusable or editable or checkable):
            continue
        if _is_ime(a.get("package", "")):
            continue
        bounds = _parse_bounds(a.get("bounds", ""))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if cy < top_bound or cy > bottom_bound:
            continue
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))

        label = (a.get("text") or "").strip() or (a.get("content-desc") or "").strip()
        rid = a.get("resource-id", "")
        if not label and rid:
            label = rid.split("/")[-1]
        cls = a.get("class", "").split(".")[-1]
        elements.append({
            "id": f"{cx}_{cy}",
            "x": cx, "y": cy,
            "bounds": [x1, y1, x2, y2],
            "label": label or cls or "unlabeled",
            "class": cls,
            "resource_id": rid,
            "editable": editable,
            "checked": a.get("checked"),
            "in_appbar": cy < height * 0.14,
        })
    return elements
