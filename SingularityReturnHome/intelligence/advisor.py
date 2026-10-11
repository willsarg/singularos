"""LLM advisor for the simulator controller.

The controller calls the advisor at its decision points (orbit triage and
recovery plan) with a context dict of controller-side information. The
advisor asks the harness for a proposal, runs its requested checks
(intelligence.review) and returns a record that the controller logs as an
llm_advice event. The record never changes the controller's command: the
deterministic decision keeps actuator authority (MISSION.md section 7).

Server or validation failures are returned as {"error": ...}; the run
continues on the deterministic path.
"""

import json
import time

from .harness import HarnessError, RecoveryHarness
from .review import review


def context_text(ctx):
    """Compact mission context for the prompt."""
    lines = [
        f"Decision point: {ctx['point']} at t={ctx['t']:.1f} s.",
        f"Deterministic decision: {ctx['decision']} (mode {ctx['mode']}). Reasons: {'; '.join(ctx['reasons'])}.",
        f"Observed state (station frame, +x radial, +y along-track): {json.dumps(ctx['observed'])}.",
        f"Thruster health estimates: {json.dumps(ctx['health_estimate'])}; usable: {', '.join(ctx['usable']) or 'none'}.",
    ]
    if ctx.get("orbit"):
        lines.append(f"Vehicle orbit: {json.dumps(ctx['orbit'])}; safe perigee {ctx['safe_perigee_km']} km.")
    for key, label in (("time_to_reentry_s", "Time to reentry (s)"), ("available_delta_v_m_s", "Delta-v available (m/s)"),
                       ("safe_orbit_delta_v_m_s", "Perigee-raise delta-v (m/s)")):
        if ctx.get(key) is not None:
            lines.append(f"{label}: {ctx[key]:.1f}.")
    lines.append("Propose the recovery mode and plan, and request the checks your proposal depends on.")
    return "\n".join(lines)


def _record(proposal, ctx, start):
    return {"proposal": proposal.raw, "review": review(proposal, ctx), "latency_s": time.monotonic() - start}


class RecoveryAdvisor:
    def __init__(self, harness: RecoveryHarness | None = None):
        self.harness = harness or RecoveryHarness()

    def __call__(self, ctx):
        start = time.monotonic()
        try:
            proposal = self.harness.propose(context_text(ctx))
        except HarnessError as e:
            return {"error": str(e), "latency_s": time.monotonic() - start}
        return _record(proposal, ctx, start)


class ContextRecorder:
    """Advisor stand-in that records each decision context and returns no
    advice, for callers that ask the model afterwards (advise_async). Advice
    never changes the run, so the recorded contexts are the ones a live
    advisor would have seen."""

    def __init__(self):
        self.contexts = []

    def __call__(self, ctx):
        self.contexts.append(ctx)
        return {"deferred": True}


async def advise_async(harness, ctx, chat):
    """The record RecoveryAdvisor returns, with chat an async callable
    (messages, schema) -> reply text; the controller's llm_advice event
    fields (t, point, decision) are added."""
    start = time.monotonic()
    event = {"t": ctx["t"], "type": "llm_advice", "point": ctx["point"], "decision": ctx["decision"]}
    try:
        proposal = await harness.propose_async(context_text(ctx), chat)
    except HarnessError as e:
        return {**event, "error": str(e), "latency_s": time.monotonic() - start}
    return {**event, **_record(proposal, ctx, start)}
