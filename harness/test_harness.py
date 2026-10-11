"""Tests for the constrained-output harness.

Unit tests run without a live llama-server (semantic checks, schema loading).
Integration tests run against a live server when SINGULAROS_LLM_URL is set.
"""

import json
import os
import unittest

from harness import (
    HarnessConfig,
    Proposal,
    ProposalValidationError,
    _semantic_check,
    load_schema,
)


def base_proposal(**overrides):
    p = {
        "schema_version": 1,
        "mode": "INVESTIGATE",
        "rationale": "Station beacon dropout suggests sensor fault.",
        "requested_checks": ["OBSERVATION_FRESHNESS"],
        "plan": {"action": "MAINTAIN", "parameters": {}},
    }
    p.update(overrides)
    return p


class SchemaTests(unittest.TestCase):
    def test_schema_loads(self):
        schema = load_schema()
        self.assertEqual(schema["properties"]["mode"]["enum"][0], "NORMAL")
        self.assertIn("ABORT", schema["properties"]["mode"]["enum"])

    def test_schema_forbids_extra_fields(self):
        schema = load_schema()
        self.assertIs(schema["additionalProperties"], False)


class SemanticCheckTests(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema()

    def test_valid_proposal_passes(self):
        self.assertEqual(_semantic_check(base_proposal(), self.schema), [])

    def test_bad_mode_flagged(self):
        problems = _semantic_check(base_proposal(mode="PANIC"), self.schema)
        self.assertTrue(any("PANIC" in p for p in problems))

    def test_docked_requires_alignment_check(self):
        problems = _semantic_check(
            base_proposal(mode="DOCKED", requested_checks=[]), self.schema
        )
        self.assertTrue(any("DOCK_ALIGNMENT" in p for p in problems))

    def test_docked_with_checks_passes(self):
        p = base_proposal(
            mode="DOCKED",
            requested_checks=["DOCK_ALIGNMENT", "OBSERVATION_FRESHNESS"],
            plan={"action": "DOCK_SEQUENCE", "parameters": {"target_id": "station-a"}},
        )
        self.assertEqual(_semantic_check(p, self.schema), [])

    def test_duplicate_checks_flagged(self):
        p = base_proposal(requested_checks=["FUEL_BUDGET", "FUEL_BUDGET"])
        problems = _semantic_check(p, self.schema)
        self.assertTrue(any("unique" in x for x in problems))

    def test_thin_rationale_flagged(self):
        problems = _semantic_check(base_proposal(rationale="RECOVERY"), self.schema)
        self.assertTrue(any("rationale" in x for x in problems))

    def test_abort_requires_retreat_like_action(self):
        problems = _semantic_check(base_proposal(mode="ABORT"), self.schema)
        self.assertTrue(any("retreat" in p for p in problems))
        p = base_proposal(mode="ABORT", plan={"action": "SAFE_RETREAT", "parameters": {}})
        self.assertEqual(_semantic_check(p, self.schema), [])


class ProposalDataclassTests(unittest.TestCase):
    def test_properties(self):
        p = Proposal(raw=base_proposal())
        self.assertEqual(p.mode, "INVESTIGATE")
        self.assertEqual(p.requested_checks, ["OBSERVATION_FRESHNESS"])
        self.assertEqual(p.plan["action"], "MAINTAIN")


@unittest.skipUnless(
    os.environ.get("SINGULAROS_LLM_URL") and os.environ.get("SINGULAROS_LLM_INTEGRATION"),
    "set SINGULAROS_LLM_URL and SINGULAROS_LLM_INTEGRATION=1 to run integration "
    "tests against a live llama-server (semantic-layer tests are model-quality "
    "dependent and expect a capable model, not a small smoke-test model)",
)
class IntegrationTests(unittest.TestCase):
    def test_health(self):
        from harness import RecoveryHarness

        h = RecoveryHarness(HarnessConfig(base_url=os.environ["SINGULAROS_LLM_URL"]))
        self.assertTrue(h.health())

    def test_constrained_proposal(self):
        from harness import RecoveryHarness

        h = RecoveryHarness(HarnessConfig(base_url=os.environ["SINGULAROS_LLM_URL"]))
        proposal = h.propose(
            "Current mode: NORMAL. The station beacon has been unavailable for 40 "
            "seconds while ranging radar still returns a value. Fuel at 62%. "
            "Propose a recovery action."
        )
        self.assertIn(proposal.mode, ("NORMAL", "INVESTIGATE", "RETURN", "HOLD"))


if __name__ == "__main__":
    unittest.main()
