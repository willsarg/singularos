"""Constrained-output harness for the LLM recovery agent.

From PR #3 (harness/harness.py, feat/llm-harness), adapted to the
intelligence.llm client so it runs against Cloudflare Workers AI (default),
Ollama, or an OpenAI-compatible server such as llama.cpp.

Every request carries the recovery-proposal JSON schema as structured output.
After parsing, three layers run before a proposal is returned:
  1. Schema re-check in Python (schema_problems): required fields, no extra
     fields, enums, const, string lengths, numeric bounds, array size and
     uniqueness. Covers servers that enforce only part of the schema.
  2. Semantic checks (semantic_problems): rules the schema cannot express.
  3. Bounded retry: failures are fed back to the model; after
     max_retries + 1 attempts, ProposalValidationError.

Per MISSION.md section 7, the LLM proposes and interprets only. A returned
proposal is not validated against the vehicle: requested_checks are run by
intelligence.review against the numerical planner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMClient, LLMError

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
VALID_MODES = ("NORMAL", "INVESTIGATE", "RETURN", "APPROACH", "DOCKED", "HOLD", "ABORT")

SYSTEM_PROMPT = (
    "You are the LLM recovery agent for SingularOS, an autonomous inspection "
    "spacecraft. You propose and interpret only. You never drive actuators and "
    "never assert numeric feasibility; the numerical planner and validator "
    "check every plan. Respond with a single JSON object matching the required "
    "schema. Request the checks your proposal depends on."
)


class HarnessError(Exception):
    """Base error for harness failures."""


class ProposalValidationError(HarnessError):
    """The model output failed validation after all retries."""


@dataclass
class Proposal:
    raw: dict = field(repr=True)

    @property
    def mode(self):
        return self.raw["mode"]

    @property
    def rationale(self):
        return self.raw["rationale"]

    @property
    def requested_checks(self):
        return list(self.raw.get("requested_checks", []))

    @property
    def plan(self):
        return self.raw["plan"]


def load_schema(name="recovery-proposal"):
    with open(SCHEMA_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def schema_problems(value, schema, path="proposal"):
    """Check value against the subset of JSON Schema the proposal schema uses."""
    problems = []
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path} must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} {value!r} not one of {schema['enum']}")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return problems + [f"{path} must be an object"]
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            problems += [f"{path}.{k} is not allowed" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                problems += schema_problems(value[key], sub, f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list):
            return problems + [f"{path} must be an array"]
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            problems.append(f"{path} has {len(value)} items")
        if schema.get("uniqueItems") and len(value) != len({json.dumps(v, sort_keys=True) for v in value}):
            problems.append(f"{path} items must be unique")
        for i, item in enumerate(value):
            problems += schema_problems(item, schema.get("items", {}), f"{path}[{i}]")
    elif kind == "string":
        if not isinstance(value, str):
            return problems + [f"{path} must be a string"]
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            problems.append(f"{path} length {len(value)} out of bounds")
    elif kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (kind == "integer" and not isinstance(value, int)):
            return problems + [f"{path} must be {kind}"]
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            problems.append(f"{path} {value} out of bounds [{schema.get('minimum')}, {schema.get('maximum')}]")
    return problems


def semantic_problems(candidate):
    """Rules the schema cannot express."""
    problems = []
    mode = candidate.get("mode")
    if mode not in VALID_MODES:
        problems.append(f"mode {mode!r} not in fixed state-machine vocabulary")
    checks = candidate.get("requested_checks", [])
    action = candidate.get("plan", {}).get("action")
    if mode == "DOCKED":
        for required in ("DOCK_ALIGNMENT", "OBSERVATION_FRESHNESS"):
            if required not in checks:
                problems.append(f"DOCKED proposal must request {required}")
    if mode == "ABORT" and action not in ("SAFE_RETREAT", "POWER_DOWN", "COAST"):
        problems.append("ABORT proposal must plan a retreat, power-down, or coast")
    if action in ("BRAKE", "RAISE_PERIGEE") and "FUEL_BUDGET" not in checks:
        problems.append(f"{action} proposal must request FUEL_BUDGET")
    if action == "RAISE_PERIGEE" and "ORBIT_SAFETY" not in checks:
        problems.append("RAISE_PERIGEE proposal must request ORBIT_SAFETY")
    if action == "RAISE_PERIGEE" and mode != "HOLD":
        problems.append("RAISE_PERIGEE ends in a safe-orbit HOLD; mode must be HOLD")
    if len(checks) != len(set(checks)):
        problems.append("requested_checks must be unique")
    if len(candidate.get("rationale", "")) < 20:
        problems.append("rationale must cite evidence in at least 20 characters")
    return problems


class RecoveryHarness:
    def __init__(self, client: LLMClient | None = None, schema: dict | None = None, max_retries: int = 2):
        self.client = client or LLMClient()
        self.schema = schema or load_schema()
        self.max_retries = max_retries

    def propose(self, context: str) -> Proposal:
        """Ask for a recovery proposal given a mission context summary."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}]
        last_problems = []
        for _ in range(self.max_retries + 1):
            prompt = list(messages)
            if last_problems:
                prompt.append({"role": "user", "content": "Your previous proposal failed validation: "
                               + "; ".join(last_problems) + ". Propose again, fixing these."})
            try:
                content = self.client.chat(prompt, self.schema)
            except LLMError as e:
                raise HarnessError(str(e)) from e
            try:
                candidate = json.loads(content)
            except json.JSONDecodeError:
                last_problems = ["reply was not valid JSON"]
                continue
            last_problems = schema_problems(candidate, self.schema)
            if not last_problems:
                last_problems = semantic_problems(candidate)
            if not last_problems:
                return Proposal(raw=candidate)
        raise ProposalValidationError(
            f"proposal failed validation after {self.max_retries + 1} attempts: {'; '.join(last_problems)}"
        )

    def health(self):
        return self.client.health()
