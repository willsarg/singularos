"""Deterministic validator for copilot (LLM) recovery plans.

The copilot proposes a decision and a short list of procedure steps; this
module decides whether the controller may execute it. It uses only
controller-side information: diagnosed thruster health, observed state and
fuel, the nominal vehicle spec, the published station geometry, and the
structured form of any station advisories. It never sees truth.

Checks (each reported by name, with the problems found):
  structure       decision matches the final step; no steps after a terminal
                  step; required parameters present
  prerequisites   COAST_IN directly after PREDOCK_STOP; a degraded craft
                  stops at the pre-dock point before FINAL_APPROACH
  capability      FINAL_APPROACH needs forward and backward; PREDOCK_STOP,
                  COAST_IN, GOTO, HOLD_AT need an intact opposing pair
  speed_limits    COAST_IN between coast_min and the docking speed limit
                  times coast_margin; GOTO/FINAL_APPROACH speeds in range
  advisories      dock-closed forbids docking; approach-speed limits apply
                  to FINAL_APPROACH and COAST_IN speeds; keep-out zones
  clearance       straight segments (GOTO, HOLD_AT, PREDOCK_STOP) stay
                  min_clearance_m from station components, except inside
                  the approach corridor (DOCK_CORRIDOR_M of the dock port),
                  where closeness to the station is the point
  fuel            estimated fuel for the plan, times the planner's margin,
                  fits observed fuel minus the reserve
"""

import math

from . import planner

TERMINAL = ("COAST_IN", "FINAL_APPROACH", "HOLD_AT", "SAFE_STOP")
DECISION_STEPS = {"DOCK": ("COAST_IN", "FINAL_APPROACH"), "HOLD": ("HOLD_AT",), "ABORT": ("SAFE_STOP",)}
MOVES = ("RETURN_TO_STAGING", "GOTO", "PREDOCK_STOP", "COAST_IN", "FINAL_APPROACH", "HOLD_AT")
DOCK_CORRIDOR_M = 3.0


def _segment_box_distance(a, b, lo, hi, step=0.25, corridor=0.0):
    """Smallest distance from the segment a-b to an axis-aligned box (sampled),
    ignoring samples within `corridor` metres of the dock port (origin)."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(length / step))
    best = math.inf
    for i in range(n + 1):
        f = i / n
        x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        if math.hypot(x, y) < corridor:
            continue
        dx = max(lo[0] - x, 0.0, x - hi[0])
        dy = max(lo[1] - y, 0.0, y - hi[1])
        best = min(best, math.hypot(dx, dy))
    return best


def plan_path(plan, ctx):
    """Waypoints the plan flies through, starting from the current position.
    Returns a list of (point, label, check_clearance)."""
    here = tuple(ctx["position_m"])
    predock = tuple(ctx["predock_m"])
    path = [(here, "current position", False)]
    for step in plan["steps"]:
        a = step["action"]
        if a == "RETURN_TO_STAGING":
            # Pre-planned loop points, validated by construction.
            path += [(tuple(p), "loop point", False) for p in ctx["return_points_m"]]
            path.append((tuple(ctx["staging_m"]), "staging", False))
        elif a in ("GOTO", "HOLD_AT") and "x_m" in step and "y_m" in step:
            path.append(((step["x_m"], step["y_m"]), a, True))
        elif a == "PREDOCK_STOP":
            path.append((predock, "pre-dock", True))
        elif a in ("COAST_IN", "FINAL_APPROACH"):
            path.append(((0.0, 0.0), "dock", False))
    return path


def validate_plan(plan, ctx):
    """Return {"accepted", "problems", "checks": {name: [problems]}, "fuel_needed_kg", "path_m"}."""
    checks = {k: [] for k in ("structure", "prerequisites", "capability", "speed_limits", "advisories",
                              "clearance", "fuel")}
    steps = plan.get("steps", [])
    decision = plan.get("decision")
    usable = set(ctx["usable"])
    has_pair = any(a in usable and b in usable for a, b in planner.PAIRS)
    axial = {"forward", "backward"} <= usable
    r = ctx["recovery"]
    dock_limit = ctx["docking"]["max_relative_speed_m_s"]
    adv = ctx.get("constraints", [])
    speed_cap = min([c["value_m_s"] for c in adv if c["type"] == "max_approach_speed"], default=None)

    # structure
    actions = [s["action"] for s in steps]
    if not steps:
        checks["structure"].append("plan has no steps")
    else:
        if actions[-1] not in DECISION_STEPS.get(decision, ()):
            checks["structure"].append(
                f"decision {decision} must end with {' or '.join(DECISION_STEPS.get(decision, ('?',)))}, not {actions[-1]}")
        for i, a in enumerate(actions[:-1]):
            if a in TERMINAL:
                checks["structure"].append(f"step {i + 1} {a} is terminal but more steps follow")
        for i, s in enumerate(steps):
            if s["action"] in ("GOTO", "HOLD_AT") and not ("x_m" in s and "y_m" in s):
                checks["structure"].append(f"step {i + 1} {s['action']} needs x_m and y_m")
        if decision == "ABORT" and actions != ["SAFE_STOP"]:
            checks["structure"].append("ABORT plan must be a single SAFE_STOP")

    # prerequisites
    for i, a in enumerate(actions):
        if a == "COAST_IN" and (i == 0 or actions[i - 1] != "PREDOCK_STOP"):
            checks["prerequisites"].append(f"step {i + 1} COAST_IN must directly follow PREDOCK_STOP")
        if a == "FINAL_APPROACH" and ctx["degraded"] and (i == 0 or actions[i - 1] != "PREDOCK_STOP"):
            checks["prerequisites"].append(
                f"step {i + 1} FINAL_APPROACH with a thruster lost must directly follow PREDOCK_STOP")
        if a in ("PREDOCK_STOP", "COAST_IN", "FINAL_APPROACH") and i == 0 and ctx["distance_to_dock_m"] > 12.0:
            checks["prerequisites"].append(
                f"step {i + 1} {a} from {ctx['distance_to_dock_m']:.0f} m out: return to staging first")

    # capability
    for i, a in enumerate(actions):
        if a == "FINAL_APPROACH" and not axial:
            lost = [n for n in ("forward", "backward") if n not in usable]
            checks["capability"].append(f"step {i + 1} FINAL_APPROACH needs forward and backward; unusable: {', '.join(lost)}")
        if a in ("RETURN_TO_STAGING", "GOTO", "PREDOCK_STOP", "COAST_IN", "HOLD_AT") and not has_pair:
            checks["capability"].append(f"step {i + 1} {a} needs an intact opposing thruster pair")
        if a == "SAFE_STOP" and has_pair:
            checks["capability"].append("SAFE_STOP chosen although an opposing pair remains: HOLD is possible")

    # speed limits
    for i, s in enumerate(steps):
        a, v = s["action"], s.get("speed_m_s")
        if a == "COAST_IN":
            hi = dock_limit * r["coast_margin"]
            if v is None:
                checks["speed_limits"].append(f"step {i + 1} COAST_IN needs speed_m_s")
            elif not r["coast_min_speed_m_s"] <= v <= hi:
                checks["speed_limits"].append(
                    f"step {i + 1} COAST_IN speed {v:.3f} m/s outside {r['coast_min_speed_m_s']:.3f}-{hi:.3f} m/s")
        if a == "FINAL_APPROACH" and v is not None and not 0.005 <= v <= r["approach_max_speed_m_s"]:
            checks["speed_limits"].append(
                f"step {i + 1} FINAL_APPROACH speed {v:.3f} m/s outside 0.005-{r['approach_max_speed_m_s']} m/s")
        if a == "GOTO" and v is not None and not 0.01 <= v <= r["transit_max_speed_m_s"]:
            checks["speed_limits"].append(
                f"step {i + 1} GOTO speed {v:.3f} m/s outside 0.01-{r['transit_max_speed_m_s']} m/s")

    # advisories
    if any(c["type"] == "dock_closed" for c in adv) and decision == "DOCK":
        ids = [c["advisory"] for c in adv if c["type"] == "dock_closed"]
        checks["advisories"].append(f"advisory {', '.join(ids)} closes the dock: docking not allowed")
    if speed_cap is not None:
        within = max(c["within_m"] for c in adv if c["type"] == "max_approach_speed")
        for i, s in enumerate(steps):
            if s["action"] == "PREDOCK_STOP":
                v = s.get("speed_m_s", r["approach_max_speed_m_s"])
                if v > speed_cap + 1e-9:
                    checks["advisories"].append(
                        f"step {i + 1} PREDOCK_STOP flies into the {within:g} m speed-limit zone at {v:.3f} m/s; "
                        f"set speed_m_s at most {speed_cap:.3f}")
            if s["action"] in ("COAST_IN", "FINAL_APPROACH"):
                v = s.get("speed_m_s", r["approach_max_speed_m_s"] if s["action"] == "FINAL_APPROACH" else None)
                if v is not None and v > speed_cap + 1e-9:
                    checks["advisories"].append(
                        f"step {i + 1} {s['action']} speed {v:.3f} m/s exceeds advisory limit {speed_cap:.3f} m/s")

    # clearance and keep-out zones
    path = plan_path(plan, ctx)
    obstacles = [c for c in ctx["station"] if c["type"] != "docking_port"]
    keep_out = [c for c in adv if c["type"] == "keep_out"]
    for (a, _, _), (b, label, check) in zip(path, path[1:]):
        if check:
            for c in obstacles:
                d = _segment_box_distance(a, b, c["min_m"], c["max_m"], corridor=DOCK_CORRIDOR_M)
                if d < r["copilot_min_clearance_m"]:
                    checks["clearance"].append(
                        f"path to {label} ({b[0]:.1f}, {b[1]:.1f}) passes {d:.1f} m from {c['name']} "
                        f"(min {r['copilot_min_clearance_m']} m); use RETURN_TO_STAGING to fly around the station "
                        f"on the pre-planned safe route instead of straight GOTO lines")
        for c in keep_out:
            if _segment_box_distance(a, b, c["min_m"], c["max_m"]) <= 0.0:
                checks["advisories"].append(f"path to {label} enters keep-out zone {c.get('label', c['advisory'])}")

    # fuel
    fuel_needed = None
    moves = sum(1 for s in steps if s["action"] in MOVES)
    if usable and moves:
        legs = moves + max(0, len(ctx["return_points_m"]) if "RETURN_TO_STAGING" in actions else 0)
        fuel_needed = planner.estimate_return_fuel(
            ctx["spec"], ctx["health"], usable, ctx["mass_kg"], legs, r["transit_max_speed_m_s"],
            ctx["speed_m_s"], r["approach_max_speed_m_s"])
        budget = ctx["fuel_kg"] - r["fuel_reserve_kg"]
        if fuel_needed * r["fuel_margin"] > budget:
            checks["fuel"].append(
                f"plan needs {fuel_needed:.3f} kg x{r['fuel_margin']} margin, budget {budget:.3f} kg after reserve")

    problems = [p for v in checks.values() for p in v]
    return {
        "accepted": not problems,
        "problems": problems,
        "checks": {k: ("pass" if not v else v) for k, v in checks.items()},
        "fuel_needed_kg": fuel_needed,
        "path_m": [list(p) for p, _, _ in path],
    }
