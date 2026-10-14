"""Properties of the checker over random parameter files.

The files are drawn from the real PX4 v1.15.4 metadata, so every name, type
and range is one a vehicle actually has.
"""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from paramguard import meta as metamod
from paramguard import params, rules
from paramguard.check import check

META = metamod.load("px4-1.15.4")
RULES = rules.load("builtin:px4")
NAMES = sorted(n for n in META.params if n.isupper())
RULED = sorted(n for n in NAMES if RULES.for_param(n))


@st.composite
def value_for(draw, name):
    pm = META.get(name)
    if pm.values is not None and pm.type == "int":
        return float(draw(st.sampled_from(sorted(pm.values))))
    lo = pm.min if pm.min is not None else -1000.0
    hi = pm.max if pm.max is not None else 1000.0
    if pm.type == "int":
        return float(draw(st.integers(int(lo), int(hi))))
    return params.as_float32(draw(st.floats(lo, hi, allow_nan=False, allow_infinity=False)))


@st.composite
def param_files(draw, min_size=0, max_size=25):
    # Lean on the parameters rules look at, so the properties get exercised.
    names = draw(
        st.lists(st.sampled_from(RULED) | st.sampled_from(NAMES), min_size=min_size, max_size=max_size, unique=True)
    )
    bitmasks = {n for n in names if META.get(n).bitmask}
    names = [n for n in names if n not in bitmasks]
    return {n: draw(value_for(n)) for n in names}


def render(values: dict[str, float], order=None) -> params.ParamFile:
    names = order or sorted(values)
    types = {n: META.get(n).type for n in names}
    lines = []
    for n in names:
        v = values[n]
        text = str(int(v)) if types[n] == "int" else repr(v)
        lines.append(f"1\t1\t{n}\t{text}\t{6 if types[n] == 'int' else 9}")
    return params.parse("\n".join(lines) + "\n", "p.params")


def key(report):
    return sorted((f.severity, f.check, f.param, f.message) for f in report.findings)


@settings(max_examples=300, deadline=None)
@given(param_files())
def test_a_file_against_itself_changes_nothing(values):
    f = render(values)
    r = check(f, f, META, RULES)
    assert r.changes == []
    assert not r.blocked


@settings(max_examples=300, deadline=None)
@given(param_files(), param_files(), st.randoms(use_true_random=False))
def test_line_order_does_not_matter(old, new, rnd):
    order = sorted(new)
    rnd.shuffle(order)
    a = check(render(old), render(new), META, RULES)
    b = check(render(old), render(new, order), META, RULES)
    assert key(a) == key(b)
    assert [c.name for c in a.changes] == [c.name for c in b.changes]


@settings(max_examples=300, deadline=None)
@given(param_files(), param_files(), st.lists(st.sampled_from(NAMES), max_size=10, unique=True))
def test_spelling_out_a_default_changes_nothing(old, new, extra):
    """Writing a parameter at its firmware default is the same as leaving it out."""
    padded = dict(new)
    for n in extra:
        if n not in padded and META.get(n).default is not None and not META.get(n).bitmask:
            padded[n] = META.get(n).default
    a = check(render(old), render(new), META, RULES)
    b = check(render(old), render(padded), META, RULES)
    assert [(c.name, c.old, c.new) for c in a.changes] == [(c.name, c.old, c.new) for c in b.changes]
    rule_ids = {r.id for r in RULES.rules}

    def rule_findings(r):
        return sorted((f.check, f.param) for f in r.findings if f.check in rule_ids)

    assert rule_findings(a) == rule_findings(b)


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(sorted(META.get("NAV_DLL_ACT").values)), st.sampled_from(sorted(META.get("NAV_DLL_ACT").values)))
def test_link_loss_failsafe_blocks_exactly_on_disabled(old, new):
    r = check(params.parse(f"NAV_DLL_ACT {old}\n"), params.parse(f"NAV_DLL_ACT {new}\n"), META, RULES)
    assert r.blocked == (new == 0 and old != 0)


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from(
        [
            n
            for n in NAMES
            if META.get(n).type == "float"
            and META.get(n).min is not None
            and META.get(n).max is not None
            and META.get(n).min < META.get(n).max
        ]
    ),
    st.data(),
)
def test_range_check_matches_the_metadata(name, data):
    pm = META.get(name)
    inside = data.draw(st.floats(pm.min, pm.max, allow_nan=False))
    span = pm.max - pm.min
    outside = data.draw(
        st.one_of(
            st.floats(pm.max + span * 0.01 + 1e-3, pm.max + span * 10 + 1),
            st.floats(pm.min - span * 10 - 1, pm.min - span * 0.01 - 1e-3),
        )
    )

    def has_range_finding(v):
        r = check(None, params.parse(f"{name} {v!r}\n"), META, rules.RuleSet())
        return any(f.check == "out-of-range" for f in r.findings)

    assert not has_range_finding(inside)
    assert has_range_finding(outside)


@settings(max_examples=200, deadline=None)
@given(st.floats(0.01, 0.5), st.floats(0.01, 0.5))
def test_an_allowance_covers_only_its_own_value(allowed, actual):
    allowed, actual = params.as_float32(allowed), params.as_float32(actual)
    old = params.parse("MC_ROLLRATE_P 0.01\n")  # any move up from here is a big step
    allow = [rules.Allow("rate-gain-step", "MC_ROLLRATE_P", allowed, "me", "test")]
    r = check(old, params.parse(f"MC_ROLLRATE_P {actual!r}\n"), META, RULES, allow)
    stepped = abs(actual - 0.01) / 0.01 > 0.5
    assert r.blocked == (stepped and not params.same_value(allowed, actual))


def test_random_typos_get_the_right_suggestion():
    rnd = random.Random(7)
    first = top3 = 0
    trials = 300
    for _ in range(trials):
        name = rnd.choice([n for n in NAMES if len(n) >= 8])
        i = rnd.randrange(1, len(name) - 1)
        typo = name[:i] + name[i + 1] + name[i] + name[i + 2 :]  # swap two letters
        if typo == name or typo in META.params:
            first += 1
            top3 += 1
            continue
        got = META.suggest(typo)
        first += got[:1] == [name]
        top3 += name in got
    # The misses are genuinely ambiguous: R1C1_MAX is one edit from RC1_MAX
    # as well as RC11_MAX. The right name is always among the three offered.
    assert first / trials > 0.97, first
    assert top3 == trials
