"""Deterministic baseline controller: inspection loop, fault response, return.

The controller acts only on observations (MISSION.md section 6) and the
nominal vehicle spec from config.controller_spec(). It never receives truth,
fault configuration or true thruster health. Its state estimate is the latest
available observation from each source; a dedicated state estimator is a
later milestone.

Mission plan, as a sequence of legs:
  DEPART   -> staging point (back out along the dock axis)
  INSPECT  -> each mission.route entry in order. "inspect" stops: come to
              rest, turn the nose to face look_at_m, station-keep for hold_s.
              "via" corners: pass within via_tolerance_m without stopping,
              pre-rotating toward the next inspect heading.
  RETURN   -> staging point, rotate back to dock heading
  APPROACH -> dock at origin, slow, until docking criteria have held for
              docking.confirm_duration_s -> DOCKED

Fault response (MISSION.md section 8 modes; DEPART/INSPECT are NORMAL):
  Fault monitor: every tick, the velocity change observed by navigation over
    a sliding window is compared with the change predicted from the
    controller's own commands and its current thruster health estimates. A
    residual above threshold for several consecutive ticks is an anomaly.
  INVESTIGATE: brake (bounded time) while comparing, per thruster, the
    velocity change it was commanded to produce with the change observed
    along its axis; a thruster that clearly produces nothing is marked failed
    straight away so braking can turn to use the others. Then settle and fire
    each remaining thruster alone for a fixed pulse; the ratio of measured to
    expected velocity change is its effectiveness estimate.
  Plan: planner.assess_recovery() decides RETURN (then APPROACH/DOCKED), HOLD
    or ABORT from the estimates. RETURN follows the remaining or completed
    part of the pre-planned loop, whichever is shorter, to stay clear of the
    station, stopping at every point.
  Degraded flight: with a thruster unusable, the controller turns so an
    intact opposing thruster pair lies along the acceleration it needs
    (either sign), for braking during INVESTIGATE and for RETURN/HOLD legs.
    It re-aims only when the demand moves well off the pair axis
    (hysteresis), and does not thrust while the pair is still turning onto
    it, so it neither chases noise nor spirals around a waypoint. A thruster
    whose opposite is unusable is not fired at all: one-sided corrections
    driven by sensor noise would ratchet up velocity that cannot be removed.
  Degraded docking: with a thruster unusable, sideways control at the dock
    heading is one-sided. The return therefore stops precisely at a pre-dock
    point on the dock axis using the aligned pair (checked on averaged
    position and velocity: small offset from the axis, almost no sideways
    velocity, held for predock_settle_s so a passing average during a
    transient does not count), turns to the dock heading without thrusting, then makes a
    short final approach. If it drifts off
    the axis beyond approach_lateral_limit_m it backs out to the pre-dock
    point and retries, up to max_dock_retries, then falls back to HOLD.
  HOLD: station-keep at the nearest loop point; declared once stable for
    recovery.hold_confirm_s. ABORT: make safe, then shut down: turn the best
    remaining thruster against the current velocity and brake to (near) rest,
    bounded by abort_brake_timeout_s, then thrusters off.

Orbital relative motion: the station's orbit is known (controller_spec), so
guidance cancels the predicted orbital acceleration (orbit.natural_accel, the
nonlinear relative equations) and commands only the difference, and the
fault monitor includes it in its prediction.

Orbit-scale triage: when an anomaly is declared, planner.assess_orbital()
checks the perigee of the vehicle's own orbit from navigation. If it is safe,
the response is the investigation above. If not:
  BRAKE: investigate as usual, with the braking time limit raised to cover
    the estimated burn (orbital.brake_timeout_margin x the estimate).
  SAFE_ORBIT: burn prograde (along the vehicle's Earth-relative velocity)
    until the observed perigee is orbital.perigee_margin_km above the safe
    perigee, then HOLD in that orbit, thrusters off. The fault monitor is
    off during this burn.
  ABORT: as below.
LLM advice (optional): if an advisor callable is given, it is called at
each decision point (orbit triage, recovery plan) with a context dict of
controller-side information (_advice_context) and its reply is logged as an
llm_advice event. The reply never changes the command: the deterministic
decision keeps actuator authority (MISSION.md section 7).
AI copilot (optional): if a copilot is given, at the recovery plan point
the controller asks it for a plan instead of using its own. The copilot
sees controller-side evidence and the text of station advisories, not the
baseline's decision. Each proposal is checked by plan_validator; rejected
proposals are fed back (recovery.copilot_max_attempts). An accepted plan is
executed as legs and the copilot holds authority; otherwise the baseline
plan is used. Authority returns to the baseline if the copilot's plan
exhausts its docking retries. Every proposal, verdict and authority change
is logged. Station advisories reach the controller as messages
(receive_advisory); the baseline itself does not act on them.
Coast-in (copilot procedure): from the pre-dock point the aligned thruster
pair sets a slow velocity toward the dock, then the final approach turns to
the dock heading without thrust and coasts in.
Far-range return: from beyond orbital.far_range_m, RETURN first flies
straight in toward the dock to orbital.far_approach_radius_m at
orbital.far_transit_speed_m_s, then follows the loop from the route point
nearest that approach point.

The route comes from config and is assumed collision-free; the controller
does no obstacle avoidance. Inspection is a pose and hold only: no imaging
or damage detection is modelled.

Translation uses the body thrusters to accelerate in any direction they can
reach regardless of heading. Heading is controlled separately by commanding
a bounded angular rate.
"""

import math
from collections import deque
from dataclasses import dataclass

from . import plan_validator, planner
from .config import ORBITAL_DEFAULTS, THRUSTER_DIRECTIONS, THRUSTER_NAMES
from .dynamics import Command, wrap_angle
from .orbit import mean_motion, natural_accel, orbit_status, station_radius_m

FINAL_PHASES = ("DOCKED", "HOLD", "ABORT")


@dataclass
class Leg:
    phase: str
    target: tuple
    heading: float
    max_speed: float
    hold_s: float = 0.0
    name: str = ""
    via: bool = False
    precise: bool = False  # pre-dock stop: arrival checked against the dock axis on averages
    coast: float | None = None  # coast-in setup: target speed toward the dock (m/s)


class Controller:
    def __init__(self, spec, mission, params, docking, recovery, orbital=None, advisor=None, copilot=None,
                 station=None):
        self.spec = spec
        self.params = params
        self.docking = docking
        self.recovery = recovery
        self.orbital = {**ORBITAL_DEFAULTS, **(orbital or {})}
        self.hold_kind = None  # "station" or "safe_orbit" once HOLD is declared
        self.safe_orbit = None  # active safe-orbit burn
        self.advisor = advisor
        self.copilot = copilot
        self.station = station or []
        self.advisories = []  # station messages received so far
        self.authority = "baseline"
        self.coast_since = None
        self.n = mean_motion(spec["orbit_period_s"])
        self.staging = tuple(mission["staging_point_m"])
        self.dock_heading = mission["dock_heading_rad"]
        transit = params["transit_max_speed_m_s"]

        route = []
        next_heading = self.dock_heading
        for stop in reversed(mission["route"]):
            point = tuple(stop["point_m"])
            if stop["kind"] == "inspect":
                look = stop["look_at_m"]
                next_heading = math.atan2(look[1] - point[1], look[0] - point[0])
                route.append(Leg("INSPECT", point, next_heading, transit, stop["hold_s"], stop["name"]))
            else:
                route.append(Leg("INSPECT", point, next_heading, transit, name="via", via=True))
        route.reverse()
        self.route_points = [leg.target for leg in route]

        self.legs = [
            Leg("DEPART", self.staging, self.dock_heading, transit, name="staging"),
            *route,
            Leg("RETURN", self.staging, self.dock_heading, transit, name="staging"),
            Leg("APPROACH", (0.0, 0.0), self.dock_heading, params["approach_max_speed_m_s"], name="dock"),
        ]
        self.leg_index = 0
        self.hold_started_at = None
        self.final = None
        self.criteria_met_since = None
        self.latest = {}
        self.last_docking_check = None
        self.completed_inspections = []

        # Thruster health as estimated by diagnostics (1.0 until tested).
        self.health = {name: 1.0 for name in THRUSTER_NAMES}
        # How each estimate was obtained: None (assumed nominal, untested),
        # "brake" (judged from braking evidence) or "pulse" (test firing).
        self.health_verified = {name: None for name in THRUSTER_NAMES}
        self.diag = None
        self.investigations = 0
        self.events = []
        self._monitor_reset()
        self.pair_heading = None
        self.stable_since = None
        self.abort_braking = False
        self.abort_started = None
        self.abort_thruster = None
        self.dock_retries = 0
        self.precise_since = None
        self.nav_history = deque(maxlen=recovery["nav_average_samples"])
        self.last_residual = None

    # ------------------------------------------------------------------ state

    @property
    def phase(self):
        if self.final:
            return self.final
        if self.safe_orbit is not None:
            return "HOLD"
        if self.diag is not None:
            return "INVESTIGATE"
        return self.legs[self.leg_index].phase

    @property
    def docked(self):
        return self.final == "DOCKED"

    @property
    def finished(self):
        return self.final is not None

    @property
    def leg_name(self):
        if self.final == "DOCKED":
            return "dock"
        if self.final == "HOLD" and self.hold_kind == "safe_orbit":
            return "safe orbit"
        if self.safe_orbit is not None:
            return "safe orbit burn"
        if self.final or self.diag is not None:
            return ""
        return self.legs[self.leg_index].name

    def inspections_completed(self):
        return list(self.completed_inspections)

    def take_events(self):
        events, self.events = self.events, []
        return events

    def _usable(self):
        return planner.usable(self.health, self.recovery["usable_effectiveness"])

    def _degraded(self):
        return len(self._usable()) < len(THRUSTER_NAMES)

    # ----------------------------------------------------------------- update

    def update(self, t, observations):
        """Consume new observations and return the actuator command for this tick."""
        for obs in observations:
            if obs.available:
                self.latest[obs.source] = obs
        self.last_docking_check = None
        self.last_residual = None

        if (self.final == "DOCKED" or (self.final == "ABORT" and not self.abort_braking)
                or (self.final == "HOLD" and self.hold_kind == "safe_orbit")):
            return Command.idle()
        nav = self.latest.get("navigation")
        att = self.latest.get("attitude_sensor")
        if nav is None or att is None:
            return Command.idle()
        v = nav.value
        self.nav_history.append((v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"]))

        if self.final == "ABORT":
            return self._abort_brake(t, nav, att)
        if self.safe_orbit is not None:
            return self._safe_orbit_burn(t, nav, att)
        if self.diag is None and not self.final and self._monitor_check(t, nav):
            triage = self._orbital_triage(t, nav)
            decision = triage["decision"] if triage else None
            if decision == "SAFE_ORBIT":
                self.safe_orbit = {"start": t}
                return self._safe_orbit_burn(t, nav, att)
            if decision == "ABORT":
                self._start_abort(t)
                self.events.append({"t": t, "type": "abort", "reason": "unsafe orbit, no feasible recovery"})
                return self._abort_brake(t, nav, att)
            brake_timeout = None
            if decision == "BRAKE":
                brake_timeout = max(self.recovery["brake_timeout_s"],
                                    triage["brake_time_s"] * self.orbital["brake_timeout_margin"])
            self._start_investigation(t, att, brake_timeout)
            if self.final:
                return Command.idle()

        if self.diag is not None:
            command = self._investigate(t, nav, att)
        else:
            command = self._fly_legs(t, nav, att)
        self._monitor_record(t, nav, att, command)
        return command

    # ------------------------------------------------------------- nominal legs

    def _advance_leg(self):
        leg = self.legs[self.leg_index]
        if leg.phase == "INSPECT" and not leg.via:
            self.completed_inspections.append(leg.name)
        self.leg_index += 1
        self.hold_started_at = None

    def _fly_legs(self, t, nav, att):
        leg = self.legs[self.leg_index]
        if leg.phase == "APPROACH":
            check = self.docking_check(t)
            if check["criteria_met"]:
                if self.criteria_met_since is None:
                    self.criteria_met_since = t
            else:
                self.criteria_met_since = None
            held = 0.0 if self.criteria_met_since is None else t - self.criteria_met_since
            check["criteria_held_s"] = held
            check["pass"] = check["criteria_met"] and held >= self.docking["confirm_duration_s"] - 1e-9
            self.last_docking_check = check
            if check["pass"]:
                self.final = "DOCKED"
                return Command.idle()
        elif leg.phase == "HOLD":
            # Once established, HOLD keeps station-keeping: under orbital
            # motion an unpowered craft drifts away from the hold point.
            if self.final != "HOLD" and self._hold_stable(t, leg, nav):
                self.final = "HOLD"
                self.hold_kind = "station"
                self.events.append({"t": t, "type": "hold_established", "point_m": list(leg.target)})
        elif leg.coast is not None:
            return self._coast_setup(t, leg, nav, att)
        elif self._arrived(leg, nav, att):
            if leg.hold_s > 0 and self.hold_started_at is None:
                self.hold_started_at = t
            if leg.hold_s <= 0 or t - self.hold_started_at >= leg.hold_s - 1e-9:
                self._advance_leg()
                leg = self.legs[self.leg_index]

        target = leg.target
        if leg.phase == "APPROACH":
            if self._degraded() and abs(self._dock_axis_frame()[1]) > self.recovery["approach_lateral_limit_m"]:
                return self._retry_docking(t, nav, att)
            # Final approach: hold position until turned to the dock heading.
            # Degraded: turn without thrusting so velocity stays near zero.
            if abs(wrap_angle(leg.heading - att.value["heading_rad"])) > self.recovery["align_tolerance_rad"]:
                if self._degraded():
                    return Command(self._idle(), self._rate_command(leg.heading, att))
                target = None
        ax, ay = self._guidance(target, leg.max_speed, nav)
        if self._degraded_leg(leg):
            heading = self._pair_heading(ax, ay, att)
            throttle = self._allocate(ax, ay, att) if self._pair_aligned(ax, ay, att) else self._idle()
        else:
            heading = leg.heading
            throttle = self._allocate(ax, ay, att)
        return Command(throttle, self._rate_command(heading, att))

    @staticmethod
    def _idle():
        return {name: 0.0 for name in THRUSTER_NAMES}

    def _dock_axis_frame(self):
        """Averaged observed position and velocity in the dock-axis frame:
        (axial distance out from the dock, lateral offset, axial velocity,
        lateral velocity). Averaging over nav_average_samples suppresses
        single-fix noise for precision checks."""
        n = len(self.nav_history)
        x, y, vx, vy = (sum(h[i] for h in self.nav_history) / n for i in range(4))
        # Outward axis points from the dock toward the approach corridor.
        ox, oy = -math.cos(self.dock_heading), -math.sin(self.dock_heading)
        return (x * ox + y * oy, -x * oy + y * ox, vx * ox + vy * oy, -vx * oy + vy * ox)

    def _retry_docking(self, t, nav, att):
        self.dock_retries += 1
        self.criteria_met_since = None
        if self.dock_retries > self.recovery["max_dock_retries"]:
            self.legs = [Leg("HOLD", self.staging, self.dock_heading, self.recovery["transit_max_speed_m_s"],
                             name="hold point")]
            self.leg_index = 0
            self.events.append({"t": t, "type": "dock_retry_limit", "decision": "HOLD", "hold_point_m": list(self.staging)})
            if self.authority == "copilot":
                self._set_authority(t, "baseline", "copilot plan exhausted its docking retries")
        else:
            self.leg_index = next(i for i, l in enumerate(self.legs) if l.name == "pre-dock")
            self.events.append({"t": t, "type": "dock_retry", "retry": self.dock_retries,
                                "lateral_offset_m": self._dock_axis_frame()[1]})
        self.pair_heading = None
        return self._fly_legs(t, nav, att)

    def _degraded_leg(self, leg):
        return self._degraded() and leg.phase in ("RETURN", "HOLD")

    def _pair_axis_offset(self):
        """Angle of the intact opposing pair's axis relative to the nose."""
        return 0.0 if {"forward", "backward"} <= self._usable() else math.pi / 2

    @staticmethod
    def _axis_diff(a, b):
        """Angle between two undirected axes, in [0, pi/2]."""
        d = abs(wrap_angle(a - b)) % math.pi
        return min(d, math.pi - d)

    def _pair_heading(self, ax, ay, att):
        """Heading that lines an intact opposing thruster pair up with the
        needed world acceleration (either sign). Re-aimed only when the demand
        is significant and well off the current pair axis."""
        r = self.recovery
        h = att.value["heading_rad"]
        offset = self._pair_axis_offset()
        demand = math.atan2(ay, ax)
        significant = math.hypot(ax, ay) >= r["pair_heading_accel_m_s2"]
        if self.pair_heading is None:
            target_axis = demand if significant else h + offset
        elif significant and (
            self._axis_diff(demand, self.pair_heading + offset) > r["pair_reaim_rad"]
            or self._axis_diff(demand, h + offset) > r["pair_gate_rad"]
        ):
            # Re-aim when the demand has moved off the planned axis, and always
            # when the thrust gate is blocking it, so the two can never deadlock.
            target_axis = demand
        else:
            return self.pair_heading
        options = (target_axis - offset, target_axis - offset + math.pi)
        self.pair_heading = wrap_angle(min(options, key=lambda o: abs(wrap_angle(o - h))))
        return self.pair_heading

    def _pair_aligned(self, ax, ay, att):
        """True if the observed pair axis is close enough to the demand to thrust."""
        if math.hypot(ax, ay) < self.params["accel_deadband_m_s2"]:
            return True
        axis = att.value["heading_rad"] + self._pair_axis_offset()
        return self._axis_diff(math.atan2(ay, ax), axis) <= self.recovery["pair_gate_rad"]

    def _arrived(self, leg, nav, att):
        p = self.params
        v = nav.value
        dist = math.hypot(leg.target[0] - v["x_m"], leg.target[1] - v["y_m"])
        if leg.via:
            return dist <= p["via_tolerance_m"]
        if leg.precise:
            r = self.recovery
            if len(self.nav_history) < self.nav_history.maxlen:
                return False
            axial, lateral, v_axial, v_lateral = self._dock_axis_frame()
            ok = (
                abs(axial - math.hypot(*leg.target)) <= r["predock_axial_tolerance_m"]
                and abs(lateral) <= r["predock_lateral_tolerance_m"]
                and abs(v_axial) <= r["predock_axial_speed_m_s"]
                and abs(v_lateral) <= r["predock_lateral_speed_m_s"]
            )
            t = self.latest["navigation"].timestamp
            if not ok:
                self.precise_since = None
                return False
            if self.precise_since is None:
                self.precise_since = t
            if t - self.precise_since >= r["predock_settle_s"] - 1e-9:
                self.precise_since = None
                return True
            return False
        speed = math.hypot(v["vx_m_s"], v["vy_m_s"])
        if dist > p["waypoint_tolerance_m"] or speed > p["waypoint_speed_tolerance_m_s"]:
            return False
        if self._degraded_leg(leg):
            return True
        return abs(wrap_angle(leg.heading - att.value["heading_rad"])) <= p["heading_tolerance_rad"]

    def _hold_stable(self, t, leg, nav):
        v = nav.value
        dist = math.hypot(leg.target[0] - v["x_m"], leg.target[1] - v["y_m"])
        speed = math.hypot(v["vx_m_s"], v["vy_m_s"])
        if dist <= self.recovery["hold_tolerance_m"] and speed <= self.params["waypoint_speed_tolerance_m_s"]:
            if self.stable_since is None:
                self.stable_since = t
            return t - self.stable_since >= self.recovery["hold_confirm_s"] - 1e-9
        self.stable_since = None
        self.abort_braking = False
        self.abort_started = None
        self.abort_thruster = None
        return False

    # ------------------------------------------------------------- allocation

    def _mass_estimate(self):
        fuel = self.latest.get("fuel_monitor")
        return self.spec["dry_mass_kg"] + (fuel.value["fuel_kg"] if fuel else 0.0)

    def _max_accel(self, mass):
        usable_set = self._usable()
        caps = [self.spec["max_thrust_n"][n] * self.health[n] for n in usable_set]
        return (min(caps) if caps else 0.0) / mass

    def _guidance(self, target, max_speed, nav):
        """World-frame acceleration demand: track a velocity toward target
        (None = null velocity), limited so the vehicle can brake in time."""
        p = self.params
        v = nav.value
        max_accel = self._max_accel(self._mass_estimate())
        if target is None:
            vdx = vdy = 0.0
        else:
            ex, ey = target[0] - v["x_m"], target[1] - v["y_m"]
            dist = math.hypot(ex, ey)
            speed_cmd = min(
                max_speed,
                p["position_gain_1_s"] * dist,
                math.sqrt(2.0 * p["braking_accel_fraction"] * max_accel * dist),
            )
            vdx, vdy = (ex / dist * speed_cmd, ey / dist * speed_cmd) if dist > 0 else (0.0, 0.0)
        # Command only what orbital motion will not provide by itself.
        nx, ny = natural_accel(self.n, v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])
        ax = p["velocity_gain_1_s"] * (vdx - v["vx_m_s"]) - nx
        ay = p["velocity_gain_1_s"] * (vdy - v["vy_m_s"]) - ny
        # Cap the demand at what the thrusters can deliver, keeping its
        # direction. An unreachable demand (orbit-scale braking) would
        # otherwise leak into the other body axis through heading noise.
        mag = math.hypot(ax, ay)
        if max_accel > 0 and mag > max_accel:
            ax, ay = ax * max_accel / mag, ay * max_accel / mag
        return ax, ay

    def _allocate(self, ax, ay, att):
        """Map a world acceleration to throttles of usable thrusters, scaling
        by estimated effectiveness. Unreachable components are dropped."""
        throttle = {name: 0.0 for name in THRUSTER_NAMES}
        if math.hypot(ax, ay) < self.params["accel_deadband_m_s2"]:
            return throttle
        mass = self._mass_estimate()
        h = att.value["heading_rad"]
        c, s = math.cos(h), math.sin(h)
        fx = mass * (c * ax + s * ay)
        fy = mass * (-s * ax + c * ay)
        usable_set = self._usable()
        for force, pos, neg in ((fx, "forward", "backward"), (fy, "left", "right")):
            if not (pos in usable_set and neg in usable_set):
                continue  # one-sided axis: see module docstring
            name = pos if force > 0 else neg
            if force != 0:
                throttle[name] = min(abs(force) / (self.spec["max_thrust_n"][name] * self.health[name]), 1.0)
        return throttle

    def _rate_command(self, heading, att):
        p = self.params
        err = wrap_angle(heading - att.value["heading_rad"])
        if abs(err) < p["heading_deadband_rad"]:
            return 0.0
        limit = self.spec["max_rate_rad_s"]
        return min(max(p["heading_gain_1_s"] * err, -limit), limit)

    # ----------------------------------------------------------- fault monitor

    def _monitor_reset(self):
        self.history = deque()
        self.residual_count = 0

    def _predicted_accel(self, att, command):
        """World-frame acceleration expected from a command, given current
        health estimates and the observed heading."""
        mass = self._mass_estimate()
        h = att.value["heading_rad"]
        c, s = math.cos(h), math.sin(h)
        fx = fy = 0.0
        for name in THRUSTER_NAMES:
            f = command.throttle[name] * self.spec["max_thrust_n"][name] * self.health[name]
            dx, dy = THRUSTER_DIRECTIONS[name]
            fx += f * dx
            fy += f * dy
        return ((c * fx - s * fy) / mass, (s * fx + c * fy) / mass)

    def _monitor_record(self, t, nav, att, command):
        v = nav.value
        ax, ay = self._predicted_accel(att, command)
        nx, ny = natural_accel(self.n, v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])
        self.history.append((t, (v["vx_m_s"], v["vy_m_s"]), (ax + nx, ay + ny)))
        window = self.recovery["monitor_window_s"]
        while self.history and self.history[0][0] < t - window - 1e-9:
            self.history.popleft()

    def _monitor_check(self, t, nav):
        """Compare observed and predicted velocity change over the window
        ending now. Returns True when an anomaly is declared."""
        r = self.recovery
        if not self.history or t - self.history[0][0] < r["monitor_window_s"] - 1e-9:
            return False
        entries = list(self.history) + [(t, (nav.value["vx_m_s"], nav.value["vy_m_s"]), None)]
        pdx = pdy = 0.0
        for (t0, _, acc), (t1, _, _) in zip(entries, entries[1:]):
            pdx += acc[0] * (t1 - t0)
            pdy += acc[1] * (t1 - t0)
        odx = entries[-1][1][0] - entries[0][1][0]
        ody = entries[-1][1][1] - entries[0][1][1]
        residual = math.hypot(odx - pdx, ody - pdy)
        self.last_residual = residual
        self.residual_count = self.residual_count + 1 if residual > r["monitor_threshold_m_s"] else 0
        if self.residual_count >= r["monitor_persistence"]:
            self.events.append({"t": t, "type": "anomaly_detected", "residual_m_s": residual,
                                "observed_dv_m_s": [odx, ody], "predicted_dv_m_s": [pdx, pdy]})
            return True
        return False

    # ------------------------------------------------------------- diagnostics

    def _start_investigation(self, t, att, brake_timeout=None):
        if self.investigations >= self.recovery["max_investigations"]:
            self._start_abort(t)
            self.events.append({"t": t, "type": "abort", "reason": "anomaly persists after max_investigations"})
            return
        self.investigations += 1
        # Where on the plan the anomaly happened, for choosing a return path.
        self.diag = {
            "stage": "brake",
            "start": t,
            "brake_timeout": brake_timeout or self.recovery["brake_timeout_s"],
            "heading": att.value["heading_rad"],
            "queue": list(THRUSTER_NAMES),
            "current": None,
            "samples": [],
            "v_before": None,
            "results": {},
            "leg_index": self.leg_index,
            # Per-thruster (expected, observed) velocity change along its axis
            # while braking; prev = (velocity, heading, throttles) last tick.
            "passive": {name: [0.0, 0.0] for name in THRUSTER_NAMES},
            "prev": None,
        }
        self.events.append({"t": t, "type": "investigate_start", "investigation": self.investigations})

    def _sample(self, t, nav, att):
        v = nav.value
        self.diag["samples"].append((t, v["vx_m_s"], v["vy_m_s"], att.value["heading_rad"]))

    def _mean_velocity(self, t):
        window = self.recovery["diag_average_s"]
        pts = [s for s in self.diag["samples"] if s[0] > t - window - 1e-9]
        n = len(pts)
        # Heading is averaged on the circle: near +/-180 deg an arithmetic mean
        # of e.g. +3.14 and -3.14 would give 0 and flip the measurement axis.
        heading = math.atan2(sum(math.sin(p[3]) for p in pts), sum(math.cos(p[3]) for p in pts))
        return (sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n, heading)

    def _investigate(self, t, nav, att):
        r = self.recovery
        d = self.diag
        idle = {name: 0.0 for name in THRUSTER_NAMES}
        rate = self._rate_command(d["heading"], att)
        elapsed = t - d["start"]

        if d["stage"] == "brake":
            self._passive_check(t, nav, att)
            speed = math.hypot(nav.value["vx_m_s"], nav.value["vy_m_s"])
            timed_out = elapsed >= d["brake_timeout"]
            if timed_out and self._closing_on_structure(nav) and elapsed < r["brake_hard_limit_factor"] * d["brake_timeout"]:
                # Never stop braking while drifting toward nearby structure:
                # the test firings and turns that follow take tens of seconds.
                timed_out = False
            if speed <= r["brake_speed_m_s"] or timed_out:
                # Hold the heading braking ended at, so test pulses fire from a
                # steady attitude rather than while turning back.
                d.update(stage="settle", start=t, samples=[], heading=att.value["heading_rad"])
                d["queue"] = [n for n in d["queue"] if n not in d["results"]]
            else:
                if self._degraded() and not self._has_pair() and self._usable():
                    # No opposing pair left: brake by aiming a single thruster.
                    command = self._single_thruster_brake(nav, att)
                    d["prev"] = ((nav.value["vx_m_s"], nav.value["vy_m_s"]), att.value["heading_rad"],
                                 command.throttle, t)
                    return command
                ax, ay = self._guidance(None, 0.0, nav)
                throttle = self._allocate(ax, ay, att)
                if self._degraded():
                    rate = self._rate_command(self._pair_heading(ax, ay, att), att)
                    if not self._pair_aligned(ax, ay, att):
                        throttle = self._idle()
                d["prev"] = ((nav.value["vx_m_s"], nav.value["vy_m_s"]), att.value["heading_rad"], throttle, t)
                return Command(throttle, rate)

        if d["stage"] == "settle":
            self._sample(t, nav, att)
            if t - d["start"] >= r["diag_settle_s"] - 1e-9:
                if not d["queue"]:
                    return self._plan(t, nav, att)
                d["v_before"] = self._mean_velocity(t)
                d["v_before_t"] = t
                self._begin_pulse(t, nav, att, d["queue"].pop(0))
            else:
                return Command(idle, rate)

        if d["stage"] == "turn":
            # Turning so the test firing pushes away from nearby structure.
            err = abs(wrap_angle(d["heading"] - att.value["heading_rad"]))
            if err > r["diag_turn_tolerance_rad"] or abs(att.value["angular_rate_rad_s"]) > 0.01:
                return Command(idle, self._rate_command(d["heading"], att))
            d.update(stage="pulse", start=t)

        if d["stage"] == "pulse":
            if t - d["start"] < r["diag_pulse_s"] - 1e-9:
                throttle = dict(idle)
                throttle[d["current"]] = 1.0
                return Command(throttle, rate)
            d.update(stage="post", start=t, samples=[])

        if d["stage"] == "post":
            self._sample(t, nav, att)
            if t - d["start"] < r["diag_settle_s"] - 1e-9:
                return Command(idle, rate)
            self._finish_pulse(t, nav)
            if d["queue"]:
                self._begin_pulse(t, nav, att, d["queue"].pop(0))
                if d["stage"] == "turn":
                    return Command(idle, self._rate_command(d["heading"], att))
                throttle = dict(idle)
                throttle[d["current"]] = 1.0
                return Command(throttle, rate)
            return self._plan(t, nav, att)
        return Command(idle, rate)

    def _closing_on_structure(self, nav):
        """True if moving toward the nearest station component faster than
        brake_speed while within diag_safe_distance_m of it."""
        v = nav.value
        dist, (ux, uy) = self._nearest_structure(v["x_m"], v["y_m"])
        closing = v["vx_m_s"] * ux + v["vy_m_s"] * uy
        return dist < self.recovery["diag_safe_distance_m"] and closing > self.recovery["brake_speed_m_s"]

    def _nearest_structure(self, x, y):
        """Distance and unit direction from (x, y) to the nearest station component."""
        best = (math.inf, (0.0, 0.0))
        for c in self.station:
            px = min(max(x, c["min_m"][0]), c["max_m"][0])
            py = min(max(y, c["min_m"][1]), c["max_m"][1])
            d = math.hypot(px - x, py - y)
            if d < best[0]:
                best = (d, ((px - x) / d, (py - y) / d) if d > 0 else (0.0, 0.0))
        return best

    def _begin_pulse(self, t, nav, att, name):
        """Start a test firing of one thruster. If it would push toward nearby
        structure, first turn 180 degrees so the same thruster pushes away."""
        d = self.diag
        d["current"] = name
        h = att.value["heading_rad"]
        dist, (ux, uy) = self._nearest_structure(nav.value["x_m"], nav.value["y_m"])
        bx, by = THRUSTER_DIRECTIONS[name]
        c, s = math.cos(h), math.sin(h)
        push = (c * bx - s * by, s * bx + c * by)
        if dist < self.recovery["diag_safe_distance_m"] and push[0] * ux + push[1] * uy > 0.2:
            d["heading"] = wrap_angle(h + math.pi)
            d.update(stage="turn", start=t)
            self.events.append({"t": t, "type": "diagnostic_turn", "thruster": name,
                                "distance_to_structure_m": round(dist, 2)})
        else:
            d["heading"] = h
            d.update(stage="pulse", start=t)

    def _natural(self, nav):
        v = nav.value
        return natural_accel(self.n, v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])

    def _passive_check(self, t, nav, att):
        """While braking, attribute the observed velocity change along each
        body axis to the thruster that fired on it, and mark a thruster
        failed once it has clearly produced nothing."""
        r = self.recovery
        d = self.diag
        if d["prev"] is not None:
            (vx0, vy0), h, throttle, t0 = d["prev"]
            # Remove what orbital motion contributed; at orbit-scale speeds it
            # is comparable to the thrust.
            nx, ny = self._natural(nav)
            dvx = nav.value["vx_m_s"] - vx0 - nx * (t - t0)
            dvy = nav.value["vy_m_s"] - vy0 - ny * (t - t0)
            c, s = math.cos(h), math.sin(h)
            body = (c * dvx + s * dvy, -s * dvx + c * dvy)
            mass = self._mass_estimate()
            for name in THRUSTER_NAMES:
                if throttle[name] <= 0 or name in d["results"]:
                    continue
                bx, by = THRUSTER_DIRECTIONS[name]
                acc = d["passive"][name]
                acc[0] += throttle[name] * self.spec["max_thrust_n"][name] * (t - t0) / mass
                acc[1] += body[0] * bx + body[1] * by
                if acc[0] >= r["diag_min_expected_dv_m_s"]:
                    ratio = acc[1] / acc[0]
                    if ratio < r["passive_failed_ratio"]:
                        self.health[name] = 0.0
                        self.health_verified[name] = "brake"
                        self.pair_heading = None
                        d["results"][name] = {"measured_ratio": ratio, "estimate": 0.0, "method": "brake"}
                        self.events.append({"t": t, "type": "diagnostic", "thruster": name, "measured_ratio": ratio,
                                            "estimate": 0.0, "method": "brake"})
                    acc[0] = acc[1] = 0.0
        d["prev"] = None

    def _finish_pulse(self, t, nav):
        r = self.recovery
        d = self.diag
        name = d["current"]
        vx0, vy0, _ = d["v_before"]
        vx1, vy1, h = self._mean_velocity(t)
        nx, ny = self._natural(nav)
        elapsed = t - d["v_before_t"]
        dvx, dvy = vx1 - vx0 - nx * elapsed, vy1 - vy0 - ny * elapsed
        # Velocity change along the thruster's push direction, in the body frame.
        bx, by = THRUSTER_DIRECTIONS[name]
        c, s = math.cos(h), math.sin(h)
        along = (c * dvx + s * dvy) * bx + (-s * dvx + c * dvy) * by
        expected = self.spec["max_thrust_n"][name] * r["diag_pulse_s"] / self._mass_estimate()
        ratio = along / expected if expected > 0 else 0.0
        if ratio < r["usable_effectiveness"]:
            estimate = 0.0
        elif ratio >= r["healthy_effectiveness"]:
            estimate = 1.0
        else:
            estimate = ratio
        self.health[name] = estimate
        self.health_verified[name] = "pulse"
        d["results"][name] = {"measured_ratio": ratio, "estimate": estimate, "method": "pulse"}
        self.events.append({"t": t, "type": "diagnostic", "thruster": name, "measured_ratio": ratio,
                            "estimate": estimate, "method": "pulse"})
        d["v_before"] = (vx1, vy1, h)
        d["v_before_t"] = t

    # ---------------------------------------------------------------- planning

    def _return_points(self, nav, leg_index):
        """Loop points from here back to staging, forward or backward along the
        pre-planned loop, whichever is shorter. Excludes staging itself."""
        here = (nav.value["x_m"], nav.value["y_m"])
        leg = self.legs[leg_index]
        if leg.phase in ("DEPART", "APPROACH") or (leg.phase == "RETURN" and leg.name == "staging"):
            return []
        if leg.phase == "INSPECT":
            k = leg_index - 1  # index into route_points of the point being flown to
            forward = self.route_points[k:]
            backward = self.route_points[:k][::-1]
        else:
            # Already on a recovery path: keep its remaining points.
            forward = [l.target for l in self.legs[leg_index:] if l.phase == "RETURN" and l.name != "staging"]
            backward = forward
        options = [forward, backward]
        return min(options, key=lambda pts: planner.path_length(here, pts + [self.staging]))

    def _far_approach(self, nav):
        """From beyond orbital.far_range_m: the approach point on the line in
        toward the dock, and the loop points from the route point nearest it
        back to staging (forward or backward, whichever is shorter)."""
        o = self.orbital
        x, y = nav.value["x_m"], nav.value["y_m"]
        dist = math.hypot(x, y)
        if dist <= o["far_range_m"]:
            return None, None
        k = o["far_approach_radius_m"] / dist
        approach = (x * k, y * k)
        near = min(range(len(self.route_points)),
                   key=lambda i: math.hypot(self.route_points[i][0] - approach[0], self.route_points[i][1] - approach[1]))
        forward = self.route_points[near:]
        backward = self.route_points[:near + 1][::-1]
        points = min((forward, backward), key=lambda pts: planner.path_length(approach, pts + [self.staging]))
        return approach, points

    def _plan(self, t, nav, att):
        r = self.recovery
        leg_index = self.diag["leg_index"]
        approach, far_points = self._far_approach(nav)
        points = far_points if approach else self._return_points(nav, leg_index)
        fuel = self.latest.get("fuel_monitor")
        fuel_kg = fuel.value["fuel_kg"] if fuel else 0.0
        speed = math.hypot(nav.value["vx_m_s"], nav.value["vy_m_s"])
        far_speed = self.orbital["far_transit_speed_m_s"]
        assessment = planner.assess_recovery(
            self.spec, self.health, fuel_kg, self._mass_estimate(), len(points) + 1, r, speed,
            # Far range: accelerate/brake at far speed, plus working against
            # orbital motion while crossing, about 2 n per metre travelled.
            2.0 * far_speed + 2.0 * self.n * math.hypot(nav.value["x_m"], nav.value["y_m"]) if approach else 0.0,
        )
        self.diag = None
        self._monitor_reset()
        self.pair_heading = None
        self.criteria_met_since = None

        if self.copilot is not None and not approach:
            command = self._consult_copilot(t, nav, att, assessment, points, fuel_kg)
            if command is not None:
                return command

        transit = r["transit_max_speed_m_s"]
        decision = assessment["decision"]
        if decision == "RETURN":
            h = self.dock_heading
            predock = (-math.cos(h) * r["predock_distance_m"], -math.sin(h) * r["predock_distance_m"])
            final = [Leg("RETURN", predock, h, r["approach_max_speed_m_s"], name="pre-dock", precise=True)]
            far = [Leg("RETURN", approach, h, far_speed, name="far approach")] if approach else []
            self.legs = [
                *far,
                *[Leg("RETURN", p, h, transit, name="return point") for p in points],
                Leg("RETURN", self.staging, h, transit, name="staging"),
                *(final if self._degraded() else []),
                Leg("APPROACH", (0.0, 0.0), h, r["approach_max_speed_m_s"], name="dock"),
            ]
            self.leg_index = 0
        elif decision == "HOLD":
            here = (nav.value["x_m"], nav.value["y_m"])
            candidates = (points[:1] or [self.staging]) + [self._previous_point(leg_index)]
            hold = min(candidates, key=lambda p: math.hypot(p[0] - here[0], p[1] - here[1]))
            self.legs = [Leg("HOLD", hold, self.dock_heading, transit, name="hold point")]
            self.leg_index = 0
            assessment["hold_point_m"] = list(hold)
        else:
            self._start_abort(t)
        assessment["return_points_m"] = [list(p) for p in points]
        if approach:
            assessment["far_approach_m"] = list(approach)
        self.events.append({"t": t, "type": "plan", **assessment})
        if self.advisor is not None:
            needed = assessment["fuel_needed_kg"]
            self._advise(t, "plan", decision, assessment["reasons"], nav, {
                "dock_feasible": decision == "RETURN",
                "return_fuel_ok": None if needed is None else needed * r["fuel_margin"] <= fuel_kg - r["fuel_reserve_kg"],
                "available_delta_v_m_s": planner.available_delta_v(
                    self.spec, self.health, self._usable(), fuel_kg, self._mass_estimate(), r["fuel_reserve_kg"]),
            })
        if self.final == "ABORT":
            return self._abort_brake(t, nav, att)
        return self._fly_legs(t, nav, att)

    # ------------------------------------------------------------- copilot

    def receive_advisory(self, t, advisory):
        """A station message (communications, not truth). The baseline does
        not act on it; the copilot reads its text and the validator enforces
        its structured constraints."""
        self.advisories.append(advisory)
        self.events.append({"t": t, "type": "advisory_received", "id": advisory["id"], "text": advisory["text"]})

    def _set_authority(self, t, owner, reason):
        self.authority = owner
        self.events.append({"t": t, "type": "authority", "owner": owner, "reason": reason})

    def _predock(self, distance):
        h = self.dock_heading
        return (-math.cos(h) * distance, -math.sin(h) * distance)

    def _copilot_context(self, t, nav, fuel_kg):
        v = nav.value
        att = self.latest.get("attitude_sensor")
        return {
            "t": t,
            "observed": {"x_m": round(v["x_m"], 2), "y_m": round(v["y_m"], 2),
                         "vx_m_s": round(v["vx_m_s"], 3), "vy_m_s": round(v["vy_m_s"], 3),
                         "heading_rad": round(att.value["heading_rad"], 3) if att else None,
                         "fuel_kg": round(fuel_kg, 3)},
            "distance_to_dock_m": math.hypot(v["x_m"], v["y_m"]),
            "health_estimate": {k: round(h, 2) for k, h in self.health.items()},
            "usable": sorted(self._usable(), key=THRUSTER_NAMES.index),
            "fuel_reserve_kg": self.recovery["fuel_reserve_kg"],
            "advisories": [{"id": a["id"], "text": a["text"]} for a in self.advisories],
        }

    def _validator_context(self, nav, points, fuel_kg):
        v = nav.value
        r = self.recovery
        return {
            "position_m": (v["x_m"], v["y_m"]),
            "speed_m_s": math.hypot(v["vx_m_s"], v["vy_m_s"]),
            "distance_to_dock_m": math.hypot(v["x_m"], v["y_m"]),
            "predock_m": self._predock(r["predock_distance_m"]),
            "staging_m": self.staging,
            "return_points_m": [tuple(p) for p in points],
            "usable": self._usable(),
            "health": dict(self.health),
            "degraded": self._degraded(),
            "recovery": r,
            "docking": self.docking,
            "constraints": [{**c, "advisory": a["id"]} for a in self.advisories for c in a.get("constraints", [])],
            "station": self.station,
            "spec": self.spec,
            "mass_kg": self._mass_estimate(),
            "fuel_kg": fuel_kg,
        }

    def _consult_copilot(self, t, nav, att, baseline, points, fuel_kg):
        """Ask the copilot for a plan; execute it if the validator accepts it.
        Returns the command, or None to fall back to the baseline plan."""
        ctx = self._copilot_context(t, nav, fuel_kg)
        vctx = self._validator_context(nav, points, fuel_kg)
        self.events.append({"t": t, "type": "baseline_assessment", "decision": baseline["decision"],
                            "reasons": baseline["reasons"]})
        history = []
        for attempt in range(1, self.recovery["copilot_max_attempts"] + 1):
            try:
                plan, latency = self.copilot.propose(ctx, history)
            except Exception as e:  # a copilot failure must not stop the controller
                self.events.append({"t": t, "type": "copilot_error", "attempt": attempt, "error": f"{type(e).__name__}: {e}"})
                break
            verdict = plan_validator.validate_plan(plan, vctx)
            self.events.append({"t": t, "type": "copilot_proposal", "attempt": attempt, "agent": self.copilot.name,
                                "latency_s": round(latency, 2), "plan": plan})
            self.events.append({"t": t, "type": "copilot_verdict", "attempt": attempt, **verdict})
            if verdict["accepted"]:
                self._set_authority(t, "copilot", f"plan accepted on attempt {attempt}")
                return self._adopt_plan(t, nav, att, plan, points, verdict)
            history.append((plan, verdict["problems"]))
        if self.advisories and baseline["decision"] == "RETURN":
            # The baseline cannot interpret station advisories, so its own
            # return may break them. Fall back to holding at a safe point.
            self._set_authority(t, "baseline", "no accepted copilot plan while station advisories are active; "
                                               "holding at a safe point")
            baseline["decision"] = "HOLD"
            baseline["reasons"] = list(baseline["reasons"]) + ["copilot plan not accepted under active advisories: hold"]
            return None
        self._set_authority(t, "baseline", "no accepted copilot plan; baseline plan used")
        return None

    def _adopt_plan(self, t, nav, att, plan, points, verdict):
        r = self.recovery
        h = self.dock_heading
        transit = r["transit_max_speed_m_s"]
        steps = plan["steps"]
        legs = []
        # Under an advisory speed limit, fly near-dock legs below it so that
        # velocity-tracking noise stays inside the limit.
        caps = [c["value_m_s"] for a in self.advisories for c in a.get("constraints", [])
                if c["type"] == "max_approach_speed"]
        cap = min(caps) * r["advisory_speed_margin"] if caps else None
        limit = (lambda v: v if cap is None else min(v, cap))
        for i, step in enumerate(steps):
            a = step["action"]
            nxt = steps[i + 1]["action"] if i + 1 < len(steps) else None
            if a == "RETURN_TO_STAGING":
                legs += [Leg("RETURN", tuple(p), h, transit, name="return point") for p in points]
                legs.append(Leg("RETURN", self.staging, h, transit, name="staging"))
            elif a == "GOTO":
                legs.append(Leg("RETURN", (step["x_m"], step["y_m"]), h, step.get("speed_m_s") or transit,
                                name="copilot waypoint"))
            elif a == "PREDOCK_STOP":
                d = r["coast_predock_distance_m"] if nxt == "COAST_IN" else r["predock_distance_m"]
                legs.append(Leg("RETURN", self._predock(d), h, limit(step.get("speed_m_s") or r["approach_max_speed_m_s"]),
                                name="pre-dock", precise=True))
            elif a == "COAST_IN":
                pre = legs[-1].target if legs else self._predock(r["coast_predock_distance_m"])
                speed = max(limit(step["speed_m_s"]), r["coast_min_speed_m_s"])
                legs.append(Leg("RETURN", pre, h, speed, name="coast setup", coast=speed))
                legs.append(Leg("APPROACH", (0.0, 0.0), h, limit(r["approach_max_speed_m_s"]), name="dock"))
            elif a == "FINAL_APPROACH":
                legs.append(Leg("APPROACH", (0.0, 0.0), h, limit(step.get("speed_m_s") or r["approach_max_speed_m_s"]),
                                name="dock"))
            elif a == "HOLD_AT":
                legs.append(Leg("HOLD", (step["x_m"], step["y_m"]), h, transit, name="hold point"))
        decision = {"DOCK": "RETURN", "HOLD": "HOLD", "ABORT": "ABORT"}[plan["decision"]]
        event = {"t": t, "type": "plan", "source": "copilot", "decision": decision,
                 "reasons": [plan["rationale"]], "return_points_m": [p for p in verdict["path_m"][1:]],
                 "fuel_needed_kg": verdict["fuel_needed_kg"]}
        if plan["decision"] == "HOLD":
            event["hold_point_m"] = [steps[-1]["x_m"], steps[-1]["y_m"]]
        self.events.append(event)
        if plan["decision"] == "ABORT":
            self._start_abort(t)
            return self._abort_brake(t, nav, att)
        self.legs = legs
        self.leg_index = 0
        return self._fly_legs(t, nav, att)

    def _coast_setup(self, t, leg, nav, att):
        """Coast-in setup, from rest at the pre-dock point: turn the intact
        opposing pair exactly onto the dock axis without thrusting, then burn
        purely along the axis to leg.coast m/s toward the dock, then hand over
        to the final approach, which turns to the dock heading without thrust
        and coasts in. Thrusting only when aligned keeps sideways velocity out."""
        r = self.recovery
        h_d = self.dock_heading
        offset = self._pair_axis_offset()
        h = att.value["heading_rad"]
        heading = min((wrap_angle(h_d - offset), wrap_angle(h_d - offset + math.pi)),
                      key=lambda o: abs(wrap_angle(o - h)))
        rate = self._rate_command(heading, att)
        if abs(wrap_angle(heading - h)) > r["coast_align_tolerance_rad"]:
            self.coast_since = None
            return Command(self._idle(), rate)

        _, lateral, v_axial_avg, _ = self._dock_axis_frame()
        if abs(-v_axial_avg - leg.coast) <= r["coast_speed_tolerance_m_s"]:
            if self.coast_since is None:
                self.coast_since = t
            if t - self.coast_since >= r["coast_settle_s"] - 1e-9:
                self.coast_since = None
                self.events.append({"t": t, "type": "coast_in_set", "speed_m_s": -v_axial_avg, "lateral_offset_m": lateral})
                self._advance_leg()
                return self._fly_legs(t, nav, att)
        else:
            self.coast_since = None
        # Burn along the dock axis only (instantaneous velocity), cancelling
        # the orbital acceleration component along the axis.
        v = nav.value
        ix, iy = math.cos(h_d), math.sin(h_d)  # unit vector toward the dock along its axis
        v_in = v["vx_m_s"] * ix + v["vy_m_s"] * iy
        nx, ny = natural_accel(self.n, v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])
        a_in = self.params["velocity_gain_1_s"] * (leg.coast - v_in) - (nx * ix + ny * iy)
        return Command(self._allocate(a_in * ix, a_in * iy, att), rate)

    # ----------------------------------------------------------- orbit scale

    def _orbital_triage(self, t, nav):
        fuel = self.latest.get("fuel_monitor")
        fuel_kg = fuel.value["fuel_kg"] if fuel else 0.0
        triage = planner.assess_orbital(self.spec, self.health, fuel_kg, self._mass_estimate(), self.n,
                                        nav.value, self.recovery, self.orbital)
        if triage is not None:
            self.events.append({"t": t, "type": "orbital_triage", **triage})
            if self.advisor is not None:
                m = self.orbital["delta_v_margin"]
                budget = triage["available_delta_v_m_s"]
                deadline = triage["time_to_reentry_s"]
                self._advise(t, "orbital_triage", triage["decision"], triage["reasons"], nav, {
                    "orbit": triage["orbit"],
                    "time_to_reentry_s": deadline,
                    "available_delta_v_m_s": budget,
                    "brake_feasible": triage["brake_delta_v_m_s"] * m <= budget
                    and (deadline is None or triage["brake_time_s"] < deadline),
                    "safe_orbit_authorized": triage["safe_orbit_safety"]["authorized"],
                    "safe_orbit_delta_v_m_s": triage["safe_orbit_plan"].get("delta_v_m_s"),
                })
        return triage

    ADVICE_MODES = {"BRAKE": "INVESTIGATE", "SAFE_ORBIT": "HOLD"}

    def _advice_context(self, t, point, decision, reasons, nav, extra):
        v = nav.value
        state = (v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])
        fuel = self.latest.get("fuel_monitor")
        att = self.latest.get("attitude_sensor")
        ctx = {
            "t": t,
            "point": point,
            "decision": decision,
            "mode": self.ADVICE_MODES.get(decision, decision),
            "reasons": list(reasons),
            "observed": {"x_m": round(v["x_m"], 2), "y_m": round(v["y_m"], 2),
                         "vx_m_s": round(v["vx_m_s"], 3), "vy_m_s": round(v["vy_m_s"], 3),
                         "heading_rad": round(att.value["heading_rad"], 3) if att else None,
                         "fuel_kg": round(fuel.value["fuel_kg"], 3) if fuel else None},
            "health_estimate": dict(self.health),
            "usable": sorted(self._usable(), key=THRUSTER_NAMES.index),
            "orbit": orbit_status(self.n, *state),
            "safe_perigee_km": self.orbital["safe_perigee_km"],
            "docking_check": None,
        }
        ctx.update(extra)
        return ctx

    def _advise(self, t, point, decision, reasons, nav, extra):
        """Ask the advisor and log its reply; the command is unaffected."""
        ctx = self._advice_context(t, point, decision, reasons, nav, extra)
        try:
            advice = self.advisor(ctx)
        except Exception as e:  # an advisor failure must not stop the controller
            advice = {"error": f"{type(e).__name__}: {e}"}
        self.events.append({"t": t, "type": "llm_advice", "point": point, "decision": decision, **advice})

    def _safe_orbit_burn(self, t, nav, att):
        """Prograde burn until the observed perigee clears the safe altitude
        by the margin, then HOLD in that orbit with thrusters off."""
        o = self.orbital
        v = nav.value
        state = (v["x_m"], v["y_m"], v["vx_m_s"], v["vy_m_s"])
        status = orbit_status(self.n, *state)
        if status["perigee_km"] >= o["safe_perigee_km"] + o["perigee_margin_km"]:
            self.safe_orbit = None
            self.final = "HOLD"
            self.hold_kind = "safe_orbit"
            self.events.append({"t": t, "type": "hold_established", "kind": "safe_orbit", "orbit": status})
            return Command.idle()
        # Earth-relative velocity direction, along the station frame's axes.
        R = station_radius_m(self.n)
        prograde = math.atan2(v["vy_m_s"] + self.n * (R + v["x_m"]), v["vx_m_s"] - self.n * v["y_m"])
        usable_set = self._usable()
        name = "forward" if "forward" in usable_set else "backward"
        heading = prograde if name == "forward" else wrap_angle(prograde + math.pi)
        throttle = self._idle()
        if name in usable_set and abs(wrap_angle(heading - att.value["heading_rad"])) <= self.recovery["pair_gate_rad"]:
            throttle[name] = 1.0
        return Command(throttle, self._rate_command(heading, att))

    # ------------------------------------------------------------------- abort

    def _start_abort(self, t):
        self.final = "ABORT"
        self.diag = None
        self.abort_braking = True
        self.abort_started = t
        self.abort_thruster = None

    def _abort_brake(self, t, nav, att):
        """Make safe before shutting down: turn one usable thruster against
        the velocity and brake. A single thruster is enough because the
        vehicle turns to aim it; thrust only while it points the right way."""
        r = self.recovery
        v = nav.value
        speed = math.hypot(v["vx_m_s"], v["vy_m_s"])
        usable_set = self._usable()
        done = speed <= r["brake_speed_m_s"] or t - self.abort_started >= r["abort_brake_timeout_s"]
        if done or not usable_set:
            self.abort_braking = False
            self.events.append({"t": t, "type": "abort_safe", "speed_m_s": speed,
                                "braked": bool(usable_set), "thrusters_off": True})
            return Command.idle()
        return self._single_thruster_brake(nav, att)

    def _has_pair(self):
        usable_set = self._usable()
        return any(a in usable_set and b in usable_set for a, b in planner.PAIRS)

    def _single_thruster_brake(self, nav, att):
        """Null velocity with no intact opposing pair: turn one usable thruster
        to face the needed push and fire it only while it points the right way."""
        r = self.recovery
        usable_set = self._usable()
        ax, ay = self._guidance(None, 0.0, nav)
        demand = math.atan2(ay, ax)
        h = att.value["heading_rad"]
        if self.abort_thruster not in usable_set:
            self.abort_thruster = None
        if self.abort_thruster is None:
            # Pick the usable thruster needing the smallest turn to face the demand.
            def turn(name):
                dx, dy = THRUSTER_DIRECTIONS[name]
                return abs(wrap_angle(demand - math.atan2(dy, dx) - h))
            self.abort_thruster = min(sorted(usable_set, key=THRUSTER_NAMES.index), key=turn)
        dx, dy = THRUSTER_DIRECTIONS[self.abort_thruster]
        heading = wrap_angle(demand - math.atan2(dy, dx))
        throttle = self._idle()
        if abs(wrap_angle(heading - h)) <= r["pair_gate_rad"]:
            force = math.hypot(ax, ay) * self._mass_estimate()
            name = self.abort_thruster
            throttle[name] = min(force / (self.spec["max_thrust_n"][name] * self.health[name]), 1.0)
        return Command(throttle, self._rate_command(heading, att))

    def _previous_point(self, leg_index):
        leg = self.legs[leg_index]
        if leg.phase == "INSPECT" and leg_index >= 2:
            return self.route_points[leg_index - 2]
        return self.staging

    # ----------------------------------------------------------------- docking

    def docking_check(self, t):
        """Evaluate all docking criteria from fresh observations only.

        Docking is declared only after the criteria have held continuously
        for docking.confirm_duration_s (see _fly_legs()), so a single noisy
        fix inside the thresholds is not enough.
        """
        d = self.docking
        nav = self.latest.get("navigation")
        att = self.latest.get("attitude_sensor")
        max_age = d["max_observation_age_s"] + 1e-9
        fresh = all(o is not None and o.available and t - o.timestamp <= max_age for o in (nav, att))
        result = {"fresh": fresh}
        if not fresh:
            return {"criteria_met": False, **result}
        v, u = nav.value, nav.uncertainty
        distance = math.hypot(v["x_m"], v["y_m"])
        speed = math.hypot(v["vx_m_s"], v["vy_m_s"])
        alignment = abs(wrap_angle(self.dock_heading - att.value["heading_rad"]))
        pos_unc = max(u["x_m"], u["y_m"])
        vel_unc = max(u["vx_m_s"], u["vy_m_s"])
        result.update(
            distance_m=distance,
            relative_speed_m_s=speed,
            alignment_error_rad=alignment,
            position_uncertainty_m=pos_unc,
            velocity_uncertainty_m_s=vel_unc,
        )
        ok = (
            distance <= d["max_distance_m"]
            and speed <= d["max_relative_speed_m_s"]
            and alignment <= d["max_alignment_error_rad"]
            and pos_unc <= d["max_position_uncertainty_m"]
            and vel_unc <= d["max_velocity_uncertainty_m_s"]
        )
        return {"criteria_met": ok, **result}
