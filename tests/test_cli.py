import json
import os
import shutil
import subprocess
import sys

import pytest

from paramguard.cli import main

ROOT = os.path.join(os.path.dirname(__file__), "..")
EX = os.path.join(ROOT, "examples", "survey-quad")


def run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "paramguard", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )


def test_exit_codes(capsys):
    assert main(["check", f"{EX}/vehicle.params", f"{EX}/vehicle.params"]) == 0
    assert main(["check", f"{EX}/vehicle.params", f"{EX}/cleanup.params"]) == 1
    assert main(["check", f"{EX}/vehicle.params", f"{EX}/missing.params"]) == 2
    assert "missing.params: No such file" in capsys.readouterr().err


def test_bad_input_is_exit_2_with_a_location(tmp_path, capsys):
    bad = tmp_path / "bad.params"
    bad.write_text("GF_ACTION 3\nNAV_DLL_ACT two\n")
    assert main(["lint", str(bad)]) == 2
    assert f"{bad}:2: NAV_DLL_ACT: 'two' is not a number" in capsys.readouterr().err
    rules = tmp_path / "r.toml"
    rules.write_text('version = 1\n[[rule]]\nid = "x"\nparams = ["GF_ACTON"]\nforbid = [0]\nreason = "r"\n')
    assert main(["rules", "--rules", str(rules)]) == 2
    assert "did you mean GF_ACTION" in capsys.readouterr().err


def test_text_output_offers_an_allowance():
    r = run("check", f"{EX}/vehicle.params", f"{EX}/cleanup.params")
    assert r.returncode == 1
    assert "NAV_DLL_ACT  2 (Return mode) -> 0 (Disabled)  (line removed: firmware default)" in r.stdout
    assert 'value = "removed"' in r.stdout


def test_json_output():
    r = run("check", "--format", "json", f"{EX}/vehicle.params", f"{EX}/tuning.params")
    doc = json.loads(r.stdout)
    assert doc[0]["blocked"] is True
    assert {f["check"] for f in doc[0]["findings"] if f["blocking"]} == {
        "geofence-action-none",
        "rate-gain-step",
        "unknown-param",
        "out-of-range",
    }


def test_github_annotations():
    r = run("check", "--format", "github", f"{EX}/vehicle.params", f"{EX}/tuning.params")
    lines = r.stdout.splitlines()
    assert any(x.startswith("::error file=") and "line=25" in x and "MPC_XY_CRUSIE" in x for x in lines)
    assert any(x.startswith("::warning ") and "descent-too-fast" in x for x in lines)
    assert "blocked: 4 problems" in r.stderr


def test_allow_file_is_picked_up_from_the_working_directory(tmp_path):
    (tmp_path / "paramguard.allow.toml").write_text(
        '[[allow]]\nrule = "gcs-loss-failsafe-off"\nparam = "NAV_DLL_ACT"\nvalue = "removed"\n'
        'by = "ops"\nreason = "tethered: the tether is the link"\n'
    )
    r = run("check", f"{EX}/vehicle.params", f"{EX}/cleanup.params", cwd=tmp_path)
    assert r.returncode == 0, r.stdout
    assert "ALLOW" in r.stdout and "allowed by ops: tethered" in r.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_against_a_git_ref(tmp_path):
    def git(*a):
        subprocess.run(
            ["git", *a],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

    git("init", "-q", "-b", "main")
    os.makedirs(tmp_path / "vehicles")
    shutil.copy(f"{EX}/vehicle.params", tmp_path / "vehicles" / "quad.params")
    git("add", ".")
    git("commit", "-q", "-m", "base")
    shutil.copy(f"{EX}/cleanup.params", tmp_path / "vehicles" / "quad.params")
    (tmp_path / "vehicles" / "new.params").write_text("GF_ACTION 0\n")
    r = run("check", "--base", "main", "vehicles/quad.params", "vehicles/new.params", cwd=tmp_path)
    assert r.returncode == 1
    assert "vehicles/quad.params  (against main)" in r.stdout
    assert "vehicles/new.params  (against firmware defaults (new at main))" in r.stdout
    assert r.stdout.count("BLOCK") == 2
    r = run("check", "--base", "nosuchref", "vehicles/quad.params", cwd=tmp_path)
    assert r.returncode == 2 and "no such commit" in r.stderr


def test_explain_and_rules():
    r = run("explain", "NAV_DLL_ACT", "GF_ACTON")
    assert r.returncode == 1
    assert "default 0 (Disabled)" in r.stdout and "did you mean GF_ACTION?" in r.stdout
    r = run("rules")
    assert r.returncode == 0 and "gcs-loss-failsafe-off" in r.stdout


def test_ardupilot_metadata_switches_the_builtin_rules(tmp_path):
    meta = os.path.join(os.path.dirname(__file__), "data", "copter-mini.pdef.xml")
    # The excerpt lacks most names the ArduCopter rules use, which is an error, not a silent pass.
    r = run("rules", "--meta", meta)
    assert r.returncode == 2 and "is not a parameter" in r.stderr
    r = run("lint", "--meta", meta, "--no-builtin", str(tmp_path / "x.params"))
    assert r.returncode == 2


def test_sigpipe_is_quiet():
    p = subprocess.Popen(
        f"{sys.executable} -m paramguard explain NAV_DLL_ACT GF_ACTION COM_LOW_BAT_ACT | head -1",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
    )
    out, err = p.communicate()
    assert out.startswith("NAV_DLL_ACT") and err == ""
