"""Comparing two parameter files, and checking one.

The unit of comparison is the *effective* value: what the vehicle runs with
after loading the file. A parameter the file sets has the file's value; one it
doesn't set has the firmware default from the metadata. So deleting a line is
a change whenever the deleted value wasn't the default, which is exactly the
change a reviewer reads as harmless cleanup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .meta import Meta, ParamMeta, _num
from .params import Param, ParamFile, same_value
from .rules import Allow, RuleSet

ORDER = {"block": 0, "warn": 1, "note": 2}


@dataclass
class Change:
    name: str
    old: float | None
    new: float | None
    old_set: bool  # the base file sets it (else: firmware default)
    new_set: bool
    line: int | None  # in the new file, if set there
    old_line: int | None

    @property
    def removed(self) -> bool:
        return self.old_set and not self.new_set

    @property
    def added(self) -> bool:
        return self.new_set and not self.old_set


@dataclass
class Finding:
    severity: str  # block | warn | note
    check: str  # rule id, or a metadata check
    param: str
    message: str
    reason: str = ""
    path: str = ""
    line: int | None = None
    removed: bool = False
    new: float | None = None
    allowed: Allow | None = None

    @property
    def blocking(self) -> bool:
        return self.severity == "block" and self.allowed is None


@dataclass
class Report:
    path: str
    base: str
    meta_source: str
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.blocking for f in self.findings)

    def sort(self) -> None:
        self.findings.sort(key=lambda f: (f.allowed is not None, ORDER[f.severity], f.param, f.check))


def effective(f: ParamFile | None, name: str, meta: Meta) -> float | None:
    if f is not None and name in f:
        return f[name].value
    pm = meta.get(name)
    return pm.default if pm else None


def _equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return same_value(a, b)


def describe(meta: Meta, name: str, v: float | None) -> str:
    pm = meta.get(name)
    if v is None:
        return "(not a parameter)" if pm is None else "unknown (no default in the metadata)"
    return pm.describe_value(v) if pm else _num(v)


def meta_problems(p: Param, pm: ParamMeta | None, meta: Meta) -> list[tuple[str, str]]:
    """(check id, message) for what the metadata says is wrong with one line."""
    if pm is None:
        hint = meta.suggest(p.name)
        did = f"; did you mean {' or '.join(hint)}?" if hint else ""
        return [("unknown-param", f"{p.name} is not a parameter in {meta.source}{did}")]
    out = []
    v = p.value
    if pm.type == "int" and v != int(v):
        out.append(("not-integer", f"{p.name} is an integer parameter; {p.text} has a fraction"))
        return out
    if pm.values is not None and pm.type == "int":
        if int(v) not in pm.values:
            known = ", ".join(f"{k} ({d})" for k, d in sorted(pm.values.items()))
            out.append(("not-in-values", f"{p.name} = {_num(v)} is not one of its values: {known}"))
    elif pm.bitmask is not None:
        allowed = 0
        for bit in pm.bitmask:
            allowed |= 1 << bit
        extra = int(v) & ~allowed if v >= 0 else -1
        if extra:
            out.append(("unknown-bits", f"{p.name} = {_num(v)} sets bits the metadata doesn't define"))
    if pm.min is not None and v < pm.min and not same_value(v, pm.min):
        out.append(("out-of-range", f"{p.name} = {_num(v)} is below its minimum {_num(pm.min)}{_u(pm)}"))
    if pm.max is not None and v > pm.max and not same_value(v, pm.max):
        out.append(("out-of-range", f"{p.name} = {_num(v)} is above its maximum {_num(pm.max)}{_u(pm)}"))
    return out


def _u(pm: ParamMeta) -> str:
    return f" {pm.units}" if pm.units else ""


META_WHY = {
    "unknown-param": "The vehicle has no parameter by this name, so the line sets nothing. PX4 sends the "
    "ground station no reply; the only trace is an error on the autopilot's own console.",
    "not-integer": "An integer parameter can't hold a fraction: depending on the tool loading the file it "
    "is truncated or the set is refused.",
    "not-in-values": "The firmware stores it anyway (PX4 SITL accepts NAV_DLL_ACT = 4), but no behaviour "
    "is defined for this value.",
    "unknown-bits": "Undefined bits are ignored today and may mean something after a firmware update.",
    "out-of-range": "PX4 stores values outside the documented range without complaint (SITL accepts "
    "MPC_Z_VEL_MAX_DN = 5 against a maximum of 4). The range is what the firmware was "
    "designed and tested for.",
}


def _apply_allows(report: Report, allows: list[Allow]) -> None:
    used = set()
    for f in report.findings:
        if f.severity == "note":
            continue
        for i, a in enumerate(allows):
            if a.rule != f.check or a.param != f.param:
                continue
            if isinstance(a.value, str):  # "removed"
                ok = f.removed
            else:
                ok = not f.removed and f.new is not None and same_value(a.value, f.new)
            if ok:
                f.allowed = a
                used.add(i)
                break
    for i, a in enumerate(allows):
        if i not in used:
            report.findings.append(
                Finding(
                    "warn",
                    "allow-unused",
                    a.param,
                    f"allowance for {a.rule} on {a.param} = {a.value if isinstance(a.value, str) else _num(a.value)} "
                    "matches nothing in this change",
                    "An allowance is for one exact value. If the value moved on, the old allowance no longer "
                    "covers it; delete it so it can't be mistaken for one that does.",
                    a.source,
                )
            )


def check(
    old: ParamFile | None, new: ParamFile, meta: Meta, rules: RuleSet, allows: list[Allow] | None = None, base: str = ""
) -> Report:
    report = Report(new.path, base or ("firmware defaults" if old is None else old.path), meta.source)
    names = set(new.params) | (set(old.params) if old else set())

    def old_state(n):
        return effective(old, n, meta)

    def new_state(n):
        return effective(new, n, meta)

    for n in sorted(names):
        o, v = old_state(n), new_state(n)
        if not _equal(o, v):
            report.changes.append(
                Change(
                    n,
                    o,
                    v,
                    old is not None and n in old,
                    n in new,
                    new[n].line if n in new else None,
                    old[n].line if old is not None and n in old else None,
                )
            )

    # What the metadata says about each line of the new file.
    for n in new.names():
        p = new[n]
        touched = old is None or n not in old or not same_value(old[n].value, p.value) or old[n].text != p.text
        for check_id, msg in meta_problems(p, meta.get(n), meta):
            sev = "block" if touched else "note"
            if not touched:
                msg += " (already in the base version)"
            report.findings.append(Finding(sev, check_id, n, msg, META_WHY[check_id], new.path, p.line, new=p.value))

    # The rules, on every change.
    changed = {c.name: c for c in report.changes}
    for c in report.changes:
        covering = rules.for_param(c.name)
        for r in covering:
            for v in r.violations(c.old, c.new, new_state):
                report.findings.append(_rule_finding(r, c, v, meta, new.path))
        if c.new is None and covering and meta.get(c.name) is not None:
            report.findings.append(
                Finding(
                    "warn",
                    "unknown-default",
                    c.name,
                    f"{c.name} was removed and the metadata has no default for it, so "
                    f"{', '.join(r.id for r in covering)} can't judge what the vehicle will now use",
                    "ArduPilot's metadata carries no defaults. Keep the line, or check the default for "
                    "this firmware version by hand.",
                    new.path,
                    None,
                    removed=True,
                )
            )
    # A change to the other side of a 'below' rule re-checks it.
    for other, rs in rules.cross_refs().items():
        if other not in changed:
            continue
        for r in rs:
            target = r.params[0]
            if target in changed:
                continue  # already judged as a change of its own
            cur = new_state(target)
            now = r.violations(cur, cur, new_state)
            before = r.violations(old_state(target), old_state(target), old_state)
            for v in now:
                if v in before:
                    continue
                c = Change(
                    target,
                    cur,
                    cur,
                    old is not None and target in old,
                    target in new,
                    new[target].line if target in new else None,
                    None,
                )
                f = _rule_finding(r, c, v, meta, new.path)
                f.message += f", after {other} changed to {describe(meta, other, new_state(other))}"
                f.line = changed[other].line
                f.removed = False
                report.findings.append(f)

    _apply_allows(report, allows or [])
    report.sort()
    return report


def _rule_finding(r, c: Change, violation: str, meta: Meta, path: str) -> Finding:
    msg = f"{c.name} {describe(meta, c.name, c.old)} -> {describe(meta, c.name, c.new)}: {violation}"
    if c.removed:
        msg += ". The line was removed, so the firmware default applies"
    elif c.added and c.old is not None:
        msg += " (it was the firmware default)"
    reason = r.reason + (f" See {r.ref}" if r.ref else "")
    return Finding(r.severity, r.id, c.name, msg, reason, path, c.line, removed=c.removed, new=c.new)


def lint(f: ParamFile, meta: Meta, rules: RuleSet, allows: list[Allow] | None = None) -> Report:
    """Check one file on its own: every line against the metadata, and every
    rule against the file's effective values, defaults included."""
    report = Report(f.path, "", meta.source)

    def state(n):
        return effective(f, n, meta)

    for n in f.names():
        p = f[n]
        for check_id, msg in meta_problems(p, meta.get(n), meta):
            report.findings.append(Finding("block", check_id, n, msg, META_WHY[check_id], f.path, p.line, new=p.value))
    for r in rules.rules:
        names = (
            [p for p in r.params if p in meta.params]
            if all(not any(ch in p for ch in "*?[") for p in r.params)
            else [n for n in meta.params if r.matches(n)]
        )
        for n in names:
            v = state(n)
            for msg in r.violations(None, v, state):
                text = f"{n} = {describe(meta, n, v)}: {msg}"
                if n not in f:
                    text += " (the firmware default; this file doesn't set it)"
                reason = r.reason + (f" See {r.ref}" if r.ref else "")
                report.findings.append(
                    Finding(r.severity, r.id, n, text, reason, f.path, f[n].line if n in f else None, new=v)
                )
    _apply_allows(report, allows or [])
    report.sort()
    return report
