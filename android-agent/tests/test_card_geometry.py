"""The shape of a screen thumbnail on the board.

This file exists because the board cropped every screenshot it showed, and nobody noticed
for as long as the crop was small enough to look like a design choice.

A card was a fixed 150x300 box — 1:2 — and `object-fit: cover` put a 9:20 screenshot in it
by filling the width and clipping whatever hung over. Every phone this harness has run
against is 9:20 (720x1600 and 1080x2400 both), so every card on every board lost 10% of its
height, 5% off each end: the status bar and app bar off the top, the bottom row of controls
off the bottom. Measured on a loaded board, not inferred — card aspect 0.5000 against image
aspect 0.4500. The screens list was worse: a 26x46 tile is ~9:16, so `cover` there threw
away a fifth of the screen's *width*.

That is a thumbnail you cannot read a screen from, on a tool whose whole job is judging
screens from thumbnails, and a card that silently hides the app bar is how a "the header is
missing" defect gets filed against an app that draws one.

So the invariant, pinned in both directions:

* the card box is derived from an aspect ratio, never a hardcoded pair of numbers that can
  drift apart from the device;
* nothing that displays a screenshot uses `object-fit: cover`, which is the only way a
  correctly shaped box can still crop when the two disagree — a board holding screens from
  two devices has two ratios in it, and the odd one out must letterbox, not clip.

These are text assertions over the frontend sources because the frontend has no test
harness. They are cheap, and they catch the exact regression: someone types `cover` back in.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "frontend" / "static"

# Every rule that sizes an <img> holding a device screenshot, and the file it lives in.
# `.phone-screen-wrap img` is the live device preview and is deliberately absent: its frame
# is 240x526 (0.456), near enough to 9:20 that the clip is under two percent, and its click
# handler maps cursor position to device coordinates through the element's own box — that
# one is worth its own change, with the tap mapping re-checked, not a drive-by here.
SCREENSHOT_IMG_RULES = [
    ("css/graph.css", ".node-card img"),
    ("css/layout.css", ".screen-thumb"),
]


def _rule_body(css: str, selector: str) -> str:
    """The declarations of the first rule whose selector list is exactly `selector`."""
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if match.group(1).strip().splitlines()[-1].strip() == selector:
            return match.group(2)
    raise AssertionError(f"no rule for {selector!r}")


@pytest.mark.parametrize("filename,selector", SCREENSHOT_IMG_RULES)
def test_screenshot_tiles_never_crop(filename, selector):
    body = _rule_body((STATIC / filename).read_text(encoding="utf-8"), selector)
    fit = re.search(r"object-fit:\s*([a-z-]+)", body)
    assert fit, f"{selector} must state an object-fit; the default (fill) distorts"
    assert fit.group(1) == "contain", (
        f"{selector} uses object-fit: {fit.group(1)} — `cover` fills the box and clips the "
        "overflow, which is what cut the status bar and the bottom controls off every card"
    )


def test_screens_list_tile_is_phone_shaped():
    """The list tile has no JS behind it, so its ratio lives in the two CSS lengths."""
    body = _rule_body((STATIC / "css/layout.css").read_text(encoding="utf-8"), ".screen-thumb")
    width = float(re.search(r"width:\s*([\d.]+)px", body).group(1))
    height = float(re.search(r"height:\s*([\d.]+)px", body).group(1))
    # Within a pixel of 9:20 — the tile is small enough that rounding is the only slack.
    assert abs(width - height * 9 / 20) <= 1, (
        f"{width}x{height} is {width / height:.3f}, not the ~0.45 a phone screenshot is; "
        "`contain` will keep it uncropped but it will sit in bars"
    )


def test_card_width_is_derived_from_the_aspect_ratio():
    """A canvas card is sized in JS, and the width must follow the ratio, not be typed in.

    Height is the fixed dimension on purpose: relayoutAll spaces rows by (CARD_H + 60), and
    boards saved before this change carry absolute node positions computed from that pitch,
    so growing the height to fix the ratio would push every saved row into the header of the
    one below it.
    """
    source = (STATIC / "js/sections.js").read_text(encoding="utf-8")
    assert re.search(r"const\s+CARD_H\s*=\s*\d+", source), "CARD_H should be the fixed side"
    assert re.search(r"CARD_W\s*=\s*Math\.round\(\s*CARD_H\s*\*", source), (
        "CARD_W must be derived from CARD_H and the card aspect ratio; a literal pair of "
        "numbers is how the box came to be 1:2 while the screenshots were 9:20"
    )
    assert "9 / 20" in source, "the default aspect should stay the 9:20 every test device is"


def test_the_card_box_image_is_built_from_the_current_card_size():
    """vis sizes a node to its (invisible) image's native pixels — see layout.js.

    If that image is ever built once from constants captured at module load, changing the
    card shape stops moving the node box and the card goes back to being the wrong shape
    with the image squeezed inside it.
    """
    source = (STATIC / "js/render.js").read_text(encoding="utf-8")
    box = re.search(r"function cardBoxImage\(\)\s*\{(.*?)\n\}", source, re.S)
    assert box, "render.js should build the node box image in cardBoxImage()"
    assert "c.width = CARD_W" in box.group(1) and "c.height = CARD_H" in box.group(1)
