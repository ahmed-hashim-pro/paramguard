import os

import pytest

from paramguard import rules
from paramguard.rules import RuleError, parse

HEAD = 'version = 1\nfirmware = "px4"\n'


def one(body: str):
    return parse(HEAD + "[[rule]]\n" + body, "t.toml").rules[0]


@pytest.mark.parametrize(
    "body, message",
    [
        ('id = "x"\nparams = ["GF_ACTION"]\nforbid = [0]\n', "needs a 'reason'"),
        ('id = "x"\nparams = ["GF_ACTION"]\nreason = "r"\n', "no condition"),
        ('id = "x"\nparams = ["GF_ACTION"]\nforbidd = [0]\nreason = "r"\n', "unknown key forbidd"),
        ('id = "X Y"\nparams = ["GF_ACTION"]\nforbid = [0]\nreason = "r"\n', "'id' must be"),
        ('id = "x"\nparams = []\nforbid = [0]\nreason = "r"\n', "'params' must be"),
        ('id = "x"\nparams = ["A"]\nforbid = [0]\nreason = "r"\nseverity = "fatal"\n', "severity"),
        ('id = "x"\nparams = ["A"]\nforbid = "0"\nreason = "r"\n', "non-empty list"),
        ('id = "x"\nparams = ["A"]\nmin = 5\nmax = 1\nreason = "r"\n', "min is above max"),
        ('id = "x"\nparams = ["A"]\nmax_step = 0\nreason = "r"\n', "greater than 0"),
        ('id = "x"\nparams = ["A", "B"]\nbelow = "C"\nreason = "r"\n', "exactly one parameter"),
        ('id = "x"\nparams = ["A"]\nno_decrease = false\nreason = "r"\n', "can only be true"),
    ],
)
def test_rule_file_mistakes_are_errors(body, message):
    with pytest.raises(RuleError, match=message):
        one(body)


def test_version_is_required():
    with pytest.raises(RuleError, match="version = 1"):
        parse('[[rule]]\nid = "x"\nparams = ["A"]\nforbid = [0]\nreason = "r"\n', "t.toml")


def test_conditions():
    def v(body, old, new, state=lambda n: None):
        return one(body + 'reason = "r"\nparams = ["P"]\nid = "x"\n').violations(old, new, state)

    assert v("forbid = [0]\n", 2, 0) == ["a forbidden value"]
    assert v("forbid = [0]\n", 2, 1) == []
    assert v("allow_only = [1, 3]\n", 1, 2) == ["not an allowed value (1, 3)"]
    assert v("min = 20\n", 60, 10) == ["below the minimum of 20"]
    assert v("max = 30\n", 10, 30) == []
    assert v("no_decrease = true\n", 0.15, 0.1) == ["lowered"]
    assert v("no_decrease = true\n", 0.15, 0.15000000596046448) == []
    assert v("no_increase = true\n", 12, 15) == ["raised"]
    assert v("max_step = 0.5\n", 0.15, 0.25) == ["a 67% step in one change (limit 50%)"]
    assert v("max_step = 0.5\n", 0.15, 0.2) == []
    assert v("max_step = 0.5\n", 0, 0.1) == ["a step from zero in one change (limit 50%)"]
    assert v("no_sign_flip = true\n", 0.05, -0.05) == ["a sign flip"]
    assert v("no_sign_flip = true\n", 0.05, 0.06) == []
    assert v("any_change = true\n", 4001, 4001) == []
    assert v("any_change = true\n", 4001, 4002) == ["changed"]
    assert v("no_new_bits = true\n", 1, 7) == ["gains bits 1, 2"]
    assert v("no_new_bits = true\n", 7, 1) == []
    assert v('below = "Q"\n', 0.07, 0.2, lambda n: 0.15) == ["not below Q (0.15)"]
    # Unknown values (ArduPilot has no defaults) are not judged.
    assert v("forbid = [0]\n", 2, None) == []
    assert v("no_decrease = true\n", None, 1) == []


def test_globs():
    r = one('id = "x"\nparams = ["MC_*RATE_[PIDK]"]\nmax_step = 0.5\nreason = "r"\n')
    assert r.matches("MC_ROLLRATE_P") and r.matches("MC_YAWRATE_K")
    assert not r.matches("MC_ROLLRATE_MAX")


def test_builtin_px4_rules_all_exist_in_the_metadata(px4, px4_rules):
    assert len(px4_rules.rules) >= 25
    for r in px4_rules.rules:
        assert any(r.matches(n) for n in px4.params), r.id


def test_validation_catches_misspelt_rule_parameters(px4):
    rs = parse(HEAD + '[[rule]]\nid = "x"\nparams = ["NAV_DLL_ACTT"]\nforbid = [0]\nreason = "r"\n', "t.toml")
    with pytest.raises(RuleError, match="did you mean NAV_DLL_ACT"):
        rules.validate_against(rs, px4)
    rs = parse(HEAD + '[[rule]]\nid = "x"\nparams = ["NOPE_*"]\nforbid = [0]\nreason = "r"\n', "t.toml")
    with pytest.raises(RuleError, match="matches no parameter"):
        rules.validate_against(rs, px4)


def test_firmware_mismatch(px4):
    rs = rules.load("builtin:ardupilot-copter")
    with pytest.raises(RuleError, match="rules are for ardupilot"):
        rules.validate_against(rs, px4)


def test_duplicate_rule_ids_across_files():
    a = parse(HEAD + '[[rule]]\nid = "x"\nparams = ["A"]\nforbid = [0]\nreason = "r"\n', "a.toml")
    b = parse(HEAD + '[[rule]]\nid = "x"\nparams = ["B"]\nforbid = [0]\nreason = "r"\n', "b.toml")
    with pytest.raises(RuleError, match="defined in both a.toml and b.toml"):
        rules.merge([a, b])


def test_allowances_must_say_who_and_why(tmp_path):
    p = tmp_path / "allow.toml"
    p.write_text('[[allow]]\nrule = "x"\nparam = "A"\nvalue = 1\nby = "me"\n')
    with pytest.raises(RuleError, match="missing reason"):
        rules.load_allows(str(p))
    p.write_text('[[allow]]\nrule = "x"\nparam = "A"\nvalue = "removed"\nby = "me"\nreason = "  "\n')
    with pytest.raises(RuleError, match="'reason' must be"):
        rules.load_allows(str(p))
    p.write_text('[[allow]]\nrule = "x"\nparam = "A"\nvalue = "removed"\nby = "me"\nreason = "ok"\n')
    assert rules.load_allows(str(p))[0].value == "removed"


AP_META = os.environ.get("PARAMGUARD_AP_META")


@pytest.mark.skipif(not AP_META, reason="set PARAMGUARD_AP_META to an ArduCopter apm.pdef.xml (make ap-meta)")
def test_builtin_ardupilot_rules_against_real_metadata():
    from paramguard import meta

    m = meta.load(AP_META)
    rs = rules.load("builtin:ardupilot-copter")
    rules.validate_against(rs, m)
    assert len(rs.rules) >= 15
