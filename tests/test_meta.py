import os

import pytest

from paramguard import meta

HERE = os.path.dirname(__file__)


def test_bundled_px4_metadata(px4):
    assert px4.firmware == "px4"
    assert len(px4.params) == 1784
    nav = px4.get("NAV_DLL_ACT")
    assert nav.type == "int" and nav.default == 0
    assert nav.values[2] == "Return mode"
    assert nav.describe_value(2) == "2 (Return mode)"
    assert px4.get("MPC_Z_VEL_MAX_DN").describe_value(1.5) == "1.5 m/s"
    assert px4.get("COM_RCL_EXCEPT").bitmask == {0: "Mission", 1: "Hold", 2: "Offboard"}


def test_suggests_real_names_for_typos(px4):
    assert px4.suggest("MPC_XY_CRUSIE") == ["MPC_XY_CRUISE"]
    assert px4.suggest("nav_dll_act") == ["NAV_DLL_ACT"]
    assert "GF_ACTION" in px4.suggest("GF_ACTON")
    assert px4.suggest("TOTALLY_UNRELATED") == []


def test_ardupilot_xml():
    m = meta.load(os.path.join(HERE, "data", "copter-mini.pdef.xml"))
    assert m.firmware == "ardupilot"
    assert set(m.params) == {"FS_GCS_ENABLE", "MOT_SPIN_ARM", "RTL_ALT", "ARMING_CHECK"}
    fs = m.get("FS_GCS_ENABLE")
    assert fs.type == "int" and fs.values == {0: "Disabled/NoAction", 1: "RTL", 5: "Land"}
    assert fs.default is None  # ArduPilot's metadata has no defaults
    # Suggested values (fractions) are not an enum: any value in range goes.
    spin = m.get("MOT_SPIN_ARM")
    assert spin.type == "float" and spin.values is None
    assert m.get("RTL_ALT").min == 30 and m.get("RTL_ALT").units == "cm"
    arming = m.get("ARMING_CHECK")
    assert arming.bitmask == {0: "All", 1: "Barometer"} and arming.reboot


def test_ardupilot_json(tmp_path):
    p = tmp_path / "apm.pdef.json"
    p.write_text(
        '{"Copter": {"FS_GCS_ENABLE": {"DisplayName": "GCS failsafe", '
        '"Values": {"0": "Disabled", "1": "RTL"}}}, '
        '"FENCE_": {"FENCE_RADIUS": {"Range": {"low": "30", "high": "10000"}, "Units": "m"}}}'
    )
    m = meta.load(str(p))
    assert m.get("FS_GCS_ENABLE").values == {0: "Disabled", 1: "RTL"}
    assert m.get("FENCE_RADIUS").max == 10000


def test_bad_metadata_is_an_error(tmp_path):
    with pytest.raises(meta.MetaError, match="bundled metadata"):
        meta.load(str(tmp_path / "missing.json"))
    (tmp_path / "x.json").write_text("not json")
    with pytest.raises(meta.MetaError, match="not JSON"):
        meta.load(str(tmp_path / "x.json"))
    (tmp_path / "y.json").write_text('{"parameters": [{"name": "X", "type": "Int64"}]}')
    with pytest.raises(meta.MetaError, match="unknown type"):
        meta.load(str(tmp_path / "y.json"))
