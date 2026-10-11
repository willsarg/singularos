"""Constrained-output harness for the SingularOS LLM recovery agent.

Talks to a llama.cpp ``llama-server`` (OpenAI-compatible API) and forces every
response to validate against a JSON Schema via grammar-constrained decoding
(``response_format: json_schema``). The model cannot emit an invalid mode,
unknown field, or malformed JSON — constraints are enforced at the token level,
not requested in the prompt.

Per MISSION.md §7, the LLM proposes and interprets only. This harness never
grants actuator authority and never treats a proposal as validated: requested
checks are handed to the numerical planner/validator module.

Stdlib only (urllib), so it runs inside the Omarchy guest or on the host with
any Python 3.9+.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

VALID_MODES = ("NORMAL", "INVESTIGATE", "RETURN", "APPROACH", "DOCKED", "HOLD", "ABORT")


class HarnessError(Exception):
    """Base error for harness failures."""


class ProposalValidationError(HarnessError):
    """Raised when the model output fails semantic validation after retries."""


@dataclass
class HarnessConfig:
    base_url: str = "http://127.0.0.1:8080"
    timeout_s: float = 300.0
    max_retries: int = 2
    temperature: float = 0.2


@dataclass
class Proposal:
    """A validated (syntactically) recovery proposal from the LLM."""

    raw: dict = field(repr=True)

    @property
    def mode(self) -> str:
        return self.raw["mode"]

    @property
    def rationale(self) -> str:
        return self.raw["rationale"]

    @property
    def requested_checks(self) -> list:
        return list(self.raw.get("requested_checks", []))

    @property
    def plan(self) -> dict:
        return self.raw["plan"]


def load_schema(name: str = "recovery-proposal") -> dict:
    path = SCHEMA_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _post(base_url: str, path: str, payload: dict, timeout_s: float) -> dict:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _semantic_check(candidate: dict, schema: dict) -> list:
    """Post-parse checks the JSON Schema cannot express.

    The grammar guarantees structure; these catch semantic violations that would
    still be schema-valid (e.g. requesting a check the plan doesn't need is fine,
    but a DOCKED proposal without DOCK_ALIGNMENT requested is not).
    """
    problems = []
    mode = candidate.get("mode")
    if mode not in VALID_MODES:
        problems.append(f"mode {mode!r} not in fixed state-machine vocabulary")
    if mode == "DOCKED":
        checks = set(candidate.get("requested_checks", []))
        for required in ("DOCK_ALIGNMENT", "OBSERVATION_FRESHNESS"):
            if required not in checks:
                problems.append(f"DOCKED proposal must request {required}")
    if mode == "ABORT" and candidate.get("plan", {}).get("action") not in (
        "SAFE_RETREAT",
        "POWER_DOWN",
        "COAST",
    ):
        problems.append("ABORT proposal must plan a retreat, power-down, or coast")
    checks = candidate.get("requested_checks", [])
    if len(checks) != len(set(checks)):
        problems.append("requested_checks must be unique")
    if len(candidate.get("rationale", "")) < 20:
        problems.append("rationale must cite evidence in at least 20 characters")
    return problems


class RecoveryHarness:
    """Constrained client for the LLM recovery agent."""

    def __init__(self, config: HarnessConfig | None = None, schema: dict | None = None):
        self.config = config or HarnessConfig()
        self.schema = schema or load_schema()

    def propose(self, context: str) -> Proposal:
        """Ask the LLM for a recovery proposal given mission context.

        ``context`` is a compact text summary of observations, estimates, and
        current mode. The response is grammar-constrained to the recovery
        proposal schema; semantic checks run after parsing. Retries on
        semantic failure with the problems appended to the prompt.

        Note on ``uniqueItems``: llama.cpp's schema-to-grammar compiler does
        not enforce array uniqueness at the token level, so duplicates are
        caught by the semantic layer and retried.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the LLM recovery agent for SingularOS. You propose "
                    "and interpret only. You never drive actuators and never "
                    "assert numeric feasibility; the numerical planner/validator "
                    "checks every plan. Respond with a single JSON object "
                    "matching the required schema."
                ),
            },
            {"role": "user", "content": context},
        ]

        last_problems: list = []
        for attempt in range(self.config.max_retries + 1):
            prompt = list(messages)
            if last_problems:
                prompt.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous proposal failed validation: "
                            + "; ".join(last_problems)
                            + ". Propose again, fixing these."
                        ),
                    }
                )

            resp = _post(
                self.config.base_url,
                "/v1/chat/completions",
                {
                    "messages": prompt,
                    "temperature": self.config.temperature,
                    "max_tokens": 1024,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "recovery_proposal",
                            "strict": True,
                            "schema": self.schema,
                        },
                    },
                },
                self.config.timeout_s,
            )

            try:
                content = resp["choices"][0]["message"]["content"]
                candidate = json.loads(content)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                raise HarnessError(f"malformed server response: {e}") from e

            problems = _semantic_check(candidate, self.schema)
            if not problems:
                return Proposal(raw=candidate)
            last_problems = problems

        raise ProposalValidationError(
            f"proposal failed semantic validation after "
            f"{self.config.max_retries + 1} attempts: {'; '.join(last_problems)}"
        )

    def health(self) -> bool:
        try:
            req = urllib.request.Request(
                self.config.base_url.rstrip("/") + "/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="SingularOS recovery harness CLI")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "context",
        help="Mission context summary (observations, estimates, current mode)",
    )
    args = parser.parse_args()

    harness = RecoveryHarness(HarnessConfig(base_url=args.base_url))
    if not harness.health():
        raise SystemExit("llama-server not reachable at " + args.base_url)
    proposal = harness.propose(args.context)
    print(json.dumps(proposal.raw, indent=2))


if __name__ == "__main__":
    main()
