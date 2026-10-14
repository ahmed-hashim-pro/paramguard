"""paramguard's command line.

    paramguard check OLD NEW               compare two parameter files
    paramguard check --base REF FILE...    compare files with their version at a git ref
    paramguard lint FILE...                check files on their own
    paramguard explain NAME...             what the metadata and rules say about a parameter
    paramguard rules                       list the rules in force

Exit status: 0 nothing blocking, 1 something blocking, 2 the input could not
be read (a bad parameter file, rule file, allowance file or metadata).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys

from . import __version__, params, report
from . import check as checks
from . import meta as metamod
from . import rules as rulesmod

DEFAULT_ALLOW = "paramguard.allow.toml"


class UsageError(Exception):
    pass


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--meta",
        default=metamod.DEFAULT_META,
        metavar="ID|PATH",
        help=f"parameter metadata: a bundled id ({', '.join(metamod.BUNDLED)}) or a PX4 "
        "parameters.json / ArduPilot apm.pdef.xml (default: %(default)s)",
    )
    p.add_argument(
        "--rules",
        action="append",
        metavar="FILE",
        help="a rule file, or builtin:px4 / builtin:ardupilot-copter; repeatable "
        "(default: the built-in set for the metadata's firmware)",
    )
    p.add_argument("--no-builtin", action="store_true", help="don't add the built-in rules to --rules")
    p.add_argument(
        "--allow", metavar="FILE", help=f"allowances for deliberate changes (default: ./{DEFAULT_ALLOW} if it exists)"
    )


def _output(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=("text", "json", "github"), default="text")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="paramguard", description="Catch dangerous autopilot parameter changes.")
    ap.add_argument("--version", action="version", version=f"paramguard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="compare parameter files and block dangerous changes")
    c.add_argument("files", nargs="+", metavar="FILE")
    c.add_argument("--base", metavar="REF", help="compare each FILE with its version at this git ref")
    _common(c)
    _output(c)

    lt = sub.add_parser("lint", help="check parameter files on their own, defaults included")
    lt.add_argument("files", nargs="+", metavar="FILE")
    _common(lt)
    _output(lt)

    e = sub.add_parser("explain", help="show the metadata and rules for parameters")
    e.add_argument("names", nargs="+", metavar="NAME")
    _common(e)

    r = sub.add_parser("rules", help="list the rules in force and check them against the metadata")
    _common(r)
    return ap


def _load_rules(args, meta: metamod.Meta) -> rulesmod.RuleSet:
    specs = list(args.rules or [])
    builtin = {"px4": "builtin:px4", "ardupilot": "builtin:ardupilot-copter"}[meta.firmware]
    if not args.no_builtin and builtin not in specs:
        specs.insert(0, builtin)
    rs = rulesmod.merge([rulesmod.load(s) for s in specs])
    rulesmod.validate_against(rs, meta)
    return rs


def _load_allows(args) -> list[rulesmod.Allow]:
    path = args.allow
    if path is None:
        if not os.path.exists(DEFAULT_ALLOW):
            return []
        path = DEFAULT_ALLOW
    return rulesmod.load_allows(path)


def _git_show(ref: str, path: str) -> str | None:
    """The file at ``ref``, or None if it didn't exist there."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise UsageError("--base needs to run inside a git repository") from None
    rel = os.path.relpath(os.path.abspath(path), top.stdout.strip())
    if (
        not subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], capture_output=True
        ).returncode
        == 0
    ):
        raise UsageError(f"--base {ref}: no such commit (in CI, fetch it first)")
    r = subprocess.run(["git", "show", f"{ref}:{rel}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout


def _emit(reports, meta, args) -> int:
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    if args.format == "json":
        print(report.as_json(reports))
    elif args.format == "github":
        for r in reports:
            lines = report.github(r)
            if lines:
                print(lines)
        for r in reports:
            print(report.text(r, meta, color=False), file=sys.stderr)
    else:
        print("\n\n".join(report.text(r, meta, color=color) for r in reports))
    return 1 if any(r.blocked for r in reports) else 0


def cmd_check(args) -> int:
    meta = metamod.load(args.meta)
    rs = _load_rules(args, meta)
    allows = _load_allows(args)
    pairs = []
    if args.base:
        for path in args.files:
            new = params.load(path)
            old_text = _git_show(args.base, path)
            old = None if old_text is None else params.parse(old_text, f"{path}@{args.base}")
            pairs.append((old, new, f"{args.base}" if old else f"firmware defaults (new at {args.base})"))
    else:
        if len(args.files) != 2:
            raise UsageError("check takes OLD NEW, or --base REF FILE...")
        pairs.append((params.load(args.files[0]), params.load(args.files[1]), args.files[0]))
    reports = [checks.check(old, new, meta, rs, allows, base=label) for old, new, label in pairs]
    return _emit(reports, meta, args)


def cmd_lint(args) -> int:
    meta = metamod.load(args.meta)
    rs = _load_rules(args, meta)
    allows = _load_allows(args)
    reports = [checks.lint(params.load(p), meta, rs, allows) for p in args.files]
    return _emit(reports, meta, args)


def cmd_explain(args) -> int:
    meta = metamod.load(args.meta)
    rs = _load_rules(args, meta)
    status = 0
    for name in args.names:
        pm = meta.get(name)
        if pm is None:
            hint = meta.suggest(name)
            print(
                f"{name}: not a parameter in {meta.source}" + (f"; did you mean {' or '.join(hint)}?" if hint else "")
            )
            status = 1
            continue
        print(f"{pm.name}  {pm.short}")
        bits = [f"type {pm.type}"]
        if pm.default is not None:
            bits.append(f"default {pm.describe_value(pm.default)}")
        if pm.min is not None or pm.max is not None:
            lo = "" if pm.min is None else metamod._num(pm.min)
            hi = "" if pm.max is None else metamod._num(pm.max)
            bits.append(f"range {lo}..{hi}{' ' + pm.units if pm.units else ''}")
        if pm.reboot:
            bits.append("needs a reboot")
        print("  " + ", ".join(bits))
        if pm.values:
            for k, d in sorted(pm.values.items()):
                print(f"    {k:>4}  {d}")
        if pm.bitmask:
            for k, d in sorted(pm.bitmask.items()):
                print(f"    bit {k:>2}  {d}")
        for r in rs.for_param(name):
            cond = ", ".join(f"{k} {_cond(v)}" for k, v in r.conditions.items())
            print(f"  rule {r.id} ({r.severity}; {cond})")
            print(report._wrap(r.reason, "    "))
        print()
    return status


def _cond(v) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(metamod._num(x) for x in v) + "]"
    if isinstance(v, float):
        return metamod._num(v)
    return str(v).lower() if isinstance(v, bool) else str(v)


def cmd_rules(args) -> int:
    meta = metamod.load(args.meta)
    rs = _load_rules(args, meta)
    print(f"{len(rs.rules)} rules from {', '.join(rs.sources)}, checked against {meta.source}")
    w = max(len(r.id) for r in rs.rules)
    for r in rs.rules:
        print(f"  {r.severity:<5}  {r.id:<{w}}  {', '.join(r.params)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Behave like a Unix tool when piped into head.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    args = build_parser().parse_args(argv)
    try:
        return {"check": cmd_check, "lint": cmd_lint, "explain": cmd_explain, "rules": cmd_rules}[args.cmd](args)
    except (params.ParamFileError, rulesmod.RuleError, metamod.MetaError, UsageError, OSError) as e:
        msg = str(e)
        if isinstance(e, OSError) and e.filename:
            msg = f"{e.filename}: {e.strerror}"
        print(f"paramguard: {msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
