"""AI copilot: plan validation, execution under authority, fallback.

Uses ScriptedCopilot (no model server). The plans are the examples in
intelligence/examples/, i.e. what a correct copilot would propose.
"""

import json
from pathlib import Path

import pytest

from intelligence.copilot import ScriptedCopilot, context_text
from singularos_sim.config import controller_spec, load_config
from singularos_sim.plan_validator import validate_plan
from singularos_sim.runner import run_scenario

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "scenarios"
EXAMPLES = ROOT / "intelligence" / "examples"


def plans(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def run(scenario, copilot, tmp_path):
    return run_scenario(load_config(SCENARIOS / f"{scenario}.toml"), tmp_path, copilot=copilot)


def events(summary, kind):
    return [e for e in summary["copilot"] if e["type"] == kind]


@pytest.fixture(scope="module")
def vctx():
    config = load_config(SCENARIOS / "copilot_coast_in.toml")
    r = config["recovery"]
    return {
        "position_m": (8.0, 0.0), "speed_m_s": 0.0, "distance_to_dock_m": 8.0,
        "predock_m": (r["predock_distance_m"], 0.0), "staging_m": (8.0, 0.0), "return_points_m": [],
        "usable": {"forward", "left", "right"},
        "health": {"forward": 1.0, "backward": 0.0, "left": 1.0, "right": 1.0},
        "degraded": True, "recovery": r, "docking": config["docking"], "constraints": [],
        "station": config["station"]["components"], "spec": controller_spec(config), "mass_kg": 51.5, "fuel_kg": 1.5,
    }


def step_plan(decision, *steps):
    return {"schema_version": 1, "assessment": "test", "decision": decision, "steps": list(steps),
            "advisories_considered": [], "rationale": "test"}


def test_validator_accepts_coast_in_and_rejects_powered_approach_without_backward(vctx):
    assert validate_plan(plans("coast_in")[0], vctx)["accepted"]
    v = validate_plan(step_plan("DOCK", {"action": "PREDOCK_STOP"}, {"action": "FINAL_APPROACH"}), vctx)
    assert not v["accepted"] and v["checks"]["capability"] != "pass"


def test_validator_structure_prerequisites_speed_and_clearance(vctx):
    v = validate_plan(step_plan("DOCK", {"action": "COAST_IN", "speed_m_s": 0.03}), vctx)
    assert v["checks"]["prerequisites"] != "pass"
    v = validate_plan(step_plan("DOCK", {"action": "PREDOCK_STOP"}, {"action": "COAST_IN", "speed_m_s": 0.09}), vctx)
    assert v["checks"]["speed_limits"] != "pass"
    v = validate_plan(step_plan("HOLD", {"action": "GOTO", "x_m": -8.0, "y_m": 0.0}, {"action": "HOLD_AT", "x_m": -8.0, "y_m": 0.0}), vctx)
    assert v["checks"]["clearance"] != "pass"  # straight through the core module
    v = validate_plan(step_plan("HOLD", {"action": "PREDOCK_STOP"}), vctx)
    assert v["checks"]["structure"] != "pass"


def test_validator_enforces_advisories(vctx):
    closed = {**vctx, "constraints": [{"type": "dock_closed", "advisory": "A1"}]}
    assert validate_plan(plans("coast_in")[0], closed)["checks"]["advisories"] != "pass"
    slow = {**vctx, "constraints": [{"type": "max_approach_speed", "value_m_s": 0.02, "within_m": 3.0, "advisory": "A2"}]}
    v = validate_plan(plans("coast_in")[0], slow)  # coast 0.03 and default pre-dock speed exceed the limit
    assert any("exceeds advisory" in p for p in v["problems"]) and any("PREDOCK_STOP" in p for p in v["problems"])


def test_copilot_coast_in_docks_where_baseline_holds(tmp_path):
    baseline = run("copilot_coast_in", None, tmp_path / "b")
    assert baseline["outcome"] == "HOLD"
    s = run("copilot_coast_in", ScriptedCopilot(plans("coast_in")), tmp_path / "c")
    assert s["outcome"] == "DOCKED" and s["truth_docking_check"]["pass"] and s["collision"] is None
    assert s["authority"] == "copilot" and events(s, "copilot_verdict")[0]["accepted"]


def test_copilot_fixes_rejected_plan_and_respects_speed_advisory(tmp_path):
    baseline = run("copilot_advisory_speed", None, tmp_path / "b")
    assert baseline["advisory_violations"]  # the baseline ignores the advisory
    s = run("copilot_advisory_speed", ScriptedCopilot(plans("advisory_speed")), tmp_path / "c")
    verdicts = events(s, "copilot_verdict")
    assert [v["accepted"] for v in verdicts] == [False, True]
    assert s["outcome"] == "DOCKED" and not s["advisory_violations"] and s["collision"] is None


def test_copilot_holds_when_dock_closed(tmp_path):
    baseline = run("copilot_trap_dock_closed", None, tmp_path / "b")
    assert baseline["outcome"] == "DOCKED" and baseline["advisory_violations"]
    s = run("copilot_trap_dock_closed", ScriptedCopilot(plans("dock_closed")), tmp_path / "c")
    assert s["outcome"] == "HOLD" and not s["advisory_violations"] and s["hold_max_deviation_m"] < 1.0


def test_rejected_plans_fall_back_to_baseline(tmp_path):
    bad = step_plan("DOCK", {"action": "PREDOCK_STOP"}, {"action": "FINAL_APPROACH"})  # needs backward
    s = run("copilot_coast_in", ScriptedCopilot([bad]), tmp_path)
    assert [v["accepted"] for v in events(s, "copilot_verdict")] == [False, False, False]
    assert s["authority"] == "baseline" and s["outcome"] == "HOLD"


def test_copilot_error_falls_back_to_baseline(tmp_path):
    class Broken:
        name = "broken"

        def propose(self, ctx, history=()):
            raise RuntimeError("model server down")

    s = run("copilot_coast_in", Broken(), tmp_path)
    assert events(s, "copilot_error") and s["authority"] == "baseline" and s["outcome"] == "HOLD"


def test_copilot_prompt_has_evidence_but_not_baseline_decision_or_truth(tmp_path):
    agent = ScriptedCopilot(plans("dock_closed"))
    run("copilot_trap_dock_closed", agent, tmp_path)
    text = agent.calls[0]["context"]
    assert "left FAILED" in text and "STA-11" in text
    for leak in ("Deterministic decision", "RETURN;", "thruster_health", "effectiveness", "truth"):
        assert leak not in text


def test_failed_copilot_under_advisories_falls_back_to_hold(tmp_path):
    # Rejected plans while a dock-closed advisory is active: the baseline would
    # dock into the closed port, so the fallback holds instead.
    bad = step_plan("HOLD", {"action": "GOTO", "x_m": 8.0, "y_m": 0.0}, {"action": "HOLD_AT", "x_m": 8.0, "y_m": 0.0})
    s = run("copilot_trap_dock_closed", ScriptedCopilot([bad]), tmp_path)
    assert s["authority"] == "baseline" and s["outcome"] == "HOLD" and not s["advisory_violations"]
