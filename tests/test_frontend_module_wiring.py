"""Names that were in scope in the old closure and are not in scope in a module.

This file exists because splitting the 3679-line dashboard IIFE into 23 ES modules left
three separate names unwired, each of which failed silently at runtime, and between them
they accounted for four reported symptoms:

* `transcript.js` did `const token = ++transcriptRequest` on a binding imported from
  `outcomes.js`. An import is a read-only binding, so that threw
  `TypeError: Assignment to constant variable.` on the *first* line of `loadTranscript` —
  every time. Clicking a module showed no chat history; opening a project showed 0 findings
  on the pills even with 13 on disk; and the "the agent is waiting for you" box was never
  restored after a reload, which is why answering a parked question meant pressing Stop.
* `socket.js` kept only one of its imports. `ingest`, `resetGraph`, `setConnState`,
  `showStatus` and the rest were simply undefined, so every `history`, `telemetry`, `clear`
  and `status` frame threw ReferenceError inside the WebSocket callback. Live runs never
  reached the board and the connection pill never left "Connecting…".
* `screens.js` read `selectedIds` where it meant `ui.selectedIds`, so the screen list threw
  the moment exactly one screen was selected — which is what clicking a row does.

None of the three is a parse error, so the page boots and looks fine. The failure only
shows up when the code path runs, inside a callback, where nothing was watching. That is
what makes them worth a static test: they are cheap to detect and expensive to notice.

The checks are deliberately narrow — they encode the three mistakes above, not a general
linter. A false alarm here would be worse than useless, so each one only fires on a pattern
that is a bug in every case.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "static" / "js"

#: Bare `name` reads that are legal because the browser (or vis) provides them.
GLOBALS = {"vis", "window", "document", "location", "navigator", "console"}


def _strip_noise(source: str) -> str:
    """Comments and string bodies removed, so a word in prose is not read as a reference.

    Line count is preserved — every substitution keeps its newlines — so reported line
    numbers still point at the real line in the file.
    """
    source = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), source, flags=re.S)
    source = "\n".join(re.sub(r"//.*", "", line) for line in source.splitlines())
    for pattern in (r"`(?:[^`\\]|\\.)*`", r"'(?:[^'\\\n]|\\.)*'", r'"(?:[^"\\\n]|\\.)*"'):
        source = re.sub(pattern, lambda m: "''" + "\n" * m.group(0).count("\n"),
                        source, flags=re.S)
    # Spread and rest blanked to the same width. The reference checks below exclude a name
    # preceded by `.` so that `ui.selectedIds` is not read as a bare `selectedIds` — and the
    # third dot of `[...selectedIds]` is a dot, so without this the one spelling that most
    # needs catching is the one spelling that slips through. Cost me a test that passed
    # against the reintroduced bug.
    return source.replace("...", "   ")


def _js_files() -> list[Path]:
    files = sorted(JS_DIR.glob("*.js"))
    assert files, f"no frontend modules found under {JS_DIR}"
    return files


def _imported_names(code: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"import\s*\{([^}]*)\}\s*from", code):
        for raw in match.group(1).split(","):
            name = raw.strip().split(" as ")[-1].strip()
            if name:
                names.add(name)
    return names


def _exported_names(code: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"export\s*\{([^}]*)\}", code):
        for raw in match.group(1).split(","):
            name = raw.strip().split(" as ")[0].strip()
            if name:
                names.add(name)
    for match in re.finditer(r"export\s+(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
                             code):
        names.add(match.group(1))
    return names


def _declared_names(code: str) -> set[str]:
    """Anything bound in this file: declarations, arrow params, destructuring, catch."""
    names = set(re.findall(r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", code))
    names |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", code))
    names |= set(re.findall(r"\(([^()]*)\)\s*=>", code)) and {
        part.strip().split("=")[0].strip()
        for group in re.findall(r"\(([^()]*)\)\s*=>", code)
        for part in group.split(",")
    }
    names |= set(re.findall(r"function\s*\w*\s*\(([^()]*)\)", code)) and {
        part.strip().split("=")[0].strip()
        for group in re.findall(r"function\s*\w*\s*\(([^()]*)\)", code)
        for part in group.split(",")
    }
    names |= set(re.findall(r"catch\s*\(\s*([A-Za-z_$][\w$]*)", code))
    names |= set(re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", code)) and {
        part.strip().split(":")[-1].split("=")[0].strip()
        for group in re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", code)
        for part in group.split(",")
    }
    return {n for n in names if n}


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_no_assignment_to_an_imported_binding(path):
    """`++x` / `x = …` on an import throws in strict mode, and modules are always strict."""
    code = _strip_noise(path.read_text(encoding="utf-8"))
    for name in sorted(_imported_names(code)):
        escaped = re.escape(name)
        # `(?![\w$.\[])` matters: `++ui.transcriptRequest` mutates a *property* of the
        # imported object, which is fine — it is rebinding the imported name itself that
        # throws. Without it this fires on the correct fix for the bug it exists to catch.
        pattern = (rf"(?<![\w.$])(?:\+\+|--)\s*{escaped}(?![\w$.\[])"
                   rf"|(?<![\w.$]){escaped}\s*(?:\+\+|--|\+=|-=|(?<![=!<>])=(?!=))")
        for match in re.finditer(pattern, code):
            line = code[:match.start()].count("\n") + 1
            pytest.fail(
                f"{path.name}:{line} assigns to `{name}`, which it imports. An imported "
                f"binding is read-only: this throws TypeError at runtime, inside whatever "
                f"callback it sits in. Move the value onto `ui` in state.js, which is what "
                f"that object is for.")


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_every_imported_name_is_actually_exported(path):
    """A rename that misses one importer is a SyntaxError the browser reports at load."""
    code = path.read_text(encoding="utf-8")
    for match in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*['\"]\./([\w.-]+)['\"]", code):
        target = JS_DIR / match.group(2)
        assert target.exists(), f"{path.name} imports from missing module {match.group(2)}"
        exported = _exported_names(target.read_text(encoding="utf-8"))
        for raw in match.group(1).split(","):
            name = raw.strip().split(" as ")[0].strip()
            if name:
                assert name in exported, (
                    f"{path.name} imports `{name}` from {match.group(2)}, which does not "
                    f"export it — the browser refuses the whole module graph for this")


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_every_cross_module_name_used_is_imported(path):
    """socket.js's failure: a name another module owns, used here with no import for it.

    This is the split's signature mistake. Inside one closure `ingest` was just there; as a
    module it is a bare identifier that resolves to nothing, and the ReferenceError waits
    inside a callback until a WebSocket frame arrives. Scoped to names some module actually
    exports, so an ordinary local typo is somebody else's problem and this stays quiet.
    """
    code = _strip_noise(path.read_text(encoding="utf-8"))
    # Import clauses removed: `import { x as y }` mentions `x`, which is by definition a
    # name this file does not bind, and matching it there would flag every renamed import.
    code = re.sub(r"import\s*\{[^}]*\}\s*from\s*[^;\n]*;?",
                  lambda m: "\n" * m.group(0).count("\n"), code)
    owners: dict[str, set[str]] = {}
    for other in _js_files():
        for name in _exported_names(other.read_text(encoding="utf-8")):
            owners.setdefault(name, set()).add(other.name)

    local = _imported_names(_strip_noise(path.read_text(encoding="utf-8")))
    local |= _declared_names(code) | GLOBALS
    for name, modules in sorted(owners.items()):
        if name in local or path.name in modules:
            continue
        for match in re.finditer(rf"(?<![\w.$]){re.escape(name)}(?![\w$:])", code):
            line = code[:match.start()].count("\n") + 1
            pytest.fail(
                f"{path.name}:{line} uses `{name}`, which {'/'.join(sorted(modules))} "
                f"exports, without importing it. Nothing binds that name here: it is a "
                f"ReferenceError whenever the line runs, and the page still boots without "
                f"it, so the only symptom is a feature that quietly does nothing.")


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: p.name)
def test_shared_ui_fields_are_read_through_ui(path):
    """`selectedIds` where `ui.selectedIds` was meant.

    Every field of the shared `ui` object was a plain variable in the old closure, so a
    missed qualification reads as valid code and throws ReferenceError only when the line
    runs. Fields whose names are ordinary words (`comments`, `sections`) are excluded —
    those collide with genuine local names and the check would cry wolf.
    """
    state = (JS_DIR / "state.js").read_text(encoding="utf-8")
    body = re.search(r"const ui = \{(.*?)\n\};", state, re.S)
    assert body, "state.js no longer declares `const ui = {…}` — update this test"
    fields = set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*:", _strip_noise(body.group(1)), re.M))
    if path.name == "state.js":
        pytest.skip("state.js is where these are declared")

    code = _strip_noise(path.read_text(encoding="utf-8"))
    local = _imported_names(code) | _declared_names(code) | GLOBALS
    for field in sorted(fields - local):
        for match in re.finditer(rf"(?<![\w.$]){re.escape(field)}(?![\w$:])", code):
            line = code[:match.start()].count("\n") + 1
            pytest.fail(
                f"{path.name}:{line} reads a bare `{field}`, but that is a field of the "
                f"shared `ui` object — write `ui.{field}`. Unqualified it is a "
                f"ReferenceError the moment the line executes.")
