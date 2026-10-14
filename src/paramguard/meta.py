"""Parameter metadata: what each name means, its type, range and default.

Two formats are read:

- PX4's ``parameters.json``, which every PX4 build generates (and QGroundControl
  downloads from the vehicle). The PX4 v1.15.4 SITL build's copy ships with
  this package as ``px4-1.15.4``.
- ArduPilot's ``apm.pdef.xml`` or ``apm.pdef.json``
  (autotest.ardupilot.org/Parameters/versioned/<vehicle>/<release>/). It has
  no defaults, so a parameter deleted from an ArduPilot file is
  reported as reverting to an unknown default.

Metadata is per build: a flight controller board compiles in drivers the SITL
build doesn't, and their parameters are unknown to the SITL metadata. Point
``--meta`` at your own build's parameters.json for those.
"""

from __future__ import annotations

import difflib
import json
import lzma
from dataclasses import dataclass, field
from importlib import resources

BUNDLED = {"px4-1.15.4": ("px4-1.15.4-sitl.json.xz", "PX4 v1.15.4 (px4_sitl_default)")}
DEFAULT_META = "px4-1.15.4"


class MetaError(ValueError):
    pass


@dataclass(frozen=True)
class ParamMeta:
    name: str
    type: str  # 'int' or 'float'
    default: float | None
    min: float | None = None
    max: float | None = None
    values: dict[int, str] | None = None
    bitmask: dict[int, str] | None = None
    units: str = ""
    short: str = ""
    group: str = ""
    reboot: bool = False

    def describe_value(self, v: float) -> str:
        """'2 (Return mode)' where the metadata names the value."""
        text = _num(v)
        if self.values is not None and v == int(v) and int(v) in self.values:
            return f"{text} ({self.values[int(v)]})"
        if self.units:
            return f"{text} {self.units}"
        return text


@dataclass
class Meta:
    source: str
    firmware: str  # 'px4' or 'ardupilot'
    params: dict[str, ParamMeta] = field(default_factory=dict)

    def get(self, name: str) -> ParamMeta | None:
        return self.params.get(name)

    def suggest(self, name: str) -> list[str]:
        """Real names close to a misspelt one."""
        by_upper = {n.upper(): n for n in self.params}
        want = name.upper()
        if want in by_upper and by_upper[want] != name:
            return [by_upper[want]]
        # Typos are mostly one or two edits: a missing, extra, wrong or swapped
        # letter. Rank by that edit distance, then by overall similarity.
        limit = 1 if len(want) <= 5 else 2
        near = []
        for cand in by_upper:
            if abs(len(cand) - len(want)) > limit:
                continue
            d = _edits(want, cand, limit)
            if d <= limit:
                near.append((d, -difflib.SequenceMatcher(None, want, cand).ratio(), cand))
        if near:
            return [by_upper[c] for _, _, c in sorted(near)[:3]]
        close = difflib.get_close_matches(want, by_upper.keys(), n=3, cutoff=0.75)
        return [by_upper[c] for c in close]


def _edits(a: str, b: str, limit: int) -> int:
    """Edit distance counting a swap of neighbouring letters as one edit
    (optimal string alignment); stops early once it exceeds ``limit``."""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _num(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:g}"


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def from_px4(doc: dict, source: str) -> Meta:
    if "parameters" not in doc:
        raise MetaError(f"{source}: not a PX4 parameters.json (no 'parameters' list)")
    meta = Meta(source, "px4")
    for p in doc["parameters"]:
        t = {"Int32": "int", "Float": "float"}.get(p.get("type"))
        if t is None:
            raise MetaError(f"{source}: {p.get('name')}: unknown type {p.get('type')!r}")
        values = {int(v["value"]): v["description"] for v in p["values"]} if p.get("values") else None
        bitmask = {int(b["index"]): b["description"] for b in p["bitmask"]} if p.get("bitmask") else None
        meta.params[p["name"]] = ParamMeta(
            name=p["name"],
            type=t,
            default=_f(p.get("default")),
            min=_f(p.get("min")),
            max=_f(p.get("max")),
            values=values,
            bitmask=bitmask,
            units=p.get("units", ""),
            short=p.get("shortDesc", ""),
            group=p.get("group", ""),
            reboot=bool(p.get("rebootRequired", False)),
        )
    return meta


def _ap_values(codes: dict[str, str], has_range: bool) -> dict[int, str] | None:
    """ArduPilot value lists are sometimes a complete enum (FS_GCS_ENABLE) and
    sometimes suggestions (MOT_SPIN_ARM: 0.0 low, 0.1 default, 0.2 high).
    Only whole-number lists on parameters without a range are treated as
    the complete set of values."""
    try:
        parsed = {float(k): v for k, v in codes.items()}
    except ValueError:
        return None
    if has_range or any(k != int(k) for k in parsed):
        return None
    return {int(k): v for k, v in parsed.items()}


def from_ardupilot(doc: dict, source: str) -> Meta:
    meta = Meta(source, "ardupilot")
    for group in doc.values():
        if not isinstance(group, dict):
            continue
        for name, p in group.items():
            if not isinstance(p, dict) or name in meta.params:
                # The same name appears once per library path (e.g. heli and
                # multicopter attitude control); the first description wins.
                continue
            rng = p.get("Range") or {}
            values = _ap_values(p["Values"], bool(rng)) if isinstance(p.get("Values"), dict) else None
            bitmask = None
            if isinstance(p.get("Bitmask"), dict):
                try:
                    bitmask = {int(k): v for k, v in p["Bitmask"].items()}
                except ValueError:
                    bitmask = None
            # ArduPilot stores every parameter as a float on the wire; enum and
            # bitmask parameters only make sense as whole numbers.
            t = "int" if values is not None or bitmask is not None else "float"
            meta.params[name] = ParamMeta(
                name=name,
                type=t,
                default=None,
                min=_f(rng.get("low")),
                max=_f(rng.get("high")),
                values=values,
                bitmask=bitmask,
                units=p.get("Units", ""),
                short=p.get("DisplayName", ""),
                group=p.get("path", ""),
                reboot=bool(p.get("RebootRequired")),
            )
    if not meta.params:
        raise MetaError(f"{source}: no parameters found (expected ArduPilot apm.pdef.json)")
    return meta


def from_ardupilot_xml(text: bytes, source: str) -> Meta:
    """ArduPilot's apm.pdef.xml, the format its versioned releases publish."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise MetaError(f"{source}: not XML ({e})") from None
    meta = Meta(source, "ardupilot")
    for el in root.iter("param"):
        name = el.get("name", "").split(":")[-1]
        if not name or name in meta.params:
            continue
        fields = {f.get("name"): (f.text or "").strip() for f in el.findall("field")}
        codes = {v.get("code", ""): (v.text or "").strip() for v in el.findall("values/value")}
        values = _ap_values(codes, "Range" in fields) if codes else None
        bitmask = {int(b.get("code", "")): (b.text or "").strip() for b in el.findall("bitmask/bit")} or None
        lo = hi = None
        if "Range" in fields:
            parts = fields["Range"].split()
            if len(parts) == 2:
                lo, hi = _f(parts[0]), _f(parts[1])
        t = "int" if values is not None or bitmask is not None else "float"
        meta.params[name] = ParamMeta(
            name=name,
            type=t,
            default=None,
            min=lo,
            max=hi,
            values=values,
            bitmask=bitmask,
            units=fields.get("Units", ""),
            short=el.get("humanName", ""),
            group="",
            reboot=fields.get("RebootRequired", "").lower() == "true",
        )
    if not meta.params:
        raise MetaError(f"{source}: no <param> elements (expected ArduPilot apm.pdef.xml)")
    return meta


def parse(doc: dict, source: str) -> Meta:
    if isinstance(doc, dict) and "parameters" in doc:
        return from_px4(doc, source)
    return from_ardupilot(doc, source)


def load(spec: str = DEFAULT_META) -> Meta:
    """A bundled metadata id (``px4-1.15.4``) or a path to a .json/.json.xz file."""
    if spec in BUNDLED:
        fname, source = BUNDLED[spec]
        raw = resources.files("paramguard.data").joinpath(fname).read_bytes()
        return parse(json.loads(lzma.decompress(raw)), source)
    try:
        with open(spec, "rb") as f:
            raw = f.read()
    except OSError as e:
        known = ", ".join(BUNDLED)
        raise MetaError(f"{spec}: {e.strerror}; bundled metadata: {known}") from None
    if spec.endswith(".xz"):
        raw = lzma.decompress(raw)
    if raw.lstrip()[:1] == b"<":
        return from_ardupilot_xml(raw, spec)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MetaError(f"{spec}: not JSON ({e})") from None
    return parse(doc, spec)
