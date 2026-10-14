# paramguard

[![CI](https://github.com/ahmed-hashim-pro/paramguard/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmed-hashim-pro/paramguard/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A CI check for PX4 and ArduPilot parameter files. It reads a parameter change
the way the vehicle will, and blocks the pull request when the change turns off
a failsafe, misspells a name, leaves the firmware's documented range, or steps
a control gain too far. Every block says why. Accepting one deliberately takes
a committed allowance that names who accepted it and why.

A parameter file decides what a drone does when something goes wrong: when
the radio link drops, the battery runs low or the fence is crossed. Teams keep
these files in git and review them as text diffs. But a diff shows lines, and
the vehicle runs *values*. Delete a line and the vehicle doesn't lose the
setting; it falls back to the firmware default, which for several multicopter
failsafes in PX4 is "do nothing". Misspell a name and PX4 doesn't complain to
the ground station; the line just sets nothing. Neither looks like a problem
in review.

```
$ paramguard check vehicle.params cleanup.params
cleanup.params  (against vehicle.params)
1 effective change:
  NAV_DLL_ACT  2 (Return mode) -> 0 (Disabled)  (line removed: firmware default)

BLOCK  gcs-loss-failsafe-off  cleanup.params
       NAV_DLL_ACT 2 (Return mode) -> 0 (Disabled): a forbidden value. The line was
       removed, so the firmware default applies
       why: With the ground-station link-loss failsafe disabled, a vehicle that loses
       its link carries on with whatever it was doing. In Hold that means hovering where
       it is, out of contact, until the battery runs down. The firmware default is 0
       (Disabled), so deleting this line does the same. See
       https://docs.px4.io/v1.15/en/config/safety.html#data-link-loss-failsafe

blocked: 1 problem, 0 warnings. To accept one deliberately, add an allowance:
  [[allow]]
  rule = "gcs-loss-failsafe-off"
  param = "NAV_DLL_ACT"
  value = "removed"
  by = "<who accepts it>"
  reason = "<why it is safe for this vehicle>"
```

That pull request deleted 17 lines "already at their defaults". Sixteen were.
`NAV_DLL_ACT` looks like one: PX4's simulated planes set it to 2 at start-up.
The firmware default is 0.

## What that change does in flight

![Two stacked charts against seconds since the radio link was lost. Top: distance from home. Both runs hover 300 m out when the link drops. With the original file the vehicle switches to Return mode 17 s later, flies home and, in the bottom height chart, descends and lands. After the cleanup it stays 300 m out and 30 m up for the whole 210 s window, holding, out of contact](docs/sitl.gif)

The same flight in PX4 v1.15.4 SITL (Gazebo), once with each file. A script
plays the ground station: it applies the parameter file, takes off, clicks
"fly here" on a point 300 m north, waits there for 5 s, then stops sending
heartbeats, the way a link drops when the vehicle goes out of range. It keeps
listening, so the telemetry after the loss is still recorded.

| | `vehicle.params` | after the cleanup |
| --- | --- | --- |
| `NAV_DLL_ACT` on the vehicle (read back) | 2 (Return mode) | 0 (Disabled) |
| Failsafe after the link loss | Return mode after 17 s | none |
| Landed | at home (0.1 m off), 162 s after the loss | no |
| 210 s after the loss | on the ground, disarmed | **still 299 m out, 30 m up, holding** |

*Every number comes from the logged telemetry in
[`docs/sitl-results.json`](docs/sitl-results.json). `make sitl` flies both runs
again and redraws the chart. CI flies them on every push and fails unless the
original file brings the vehicle home, the cleanup leaves it out there, and
paramguard blocks the cleanup. Return mode starts about 17 s after the last
heartbeat: PX4 notices the missing heartbeats, waits `COM_DL_LOSS_T` (10 s),
then holds for `COM_FAIL_ACT_T` (5 s) before acting.*

A second example pull request, "survey tuning", shows the other kinds of
problem:

![Terminal recording: paramguard blocks the cleanup pull request on NAV_DLL_ACT; then the tuning pull request is blocked four times (geofence action set to warning only, a 67% roll-rate gain step, the misspelt MPC_XY_CRUSIE with a suggestion of MPC_XY_CRUISE, and a descent speed of 5 m/s above its 4 m/s maximum) with one warning about descending faster than 3 m/s](docs/demo.gif)

## Quickstart

Python 3.11 or newer. No dependencies.

```bash
git clone https://github.com/ahmed-hashim-pro/paramguard && cd paramguard
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

cd examples/survey-quad
paramguard check vehicle.params cleanup.params   # the block above
paramguard check vehicle.params tuning.params    # four blocks and a warning
paramguard lint cleanup.params                   # one file on its own, defaults included
paramguard explain NAV_DLL_ACT GF_ACTON          # metadata and rules; a typo gets a suggestion
cd ../..
pytest -q                                        # 66 tests, about 10 s
```

`check` exits 1 when something blocks, 0 when nothing does, and 2 when an input
can't be read: a malformed parameter, rule or allowance file, or bad metadata.
A file that is half read is worse than one that is refused, so every format
error stops the run and names its line.

## In CI

Compare the files in a pull request with the base branch:

```bash
paramguard check --base origin/main vehicles/*.params
```

A file that is new on the branch is compared with the firmware defaults.
`--format github` writes annotations onto the pull request's diff, and
`--format json` writes a report for other tools.

As a GitHub Action:

```yaml
on: pull_request
jobs:
  params:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: ahmed-hashim-pro/paramguard@main
        with:
          files: vehicles/*.params
          # meta: path/to/parameters.json   # your board's PX4 build
          # rules: ci/our-rules.toml        # added to the built-in rules
```

## What it checks

**Effective values, not lines.** Each parameter is compared as the vehicle
will run it: the file's value, or the firmware default when the file doesn't
set it. Removing a line is a change whenever the removed value wasn't the
default. Writing out a default changes nothing. Line order doesn't matter, and
float spellings such as `0.15` and `0.15000000596046448` are the same value,
because that is how QGroundControl writes float32.

**Against the firmware's own metadata.** Every PX4 build generates a
`parameters.json` that lists every parameter's type, range, enum values,
bitmask bits and default. The PX4 v1.15.4 SITL build's copy (1,784 parameters)
ships with paramguard, and `--meta` points at any other build's. Every line
that changes is checked:

| Check | Why it blocks |
| --- | --- |
| `unknown-param` | The vehicle has no parameter by this name, so the line sets nothing. PX4 sends the ground station no reply; the only trace is an error on the autopilot's own console. paramguard suggests the name you meant. |
| `out-of-range` | PX4 stores values outside the documented range without complaint. In SITL it accepted `MPC_Z_VEL_MAX_DN = 5` against a maximum of 4, and `MC_ROLLRATE_P = -1`. |
| `not-in-values` | PX4 stored `NAV_DLL_ACT = 4`, which has no defined behaviour. |
| `not-integer` | An integer parameter can't hold a fraction. |
| `unknown-bits` | A bitmask sets bits the firmware doesn't define. |

A problem that was already in the base version is reported as a note, not a
block, so adopting paramguard doesn't fail on a vehicle's history.

**Against rules.** 28 built-in PX4 rules (`paramguard rules` lists them) and 17
for ArduCopter. Each rule has a reason written for the reviewer:

| | Examples |
| --- | --- |
| Failsafes | link-loss failsafe disabled (`NAV_DLL_ACT 0`), battery failsafe warning-only, battery thresholds out of order, stick input disabled, a new RC-loss exception mode |
| Geofence | action None or Warning, distance limits set to 0, Terminate (a warning) |
| Circuit breakers | power-supply check bypassed, safety switch bypassed, flight logging off |
| Airframe | `SYS_AUTOSTART` changed, motor output mapping changed, a rotor's spin direction flipped (`CA_ROTORn_KM` sign) |
| Tuning | a rate-loop gain moved by more than 50% in one change; descent faster than 3 m/s; tilt over 45°; return altitude under 20 m |

`paramguard explain GF_ACTION` shows the metadata and every rule for a
parameter.

## Writing rules

Rules are TOML, versioned, and add to the built-in set:

```toml
version = 1
firmware = "px4"

[[rule]]
id = "survey-speed"
params = ["MPC_XY_CRUISE", "MPC_XY_VEL_MAX"]
max = 8
severity = "block"            # or "warn"
reason = "The survey camera blurs above 8 m/s; site permit caps speed at 10 m/s."
```

| Condition | Fires when the new value… |
| --- | --- |
| `forbid = [..]` / `allow_only = [..]` | is / isn't one of these |
| `min`, `max` | is outside the band |
| `no_decrease`, `no_increase` | went down / up |
| `max_step = 0.5` | moved by more than this fraction of the old value |
| `no_sign_flip` | changed sign |
| `any_change` | changed at all |
| `no_new_bits` | gained bitmask bits |
| `below = "PARAM"` | isn't below another parameter (re-checked when either side changes) |

`params` takes names or globs (`MC_*RATE_[PIDK]`). Rules are checked against
the metadata before anything runs: a misspelt parameter in a rule, a glob that
matches nothing, an unknown key, or a rule without a reason is an error. A rule
that silently never fires is the failure this tool exists to prevent.

## Allowances

Some blocked changes are right. A tethered vehicle doesn't need a link-loss
failsafe, and a retune after a prop change does move gains a long way. An
allowance in `paramguard.allow.toml` accepts one finding:

```toml
[[allow]]
rule = "rate-gain-step"
param = "MC_ROLLRATE_P"
value = 0.25                  # this exact value, or "removed"
by = "A. Pilot"
reason = "Retuned for 13-inch props; step response checked in flight log 2026-10-02."
```

It covers that exact value. If a later change moves the gain to 0.3, the block
comes back, and the stale allowance is reported so it can be deleted. Who
accepted what, and why, stays in git next to the parameters.

## ArduPilot

```bash
curl -sSfLO https://autotest.ardupilot.org/Parameters/versioned/Copter/stable-4.6.3/apm.pdef.xml
paramguard check --meta apm.pdef.xml old.param new.param
```

ArduPilot's metadata ships in XML for each release, and in JSON for the
development branch. Both are read. With ArduPilot metadata the built-in
ArduCopter rules apply; CI checks them against the 4.6.3 metadata. Two
differences from PX4:

- **No defaults.** ArduPilot's metadata doesn't include defaults, so a deleted
  line can't be judged. It is reported as a warning naming the rules that
  couldn't judge it.
- **Suggested values.** Some value lists are suggestions, such as
  `MOT_SPIN_ARM`'s 0.0/0.1/0.2. Lists with fractions, or on parameters that
  also have a range, aren't treated as the complete set.

ArduPilot's metadata is GPL-licensed, so it isn't bundled.

## Formats

All of these are read, and may be mixed in one file:

```
1	1	GF_ACTION	3	6        QGroundControl export (the type column is checked)
param set GF_ACTION 3            PX4 airframe or startup script
param set-default GF_ACTION 3
GF_ACTION 3                      plain
GF_ACTION,3                      ArduPilot .param, Mission Planner
```

A name set twice in one file is an error, because which value wins depends on
the loader.

## Tests

`pytest -q` runs 66 tests, and a 67th with `make ap-meta`:

- **Parser, metadata and rules.** Every line format, error with its line number,
  rule-file mistake and rule condition. The built-in rules are checked against
  the bundled metadata.
- **Both example pull requests**, end to end, plus allowances, cross-parameter
  rules, notes for problems already in the base version, and ArduPilot without
  defaults.
- **Property tests (Hypothesis)** over random files drawn from the real PX4
  metadata:
  - a file against itself never changes or blocks anything
  - line order never matters
  - writing out a default never changes the result
  - the link-loss rule blocks exactly when the value becomes 0
  - the range check agrees with the metadata on every float parameter with a range
  - an allowance covers its own value and no other
- **Typo suggestions.** 300 random swapped-letter typos of real PX4 names: the
  first suggestion is right for more than 97% of them, and the right name is
  always among the three offered. The misses are genuinely ambiguous:
  `R1C1_MAX` is one edit from `RC1_MAX` as well as `RC11_MAX`.
- **The CLI.** Exit codes, JSON, GitHub annotations, `--base` against a real git
  repository, allowance files, and quiet output when piped into `head`.

`make check` adds ruff and mypy. `make ap-meta` downloads ArduCopter 4.6.3's
metadata and checks the built-in ArduCopter rules against it, as CI does. CI
runs the tests on Python 3.11 to 3.13, runs the GitHub Action on this
repository's own example, and flies the SITL comparison.

## What this does not do

- **Prove a parameter set is safe.** It catches the kinds of change it has rules
  for, and a file that passes can still fly badly. Tuning needs flight testing;
  the gain-step rule only stops big jumps between tests.
- **Know your vehicle.** The built-in rules are general multicopter sense, not
  your site's limits. The 20 m return altitude, the 3 m/s descent and the 50%
  gain step are starting points; put your own numbers in your own rule file.
- **Know your build's defaults unless you give it the metadata.** The bundled
  metadata is PX4 v1.15.4's SITL build. A flight-controller build adds driver
  parameters, and airframe scripts change defaults with `param set-default`
  (PX4's simulated plane airframes set `NAV_DLL_ACT 2`). Point `--meta` at your
  build's `parameters.json` for exact defaults. QGroundControl downloads the
  same file from the vehicle.
- **Read the vehicle.** It checks files in a repository. If someone changes a
  parameter from a ground station and never commits it, paramguard never sees
  it. Exporting the vehicle's parameters and checking them against the
  committed file is a way to catch that.
- **Test on hardware.** The flight above is PX4 SITL with Gazebo. The parameter
  behaviour it shows is the same firmware code, but no real aircraft flew.

## Layout

```
src/paramguard/
  params.py      reading parameter files (QGC, PX4 shell, plain, ArduPilot)
  meta.py        PX4 parameters.json and ArduPilot apm.pdef.{xml,json}
  rules.py       rule and allowance files
  check.py       effective values, the diff, metadata checks, rules
  report.py      text, JSON and GitHub annotation output
  cli.py         check, lint, explain, rules
  data/          PX4 v1.15.4 SITL metadata, built-in PX4 and ArduCopter rules
examples/survey-quad/
  vehicle.params, cleanup.params, tuning.params
sitl/            the SITL flight: fly.py (the ground station), run.sh, analyze.py
action.yml       the GitHub Action
docs/            sitl.gif, sitl.svg, sitl-results.json, demo.gif (make sitl / make demo)
```

## License

MIT. The bundled PX4 parameter metadata
(`src/paramguard/data/px4-1.15.4-sitl.json.xz`) is generated by the PX4 build
and is under PX4's BSD 3-Clause license, included as
`src/paramguard/data/PX4-LICENSE`.
