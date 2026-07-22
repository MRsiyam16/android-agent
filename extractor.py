"""XML UI-dump parsing: structural state hashing and clickable-action extraction."""
from __future__ import annotations

import hashlib
import re
from xml.etree import ElementTree as ET

import config

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# Attribute values that look dynamic (clock, battery %, dates, live counters) and should
# not influence the structural state hash — two otherwise-identical screens captured a
# minute apart must hash the same.
_DYNAMIC_PATTERNS = [
    re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM|am|pm)?$"),   # clock e.g. "10:42"
    re.compile(r"^\d{1,3}\s?%$"),                                # battery / progress "87%"
    re.compile(r"^\d+$"),                                        # bare numeric counters
    re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s"),     # weekday-prefixed dates
    re.compile(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$"),          # date formats
]


def _is_dynamic(value: str) -> bool:
    if not value:
        return True
    v = value.strip()
    return any(p.match(v) for p in _DYNAMIC_PATTERNS)


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    m = _BOUNDS_RE.match(bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return x1, y1, x2, y2


def compute_state_hash(package: str, activity: str, xml: str) -> str:
    """SHA-256 over the screen's structural signature, ignoring dynamic text/timestamps."""
    signature_parts: list[str] = [package or "", activity or ""]

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # Malformed dump — hash the raw string so the state is still distinguishable,
        # but never crash the exploration loop over it.
        signature_parts.append(xml or "")
        digest = hashlib.sha256("|".join(signature_parts).encode("utf-8", "ignore")).hexdigest()
        return digest

    for node in root.iter():
        attrib = node.attrib
        cls = attrib.get("class", "")
        resource_id = attrib.get("resource-id", "")
        clickable = attrib.get("clickable", "false")
        focusable = attrib.get("focusable", "false")
        text = attrib.get("text", "")
        content_desc = attrib.get("content-desc", "")

        # Interactive elements (buttons, input fields, calculator displays) show their own
        # entered/computed value as text — that's exactly the content that must NOT fragment
        # the state, or every digit typed becomes a "new screen". Only non-interactive labels
        # (screen titles, static captions) contribute their text to the structural signature.
        is_interactive = clickable == "true" or focusable == "true"
        text_component = "" if (is_interactive or _is_dynamic(text)) else text
        desc_component = "" if (is_interactive or _is_dynamic(content_desc)) else content_desc

        signature_parts.append(f"{cls}:{resource_id}:{clickable}:{text_component}:{desc_component}")

    digest = hashlib.sha256("|".join(signature_parts).encode("utf-8", "ignore")).hexdigest()
    return digest


def _label_for(attrib: dict) -> str:
    for key in ("text", "content-desc"):
        val = attrib.get(key, "").strip()
        if val:
            return val[:80]
    resource_id = attrib.get("resource-id", "")
    if resource_id:
        return resource_id.split("/")[-1]
    cls = attrib.get("class", "")
    return cls.split(".")[-1] if cls else "Unlabeled element"


def extract_actions(xml: str, width: int, height: int) -> list[dict]:
    """Walk the UI dump and return every clickable/focusable element as a candidate action."""
    if not xml or width <= 0 or height <= 0:
        return []

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    top_bound = height * config.EXCLUDE_TOP_PCT
    bottom_bound = height * (1 - config.EXCLUDE_BOTTOM_PCT)

    actions: list[dict] = []
    seen_coords: set[tuple[int, int]] = set()

    for node in root.iter():
        attrib = node.attrib
        clickable = attrib.get("clickable") == "true"
        focusable = attrib.get("focusable") == "true"
        if not (clickable or focusable):
            continue
        if attrib.get("enabled") == "false":
            continue

        pkg = attrib.get("package", "")
        if pkg and pkg in config.BLOCKED_PACKAGES:
            continue

        bounds = _parse_bounds(attrib.get("bounds", ""))
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue

        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2

        if cy < top_bound or cy > bottom_bound:
            continue
        if cx < 0 or cx > width or cy < 0 or cy > height:
            continue

        coord_key = (cx, cy)
        if coord_key in seen_coords:
            continue
        seen_coords.add(coord_key)

        actions.append({
            "id": f"{cx}_{cy}",
            "x": cx,
            "y": cy,
            "bounds": [x1, y1, x2, y2],
            "label": _label_for(attrib),
            "class": attrib.get("class", ""),
            "resource_id": attrib.get("resource-id", ""),
            "clickable": clickable,
            "focusable": focusable,
        })

    return actions
