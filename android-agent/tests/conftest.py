"""Shared fixtures and UI-dump builders for the test suite.

The tests here exist to keep a specific set of false defects from coming back. Every
incident in SYSTEM_MEMORY.md was learned from a wrong bug report; until now those lessons
lived only in prose (a comment, a prompt paragraph), which nothing enforces. A refactor
that quietly re-broke `package_ranking`'s IME exclusion or `compute_state_hash`'s dynamic
filtering would produce exactly the same wrong conclusions again, and the only signal would
be another bad report weeks later.

So the dumps below are not generic samples: each one reproduces a screen shape that has
actually misled this harness.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The modules under test (config, extractor, adb_device…) import each other by bare name and
# assume the android-agent directory is on the path, which is true when server.py or
# run_agent.py is the entry point. pytest's rootdir is not, so put it there.
ANDROID_AGENT = Path(__file__).resolve().parent.parent
if str(ANDROID_AGENT) not in sys.path:
    sys.path.insert(0, str(ANDROID_AGENT))


def node(**attrs: object) -> str:
    """One <node> element with sensible defaults, overridable per attribute."""
    base = {
        "class": "android.widget.TextView",
        "package": "com.example.app",
        "text": "",
        "content-desc": "",
        "resource-id": "",
        "clickable": "false",
        "focusable": "false",
        "enabled": "true",
        "bounds": "[0,0][100,100]",
    }
    base.update(attrs)  # type: ignore[arg-type]
    rendered = " ".join(f'{k}="{v}"' for k, v in base.items())
    return f"<node {rendered} />"


def dump(*nodes: str) -> str:
    """Wrap nodes in the hierarchy envelope uiautomator2 actually returns."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<hierarchy rotation="0">'
            + "".join(nodes) + "</hierarchy>")


# A 1080x2400 phone, the geometry every bounds-related lesson was measured on.
SCREEN_W, SCREEN_H = 1080, 2400


@pytest.fixture
def screen() -> tuple[int, int]:
    return SCREEN_W, SCREEN_H


# ---------------------------------------------------------------------------------------
# Dumps that have each caused a wrong conclusion
# ---------------------------------------------------------------------------------------
@pytest.fixture
def login_form_dump() -> str:
    """A pristine login form: app bar and primary button share the label 'Login'.

    SYSTEM_MEMORY: appbar-shares-label-with-primary-button — a first-match label lookup taps
    the back header and navigates away, which then reads as the app rejecting valid input.
    """
    return dump(
        node(**{"class": "android.widget.FrameLayout", "package": "com.android.systemui",
                "resource-id": "com.android.systemui:id/status_bar",
                "bounds": "[0,0][1080,96]"}),
        node(**{"class": "android.view.ViewGroup", "content-desc": "Login",
                "clickable": "true", "bounds": "[0,100][200,290]"}),          # back header, cy=195
        node(**{"class": "android.widget.TextView", "text": "Sign in to continue",
                "bounds": "[100,400][980,470]"}),
        node(**{"class": "android.widget.EditText", "resource-id": "com.example.app:id/email",
                "text": "", "focusable": "true", "bounds": "[100,600][980,720]"}),
        node(**{"class": "android.widget.EditText", "resource-id": "com.example.app:id/password",
                "text": "", "focusable": "true", "bounds": "[100,760][980,880]"}),
        node(**{"class": "android.widget.Button", "content-desc": "Login",
                "clickable": "true", "bounds": "[100,960][980,1080]"}),       # submit, cy=1020
    )


@pytest.fixture
def splash_dump() -> str:
    """A cold start: plenty of nodes, no text at all.

    SYSTEM_MEMORY: empty-dump-means-wait — node count says "ready", text says "not yet".
    """
    return dump(*[
        node(**{"class": "android.view.View", "bounds": f"[0,{i * 100}][1080,{(i + 1) * 100}]"})
        for i in range(15)
    ])


@pytest.fixture
def permission_dialog_dump() -> str:
    """A permission prompt covering the app. It belongs to another package entirely.

    SYSTEM_MEMORY: system-dialogs-are-invisible-to-package-filters + dump-shows-top-window-only.
    """
    return dump(
        node(**{"class": "android.widget.FrameLayout", "package": "com.android.systemui",
                "bounds": "[0,0][1080,96]"}),
        *[node(**{"package": "com.android.permissioncontroller",
                  "class": "android.widget.TextView",
                  "text": "Allow QA App to access photos?" if i == 0 else "",
                  "bounds": f"[60,{800 + i * 120}][1020,{900 + i * 120}]"}) for i in range(6)],
        node(**{"package": "com.android.permissioncontroller", "class": "android.widget.Button",
                "text": "Allow", "clickable": "true", "bounds": "[560,1600][1020,1720]"}),
        node(**{"package": "com.android.permissioncontroller", "class": "android.widget.Button",
                "text": "Deny", "clickable": "true", "bounds": "[60,1600][520,1720]"}),
    )


@pytest.fixture
def keyboard_open_dump() -> str:
    """A search screen with the soft keyboard up: the IME contributes ~180 nodes.

    Counting them in the ownership ranking would trip the wrong-app guard on every single
    search flow, so the IME must be excluded before ranking.
    """
    app_nodes = [
        node(**{"class": "android.widget.EditText", "resource-id": "com.example.app:id/search",
                "text": "gre", "focusable": "true", "bounds": "[60,200][1020,320]"}),
        node(**{"class": "android.widget.TextView", "text": "Recent searches",
                "bounds": "[60,380][1020,450]"}),
    ]
    ime_nodes = [
        node(**{"package": "com.google.android.inputmethod.latin",
                "class": "android.inputmethodservice.SoftKeyboardView",
                "content-desc": f"key-{i}", "clickable": "true",
                "bounds": f"[{(i % 10) * 108},{1800 + (i // 10) * 140}]"
                          f"[{(i % 10) * 108 + 108},{1940 + (i // 10) * 140}]"})
        for i in range(180)
    ]
    return dump(*(app_nodes + ime_nodes))


@pytest.fixture
def calculator_dump() -> str:
    """A calculator: the '=' key sits in the bottom 8% of a 2400px screen.

    SYSTEM_MEMORY: exploration-edge-exclusion-hides-real-controls — EXCLUDE_BOTTOM_PCT blanks
    the bottom 192px and with it a whole keypad row, so the agent could not find '='.
    """
    return dump(
        node(**{"class": "android.widget.TextView",
                "resource-id": "com.example.app:id/result", "text": "1,234.56",
                "bounds": "[0,300][1080,500]"}),
        node(**{"class": "android.widget.Button", "content-desc": "7", "clickable": "true",
                "bounds": "[0,1600][270,1780]"}),
        # cy = 2290, inside the bottom 8% band (bottom_bound = 2208) but outside the agent's 2%.
        node(**{"class": "android.widget.Button", "content-desc": "equals", "clickable": "true",
                "bounds": "[810,2200][1080,2380]"}),
    )
