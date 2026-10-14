"""Rule files: which parameter changes are dangerous, and why.

A rule file is TOML:

    version = 1
    firmware = "px4"

    [[rule]]
    id = "gcs-loss-failsafe-off"
    params = ["NAV_DLL_ACT"]
    forbid = [0]
    severity = "block"
    reason = "With the GCS link-loss failsafe disabled, ..."

Each rule names its parameters (exact names or globs such as ``MC_*RATE_P``),
one or more conditions, a severity and the reason a reviewer will read. Rules
are checked against changes, so a rule never fails a file for something it
already had; ``paramguard lint`` checks a whole file instead.

Conditions, each judged on the parameter's effective value (the file's value,
or the firmware default when the file doesn't set it):

    forbid = [values]       the new value is one of these
    allow_only = [values]   the new value is not one of these
    min / max = number      the new value is outside this band
    no_decrease = true      the value went down
    no_increase = true      the value went up
    max_step = 0.5          the value moved by more than this fraction of the old one
    no_sign_flip = true     the value changed sign
    any_change = true       the value changed at all
    no_new_bits = true      a bitmask gained bits it didn't have
    below = "PARAM"         the value is not below another parameter's value

Unknown keys are an error: a misspelt condition that silently never fires is
the failure this tool exists to prevent.
"""

from __future__ import annotations

import fnmatch
import math
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from .meta import Meta, _num
from .params import same_value

SEVERITIES = ("block", "warn")
CONDITIONS = (
    "forbid",
    "allow_only",
    "min",
    "max",
    "no_decrease",
    "no_increase",
    "max_step",
    "no_sign_flip",
    "any_change",
    "no_new_bits",
    "below",
)
RULE_KEYS = {"id", "params", "severity", "reason", "ref", *CONDITIONS}
ALLOW_KEYS = {"rule", "param", "value", "by", "reason"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUILTIN = {"px4": "px4.toml", "ardupilot-copter": "ardupilot-copter.toml"}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    params: tuple[str, ...]
    severity: str
    reason: str
    conditions: dict
    ref: str = ""
    source: str = ""

    def matches(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self.params)

    def violations(self, old: float | None, new: float | None, state) -> list[str]:
        """What is wrong with moving from old to new; empty if nothing.

        ``None`` means unknown: a parameter with no default in the metadata
        (ArduPilot) that the file doesn't set. A condition that needs an
        unknown value is not judged.
        """
        c = self.conditions
        out = []
        if new is not None:
            if "forbid" in c and any(same_value(new, v) for v in c["forbid"]):
                out.append("a forbidden value")
            if "allow_only" in c and not any(same_value(new, v) for v in c["allow_only"]):
                out.append(f"not an allowed value ({', '.join(_num(v) for v in c['allow_only'])})")
            if "min" in c and new < c["min"] and not same_value(new, c["min"]):
                out.append(f"below the minimum of {_num(c['min'])}")
            if "max" in c and new > c["max"] and not same_value(new, c["max"]):
                out.append(f"above the maximum of {_num(c['max'])}")
            if "below" in c:
                other = state(c["below"])
                if other is not None and not new < other:
                    out.append(f"not below {c['below']} ({_num(other)})")
        if old is not None and new is not None and not same_value(old, new):
            if c.get("no_decrease") and new < old:
                out.append("lowered")
            if c.get("no_increase") and new > old:
                out.append("raised")
            if "max_step" in c:
                step = math.inf if old == 0 else abs(new - old) / abs(old)
                if step > c["max_step"]:
                    pct = "a step from zero" if old == 0 else f"a {step:.0%} step"
                    out.append(f"{pct} in one change (limit {c['max_step']:.0%})")
            if c.get("no_sign_flip") and (old < 0) != (new < 0) and old != 0 and new != 0:
                out.append("a sign flip")
            if c.get("any_change"):
                out.append("changed")
            if c.get("no_new_bits"):
                gained = int(new) & ~int(old)
                if gained:
                    bits = [i for i in range(32) if gained >> i & 1]
                    out.append(f"gains bit{'s' if len(bits) > 1 else ''} {', '.join(map(str, bits))}")
        return out


@dataclass(frozen=True)
class Allow:
    rule: str
    param: str
    value: float | str  # a number, or "removed"
    by: str
    reason: str
    source: str = ""


@dataclass
class RuleSet:
    firmware: str | None = None
    rules: list[Rule] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def for_param(self, name: str) -> list[Rule]:
        return [r for r in self.rules if r.matches(name)]

    def cross_refs(self) -> dict[str, list[Rule]]:
        """Parameters that 'below' rules compare against, so a change to the
        other side re-checks them."""
        out: dict[str, list[Rule]] = {}
        for r in self.rules:
            if "below" in r.conditions:
                out.setdefault(r.conditions["below"], []).append(r)
        return out


def _number(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RuleError(f"{where}: {v!r} is not a number")
    return float(v)


def _parse_rule(raw: dict, source: str, idx: int) -> Rule:
    where = f"{source}: rule {idx + 1}"
    if not isinstance(raw, dict):
        raise RuleError(f"{where}: not a table")
    rid = raw.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        raise RuleError(f"{where}: 'id' must be lowercase words joined by hyphens")
    where = f"{source}: rule {rid}"
    unknown = sorted(set(raw) - RULE_KEYS)
    if unknown:
        raise RuleError(f"{where}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}")
    params = raw.get("params")
    if isinstance(params, str):
        params = [params]
    if not params or not all(isinstance(p, str) and p for p in params):
        raise RuleError(f"{where}: 'params' must be a list of parameter names or globs")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RuleError(f"{where}: every rule needs a 'reason' a reviewer can read")
    severity = raw.get("severity", "block")
    if severity not in SEVERITIES:
        raise RuleError(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
    cond: dict[str, Any] = {}
    for k in CONDITIONS:
        if k not in raw:
            continue
        v = raw[k]
        if k in ("forbid", "allow_only"):
            if not isinstance(v, list) or not v:
                raise RuleError(f"{where}: '{k}' must be a non-empty list of numbers")
            cond[k] = [_number(x, where) for x in v]
        elif k in ("min", "max"):
            cond[k] = _number(v, where)
        elif k == "max_step":
            cond[k] = _number(v, where)
            if cond[k] <= 0:
                raise RuleError(f"{where}: 'max_step' is a fraction greater than 0")
        elif k == "below":
            if not isinstance(v, str) or any(ch in v for ch in "*?["):
                raise RuleError(f"{where}: 'below' names one parameter")
            cond[k] = v
        else:
            if v is not True:
                raise RuleError(f"{where}: '{k}' can only be true")
            cond[k] = True
    if not cond:
        raise RuleError(f"{where}: no condition (one of {', '.join(CONDITIONS)})")
    if "min" in cond and "max" in cond and cond["min"] > cond["max"]:
        raise RuleError(f"{where}: min is above max")
    if "below" in cond and len(params) != 1:
        raise RuleError(f"{where}: a 'below' rule applies to exactly one parameter")
    ref = raw.get("ref", "")
    return Rule(rid, tuple(params), severity, " ".join(reason.split()), cond, ref, source)


def parse(text: str, source: str) -> RuleSet:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RuleError(f"{source}: {e}") from None
    unknown = sorted(set(doc) - {"version", "firmware", "rule"})
    if unknown:
        raise RuleError(f"{source}: unknown top-level key {', '.join(unknown)}")
    if doc.get("version") != 1:
        raise RuleError(f"{source}: 'version = 1' is required (this is rule format 1)")
    fw = doc.get("firmware")
    if fw not in (None, "px4", "ardupilot"):
        raise RuleError(f"{source}: firmware must be 'px4' or 'ardupilot'")
    rs = RuleSet(fw, [], [source])
    for i, raw in enumerate(doc.get("rule", [])):
        rs.rules.append(_parse_rule(raw, source, i))
    return rs


def load(spec: str) -> RuleSet:
    if spec.startswith("builtin:"):
        name = spec.split(":", 1)[1]
        if name not in BUILTIN:
            raise RuleError(f"{spec}: no such built-in rule set (have: {', '.join(BUILTIN)})")
        text = resources.files("paramguard.data").joinpath(BUILTIN[name]).read_text()
        return parse(text, spec)
    try:
        with open(spec, encoding="utf-8") as f:
            return parse(f.read(), spec)
    except OSError as e:
        raise RuleError(f"{spec}: {e.strerror}") from None


def merge(sets: list[RuleSet]) -> RuleSet:
    out = RuleSet()
    seen: dict[str, str] = {}
    for s in sets:
        if s.firmware and out.firmware and s.firmware != out.firmware:
            raise RuleError(f"{s.sources[0]} is for {s.firmware} but other rules are for {out.firmware}")
        out.firmware = out.firmware or s.firmware
        out.sources += s.sources
        for r in s.rules:
            if r.id in seen:
                raise RuleError(f"rule {r.id} is defined in both {seen[r.id]} and {r.source}")
            seen[r.id] = r.source
            out.rules.append(r)
    return out


def validate_against(rs: RuleSet, meta: Meta) -> None:
    """Every name a rule mentions must exist in the metadata, or the rule can
    never fire."""
    if rs.firmware and rs.firmware != meta.firmware:
        raise RuleError(f"rules are for {rs.firmware}, but the metadata ({meta.source}) is {meta.firmware}")
    for r in rs.rules:
        for p in r.params:
            if any(ch in p for ch in "*?["):
                continue
            if p not in meta.params:
                hint = meta.suggest(p)
                did = f" (did you mean {hint[0]}?)" if hint else ""
                raise RuleError(f"{r.source}: rule {r.id}: {p} is not a parameter in {meta.source}{did}")
        # A glob may match nothing in one build (board outputs aren't in the
        # SITL metadata), but a rule that matches nothing at all can't fire.
        if not any(r.matches(n) for n in meta.params):
            raise RuleError(f"{r.source}: rule {r.id}: {', '.join(r.params)} matches no parameter in {meta.source}")
        other = r.conditions.get("below")
        if other and other not in meta.params:
            raise RuleError(f"{r.source}: rule {r.id}: {other} is not a parameter in {meta.source}")


def load_allows(path: str) -> list[Allow]:
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except OSError as e:
        raise RuleError(f"{path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise RuleError(f"{path}: {e}") from None
    unknown = sorted(set(doc) - {"allow"})
    if unknown:
        raise RuleError(f"{path}: unknown top-level key {', '.join(unknown)}")
    out = []
    for i, a in enumerate(doc.get("allow", [])):
        where = f"{path}: allow {i + 1}"
        extra = sorted(set(a) - ALLOW_KEYS)
        if extra:
            raise RuleError(f"{where}: unknown key {', '.join(extra)}")
        missing = sorted(k for k in ALLOW_KEYS if k not in a)
        if missing:
            raise RuleError(f"{where}: missing {', '.join(missing)} (an allowance names who and why)")
        for k in ("rule", "param", "by", "reason"):
            if not isinstance(a[k], str) or not a[k].strip():
                raise RuleError(f"{where}: '{k}' must be a non-empty string")
        v = a["value"]
        if v != "removed":
            v = _number(v, where)
        out.append(Allow(a["rule"], a["param"], v, a["by"], " ".join(a["reason"].split()), path))
    return out
