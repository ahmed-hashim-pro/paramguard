import os

from paramguard import params, rules
from paramguard.check import check, lint
from paramguard.rules import Allow

EX = os.path.join(os.path.dirname(__file__), "..", "examples", "survey-quad")


def load(name):
    return params.load(os.path.join(EX, name))


def ids(report, blocking_only=True):
    return sorted((f.check, f.param) for f in report.findings if f.blocking or not blocking_only)


def test_cleanup_that_deletes_a_non_default_line_is_blocked(px4, px4_rules):
    r = check(load("vehicle.params"), load("cleanup.params"), px4, px4_rules)
    # 17 lines deleted, 16 of them at the firmware default: one effective change.
    assert [(c.name, c.old, c.new, c.removed) for c in r.changes] == [("NAV_DLL_ACT", 2, 0, True)]
    assert ids(r) == [("gcs-loss-failsafe-off", "NAV_DLL_ACT")]
    assert r.blocked


def test_tuning_change(px4, px4_rules):
    r = check(load("vehicle.params"), load("tuning.params"), px4, px4_rules)
    assert ids(r) == [
        ("geofence-action-none", "GF_ACTION"),
        ("out-of-range", "MPC_Z_VEL_MAX_DN"),
        ("rate-gain-step", "MC_ROLLRATE_P"),
        ("unknown-param", "MPC_XY_CRUSIE"),
    ]
    warns = [(f.check, f.param) for f in r.findings if f.severity == "warn"]
    assert warns == [("descent-too-fast", "MPC_Z_VEL_MAX_DN")]
    # MC_PITCHRATE_P moved 20%: under the 50% limit.
    assert not [f for f in r.findings if f.param == "MC_PITCHRATE_P"]
    typo = next(f for f in r.findings if f.check == "unknown-param")
    assert "did you mean MPC_XY_CRUISE?" in typo.message and typo.line == 25


def test_same_file_has_no_changes(px4, px4_rules):
    r = check(load("vehicle.params"), load("vehicle.params"), px4, px4_rules)
    assert r.changes == [] and not r.blocked


def test_new_file_is_compared_with_firmware_defaults(px4, px4_rules):
    new = params.parse("GF_ACTION 1\nMPC_XY_CRUISE 5\n", "new.params")
    r = check(None, new, px4, px4_rules)
    assert r.base == "firmware defaults"
    assert [c.name for c in r.changes] == ["GF_ACTION"]  # MPC_XY_CRUISE 5 is the default
    assert ids(r) == [("geofence-action-none", "GF_ACTION")]


def test_allowance_covers_one_exact_value(px4, px4_rules):
    old = params.parse("MC_ROLLRATE_P 0.15\n", "a")
    allow = [Allow("rate-gain-step", "MC_ROLLRATE_P", 0.25, "A. Pilot", "retuned for 13-inch props")]
    ok = check(old, params.parse("MC_ROLLRATE_P 0.25\n", "b"), px4, px4_rules, allow)
    assert not ok.blocked
    assert ok.findings[0].allowed.by == "A. Pilot"
    moved_on = check(old, params.parse("MC_ROLLRATE_P 0.3\n", "b"), px4, px4_rules, allow)
    assert moved_on.blocked
    assert ("allow-unused", "MC_ROLLRATE_P") in [(f.check, f.param) for f in moved_on.findings]


def test_allowance_for_a_removal(px4, px4_rules):
    allow = [Allow("gcs-loss-failsafe-off", "NAV_DLL_ACT", "removed", "ops", "tethered vehicle")]
    r = check(load("vehicle.params"), load("cleanup.params"), px4, px4_rules, allow)
    assert not r.blocked
    # The same allowance doesn't cover setting the value explicitly.
    explicit = params.parse(open(os.path.join(EX, "vehicle.params")).read().replace("NAV_DLL_ACT\t2", "NAV_DLL_ACT\t0"))
    assert check(load("vehicle.params"), explicit, px4, px4_rules, allow).blocked


def test_cross_parameter_rule_fires_when_the_other_side_moves(px4, px4_rules):
    old = params.parse("BAT_LOW_THR 0.15\nBAT_CRIT_THR 0.10\n", "a")
    # Both move, and the critical threshold ends up above the low one.
    new = params.parse("BAT_LOW_THR 0.12\nBAT_CRIT_THR 0.13\n", "b")
    r = check(old, new, px4, px4_rules)
    assert ("battery-critical-order", "BAT_CRIT_THR") in ids(r)
    only_low = check(
        params.parse("BAT_LOW_THR 0.15\nBAT_CRIT_THR 0.13\n", "a"),
        params.parse("BAT_LOW_THR 0.12\nBAT_CRIT_THR 0.13\n", "b"),
        px4,
        px4_rules,
    )
    f = next(f for f in only_low.findings if f.check == "battery-critical-order")
    assert "after BAT_LOW_THR changed to 0.12" in f.message and f.blocking


def test_problems_already_in_the_base_are_notes(px4, px4_rules):
    old = params.parse("MPC_XY_CRUSIE 8\nGF_ACTION 3\n", "a")
    new = params.parse("MPC_XY_CRUSIE 8\nGF_ACTION 2\n", "b")
    r = check(old, new, px4, px4_rules)
    assert not r.blocked
    note = next(f for f in r.findings if f.check == "unknown-param")
    assert note.severity == "note" and "already in the base" in note.message


def test_rotor_direction_flip_and_airframe_change(px4, px4_rules):
    old = params.parse("CA_ROTOR2_KM -0.05\nSYS_AUTOSTART 4001\n", "a")
    new = params.parse("CA_ROTOR2_KM 0.05\nSYS_AUTOSTART 4014\n", "b")
    assert ids(check(old, new, px4, px4_rules)) == [
        ("airframe-changed", "SYS_AUTOSTART"),
        ("rotor-direction-flipped", "CA_ROTOR2_KM"),
    ]


def test_metadata_checks(px4, px4_rules):
    new = params.parse("NAV_DLL_ACT 4\nGF_ACTION 2.5\nCOM_RCL_EXCEPT 9\nMC_ROLLRATE_P -1\n", "b")
    got = ids(check(None, new, px4, px4_rules))
    assert ("not-in-values", "NAV_DLL_ACT") in got
    assert ("not-integer", "GF_ACTION") in got
    assert ("unknown-bits", "COM_RCL_EXCEPT") in got  # bit 3 isn't defined
    assert ("out-of-range", "MC_ROLLRATE_P") in got


def test_lint_includes_defaults(px4, px4_rules):
    r = lint(load("cleanup.params"), px4, px4_rules)
    nav = next(f for f in r.findings if f.param == "NAV_DLL_ACT")
    assert nav.blocking and "the firmware default" in nav.message
    assert not lint(load("vehicle.params"), px4, px4_rules).blocked


def test_ardupilot_without_defaults():
    from paramguard import meta

    m = meta.load(os.path.join(os.path.dirname(__file__), "data", "copter-mini.pdef.xml"))
    rs = rules.parse(
        'version = 1\nfirmware = "ardupilot"\n[[rule]]\nid = "gcs"\nparams = ["FS_GCS_ENABLE"]\n'
        'forbid = [0]\nreason = "r"\n',
        "t",
    )
    rules.validate_against(rs, m)
    r = check(params.parse("FS_GCS_ENABLE 1\n"), params.parse("FS_GCS_ENABLE 0\n"), m, rs)
    assert ids(r) == [("gcs", "FS_GCS_ENABLE")]
    removed = check(params.parse("FS_GCS_ENABLE 1\n"), params.parse(""), m, rs)
    assert [(c.old, c.new) for c in removed.changes] == [(1, None)]
    assert not removed.blocked  # the default is unknown, so it can't be judged...
    assert [(f.check, f.severity) for f in removed.findings] == [("unknown-default", "warn")]  # ...but it's said
