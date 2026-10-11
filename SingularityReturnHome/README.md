# Singularity Return Home

Autonomous recovery simulator for a small inspection spacecraft working
around a station in low Earth orbit. The craft flies an inspection loop,
detects faults from its own observations, diagnoses what still works, and
returns to dock, holds in a safe state, or aborts. Faults range from a dead
thruster near the station to a debris strike that puts the craft on a
reentry path.

The simulator (`singularos_sim/`) is the 2D simulator from `sim_1/`
(see [MISSION.md](../MISSION.md)), extended to orbit scale. The orbit-scale
recovery uses the two-body modules in this directory: `simulation/orbit.py`,
`guidance/recovery.py`, `safety/validator.py` and `navigation/deadline.py`.

## Layout

| Path | Contents |
|---|---|
| `singularos_sim/` | Simulator: dynamics, sensors, controller, planner, runner, replay |
| `scenarios/` | Scenario files (TOML); each run is defined by a scenario and a seed |
| `simulation/orbit.py` | Two-body orbit status (altitude, eccentricity, perigee) |
| `guidance/recovery.py` | Prograde burn that raises perigee to 200 km |
| `safety/validator.py` | Independent re-verification of a proposed burn |
| `navigation/deadline.py` | Time until the orbit descends through a given altitude |
| `telemetry/api.py` | HTTP API for the dashboard |
| `telemetry/export_web.py` | Exports runs as playback data for the 3D site in `../web/` |
| `worker/entry.py` | Cloudflare Python Worker for the 3D site: runs scenarios and asks Workers AI per request |
| `dashboard/` | React dashboard |
| `intelligence/` | LLM client (Cloudflare Workers AI, Ollama or OpenAI-compatible), recovery-proposal harness, check review, advisor |
| `mission.py`, `control/` | One-shot two-body recovery script |
| `tests/` | pytest suite |

## Running

Python 3.11 or newer. From this directory:

```sh
uv venv .venv && uv pip install -p .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/python -m singularos_sim scenarios/orbital_strike_safe_orbit.toml
```

The CLI prints the outcome and writes `runs/<scenario>-seed<seed>/` with
`config.json`, `log.jsonl` (one line per step, truth and observations
separate), `summary.json` and `replay.html` (self-contained, opens in a
browser). `--seed N` overrides the scenario seed; `--out DIR` sets the
output directory; `--all` runs every scenario, prints a result table and
links the replays through a scenario picker. The replay shows truth and the
controller's belief side by side: system health, thruster output vs.
command, and station-frame (LVLH) navigation.

Dashboard:

```sh
.venv/bin/python -m telemetry.api          # http://127.0.0.1:8000
cd dashboard && npm ci && npm run dev      # http://127.0.0.1:5173, proxies /api
```

API endpoints:

| Endpoint | Returns |
|---|---|
| `GET /api/mission?disturbance=150` | One-shot two-body recovery analysis |
| `GET /api/scenarios` | Scenario names, expected outcomes, faults |
| `GET /api/run?scenario=NAME[&seed=N]` | Runs the scenario (or reuses a finished run): summary, track sampled every 1 s, controller events |
| `GET /api/replay?scenario=NAME[&seed=N]` | The run's `replay.html` |

3D site (`../web/`, singularos.willsarg.com): plays runs with the station,
vehicle, thrusters and Earth to scale. Deployed, the site is a Cloudflare
Python Worker (`worker/entry.py`, bundled with the simulator packages by
`../web/scripts/bundle-worker.mjs`):

| Endpoint | Returns |
|---|---|
| `GET /api/scenarios` | Scenario names, expected outcomes, faults |
| `GET /api/run?scenario=NAME[&seed=N]` | Runs the scenario in the Worker (random seed if omitted); playback data as written by `export_web`, plus `seed` |
| `GET /api/advice?scenario=NAME&seed=N` | Re-runs the same scenario and seed, recording each decision context, then asks Workers AI through the Worker's AI binding; returns the `llm_advice` events |

Model calls go through the AI Gateway `singularos`, which has a spend limit of
USD 10 per rolling 30 days; once reached, advice reports that the budget is
used up and the runs continue without it. Advice never changes the run, so the
advice for a seed belongs to the run
`/api/run` returned for that seed; the site plays the run while advice is
requested. Both routes are rate limited per client address (20 runs and 6
advice requests per minute). The Worker needs the Workers Paid plan: a run
takes more CPU than the free plan's 10 ms per request. Deploy from `../web/`
with `pnpm run deploy`.

Exported runs (below) are used by the tests, `pnpm run dev:static`, and the
recorded-advice demo build. The data is not committed.
After setting up the Python environment above, the site's development, test,
and build commands regenerate it automatically:

```sh
cd ../web && pnpm install && pnpm dev
# Regenerate after simulator changes while the development server is running:
pnpm run export-runs
```

Each run is sampled every 0.4 s of sim time, plus its terminal physics record
when the run ends between samples. Truth (position, heading,
delivered thrust, thruster health) and the controller's view (navigation
fix, thruster health estimate, decisions) are kept separate; events are
tagged `truth` or `controller`. The export is deterministic for a given
scenario and seed.

## LLM advisor

The LLM proposes and interprets; it never drives actuators and never
asserts feasibility (MISSION.md section 7). With `--llm`, the controller asks
it for a proposal at each decision point (orbit triage, recovery plan) and
logs the reply as an `llm_advice` event next to the deterministic decision,
which keeps actuator authority. Without a reachable server and model, the
run continues deterministic-only.

The default backend is Cloudflare Workers AI with `@cf/qwen/qwen3.8-27b`. It
needs an account ID and an API token with Workers AI Read and Workers AI Edit
permissions (dashboard: AI > Workers AI > Use REST API > Create a Workers AI
API Token):

```sh
export CLOUDFLARE_ACCOUNT_ID=<account id> CLOUDFLARE_API_TOKEN=<token>
.venv/bin/python -m singularos_sim scenarios/orbital_strike_safe_orbit.toml --llm
```

Wrangler also reads `CLOUDFLARE_API_TOKEN`. A token with only Workers AI
permissions cannot deploy the site, so set it for the simulator command
rather than in the shell used for `pnpm run deploy`.

Ollama remains available locally with `SINGULAROS_LLM_BACKEND=ollama`
(`ollama pull hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M` once, 5.7 GB).

To include advice in the Three.js demo, run `pnpm run build:demo` from `../web/`,
then `pnpm preview`. This uses `telemetry.export_web --llm`, which requires a
healthy configured server and model before exporting. It stores separate,
ignored demo recordings and includes advisor metadata, proposals, and check
reviews. Playback shows advice only after its decision timestamp; it does not
make live model requests or change the recorded flight.

Settings (environment):

| Variable | Default | |
|---|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | none | `workers-ai`: account that runs the model |
| `CLOUDFLARE_API_TOKEN` | none | `workers-ai`: Workers AI Read and Edit |
| `SINGULAROS_LLM_BACKEND` | `workers-ai` | `ollama`; `openai` for an OpenAI-compatible server, e.g. llama.cpp in `docker/` |
| `SINGULAROS_LLM_URL` | `https://api.cloudflare.com/client/v4` (`ollama`: `http://127.0.0.1:11434`; `openai`: `http://127.0.0.1:8080`) | From inside a QEMU guest (user-mode networking), Ollama on the host is `http://10.0.2.2:11434` |
| `SINGULAROS_LLM_MODEL` | `@cf/qwen/qwen3.8-27b` (`ollama`: `hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M`) | |
| `SINGULAROS_LLM_THINK` | `false` | Model reasoning; `default` leaves it to the server. On the recovery prompts qwen3.8-27b replied in about 7 s with thinking off and 30-95 s with it on (`reasoning_effort` low) |
| `SINGULAROS_LLM_TIMEOUT` | `180` | Seconds per request |

Pipeline (`intelligence/`, harness from PR #3):

1. `llm.py` sends the chat with the proposal schema as structured output
   (Workers AI: `POST /accounts/{account}/ai/run/{model}` with
   `response_format` `json_schema`, which Workers AI compiles into a
   decoding grammar; `uniqueItems` is not supported there and is removed
   from the request. Ollama: `POST /api/chat` with `format`;
   OpenAI-compatible: `response_format` `json_schema`).
2. `harness.py` re-checks the reply against the schema in Python, then
   semantic rules (e.g. `DOCKED` must request `DOCK_ALIGNMENT` and
   `OBSERVATION_FRESHNESS`; `RAISE_PERIGEE` must request `FUEL_BUDGET` and
   `ORBIT_SAFETY` and mode `HOLD`), retrying with feedback up to twice.
3. `review.py` runs the requested checks against the planner's numbers
   (delta-v budget, orbit safety, independent perigee-raise validation,
   thruster health, docking criteria) and records whether the proposal
   agrees with the deterministic decision. `COLLISION_CHECK` reports not
   evaluated: routes are pre-planned.

`schemas/recovery-proposal.json` (version 2): mode from the MISSION.md
section 8 vocabulary; plan actions include `BRAKE` and `RAISE_PERIGEE`;
`max_delta_v_mps` up to 1000 and `max_relative_speed_mps` up to 20 for
orbit-scale recovery.

`mission.py` and `intelligence/assess.py` use the same client for their
incident summaries. Tests run against an in-process fake server (Workers AI,
Ollama and OpenAI-compatible endpoints); set `SINGULAROS_LLM_INTEGRATION=1`
to also test the configured live server.

## Physics

2D, in-plane, in a frame centred on the station's dock: +x radial (away from
Earth), +y along-track. The station is on a circular orbit of period
`[orbit] period_s` (default 5554 s, about 400 km altitude).

The vehicle follows the exact nonlinear equations of relative motion for
two-body gravity (`singularos_sim/orbit.py`):

```
x'' =  2n y' + n²(R + x) − μ(R + x)/r³ + aₓ
y'' = −2n x' + n² y      − μ y/r³       + a_y
```

with `n` the station's mean motion, `R` its orbit radius and `r` the
vehicle's distance from Earth's centre. Within tens of metres of the station
this agrees with the Clohessy-Wiltshire equations to well under a millimetre.
At orbit scale it does not: for a 150 m/s retrograde strike, the linear model
is about 2.5 km off by the time the craft reaches 120 km altitude.

Also modelled: four body-frame thrusters with thrust, fuel rate, response
delay and true health; rate-limited attitude; mass from remaining fuel;
seeded Gaussian sensor noise; station geometry for truth-side collision
checks. Integrator: semi-implicit Euler at `sim_dt_s`.

Not modelled: out-of-plane motion, J2, drag, third bodies, imaging. Reentry
is the vehicle descending through 120 km; the run ends there.

## Controller

The controller sees only sensor observations and the nominal vehicle spec,
never truth, fault configuration or true thruster health.

- **Inspection loop:** depart to staging, fly the route in
  `[[mission.route]]` (inspect stops and via corners), return and dock.
- **Fault monitor:** observed velocity change over a sliding window vs. the
  change predicted from its own commands, its thruster health estimates and
  orbital motion.
- **Orbit triage** (`planner.assess_orbital`): if the vehicle's own orbit has
  a perigee below `[orbital] safe_perigee_km` (default 200 km), the first
  response is chosen from the delta-v budget (rocket equation on observed
  fuel above the reserve):
  - `BRAKE`: null the velocity relative to the station, which restores the
    station's orbit, then investigate as below. Chosen if braking x margin
    fits the budget and finishes before reentry.
  - `SAFE_ORBIT`: prograde burn planned by `guidance/recovery.py` and
    authorised by `safety/validator.py` against the budget; the craft holds
    in that orbit with thrusters off.
  - `ABORT`: neither fits; the time to reentry from
    `navigation/deadline.py` is logged.
- **Investigation:** brake, then fire each thruster alone and compare
  measured with expected velocity change (orbital acceleration removed) to
  estimate its health.
- **Plan** (`planner.assess_recovery`): RETURN and dock if both axial
  thrusters work and the return fuel x margin fits; else HOLD (station-keep
  at a loop point) if an opposing thruster pair and fuel remain; else ABORT.
  From beyond `far_range_m` (200 m), the return first flies straight in to
  `far_approach_radius_m` (100 m) at `far_transit_speed_m_s` (10 m/s), then
  follows the loop; its fuel estimate includes about 2n per metre for
  orbital motion.

## Scenarios

| Scenario | Fault | Expected |
|---|---|---|
| `nominal` | none | DOCKED |
| `thruster_fault` | small impact, left thruster dead | DOCKED |
| `thruster_degraded` | small impact, forward thruster at 40% | DOCKED |
| `thruster_fault_two_lateral` | small impact, both lateral thrusters dead | DOCKED |
| `thruster_fault_infeasible` | small impact, backward thruster dead | HOLD |
| `thruster_fault_two_axial` | small impact, both axial thrusters dead | HOLD |
| `thruster_fault_abort` | small impact, forward, backward and left dead | ABORT |
| `orbital_base` | none, orbit-scale thrusters (40 N) and 20 kg fuel | DOCKED |
| `orbital_strike_return` | 150 m/s retrograde strike | DOCKED via BRAKE |
| `orbital_strike_safe_orbit` | 150 m/s strike, 3.2 kg fuel | HOLD in safe orbit |
| `orbital_strike_abort` | 300 m/s strike, 3.2 kg fuel | ABORT, reentry |

`orbital_base` raises `[recovery] monitor_threshold_m_s` to 0.3 m/s: the
40 N thrusters' 0.1 s response delay alone produces up to ~0.1 m/s of
monitor residual. Scenario files may start with `extends = "base.toml"`;
tables merge key by key.
