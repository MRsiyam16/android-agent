"""Moving, copying and retiring files and folders, for the ecosystem manager only.

The tester tier has `Read`, `Write` and `Edit` and nothing else, and that is right for it: it
writes its memory file and its report, and a shell inside a server process that drives a phone
would be a blast radius with no matching benefit. But the tier above it is asked to *keep the
product tidy* — fold a stray project into the right folder, archive a suite nobody runs, copy
a set of screenshots somewhere a human will find them — and none of that is expressible as
"write this text to that path".

So this is the shape those operations take here: named tools over a closed set of roots,
rather than `Bash`. Three reasons it is not simply `Bash`:

* **A refusal can explain itself.** `move_path` outside the roots names the roots. A shell
  returns `Access is denied.` and the agent tries again with quotes.
* **Deleting is not a thing this can do.** `trash` moves into a dated folder under
  `projects/_trash/`. Every deletion in this system is recoverable by hand, including the ones
  an agent makes at 2am because a folder "looked empty".
* **The audit is legible.** One tool call, one path pair, in the transcript, in words the user
  reads. `rm -rf` behind a `&&` is not.

**Roots.** The harness tree, every registered project root (a project may live anywhere the
user pointed it), and anything named in `QA_MANAGER_FS_ROOTS`. Outside them is refused.

**What is protected inside them.** `.git`, and the running source tree. Not paternalism about
the user's own files: this code is executing out of `app/`, and an agent that moves it mid-turn
takes away the process it is talking through — the failure is not "a mistake I can undo", it is
"no chat to undo it in".
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import project_paths

#: This file lives in `app/`; the harness tree is its parent.
APP_DIR = Path(__file__).resolve().parent
HARNESS_ROOT = APP_DIR.parent

#: Extra roots, `;`-separated, for source trees that live outside both.
_EXTRA_ROOTS = os.environ.get("QA_MANAGER_FS_ROOTS", "")

#: Never moved, copied over, or retired. `.git` because losing it loses the history; the app
#: source because it is the process running this call.
PROTECTED = ("app", ".git")


class FsRefused(RuntimeError):
    """An operation outside the roots, or onto something protected. The message says which."""


def roots() -> list[Path]:
    """Every directory this tier may touch, deduplicated and resolved."""
    found: list[Path] = [HARNESS_ROOT]
    try:
        found.append(project_paths.DEFAULT_PROJECTS_DIR.resolve())
    except OSError:
        pass
    for package in project_paths.known_packages():
        try:
            found.append(project_paths.project_dir(package).resolve())
        except (OSError, ValueError):
            continue
    for raw in _EXTRA_ROOTS.split(";"):
        if raw.strip():
            try:
                found.append(Path(raw.strip()).resolve())
            except (OSError, ValueError):
                continue

    unique: list[Path] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def root_labels() -> str:
    return "; ".join(str(r) for r in roots())


def _protected(path: Path) -> Optional[str]:
    """Why this path may not be moved or retired, or None if it may."""
    if path == APP_DIR or APP_DIR.is_relative_to(path):
        return (f"{path} is (or contains) the harness source at {APP_DIR}, which is the code "
                f"running this call. Moving it takes away the session you are speaking through.")
    if path.name == ".git" or any(part == ".git" for part in path.parts):
        return f"{path} is inside a git repository's own storage. That is history, not files."
    if path in roots():
        return (f"{path} is one of the roots this tier works inside. Move what is in it, not "
                f"the root itself.")
    return None


def resolve(raw: str, *, must_exist: bool = True) -> Path:
    """A path inside the roots, or `FsRefused` naming them.

    Resolved before the check, so `..` cannot walk out of a root and a symlink cannot point
    out of one either.
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise FsRefused("No path given.")
    try:
        path = Path(text).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise FsRefused(f"{raw!r} is not a usable path: {exc}") from exc

    if not any(path == r or path.is_relative_to(r) for r in roots()):
        raise FsRefused(
            f"{path} is outside the folders this tier may touch. They are: {root_labels()}. "
            f"Add another with QA_MANAGER_FS_ROOTS in app/.env if the user wants one.")
    if must_exist and not path.exists():
        raise FsRefused(f"{path} does not exist.")
    return path


def list_dir(raw: str) -> dict[str, Any]:
    """One directory's contents, folders first."""
    path = resolve(raw)
    if not path.is_dir():
        return {"path": str(path), "is_dir": False,
                "size": path.stat().st_size, "entries": []}
    entries = []
    for child in sorted(path.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({"name": child.name, "is_dir": child.is_dir(),
                        "size": 0 if child.is_dir() else stat.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M",
                                                  time.localtime(stat.st_mtime))})
    return {"path": str(path), "is_dir": True, "entries": entries}


def _free_destination(dest: Path, source_name: str) -> Path:
    """Where a move or copy actually lands.

    A destination that is an existing directory means "into it", the way every file manager
    behaves. A destination that is an existing *file* is refused rather than overwritten —
    silently replacing a file is the one outcome nobody can undo from the chat.
    """
    if dest.is_dir():
        return dest / source_name
    return dest


def move(raw_source: str, raw_dest: str) -> dict[str, str]:
    source = resolve(raw_source)
    why = _protected(source)
    if why:
        raise FsRefused(why)
    dest_parent = resolve(str(Path(raw_dest).expanduser().parent), must_exist=False)
    dest = resolve(raw_dest, must_exist=False)
    if not dest_parent.exists():
        raise FsRefused(f"{dest_parent} does not exist. Create it first with make_dir.")
    target = _free_destination(dest, source.name)
    if target.exists():
        raise FsRefused(f"{target} already exists. Nothing was moved — rename or retire the "
                        f"existing one first.")
    if target == source or target.is_relative_to(source):
        raise FsRefused(f"{target} is inside {source}. A folder cannot be moved into itself.")
    try:
        shutil.move(str(source), str(target))
    except (OSError, shutil.Error) as exc:
        raise FsRefused(f"Could not move {source} to {target}: {exc}") from exc
    return {"from": str(source), "to": str(target)}


def copy(raw_source: str, raw_dest: str) -> dict[str, str]:
    source = resolve(raw_source)
    dest = resolve(raw_dest, must_exist=False)
    target = _free_destination(dest, source.name)
    if target.exists():
        raise FsRefused(f"{target} already exists. Nothing was copied.")
    if source.is_dir() and target.is_relative_to(source):
        raise FsRefused(f"{target} is inside {source}. A folder cannot be copied into itself.")
    try:
        if source.is_dir():
            shutil.copytree(str(source), str(target))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
    except (OSError, shutil.Error) as exc:
        raise FsRefused(f"Could not copy {source} to {target}: {exc}") from exc
    return {"from": str(source), "to": str(target)}


def make_dir(raw: str) -> dict[str, str]:
    path = resolve(raw, must_exist=False)
    if path.exists():
        return {"path": str(path), "created": "already existed"}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FsRefused(f"Could not create {path}: {exc}") from exc
    return {"path": str(path), "created": "yes"}


def trash_dir() -> Path:
    return project_paths.DEFAULT_PROJECTS_DIR / "_trash"


def trash(raw: str) -> dict[str, str]:
    """Retire a path into `projects/_trash/<date>/`. Nothing here deletes.

    Named `trash` rather than `delete` on purpose: the tool's name is what the agent tells the
    user it did, and "I deleted it" and "I moved it to the trash folder" are different
    sentences to be reading three days later.
    """
    path = resolve(raw)
    why = _protected(path)
    if why:
        raise FsRefused(why)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bin_dir = trash_dir() / stamp
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FsRefused(f"Could not create the trash folder {bin_dir}: {exc}") from exc
    target = bin_dir / path.name
    try:
        shutil.move(str(path), str(target))
    except (OSError, shutil.Error) as exc:
        raise FsRefused(f"Could not retire {path}: {exc}") from exc
    return {"from": str(path), "to": str(target)}
