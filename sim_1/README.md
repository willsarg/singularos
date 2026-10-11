# SingularOS 2D simulator (sim_1)

> **Frozen.** Active development continues in
> [`SingularityReturnHome/`](../SingularityReturnHome/), which contains this
> simulator (`singularos_sim/`) extended to orbit scale, plus the LLM advisor.
> `sim_1/` is kept as a reference and is not updated.

Headless Python simulator for the SingularOS mission (see [MISSION.md](../MISSION.md)).
It implements build-order steps 1 and 2:

1. Nominal: the inspection craft departs the dock, flies a loop around the
   station inspecting its exterior (docking port, solar arrays, radiator,
   hull), returns and docks.
2. Thruster fault: partway through the loop a particle impact knocks the craft
   off course and disables or weakens one thruster. The controller detects it
   from observations alone, diagnoses which thruster is affected, and either
   returns and docks with the surviving thrusters or falls back to HOLD when
   docking is infeasible.

![Replay of the thruster_fault_abort scenario: after a particle impact disables three thrusters, the controller diagnoses them, brakes with the remaining thruster and stops safely; the side panel shows relative navigation and system health](docs/replay-health-panel.png)

*Replay of `scenarios/thruster_fault_abort.toml` (seed 1) just after the ABORT
decision: the scenario picker, the coordinate tag on the vehicle, relative
navigation (truth vs. navigation estimate), the thruster schematics and the
system-health table comparing truth with the controller's belief.*

## Physics scope

The model is 2D proximity dynamics in a station-centred frame with the dock at
the origin, in SI units. The station is on a circular low Earth orbit
(`[orbit] period_s`, default 5554 s, similar to the ISS) and the frame moves
with it:

- **+x is radial, away from Earth; +y is along-track, the direction of
  orbital motion.** The dock approach from +x is an approach from above
  (R-bar).
- Relative motion follows the linearised **Clohessy-Wiltshire (Hill)
  equations**, in-plane only:
  `x'' = 3n²x + 2n·y' + aₓ`, `y'' = -2n·x' + a_y`, with `n = 2π / period`.
  An unpowered craft does not stay put: it drifts and curves relative to the
  station, so holding position costs fuel.
- Not modelled: out-of-plane motion, orbit eccentricity, drag, J2 and other
  perturbations, the frame's own rotation in the attitude model (one turn per
  orbit, about 0.065°/s), and gravity-gradient torque. The linearisation is
  accurate for distances of metres to kilometres from the station.
- Applied forces are the vehicle's own body-frame thrusters and, in fault
  scenarios, a configured particle impact.

- Truth state: position, velocity, heading, angular rate, fuel.
- Four body-frame thrusters (forward, backward, left, right) that rotate with the
  vehicle. Each has max thrust, fuel rate at full throttle, response delay (pure
  transport delay) and effectiveness (true hardware health; scales delivered
  force, not fuel use).
- Attitude is commanded as a target angular rate, limited by a maximum rate and
  a maximum angular acceleration. Changing angular rate consumes fuel in
  proportion to the size of the change.
- Mass is dry mass plus remaining fuel. Thrust and rotation are scaled down when
  fuel runs out; fuel never goes negative.
- Integrator: semi-implicit Euler at a fixed timestep.

## Station and inspection loop

The station is static geometry in `[station]` of the scenario file: a docking
port at the origin, a core module, two solar arrays on trusses and a radiator,
each an axis-aligned rectangle. The route in `[[mission.route]]` is flown in
order after backing out to the staging point:

- `inspect` stops: come to rest at `point_m`, turn the nose to face
  `look_at_m`, hold for `hold_s`. `target` names the component inspected.
- `via` corners: pass within `via_tolerance_m` without stopping, keeping the
  route clear of the station.

Inspection is a pose and hold only. No imaging or damage detection is
simulated. The route is pre-planned in config to keep at least 5 m from all
components; the controller does no obstacle avoidance.

Every step the runner computes truth clearance between the vehicle (a disc of
`vehicle.radius_m`) and the station components, excluding the docking port.
Negative clearance is recorded as a collision. This is evaluation only and is
never given to the controller.

## Faults (scenario `[[faults]]`)

Faults are configured in the scenario file and are fully reproducible; the
seed only affects sensor noise. They act on truth only, through physics:

- `type = "impact"`: a world-frame `delta_v_m_s` and `delta_omega_rad_s`
  delivered as a constant external acceleration over `duration_s`. Position
  and heading stay continuous.
- `type = "thruster"`: from `t_s`, the named thruster's `effectiveness`
  becomes the given value (0 = dead). It still burns fuel at the commanded
  rate.

The controller never receives the fault list or true thruster health. Fault
onsets are logged as truth events.

## Recovery (baseline controller, `[recovery]` parameters)

Modes follow MISSION.md section 8. DEPART and INSPECT are NORMAL operation.
The station's orbit is known to the controller: its guidance cancels the
predicted orbital acceleration and commands only the difference, and the fault
monitor includes orbital motion in its prediction.

1. **Fault monitor.** Every controller tick, the velocity change observed by
   navigation over `monitor_window_s` is compared with the change predicted
   from the controller's own commands and its thruster health estimates. A
   residual above `monitor_threshold_m_s` for `monitor_persistence` ticks is an
   anomaly.
2. **INVESTIGATE.** Brake toward zero velocity. While braking, a thruster that
   was commanded but clearly produced nothing is marked failed immediately, so
   braking can turn to use the others. Then each remaining thruster is fired
   alone for `diag_pulse_s`; measured vs. expected velocity change gives its
   effectiveness estimate (failed, degraded, or nominal).
3. **Plan** (`planner.py`). RETURN if docking is feasible, else HOLD if
   feasible, else ABORT. The return follows the pre-planned loop (forward or
   backward, whichever is shorter) so it stays clear of the station.
4. **Degraded flight.** With a thruster unusable, the craft turns so an intact
   opposing thruster pair lies along the acceleration it needs, re-aiming with
   hysteresis and not thrusting until aligned. A thruster whose opposite is
   unusable is never fired. Degraded docking stops precisely at a pre-dock
   point on the dock axis, turns to the dock heading without thrusting, then
   makes a short final approach, backing out to retry if it drifts off the
   axis.
5. **HOLD** station-keeps at the nearest loop point and is declared once
   stable for `hold_confirm_s`; it keeps station-keeping afterwards, since
   orbital motion would otherwise carry the craft away. **ABORT** makes safe
   before shutting down: it turns the best remaining thruster against the
   current velocity and brakes to near rest (at most `abort_brake_timeout_s`),
   then turns thrusters off. If no opposing pair is left during INVESTIGATE,
   braking uses the same single-thruster aiming. After HOLD or ABORT the run continues for
   `scenario.post_outcome_observe_s` (default 300 s) so the log shows whether
   the hold is maintained, or where the unpowered craft drifts.

### Baseline capabilities (for comparison with later agents)

- Detects anomalies from navigation velocity vs. commanded thrust only.
- Diagnoses thrusters by brake-time evidence and single-thruster pulses.
- Docks only if both axial thrusters (forward and backward) are usable and the
  estimated return fuel, times `fuel_margin`, fits within observed fuel minus
  `fuel_reserve_kg`. It has no procedure for docking without an axial
  thruster; that case falls back to HOLD.
- HOLD requires an intact opposing thruster pair (forward/backward or
  left/right) and fuel above the reserve; the pair is turned toward each push
  it needs.
- No obstacle avoidance; routes come from config.

## Scenarios

| File | Fault | Expected outcome |
|---|---|---|
| `nominal.toml` | none | DOCKED |
| `thruster_fault.toml` | impact at t=160 s, left thruster dead | DOCKED |
| `thruster_degraded.toml` | impact at t=160 s, forward thruster at 40% | DOCKED |
| `thruster_fault_infeasible.toml` | impact at t=160 s, backward thruster dead | HOLD |
| `thruster_fault_two_lateral.toml` | impact at t=160 s, left and right thrusters dead | DOCKED |
| `thruster_fault_two_axial.toml` | impact at t=160 s, forward and backward thrusters dead | HOLD |
| `thruster_fault_abort.toml` | impact at t=160 s, forward, backward and left thrusters dead | ABORT |

Fault scenarios start with `extends = "nominal.toml"` and override only what
differs: tables merge key by key, arrays replace. `config.json` in each run
directory is the fully resolved config.

## Observations

Every observation has exactly `source`, `timestamp`, `available`, `value` and
`uncertainty` (1-sigma, same units as the value). Sources:

| Source | Value | Default period |
|---|---|---|
| `attitude_sensor` | heading, angular rate | 0.1 s |
| `navigation` | station-relative x, y, vx, vy (full fix) | 0.2 s |
| `fuel_monitor` | remaining fuel | 1.0 s |

Noise is Gaussian. Each sensor has its own random stream seeded from the
scenario seed and its source id.

## Controller

A deterministic baseline controller acts only on observations and the nominal
vehicle spec (no truth, no thruster health, no fault list). Its phases are
DEPART, INSPECT, INVESTIGATE, RETURN, APPROACH, DOCKED, HOLD and ABORT, with
the current route stop logged as `leg`. Docking is declared only when distance, relative
speed, alignment and estimation uncertainty are all within the `[docking]`
thresholds, using observations no older than `max_observation_age_s`, and the
criteria have held continuously for `confirm_duration_s`.

## Setup

Requires Python 3.11 or newer (uses `tomllib`). The simulator itself has no
third-party dependencies; pytest is pinned for tests.

```sh
cd sim_1
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```sh
cd sim_1
.venv/bin/python -m singularos_sim                         # scenarios/nominal.toml
.venv/bin/python -m singularos_sim scenarios/nominal.toml --seed 42
.venv/bin/python -m singularos_sim scenarios/nominal.toml --out runs/mytest
```

```sh
.venv/bin/python -m singularos_sim scenarios/thruster_fault.toml
.venv/bin/python -m singularos_sim --all              # every scenario, one seed
.venv/bin/python -m singularos_sim --all --seed 7
```

`--all` runs every valid scenario in `scenarios/` with the same seed, prints a
one-line result per scenario, and links the replays: each replay's header has
a scenario picker to switch between them.

The command prints the outcome (`DOCKED`, `HOLD`, `ABORT` or `NOT_DOCKED`) and
the scenario's expected outcome, any injected faults, when the anomaly was
detected, the diagnosed thruster health next to the truth (evaluation only),
the recovery decision and its reasons, inspections completed, any station
collision and the minimum clearance in flight. It exits 0 only if the outcome
matches `expected_outcome`, there was no collision, and, if docked, the final
truth state is within the docking thresholds. Output goes to `sim_1/runs/<scenario>-seed<seed>/` by default (ignored
by Git):

- `config.json`: resolved scenario config, including the seed used
- `log.jsonl`: one line per sim step, with `truth` (including thruster health
  and fault `events`), `observations` and, on controller ticks, `controller`
  (phase, command, health estimates, monitor residual, controller `events`)
  under separate keys
- `summary.json`: outcome and end-of-run metrics
- `replay.html`: self-contained visual replay of the run (see below)

A scenario TOML file plus a seed fully defines a run. All parameters, including
timestep and controller cadence, are in the scenario file.

## Visual replay

Every run writes `replay.html` into its output directory. Open it in a browser
(no server or network needed):

```sh
cd sim_1
open runs/nominal-seed1/replay.html        # macOS; or open the file in any browser
```

The replay shows the station components, the planned route with numbered
inspection stops, the truth path and vehicle (amber; the wedge is the nose),
the direction of travel (white arrow), the navigation observations and observed
pose the controller acted on (blue), and the camera view toward the component
being inspected. The side panel has the inspection checklist, truth vs.
observed state, station clearance, the docking check, thruster output and a
dock close-up.

The side panel is laid out like an operations console:

- **Status strip:** current mode, mission elapsed time, and a master alarm
  (NOMINAL / CAUTION / WARNING) driven by what the controller knows.
- **Relative navigation (LVLH):** range and range rate to the dock port,
  R-bar (radial) and V-bar (along-track) position and rates, heading, fuel,
  station clearance and navigation-fix age, with truth next to the navigation
  estimate. The map shows the same R-bar/V-bar axes and a live coordinate tag
  beside the vehicle.
- **Thruster schematic:** two top-view diagrams, what each thruster actually
  delivers (truth) and what the controller commands and believes. A thruster
  commanded but delivering nothing is visible at a glance.
- **System health:** one row per component (thrusters, attitude control,
  sensors, fault monitor, recovery plan) with a TRUTH column and a CONTROLLER
  BELIEF column. Thrusters read NOMINAL, DEGRADED, FAILED or UNVERIFIED (assumed
  OK, not yet tested) and say how they were verified. Sensors show availability,
  data age and stated uncertainty. Rows where belief differs from truth are
  highlighted, and a "truth view" line counts faults the controller does not
  know about yet.
- **Event log**, then collapsible sections for the docking check, inspection
  plan, navigation state, dock close-up and legend.

In fault runs the map also shows the particle impact, where the anomaly was
detected, the recovery path or hold point, and a red mark on the vehicle at the
failed thruster.

Playback has play/pause, speed, a timeline scrubber and a phase bar. `?t=SECONDS` in the URL
opens the replay at that time. To rebuild the page for an existing run:

```sh
.venv/bin/python -m singularos_sim.replay runs/nominal-seed1
```

## Test

```sh
cd sim_1
.venv/bin/pytest
```
