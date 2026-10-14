import pytest

from paramguard import params
from paramguard.params import ParamFileError, parse, same_value


def test_reads_all_three_line_formats():
    f = parse(
        "# a comment\n"
        "1\t1\tGF_ACTION\t3\t6\n"
        "param set NAV_DLL_ACT 2\n"
        "param set-default COM_DL_LOSS_T 10\n"
        "MPC_XY_CRUISE 5.5\n"
        "BAT_LOW_THR,0.2\n"
        "\n"
    )
    assert f.names() == ["BAT_LOW_THR", "COM_DL_LOSS_T", "GF_ACTION", "MPC_XY_CRUISE", "NAV_DLL_ACT"]
    assert f["GF_ACTION"].declared_type == "int"
    assert f["GF_ACTION"].line == 2
    assert f["MPC_XY_CRUISE"].value == 5.5
    assert f["BAT_LOW_THR"].value == 0.2
    assert f["NAV_DLL_ACT"].declared_type is None


@pytest.mark.parametrize(
    "text, message",
    [
        ("GF_ACTION\n", "expected NAME VALUE"),
        ("GF_ACTION three\n", "'three' is not a number"),
        ("GF_ACTION nan\n", "is not a number"),
        ("9BAD 1\n", "is not a parameter name"),
        ("A_VERY_LONG_PARAMETER_NAME 1\n", "at most 16"),
        ("param set GF_ACTION\n", "expected 'param set NAME VALUE'"),
        ("1\t1\tGF_ACTION\t2.5\t6\n", "declared integer but has a fraction"),
        ("1\t1\tGF_ACTION\t2\t42\n", "not a MAV_PARAM_TYPE"),
        ("GF_ACTION 3\nNAV_DLL_ACT 2\nGF_ACTION 1\n", "set again (first set on line 1)"),
    ],
)
def test_bad_lines_are_refused_with_their_line_number(text, message):
    with pytest.raises(ParamFileError) as e:
        parse(text, "v.params")
    assert message in str(e.value)
    assert str(e.value).startswith("v.params:")


def test_error_points_at_the_offending_line():
    with pytest.raises(ParamFileError) as e:
        parse("# header\nGF_ACTION 3\nNAV_DLL_ACT two\n", "v.params")
    assert e.value.line == 3


def test_float32_spellings_are_the_same_value():
    # QGroundControl writes 0.15 as the float32 it is stored as.
    assert same_value(0.15, 0.15000000596046448)
    assert same_value(5, 5.0)
    assert not same_value(0.15, 0.16)
    assert not same_value(0.15, 0.15000002)


def test_qgc_round_trip():
    values = {"GF_ACTION": 3.0, "BAT_LOW_THR": 0.15000000596046448}
    text = params.to_qgc(values, {"GF_ACTION": "int"})
    f = parse(text)
    assert f["GF_ACTION"].value == 3 and f["GF_ACTION"].declared_type == "int"
    assert same_value(f["BAT_LOW_THR"].value, 0.15)
