#!/usr/bin/env python3
"""Measure the SITL runs from their telemetry, and draw them.

    analyze.py OUT_DIR [--check] [--svg FILE] [--gif FILE] [--json FILE]

Reads OUT_DIR/{before,after}.csv and .json (written by fly.py). Everything
reported comes from the logged positions and flight modes, not from what
paramguard or PX4 said would happen. --check fails unless the run shows the
behaviour the README claims: with the original file the vehicle comes home and
lands; with the cleanup it is still out there when the window ends; and
paramguard blocks the cleanup.
"""

import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, "/repo/src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

RUNS = [
    ("before", "vehicle.params (NAV_DLL_ACT 2)", "#2a78d6"),
    ("after", "after the cleanup (line deleted)", "#eb6834"),
]
INK, MUTED, GRID, SURFACE = "#24292f", "#57606a", "#d8dee4", "#ffffff"


def load(out, name):
    rows = []
    with open(os.path.join(out, f"{name}.csv")) as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "t": float(r["t"]),
                    "n": float(r["north"]),
                    "e": float(r["east"]),
                    "up": float(r["up"]),
                    "mode": r["mode"],
                    "armed": r["armed"] != "0",
                    "link": r["link"] == "1",
                }
            )
    with open(os.path.join(out, f"{name}.json")) as f:
        meta = json.load(f)
    t0 = meta["link_lost_t"]
    for r in rows:
        r["s"] = r["t"] - t0  # seconds since the link was lost
        r["dist"] = math.hypot(r["n"], r["e"])
    return rows, meta


def measure(rows, meta):
    after = [r for r in rows if r["s"] >= 0]
    at_loss = after[0]
    first_mode = next((r for r in after if r["mode"] != at_loss["mode"]), None)
    landed = next((r for r in after if not r["armed"]), None)
    end = after[-1]
    return {
        "params": meta["params"],
        "on_vehicle": meta["on_vehicle"],
        "distance_at_link_loss_m": round(at_loss["dist"], 1),
        "mode_at_link_loss": at_loss["mode"],
        "failsafe_mode": first_mode["mode"] if first_mode else None,
        "failsafe_after_s": round(first_mode["s"], 1) if first_mode else None,
        "landed_after_s": round(landed["s"], 1) if landed else None,
        "landed_distance_from_home_m": round(landed["dist"], 1) if landed else None,
        "observed_for_s": round(end["s"], 1),
        "distance_at_end_m": round(end["dist"], 1),
        "altitude_at_end_m": round(end["up"], 1),
        "mode_at_end": end["mode"],
        "armed_at_end": end["armed"],
    }


def check_claims(results, repo):
    from paramguard import meta as metamod
    from paramguard import params, rules
    from paramguard.check import check

    b, a = results["before"], results["after"]
    problems = []
    if b["failsafe_mode"] != "RTL":
        problems.append(f"before: expected Return mode after link loss, got {b['failsafe_mode']}")
    if b["landed_distance_from_home_m"] is None or b["landed_distance_from_home_m"] > 10:
        problems.append(f"before: expected to land within 10 m of home, got {b['landed_distance_from_home_m']}")
    if a["failsafe_mode"] is not None:
        problems.append(f"after: expected no failsafe, got {a['failsafe_mode']}")
    if a["distance_at_end_m"] < 250 or not a["armed_at_end"] or a["observed_for_s"] < 200:
        problems.append(f"after: expected to still be out there after 200 s, got {a}")
    m = metamod.load()
    rs = rules.load("builtin:px4")
    ex = os.path.join(repo, "examples", "survey-quad")
    report = check(params.load(f"{ex}/vehicle.params"), params.load(f"{ex}/cleanup.params"), m, rs)
    blocked = sorted({f.check for f in report.findings if f.blocking})
    if blocked != ["gcs-loss-failsafe-off"]:
        problems.append(f"paramguard: expected to block gcs-loss-failsafe-off, got {blocked}")
    return problems


def figure(runs, upto=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "svg.fonttype": "none"})
    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(10, 6.6), dpi=100, sharex=True, gridspec_kw={"height_ratios": [3, 2], "hspace": 0.12}
    )
    fig.patch.set_facecolor(SURFACE)
    xmax = 210
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(GRID)
        ax.tick_params(colors=MUTED, length=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.axvline(0, color=INK, linewidth=1.2, linestyle=(0, (5, 3)))
    top.set_xlim(-30, xmax + 2)
    top.set_ylim(0, 345)
    bottom.set_ylim(-12, 72)
    top.set_ylabel("distance from home (m)", color=MUTED)
    bottom.set_ylabel("height above take-off (m)", color=MUTED, fontsize=10)
    bottom.set_xlabel("seconds since the radio link was lost", color=MUTED)
    top.text(1.5, 338, "link lost", color=INK, fontsize=10, va="top")
    # Draw the cleanup first so the original stays visible where they overlap.
    for (name, label, color), (rows, res) in reversed(list(zip(RUNS, runs, strict=True))):
        pts = [r for r in rows if -30 <= r["s"] <= xmax and (upto is None or r["s"] <= upto)]
        if not pts:
            continue
        xs = [r["s"] for r in pts]
        for ax, key in ((top, "dist"), (bottom, "up")):
            ax.plot(
                xs,
                [r[key] for r in pts],
                color=color,
                linewidth=2.2,
                solid_capstyle="round",
                label=label if ax is top else None,
            )
            ax.plot(
                [xs[-1]], [pts[-1][key]], "o", color=color, markersize=8, markeredgecolor=SURFACE, markeredgewidth=2
            )

        def shown(key, res=res):
            return res[key] is not None and (upto is None or upto >= res[key])

        if name == "before" and shown("failsafe_after_s"):
            fs = next(r for r in pts if r["s"] >= res["failsafe_after_s"])
            top.annotate(
                f"Return mode after {res['failsafe_after_s']:.0f} s",
                (fs["s"], fs["dist"]),
                xytext=(3, fs["dist"] - 105),
                color=INK,
                fontsize=10,
                arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.8},
            )
        if name == "before" and shown("landed_after_s"):
            last = pts[-1]
            bottom.annotate(
                f"landed at home and disarmed, {res['landed_after_s']:.0f} s",
                (last["s"], last["up"]),
                xytext=(last["s"] - 4, 40),
                color=INK,
                fontsize=10,
                ha="right",
                arrowprops={"arrowstyle": "-", "color": MUTED, "linewidth": 0.8},
            )
        if name == "after" and (upto is None or upto >= xmax - 1):
            last = pts[-1]
            top.text(
                xmax - 2,
                last["dist"] - 18,
                f"still {last['dist']:.0f} m out, {last['up']:.0f} m up,\nholding, out of contact",
                color=INK,
                fontsize=10,
                ha="right",
                va="top",
            )
    handles, labels = top.get_legend_handles_labels()
    top.legend(
        handles[::-1],
        labels[::-1],
        loc="center right",
        bbox_to_anchor=(1.0, 0.5),
        frameon=False,
        labelcolor=INK,
        fontsize=10,
    )
    fig.suptitle(
        "The same flight in PX4 SITL with two parameter files",
        x=0.08,
        y=0.975,
        ha="left",
        fontsize=14,
        fontweight="bold",
        color=INK,
    )
    fig.text(
        0.08,
        0.915,
        "The cleanup deleted 17 lines that looked like defaults. One was NAV_DLL_ACT 2; "
        "its firmware default is 0 (Disabled).",
        fontsize=10.5,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.87, bottom=0.09)
    return fig, plt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--svg")
    ap.add_argument("--gif")
    ap.add_argument("--json")
    args = ap.parse_args()
    runs = []
    results = {}
    for name, _, _ in RUNS:
        rows, meta = load(args.out, name)
        res = measure(rows, meta)
        runs.append((rows, res))
        results[name] = res
    print(json.dumps(results, indent=1))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=1)
            f.write("\n")
    if args.svg:
        fig, plt = figure(runs)
        fig.savefig(args.svg, format="svg", metadata={"Date": None})
        plt.close(fig)
        print(f"wrote {args.svg}")
    if args.gif:
        from PIL import Image

        frames = []
        for s in list(range(-30, 211, 3)) + [210] * 15:
            fig, plt = figure(runs, upto=s)
            fig.canvas.draw()
            img = Image.frombuffer("RGBA", fig.canvas.get_width_height(), fig.canvas.buffer_rgba()).convert("RGB")
            frames.append(img.quantize(colors=64, method=Image.Quantize.MEDIANCUT))
            plt.close(fig)
        frames[0].save(args.gif, save_all=True, append_images=frames[1:], duration=90, loop=0, optimize=True)
        print(f"wrote {args.gif}")
    if args.check:
        repo = "/repo" if os.path.isdir("/repo/src") else os.path.join(os.path.dirname(__file__), "..")
        problems = check_claims(results, repo)
        for p in problems:
            print("check failed:", p)
        if problems:
            sys.exit(1)
        print(
            "check: with the original file the vehicle returned and landed at home; after the cleanup it "
            "was still out there; paramguard blocks the cleanup"
        )


if __name__ == "__main__":
    main()
