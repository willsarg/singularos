"""Export scenario runs as playback data for the 3D site in web/.

For each scenario this runs the simulator into runs/web-<scenario>/ (always a
fresh run, so the export matches the current code), samples the log every SAMPLE_PERIOD_S of sim time and writes
<out>/<scenario>.json plus <out>/index.json listing the runs.

Samples are columnar (one array per field) to keep the files small. Truth and
observations stay separate: x/y/heading/thrust/health are truth; nav_* is the
latest available navigation observation and est is the controller's thruster
health estimate. Events carry source "truth" (what happened) or "controller"
(what the controller decided), as in the replay.

Usage: python -m telemetry.export_web [--out ../web/public/runs] [scenario ...]
"""

import argparse
import json
from pathlib import Path

from singularos_sim.config import THRUSTER_NAMES, load_config, with_seed
from singularos_sim.orbit import EARTH_RADIUS_M, mean_motion, orbit_status, station_radius_m
from singularos_sim.replay import build_frames, describe_faults
from singularos_sim.runner import DEFAULT_RUNS_DIR, run_scenario

from .api import SCENARIOS_DIR, scenario_names

SAMPLE_PERIOD_S = 0.4
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "web" / "public" / "runs"
FIELDS = ("t", "x", "y", "heading", "fuel", "clear", "phase", "leg", "alt", "perigee",
          "nav_x", "nav_y", "nav_age", "thrust", "health", "est")


def _r(v, nd):
    return None if v is None else round(v, nd)


def scenario_description(name):
    """The leading comment block of the scenario file, as one paragraph."""
    lines = []
    for line in (SCENARIOS_DIR / f"{name}.toml").read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("#").strip())
    return " ".join(lines)


def export_run(name, out_dir, seed=None):
    config = with_seed(load_config(SCENARIOS_DIR / f"{name}.toml"), seed)
    run = DEFAULT_RUNS_DIR / f"web-{name}"
    run_scenario(config, run)
    config = json.loads((run / "config.json").read_text())
    summary = json.loads((run / "summary.json").read_text())
    summary.pop("out_dir", None)
    n = mean_motion(config["orbit"]["period_s"])
    max_thrust = [config["vehicle"]["thrusters"][k]["max_thrust_n"] for k in THRUSTER_NAMES]

    phases, legs, events = [], [], []
    samples = {k: [] for k in FIELDS}
    frames = build_frames(run / "log.jsonl")
    # Events straight from the log: build_frames attaches events to the next
    # controller frame, which drops any after the last one (e.g. reentry).
    latest_nav = None
    with open(run / "log.jsonl") as log:
        for line in log:
            rec = json.loads(line)
            for observation in rec["observations"]:
                if observation["source"] == "navigation" and observation["available"]:
                    latest_nav = observation
            sources = [("truth", rec["truth"].get("events", []))]
            if rec.get("controller"):
                sources.append(("controller", rec["controller"].get("events", [])))
            for source, batch in sources:
                events += [{**e, "t": round(e.get("t", rec["t"]), 2), "source": source} for e in batch]
    # Physics may terminate between controller ticks. Preserve the terminal
    # truth and latest navigation observation, carrying forward only the
    # controller's phase, leg and health estimates from its last tick.
    if rec["t"] > frames[-1]["t"] + 1e-9:
        truth = rec["truth"]
        terminal = {
            **frames[-1],
            "t": round(rec["t"], 3),
            "truth": [truth[k] for k in ("x_m", "y_m", "vx_m_s", "vy_m_s", "heading_rad", "angular_rate_rad_s", "fuel_kg")],
            "clear": truth["station_clearance_m"],
            "thrust": [truth["thrust_n"][k] for k in THRUSTER_NAMES],
            "health": [truth["thruster_health"][k] for k in THRUSTER_NAMES],
            "nav": None if latest_nav is None else
                [latest_nav["value"][k] for k in ("x_m", "y_m", "vx_m_s", "vy_m_s")]
                + [rec["t"] - latest_nav["timestamp"]],
        }
        frames.append(terminal)
    next_t = 0.0
    for i, f in enumerate(frames):
        if f["t"] + 1e-9 < next_t and i != len(frames) - 1:
            continue
        next_t += SAMPLE_PERIOD_S
        x, y, vx, vy, heading, _, fuel = f["truth"]
        orbit = orbit_status(n, x, y, vx, vy)
        if f["phase"] not in phases:
            phases.append(f["phase"])
        if f["leg"] not in legs:
            legs.append(f["leg"])
        nav = f["nav"]
        row = {
            "t": f["t"], "x": _r(x, 2), "y": _r(y, 2), "heading": _r(heading, 3), "fuel": _r(fuel, 3),
            "clear": _r(f["clear"], 2), "phase": phases.index(f["phase"]), "leg": legs.index(f["leg"]),
            "alt": _r(orbit["altitude_km"], 2), "perigee": _r(orbit["perigee_km"], 2),
            "nav_x": None if nav is None else _r(nav[0], 2), "nav_y": None if nav is None else _r(nav[1], 2),
            "nav_age": None if nav is None else _r(nav[4], 2),
            # Delivered thrust as a fraction of the thruster's rated maximum.
            "thrust": [_r(th / m, 3) for th, m in zip(f["thrust"], max_thrust)],
            "health": f["health"], "est": f["est"],
        }
        for k in FIELDS:
            samples[k].append(row[k])

    nav_sigma = config["sensors"]["navigation"]["position_std_m"]
    data = {
        "scenario": name,
        "description": scenario_description(name),
        "faults": describe_faults(config["faults"]),
        "summary": summary,
        "sample_period_s": SAMPLE_PERIOD_S,
        "thrusters": list(THRUSTER_NAMES),
        "station": config["station"]["components"],
        "vehicle_radius_m": config["vehicle"]["radius_m"],
        "nav_sigma_m": nav_sigma,
        "orbit": {
            "period_s": config["orbit"]["period_s"],
            "station_radius_m": station_radius_m(n),
            "earth_radius_m": EARTH_RADIUS_M,
        },
        "phases": phases,
        "legs": legs,
        "events": events,
        "samples": samples,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(data, separators=(",", ":")))
    return {"name": name, "expected_outcome": summary["expected_outcome"], "outcome": summary["outcome"],
            "faults": data["faults"], "sim_time_s": summary["sim_time_s"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("scenarios", nargs="*", help="default: every scenario")
    args = parser.parse_args(argv)
    names = args.scenarios or scenario_names()
    unknown = sorted(set(names) - set(scenario_names()))
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    index = [export_run(name, args.out) for name in names]
    (args.out / "index.json").write_text(json.dumps(index, indent=1))
    for entry in index:
        print(f"{entry['name']}: {entry['outcome']} (expected {entry['expected_outcome']})")


if __name__ == "__main__":
    main()
