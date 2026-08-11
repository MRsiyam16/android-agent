"""One command to bring the web-testing stack up: `python start_web.py`

`start.py` is enough on Android because adb is either there or the device is not, and the iOS
launcher exists because an iPhone needs a multi-process USB/pairing/tunnel dance before a
single tap can be sent. A browser needs neither: Playwright and a Chromium binary are either
installed or they are not, so this is a short preflight — not a process ladder — that checks
for both, prints the exact command that fixes whichever is missing, and then hands off to
`start.py` exactly the way `start_ios.py` does.

There is nothing here to "bring up": Chromium is launched on demand, per project, by
`web_device.WebDevice` itself — this script only confirms that launch will actually work before
the dashboard opens and a user's first click discovers otherwise.
"""
from __future__ import annotations

import subprocess
import sys


def playwright_importable() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
        return True, ""
    except ImportError as exc:
        return False, str(exc)


def chromium_installed() -> tuple[bool, str]:
    """A throwaway launch/close is the only reliable check — Playwright does not expose a
    "is this browser's binary present" query, and the launch error message already names
    the exact fix (`playwright install chromium`)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            browser.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001 - anything here means "not ready", message varies
        return False, str(exc)


def main() -> int:
    print("Checking the browser stack ...")

    ok, detail = playwright_importable()
    if not ok:
        print("\n  playwright : not installed")
        print(f"               {detail}")
        print("               Install it with:  pip install playwright")
        return 1
    print("  playwright : installed")

    ok, detail = chromium_installed()
    if not ok:
        print("\n  chromium   : not ready")
        print(f"               {detail.splitlines()[0] if detail else ''}")
        print("               Install the browser binary with:  playwright install chromium")
        return 1
    print("  chromium   : ready")

    # -- hand over -------------------------------------------------------------
    # Subprocess rather than an import, so start.py sees a clean argv and keeps owning the
    # port check, the browser tab and the shutdown path exactly as it does on Android/iOS.
    print("\n  Starting the dashboard ...\n")
    try:
        return subprocess.call([sys.executable, "start.py", *sys.argv[1:]])
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
