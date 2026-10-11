# SingularOS LLM Harness

Constrained-output client for the LLM recovery agent. Wraps the llama.cpp
`llama-server` (OpenAI-compatible API, see `docker/`) and forces every
response to validate against a JSON Schema via grammar-constrained decoding
(`response_format: json_schema`). The model cannot emit an invalid mode,
unknown field, or malformed JSON — constraints are enforced at the token
level, not requested in the prompt.

Per `MISSION.md` §7, the LLM proposes and interprets only. The harness never
grants actuator authority and never treats a proposal as validated:
`requested_checks` are handed to the numerical planner/validator module.

## Layout

```
harness/
├── harness.py                  # stdlib-only client + CLI
├── test_harness.py             # unit + optional integration tests
└── schemas/
    └── recovery-proposal.json  # forced output schema (mission modes, checks)
```

## Usage

With the `llm` service running (`docker compose up llm` from `docker/`):

```sh
python3 harness/harness.py \
  "Current mode: NORMAL. Station beacon unavailable 40s; radar nominal. Fuel 62%."
```

Prints a validated proposal JSON object, e.g.:

```json
{
  "schema_version": 1,
  "mode": "INVESTIGATE",
  "rationale": "Beacon dropout with nominal radar suggests beacon sensor fault.",
  "requested_checks": ["OBSERVATION_FRESHNESS"],
  "plan": { "action": "MAINTAIN", "parameters": {} }
}
```

Options: `--base-url` (default `http://127.0.0.1:8080`).

## Guarantee layers

1. **Grammar (llama.cpp)** — mode ∈ fixed vocabulary, no extra fields, bounded
   numerics, valid JSON. Violations are impossible, not retried. Exception:
   `uniqueItems` is not enforced by llama.cpp's schema-to-grammar compiler and
   falls through to layer 2.
2. **Semantic checks (harness)** — rules JSON Schema can't express: a `DOCKED`
   proposal must request `DOCK_ALIGNMENT` and `OBSERVATION_FRESHNESS`; an
   `ABORT` must plan a retreat/power-down/coast. Failures are fed back to the
   model and retried (bounded), then raise `ProposalValidationError`.
3. **Numerical validation (planner/validator module)** — fuel, trajectory,
   collision, and freshness thresholds are checked outside the harness. The
   harness output is input to that module, never a substitute for it.

## Tests

```sh
cd harness
python3 -m unittest test_harness -v          # unit tests, no server needed
SINGULAROS_LLM_URL=http://127.0.0.1:8080 SINGULAROS_LLM_INTEGRATION=1 \
  python3 -m unittest test_harness -v        # + integration against live server
```

Integration tests exercise the semantic retry loop end-to-end and are model
quality dependent: a small smoke-test model may exhaust retries on
free-text fields (rationale, unique checks) even though the grammar layer
works. Run them against the model you actually deploy.

## Model

Model selection is runtime configuration (`LLM_MODEL` in `docker/.env`); the
harness is model-agnostic and works with any GGUF the server can load.
