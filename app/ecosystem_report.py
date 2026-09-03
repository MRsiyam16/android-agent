"""A snapshot of one ecosystem's QA state, written to disk as Markdown or HTML.

Where `ecosystem.py` builds the numbers the manager dashboard reads live, this turns the same
numbers into a file — something the user can download, attach to an email, or hand to someone
who has never opened the dashboard. Both formats are rendered from one `_build_model()` so a
number can never say two different things in the two formats.

Files land in the supervisor project's own folder (`projects/<ecosystem>/reports/`), since
`ecosystem.create_supervisor` already makes the ecosystem's name the supervisor's package — the
report lives next to the conversation that made it, the same way a module's REPORT.md lives
next to that module.
"""
from __future__ import annotations

import html as html_escape
import time
from pathlib import Path
from typing import Any

import ecosystem
from agent import store

#: Worst-first, matching `ecosystem.DEFECT_KINDS` plus the one kind it leaves out on purpose.
_KIND_ORDER = ("bug", "warning", "suggestion", "pass")

_KIND_LABEL = {"bug": "Bugs", "warning": "Warnings", "suggestion": "Suggestions", "pass": "Passes"}


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _now_readable() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def reports_dir(name: str) -> Path:
    supervisor = ecosystem.supervisor(name) or name
    path = store.project_dir(supervisor) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- one model, two renderers ----------------------------------------------------------------
def _build_model(name: str) -> dict[str, Any]:
    """Everything a report needs, gathered once. Findings are grouped per app and, within an
    app, by kind worst-first — the same ordering `ecosystem.py`'s own digest and the manager
    tools use, so a reader moving between the dashboard, the chat and this file sees the same
    order every time."""
    s = ecosystem.summary(name)
    apps = ecosystem.app_index(name)

    by_app: list[dict[str, Any]] = []
    for app in apps:
        package = app["package"]
        findings = [f for f in store.list_all_findings(package) if f.get("kind") in _KIND_ORDER]
        grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in _KIND_ORDER}
        for f in findings:
            grouped.setdefault(f.get("kind", "bug"), []).append(f)
        by_app.append({**app, "findings_by_kind": grouped})

    return {"ecosystem": name, "generated_at": _now_readable(), "summary": s, "apps": by_app}


def _finding_parts(f: dict[str, Any]) -> tuple[str, str, str]:
    """(id/severity/title head, detail line, plain title) — one place that decides what a
    finding shows, so Markdown and HTML cannot drift into listing different fields."""
    head = f.get("id", "?")
    if f.get("severity"):
        head += f" [{f['severity']}]"
    title = f.get("title") or "(untitled)"

    detail_bits = []
    if f.get("expected"):
        detail_bits.append(f"expected: {f['expected']}")
    if f.get("actual"):
        detail_bits.append(f"actual: {f['actual']}")
    if f.get("module_title"):
        detail_bits.append(f"module: {f['module_title']}")
    accounts = f.get("accounts") or []
    if accounts:
        who = ", ".join(a.get("email") or a.get("label") or a.get("role", "") for a in accounts)
        detail_bits.append(f"account: {who}")
    if f.get("resolved"):
        detail_bits.append("marked resolved")
    return head, title, "; ".join(detail_bits)


def render_markdown(name: str) -> str:
    m = _build_model(name)
    s = m["summary"]
    lines = [f"# {name} — QA Report", "", f"_Generated {m['generated_at']}_", ""]

    lines += [
        f"**{s['apps']} apps · {s['modules']} modules "
        f"({s['modules_tested']} tested, {s['modules_untested']} not) · "
        f"{s['defects']} open defects**",
        "",
    ]
    cov = s["coverage"]
    if cov["percent"] is not None:
        lines.append(f"Coverage: {cov['tested']}/{cov['testable']} scoped modules tested "
                     f"({cov['percent']}%).")
    if cov["unsurveyed"]:
        lines.append(f"Not yet surveyed at all: {', '.join(cov['unsurveyed'])}.")
    if s["incomplete"]:
        lines.append(f"Incomplete coverage: {', '.join(s['incomplete'])} — a clean count from "
                     "these is a floor, not the whole picture.")
    lines.append("")

    ds = s["defect_status"]
    lines += ["| Bugs | Warnings | Suggestions | Resolved |", "|---|---|---|---|",
             f"| {ds['bug']} | {ds['warning']} | {ds['suggestion']} | {ds['resolved']} |", ""]

    lines += ["## Apps", "", "| Role | Package | Platform | Modules tested | Coverage | Open defects |",
             "|---|---|---|---|---|---|"]
    for app in m["apps"]:
        cov = app["coverage"]
        cov_text = f"{cov['percent']}%" if cov["percent"] is not None else "not surveyed"
        lines.append(f"| `{app['role']}` | {app['package']} | {app['platform']} | "
                     f"{app['modules_tested']}/{app['modules']} | {cov_text} | {app['defects']} |")
    lines.append("")

    lines.append("## Findings, by app")
    for app in m["apps"]:
        lines += ["", f"### {app['role']} — {app['package']}"]
        any_findings = False
        for kind in ("bug", "warning", "suggestion"):
            entries = app["findings_by_kind"].get(kind) or []
            if not entries:
                continue
            any_findings = True
            lines += ["", f"#### {_KIND_LABEL[kind]} ({len(entries)})"]
            for f in entries:
                head, title, detail = _finding_parts(f)
                bullet = f"- **{head}** {title}"
                if detail:
                    bullet += f"  \n    {detail}"
                lines.append(bullet)
        if not any_findings:
            lines += ["", "No open defects filed."]
    lines.append("")
    return "\n".join(lines)


_KIND_COLOR = {"bug": "#dc4b4b", "warning": "#c9862b", "suggestion": "#3f7bd6", "pass": "#3a9a5c"}

_HTML_STYLE = """
:root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --border:#e0e0e0;
        --card:#f7f7f8; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#15161a; --fg:#e8e8ea; --muted:#9a9aa2; --border:#2c2d33; --card:#1d1e23; }
}
* { box-sizing: border-box; }
body { background:var(--bg); color:var(--fg); font: 15px/1.5 -apple-system, Segoe UI, Inter,
       sans-serif; max-width: 920px; margin: 0 auto; padding: 32px 20px 80px; }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 17px; margin-top: 40px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
h3 { font-size: 15px; margin-top: 28px; }
h4 { font-size: 13px; margin: 16px 0 8px; text-transform: uppercase; letter-spacing: .03em; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.headline { font-size: 15px; margin: 10px 0; }
table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13.5px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; }
code, .pkg { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
.tally { display:flex; gap:10px; margin: 10px 0 20px; flex-wrap: wrap; }
.tally span { padding: 3px 10px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
              background: var(--card); border: 1px solid var(--border); }
ul.findings { list-style: none; margin: 0 0 10px; padding: 0; }
ul.findings li { padding: 8px 10px; margin: 4px 0; border-radius: 8px; background: var(--card);
                  border-left: 3px solid var(--border); font-size: 13.5px; }
.detail { color: var(--muted); font-size: 12.5px; }
.empty { color: var(--muted); font-style: italic; font-size: 13.5px; }
"""


def render_html(name: str) -> str:
    m = _build_model(name)
    s = m["summary"]
    esc = html_escape.escape

    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{esc(name)} — QA Report</title>",
        f"<style>{_HTML_STYLE}</style></head><body>",
        f"<h1>{esc(name)} — QA Report</h1>",
        f"<div class='meta'>Generated {esc(m['generated_at'])}</div>",
        f"<div class='headline'>{s['apps']} apps · {s['modules']} modules "
        f"({s['modules_tested']} tested, {s['modules_untested']} not) · "
        f"{s['defects']} open defects</div>",
    ]
    cov = s["coverage"]
    if cov["percent"] is not None:
        parts.append(f"<div class='headline'>Coverage: {cov['tested']}/{cov['testable']} "
                     f"scoped modules tested ({cov['percent']}%).</div>")
    if cov["unsurveyed"]:
        parts.append(f"<div class='headline'>Not yet surveyed at all: "
                     f"{esc(', '.join(cov['unsurveyed']))}.</div>")
    if s["incomplete"]:
        parts.append(f"<div class='headline'>Incomplete coverage: "
                     f"{esc(', '.join(s['incomplete']))} — a clean count from these is a floor, "
                     f"not the whole picture.</div>")

    ds = s["defect_status"]
    parts.append("<div class='tally'>" + "".join(
        f"<span style='color:{_KIND_COLOR.get(k, '#888')}'>{ds[k]} {k}</span>"
        for k in ("bug", "warning", "suggestion", "resolved")) + "</div>")

    parts.append("<h2>Apps</h2><table><tr><th>Role</th><th>Package</th><th>Platform</th>"
                 "<th>Modules tested</th><th>Coverage</th><th>Open defects</th></tr>")
    for app in m["apps"]:
        c = app["coverage"]
        cov_text = f"{c['percent']}%" if c["percent"] is not None else "not surveyed"
        parts.append(f"<tr><td class='pkg'>{esc(app['role'])}</td><td class='pkg'>"
                     f"{esc(app['package'])}</td><td>{esc(app['platform'])}</td>"
                     f"<td>{app['modules_tested']}/{app['modules']}</td><td>{esc(cov_text)}</td>"
                     f"<td>{app['defects']}</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Findings, by app</h2>")
    for app in m["apps"]:
        parts.append(f"<h3>{esc(app['role'])} — <span class='pkg'>{esc(app['package'])}</span></h3>")
        any_findings = False
        for kind in ("bug", "warning", "suggestion"):
            entries = app["findings_by_kind"].get(kind) or []
            if not entries:
                continue
            any_findings = True
            parts.append(f"<h4 style='color:{_KIND_COLOR[kind]}'>{_KIND_LABEL[kind]} "
                         f"({len(entries)})</h4><ul class='findings'>")
            for f in entries:
                head, title, detail = _finding_parts(f)
                item = f"<strong>{esc(head)}</strong> {esc(title)}"
                if detail:
                    item += f"<br><span class='detail'>{esc(detail)}</span>"
                parts.append(f"<li style='border-left-color:{_KIND_COLOR[kind]}'>{item}</li>")
            parts.append("</ul>")
        if not any_findings:
            parts.append("<div class='empty'>No open defects filed.</div>")

    parts.append("</body></html>")
    return "".join(parts)


_RENDERERS = {"md": render_markdown, "markdown": render_markdown, "html": render_html}
_EXT = {"md": "md", "markdown": "md", "html": "html"}


def write_report(name: str, fmt: str = "md") -> Path:
    """Render and save one report file. `fmt` is 'md' (or 'markdown') or 'html'. Returns the
    path it was written to — always inside `reports_dir(name)`, never overwriting an earlier
    report, since each is a snapshot of the state at the moment it was asked for."""
    key = fmt.strip().lower()
    if key not in _RENDERERS:
        raise ValueError(f"Unknown report format {fmt!r} — use 'md' or 'html'.")
    text = _RENDERERS[key](name)
    ext = _EXT[key]
    path = reports_dir(name) / f"{_now_stamp()}-{name}-report.{ext}"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def list_reports(name: str) -> list[dict[str, Any]]:
    """Every report on disk for this ecosystem, newest first."""
    out = []
    for path in reports_dir(name).glob("*"):
        if path.suffix.lstrip(".") not in ("md", "html") or not path.is_file():
            continue
        stat = path.stat()
        out.append({"filename": path.name, "format": path.suffix.lstrip("."),
                    "size": stat.st_size, "modified_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))})
    return sorted(out, key=lambda r: r["modified_at"], reverse=True)
