"""CLI: python -m singularos_sim [scenario.toml] [--seed N] [--out DIR] [--llm]
        python -m singularos_sim --all [--seed N]"""

import argparse
import sys
from pathlib import Path

from .config import default_scenario_path, load_config, with_seed
from .replay import build_replay, describe_faults
from .runner import run_scenario


def make_advisor():
    """LLM advisor from SINGULAROS_LLM_* settings, or None if the server or
    model is unavailable (the run then continues deterministic-only)."""
    from intelligence.advisor import RecoveryAdvisor
    from intelligence.harness import RecoveryHarness
    from intelligence.llm import LLMClient

    client = LLMClient()
    status = client.health()
    print(f"llm: {client.config.backend} at {client.config.base_url}, model {client.config.model or '(unset)'}: "
          + ("ready" if status["ok"] else f"unavailable ({status['detail']}); running without advice"))
    return RecoveryAdvisor(RecoveryHarness(client)) if status["ok"] else None


def make_copilot(plan_file=None):
    """Copilot for --copilot: scripted plans from a JSON file (offline demo
    and tests), or the LLM from SINGULAROS_LLM_* settings. None if the LLM is
    unavailable (the run then uses the deterministic baseline)."""
    import json

    from intelligence.copilot import CopilotAgent, ScriptedCopilot
    from intelligence.llm import LLMClient

    if plan_file:
        plans = json.loads(Path(plan_file).read_text())
        print(f"copilot: scripted plans from {plan_file}")
        return ScriptedCopilot(plans if isinstance(plans, list) else [plans])
    client = LLMClient()
    status = client.health()
    print(f"copilot: {client.config.backend} at {client.config.base_url}, model {client.config.model or '(unset)'}: "
          + ("ready" if status["ok"] else f"unavailable ({status['detail']}); baseline only"))
    return CopilotAgent(client) if status["ok"] else None


def passed(s):
    ok = s["outcome"] == s["expected_outcome"] and s["collision"] is None and not s["advisory_violations"]
    return ok and (s["outcome"] != "DOCKED" or s["truth_docking_check"]["pass"])


def run_all(seed, copilot=None):
    """Run every scenario in the scenarios directory with one seed and link the
    replays with a scenario picker. With a copilot, the copilot_* scenarios
    are also run a second time with the copilot attached, so baseline and
    copilot runs sit side by side in the picker. Returns the exit code."""
    scenario_dir = default_scenario_path().parent
    runs = []
    config_of = {}
    for path in sorted(scenario_dir.glob("*.toml")):
        try:
            config = with_seed(load_config(path), seed)
        except (KeyError, ValueError) as exc:
            print(f"skipped {path.name}: not a valid scenario ({exc!r})")
            continue
        name = config["scenario"]["name"]
        config_of[name] = config["scenario"]
        runs.append(run_scenario(config))
        if copilot is not None and name.startswith("copilot_"):
            print(f"running {name} with the AI copilot...", flush=True)
            out = Path(runs[-1]["out_dir"]).parent / f"{name}-copilot-seed{config['scenario']['seed']}"
            runs.append(run_scenario(config, out, copilot=copilot))
    # Nominal first, then by name, baseline before copilot.
    runs.sort(key=lambda s: (s["scenario"] != "nominal", s["scenario"], s["mode"] == "copilot"))
    entries = []
    for s in runs:
        ok = passed(s)
        s["ok"] = ok
        s["expected_fail"] = not ok and s["mode"] == "baseline" and config_of[s["scenario"]].get("baseline_expected_to_fail", False)
        tag = " [AI copilot]" if s["mode"] == "copilot" else (" [baseline]" if s["scenario"].startswith("copilot_") else "")
        entries.append({
            "name": s["scenario"],
            "href": f"../{Path(s['out_dir']).name}/replay.html",
            "label": f"{s['scenario']}{tag} \u2014 {describe_faults(s['faults_injected'])} \u2014 {s['outcome']}"
                     + ("" if ok or s["expected_fail"] else " (FAIL)"),
        })
    print(f"{'scenario':<28} {'mode':<9} {'faults':<26} {'outcome':<11} {'expected':<11} result")
    for s in runs:
        build_replay(s["out_dir"], entries)
        print(f"{s['scenario']:<28} {s['mode']:<9} {describe_faults(s['faults_injected']):<26} {s['outcome']:<11} "
              f"{s['expected_outcome']:<11} "
              + ("PASS" if s["ok"] else "FAIL (expected: baseline ignores advisories)" if s["expected_fail"] else "FAIL"))
    if runs:
        print(f"replay (pick any scenario from the header): {Path(runs[0]['out_dir']) / 'replay.html'}")
    return 0 if runs and all(s["ok"] or s["expected_fail"] for s in runs) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="singularos_sim", description="Run a Singularity Return Home simulator scenario.")
    parser.add_argument("scenario", nargs="?", default=str(default_scenario_path()), help="scenario TOML file")
    parser.add_argument("--seed", type=int, default=None, help="override the scenario seed")
    parser.add_argument("--out", default=None, help="output directory (default: runs/<name>-seed<seed>)")
    parser.add_argument("--all", action="store_true",
                        help="run every scenario in scenarios/ and link the replays with a scenario picker")
    parser.add_argument("--llm", action="store_true",
                        help="ask the LLM advisor at decision points (SINGULAROS_LLM_URL, SINGULAROS_LLM_MODEL); logged only")
    parser.add_argument("--copilot", action="store_true",
                        help="let the AI copilot propose the recovery plan; validated plans are executed")
    parser.add_argument("--copilot-plan", default=None, metavar="FILE",
                        help="with --copilot: use scripted plan(s) from a JSON file instead of the LLM")
    args = parser.parse_args(argv)
    copilot = make_copilot(args.copilot_plan) if args.copilot else None

    if args.all:
        return run_all(args.seed, copilot)

    config = with_seed(load_config(args.scenario), args.seed)
    s = run_scenario(config, args.out, make_advisor() if args.llm else None, copilot)
    replay = build_replay(s["out_dir"])
    truth = s["truth_docking_check"]
    print(f"scenario: {s['scenario']}  seed: {s['seed']}")
    print(f"outcome: {s['outcome']} ({s['reason']})  expected: {s['expected_outcome']}")
    for fault in s["faults_injected"]:
        detail = (f"{fault['thruster']} thruster effectiveness -> {fault['effectiveness']}" if fault["type"] == "thruster"
                  else f"delta-v {fault['delta_v_m_s']} m/s, delta-omega {fault.get('delta_omega_rad_s', 0.0)} rad/s")
        print(f"fault injected at t={fault['t_s']:.1f} s: {fault['type']}, {detail}")
    if s["anomaly_detected_t"] is not None:
        latency = f" (latency {s['detection_latency_s']:.1f} s)" if s["detection_latency_s"] is not None else ""
        print(f"anomaly detected at t={s['anomaly_detected_t']:.1f} s{latency}")
        est = ", ".join(f"{k} {v['estimate']:.2f} (truth {v['truth']:.2f})" for k, v in s["health_estimate_vs_truth"].items())
        print(f"diagnosed thruster health: {est}")
    if s["post_outcome_observed_s"]:
        dev = s["hold_max_deviation_m"]
        print(f"after {s['outcome']}: observed {s['post_outcome_observed_s']:.0f} s more; "
              + (f"max distance from hold point {dev:.2f} m" if dev is not None else
                 f"final position ({s['final_position_m'][0]:.1f}, {s['final_position_m'][1]:.1f}) m"))
    triage = s["orbital_triage"]
    if triage:
        print(f"orbital triage: {triage['decision']}; " + "; ".join(triage["reasons"]))
    end = s["orbit_at_end"]
    print(f"vehicle orbit at end: altitude {end['altitude_km']:.1f} km, perigee {end['perigee_km']:.1f} km"
          + (f"  REENTRY at t={s['reentry']['t']:.0f} s" if s["reentry"] else ""))
    for advice in s["llm_advice"]:
        if "error" in advice:
            print(f"llm advice at {advice['point']} t={advice['t']:.1f} s: error: {advice['error']}")
        else:
            rv = advice["review"]
            print(f"llm advice at {advice['point']} t={advice['t']:.1f} s: {advice['proposal']['mode']} "
                  f"/ {advice['proposal']['plan']['action']} ("
                  + ("agrees" if rv["agrees_with_deterministic"] else f"differs from {rv['deterministic_mode']}")
                  + (f", failed checks: {', '.join(rv['failed'])}" if rv["failed"] else "") + ")")
    for adv in config["advisories"]:
        print(f"station advisory [{adv['id']}] at t={adv['t_s']:.0f} s: {adv['text']}")
    for e in s["copilot"]:
        if e["type"] == "copilot_proposal":
            p = e["plan"]
            steps = " -> ".join(st["action"] + (f"({st['speed_m_s']})" if "speed_m_s" in st else "") for st in p["steps"])
            print(f"copilot proposal {e['attempt']} ({e['agent']}, {e['latency_s']:.1f} s): {p['decision']}: {steps}")
        elif e["type"] == "copilot_verdict":
            print(f"  validator: {'ACCEPTED' if e['accepted'] else 'REJECTED: ' + '; '.join(e['problems'])}")
        elif e["type"] == "copilot_error":
            print(f"copilot error (attempt {e['attempt']}): {e['error']}")
    if s["mode"] == "copilot":
        print(f"authority at end: {s['authority']}")
    if s["plan"]:
        print(f"recovery decision ({s['plan'].get('source', 'baseline')}): {s['plan']['decision']}; " + "; ".join(s["plan"]["reasons"]))
    for v in s["advisory_violations"]:
        print(f"ADVISORY VIOLATION: {v['type']} first at t={v['t']:.1f} s" + (f" ({v['steps']} steps)" if v["steps"] > 1 else ""))
    print(f"sim time: {s['sim_time_s']:.2f} s  steps: {s['steps']}")
    print(f"fuel used: {s['fuel_used_kg']:.4f} kg  remaining: {s['fuel_remaining_kg']:.4f} kg")
    print(
        f"truth at end: distance {truth['distance_m']:.4f} m, speed {truth['relative_speed_m_s']:.4f} m/s, "
        f"alignment error {truth['alignment_error_rad']:.4f} rad, within thresholds: {truth['pass']}"
    )
    col = s["collision"]
    clr = s["min_clearance_in_flight"]
    print(f"inspections completed: {len(s['inspections_completed'])} ({', '.join(s['inspections_completed']) or 'none'})")
    print(
        f"station collision: {'YES at t=%.1f s with %s' % (col['t'], col['component']) if col else 'none'}  "
        f"min clearance in flight: {clr['clearance_m']:.2f} m ({clr['component']})"
    )
    print(f"log: {s['out_dir']}")
    print(f"replay: {replay}")
    ok = passed(s)
    checks = [f"outcome {'matches' if s['outcome'] == s['expected_outcome'] else 'differs from'} expected",
              "no collision" if col is None else "collision",
              "advisories respected" if not s["advisory_violations"] else "ADVISORY VIOLATED"]
    if s["outcome"] == "DOCKED":
        checks.append("truth within docking thresholds" if truth["pass"] else "truth OUTSIDE docking thresholds")
    print(f"result: {'PASS' if ok else 'FAIL'} ({', '.join(checks)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
