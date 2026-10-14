"""Reading parameter files.

Three line formats are accepted, and may be mixed in one file:

    1	1	GF_ACTION	3	6       QGroundControl export (tab-separated:
                                    system, component, name, value, type)
    param set GF_ACTION 3           PX4 shell / airframe script
    param set-default GF_ACTION 3
    GF_ACTION 3                     plain (also ArduPilot .param and
    GF_ACTION,3                     Mission Planner exports)

Blank lines and lines starting with '#' are ignored. Anything else is an
error with its line number: a parameter file that is half-read is worse than
one that is refused.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field

# MAV_PARAM_TYPE values QGroundControl writes in its fifth column.
MAV_INT_TYPES = {1, 2, 3, 4, 5, 6, 7, 8}
MAV_FLOAT_TYPES = {9, 10}

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# PX4 and ArduPilot both cap names at 16 characters (the MAVLink param_id field).
MAX_NAME_LEN = 16


class ParamFileError(ValueError):
    def __init__(self, path: str, line: int, message: str):
        super().__init__(f"{path}:{line}: {message}")
        self.path = path
        self.line = line
        self.message = message


@dataclass(frozen=True)
class Param:
    name: str
    value: float
    text: str  # the value exactly as written
    line: int
    # 'int' or 'float' when the file says so (QGC's type column), else None.
    declared_type: str | None = None


@dataclass
class ParamFile:
    path: str
    params: dict[str, Param] = field(default_factory=dict)

    def __contains__(self, name: str) -> bool:
        return name in self.params

    def __getitem__(self, name: str) -> Param:
        return self.params[name]

    def names(self) -> list[str]:
        return sorted(self.params)


def parse_number(text: str) -> float | None:
    try:
        v = float(text)
    except ValueError:
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def as_float32(v: float) -> float:
    """The value a float parameter actually holds on the vehicle."""
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except OverflowError:
        return math.copysign(math.inf, v)


def same_value(a: float, b: float) -> bool:
    """Equal as the autopilot would store them.

    QGroundControl writes float32 values out in full (0.15 becomes
    0.15000000596046448); both spellings are the same parameter value.
    """
    return a == b or as_float32(a) == as_float32(b)


def _parse_line(raw: str) -> tuple[str, str, str | None] | str | None:
    """(name, value text, declared type) for a data line, None for a blank or
    comment line, or an error message."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if "\t" in line:
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) == 5 and cols[0].isdigit() and cols[1].isdigit():
            try:
                t = int(cols[4])
            except ValueError:
                return f"type column {cols[4]!r} is not a MAV_PARAM_TYPE number"
            if t in MAV_INT_TYPES:
                declared = "int"
            elif t in MAV_FLOAT_TYPES:
                declared = "float"
            else:
                return f"type column {t} is not a MAV_PARAM_TYPE this tool knows"
            return cols[2], cols[3], declared
    words = line.split()
    if words[0] == "param":
        if len(words) == 4 and words[1] in ("set", "set-default"):
            return words[2], words[3], None
        return "expected 'param set NAME VALUE'"
    if len(words) == 1 and "," in words[0]:
        words = words[0].split(",")
    if len(words) == 2:
        return words[0], words[1], None
    return "expected NAME VALUE, NAME,VALUE, 'param set NAME VALUE' or a QGroundControl line"


def parse(text: str, path: str = "<params>") -> ParamFile:
    out = ParamFile(path)
    for i, raw in enumerate(text.splitlines(), start=1):
        parsed = _parse_line(raw)
        if parsed is None:
            continue
        if isinstance(parsed, str):
            raise ParamFileError(path, i, parsed)
        name, value_text, declared = parsed
        if not NAME_RE.match(name):
            raise ParamFileError(path, i, f"{name!r} is not a parameter name")
        if len(name) > MAX_NAME_LEN:
            raise ParamFileError(path, i, f"{name} is {len(name)} characters; MAVLink names are at most {MAX_NAME_LEN}")
        value = parse_number(value_text)
        if value is None:
            raise ParamFileError(path, i, f"{name}: {value_text!r} is not a number")
        if declared == "int" and value != int(value):
            raise ParamFileError(path, i, f"{name}: {value_text} is declared integer but has a fraction")
        if name in out.params:
            first = out.params[name].line
            raise ParamFileError(
                path, i, f"{name} is set again (first set on line {first}); which one wins depends on the loader"
            )
        out.params[name] = Param(name, value, value_text, i, declared)
    return out


def load(path: str) -> ParamFile:
    with open(path, encoding="utf-8") as f:
        return parse(f.read(), path)


def format_value(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(as_float32(v)) if len(repr(v)) > 12 else repr(v)


def to_qgc(params: dict[str, float], types: dict[str, str], sysid: int = 1, compid: int = 1) -> str:
    """A QGroundControl-style export, for round trips and tests."""
    lines = [
        "# Onboard parameters for Vehicle 1",
        "#",
        "# Stack: PX4 Pro",
        "#",
        "# Vehicle-Id Component-Id Name Value Type",
    ]
    for name in sorted(params):
        t = 6 if types.get(name) == "int" else 9
        v = params[name]
        text = str(int(v)) if t == 6 else repr(v)
        lines.append(f"{sysid}\t{compid}\t{name}\t{text}\t{t}")
    return "\n".join(lines) + "\n"
