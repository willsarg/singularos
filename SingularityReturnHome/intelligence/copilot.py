"""AI copilot: proposes a multi-step recovery plan for the controller.

Unlike the advisor (advisor.py, logged only), an accepted copilot plan is
executed. The flow per decision point:

  controller -> CopilotAgent.propose(context, feedback) -> plan (JSON)
             -> singularos_sim.plan_validator.validate_plan -> accepted?
             -> yes: controller executes the steps (authority: copilot)
             -> no:  problems fed back; bounded attempts; then the
                     deterministic baseline keeps authority

The prompt contains the vehicle manual, controller-side evidence (diagnosed
thruster health, observed state, fuel) and the text of active station
advisories. It does not contain the baseline's own decision, so the copilot
reasons independently. It never contains truth.

Structured output (the copilot-plan schema) guarantees the reply's shape,
not its correctness; correctness is the validator's job.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .harness import schema_problems
from .llm import LLMClient, LLMError

HERE = Path(__file__).resolve().parent
MANUAL_PATH = HERE / "manual" / "vehicle-manual.md"
SCHEMA_PATH = HERE / "schemas" / "copilot-plan.json"

SYSTEM_PROMPT = (
    "You are the AI copilot of SingularOS, an autonomous inspection spacecraft. "
    "After a fault you propose a recovery plan built only from the procedure steps in the manual. "
    "You never fly the craft and never claim a plan is feasible: a numerical validator checks every plan "
    "and the controller executes only accepted plans. Follow station advisories. "
    "Reply with one JSON object matching the schema.\n\n"
)


def load_manual():
    return MANUAL_PATH.read_text()


def load_schema():
    return json.loads(SCHEMA_PATH.read_text())


def context_text(ctx):
    """Evidence summary for the prompt. Deliberately excludes the baseline decision."""
    o = ctx["observed"]
    health = ", ".join(f"{k} {'FAILED' if v == 0 else f'{round(100 * v)}%'}" for k, v in ctx["health_estimate"].items())
    lines = [
        f"Time t={ctx['t']:.1f} s. A fault was detected and diagnosed; the craft has braked and is waiting for a plan.",
        f"Position (station frame): x={o['x_m']:.2f} m, y={o['y_m']:.2f} m; velocity vx={o['vx_m_s']:.3f}, vy={o['vy_m_s']:.3f} m/s; "
        f"distance to dock {ctx['distance_to_dock_m']:.1f} m.",
        f"Diagnosed thruster health: {health}. Usable thrusters: {', '.join(ctx['usable']) or 'none'}.",
        f"Fuel: {o['fuel_kg']:.3f} kg (reserve {ctx['fuel_reserve_kg']} kg).",
    ]
    if ctx["advisories"]:
        lines.append("Active station advisories:")
        lines += [f"  [{a['id']}] {a['text']}" for a in ctx["advisories"]]
    else:
        lines.append("Active station advisories: none.")
    lines.append("Propose the recovery plan.")
    return "\n".join(lines)


class CopilotAgent:
    """LLM-backed copilot. Call propose(); raises CopilotError on failure."""

    name = "llm"

    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()
        self.schema = load_schema()
        self.manual = load_manual()

    def propose(self, ctx, history=()):
        """history: list of (plan, problems) from earlier rejected attempts."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "MANUAL\n" + self.manual},
            {"role": "user", "content": context_text(ctx)},
        ]
        for plan, problems in history:
            messages.append({"role": "assistant", "content": json.dumps(plan)})
            messages.append({"role": "user", "content": "The validator REJECTED that plan: " + "; ".join(problems)
                             + ". Fix these problems and propose again."})
        start = time.monotonic()
        try:
            content = self.client.chat(messages, self.schema)
        except LLMError as e:
            raise CopilotError(str(e)) from e
        latency = time.monotonic() - start
        try:
            plan = json.loads(content)
        except json.JSONDecodeError as e:
            raise CopilotError(f"reply was not JSON: {e}") from e
        problems = schema_problems(plan, self.schema)
        if problems:
            raise CopilotError("reply does not match the plan schema: " + "; ".join(problems))
        return plan, latency


class CopilotError(Exception):
    """The copilot could not produce a usable plan (server, JSON or schema)."""


class ScriptedCopilot:
    """Deterministic stand-in for tests and offline demos: returns the given
    plans in order (the last one repeats)."""

    name = "scripted"

    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []

    def propose(self, ctx, history=()):
        self.calls.append({"context": context_text(ctx), "history": list(history)})
        plan = self.plans[min(len(history), len(self.plans) - 1)]
        return plan, 0.0
