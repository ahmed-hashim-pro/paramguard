"""Rendering a report: for a terminal, as JSON, or as GitHub annotations."""

from __future__ import annotations

import json
import textwrap

from .check import Report, describe
from .meta import Meta, _num

LABEL = {"block": "BLOCK", "warn": "WARN ", "note": "note "}


def _color(on: bool):
    def c(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if on else s

    return c


def text(report: Report, meta: Meta, color: bool = False, show_changes: bool = True) -> str:
    c = _color(color)
    out = []
    head = f"{report.path}"
    if report.base:
        head += f"  (against {report.base})"
    out.append(c("1", head))
    if show_changes and report.base:
        if report.changes:
            n = len(report.changes)
            out.append(f"{n} effective change{'s' if n != 1 else ''}:")
            width = max(len(ch.name) for ch in report.changes)
            for ch in report.changes:
                how = ""
                if ch.removed:
                    how = "  (line removed: firmware default)"
                elif ch.added:
                    how = "  (new line)"
                out.append(
                    f"  {ch.name:<{width}}  {describe(meta, ch.name, ch.old)} -> {describe(meta, ch.name, ch.new)}{how}"
                )
        else:
            out.append("no effective changes")
    if report.findings:
        out.append("")
    for f in report.findings:
        if f.allowed:
            label = c("32", "ALLOW")
        elif f.severity == "block":
            label = c("1;31", LABEL["block"])
        elif f.severity == "warn":
            label = c("33", LABEL["warn"])
        else:
            label = c("2", LABEL["note"])
        where = f"{f.path}:{f.line}" if f.line else f.path
        out.append(f"{label}  {c('1', f.check)}  {where}")
        out.append(_wrap(f.message, "       "))
        if f.allowed:
            a = f.allowed
            out.append(_wrap(f"allowed by {a.by}: {a.reason}", "       "))
        elif f.reason and f.severity != "note":
            out.append(c("2", _wrap("why: " + f.reason, "       ")))
    blocking = [f for f in report.findings if f.blocking]
    warns = [f for f in report.findings if f.severity == "warn" and not f.allowed]
    out.append("")
    if blocking:
        n = len(blocking)
        out.append(
            c("1;31", f"blocked: {n} problem{'s' if n != 1 else ''}")
            + f", {len(warns)} warning{'s' if len(warns) != 1 else ''}. "
            "To accept one deliberately, add an allowance:"
        )
        f = blocking[0]
        value = '"removed"' if f.removed else (_num(f.new) if f.new is not None else "?")
        out.append(
            c(
                "2",
                textwrap.indent(
                    f'[[allow]]\nrule = "{f.check}"\nparam = "{f.param}"\nvalue = {value}\n'
                    'by = "<who accepts it>"\nreason = "<why it is safe for this vehicle>"',
                    "  ",
                ),
            )
        )
    else:
        out.append(c("32", "ok") + f": nothing blocking, {len(warns)} warning{'s' if len(warns) != 1 else ''}")
    return "\n".join(out)


def _wrap(s: str, indent: str) -> str:
    return textwrap.fill(
        s, width=88, initial_indent=indent, subsequent_indent=indent, break_on_hyphens=False, break_long_words=False
    )


def as_dict(report: Report) -> dict:
    return {
        "path": report.path,
        "base": report.base,
        "metadata": report.meta_source,
        "blocked": report.blocked,
        "changes": [
            {"param": ch.name, "old": ch.old, "new": ch.new, "removed": ch.removed, "added": ch.added, "line": ch.line}
            for ch in report.changes
        ],
        "findings": [
            {
                "severity": f.severity,
                "check": f.check,
                "param": f.param,
                "message": f.message,
                "reason": f.reason,
                "path": f.path,
                "line": f.line,
                "blocking": f.blocking,
                "allowed_by": None if f.allowed is None else {"by": f.allowed.by, "reason": f.allowed.reason},
            }
            for f in report.findings
        ],
    }


def as_json(reports: list[Report]) -> str:
    return json.dumps([as_dict(r) for r in reports], indent=2)


def github(report: Report) -> str:
    """Workflow commands that GitHub turns into annotations on the diff."""
    out = []
    for f in report.findings:
        if f.allowed or f.severity == "note":
            continue
        level = "error" if f.severity == "block" else "warning"
        props = f"file={f.path}"
        if f.line:
            props += f",line={f.line}"
        props += f",title=paramguard {f.check}"
        msg = f"{f.message}. {f.reason}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        out.append(f"::{level} {props}::{msg}")
    return "\n".join(out)
