// Playback of simulator runs exported by SingularityReturnHome/telemetry/export_web.py.
// Times are simulator seconds. Positions are in the station frame: x radial
// (away from Earth), y along-track, metres.

export const DEFAULT_SCENARIO = 'thruster_fault';
export const TARGET_LOOP_S = 60;
export const SPEEDS = [1, 5, 10, 20, 50];
export const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));
const pct = (value) => `${Math.round(value * 100)}%`;

export function formatMET(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor(s / 60) % 60).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return h ? `T+${h}:${mm}:${ss}` : `T+${mm}:${ss}`;
}

export function formatDistance(metres) {
  if (metres >= 10000) return `${Math.round(metres / 1000).toLocaleString('en-US')} km`;
  if (metres >= 1000) return `${(metres / 1000).toFixed(1)} km`;
  return metres >= 10 ? `${Math.round(metres)} m` : `${metres.toFixed(1)} m`;
}

export function parseFrozenTime(value, duration) {
  if (value === null || value.trim() === '') return null;
  const time = Number(value);
  return Number.isFinite(time) ? clamp(time, 0, duration) : null;
}

// Default playback speed: the slowest listed speed that plays the run in about
// TARGET_LOOP_S seconds.
export function autoSpeed(duration) {
  return SPEEDS.find((speed) => duration / speed <= TARGET_LOOP_S * 1.5) ?? SPEEDS[SPEEDS.length - 1];
}

const EVENT_TEXT = {
  impact: (e) => {
    const dv = Math.hypot(...e.delta_v_m_s);
    return `Impact: ${dv >= 1 ? dv.toFixed(0) : dv.toFixed(2)} m/s, ${e.delta_omega_rad_s.toFixed(2)} rad/s spin`;
  },
  thruster_fault: (e) => (e.effectiveness === 0 ? `${e.thruster} thruster fails` : `${e.thruster} thruster drops to ${pct(e.effectiveness)}`),
  reentry: (e) => `Reentry: altitude ${e.altitude_km.toFixed(0)} km`,
  anomaly_detected: (e) => `Anomaly: motion differs from commands by ${e.residual_m_s.toFixed(2)} m/s`,
  orbital_triage: (e) => `Orbit triage: ${e.decision}. ${e.reasons.join('; ')}`,
  llm_advice: (e) => (e.error ? `LLM advice (${e.point}): unavailable` : `LLM advice (${e.point}, advisory only): ${e.proposal.mode} / ${e.proposal.plan.action}`),
  investigate_start: () => 'Investigating: braking, then testing thrusters',
  diagnostic: (e) => `${e.thruster} thruster test: ${e.estimate === 0 ? 'failed' : pct(e.estimate)} (${e.method})`,
  plan: (e) => `Plan: ${e.decision}. ${e.reasons.join('; ')}`,
  hold_established: (e) => (e.kind === 'safe_orbit' ? `Holding in safe orbit, perigee ${e.orbit.perigee_km.toFixed(0)} km` : 'Holding at safe point'),
  abort: (e) => `Abort: ${e.reason}`,
  abort_safe: () => 'Abort: braked, thrusters off',
  dock_retry: (e) => `Docking retry ${e.retry}`,
  dock_retry_limit: () => 'Docking retries exhausted: hold',
};

export function eventText(event) {
  const text = EVENT_TEXT[event.type];
  return text ? text(event) : null;
}

const PHASE_LABELS = {
  DEPART: 'Depart', INSPECT: 'Inspect', INVESTIGATE: 'Investigate', RETURN: 'Return',
  APPROACH: 'Approach', DOCKED: 'Docked', HOLD: 'Hold', ABORT: 'Abort',
};
export const phaseLabel = (phase) => PHASE_LABELS[phase] ?? phase;

export function createRun(data) {
  const s = data.samples;
  const count = s.t.length;
  const duration = s.t[count - 1];
  const dt = data.sample_period_s;
  // The terminal sample may fall between regular samples. Find the latest
  // actual timestamp so discrete state never appears before it happened.
  const index = (time) => {
    const t = clamp(time, 0, duration);
    let low = 0, high = count;
    while (low < high) {
      const mid = Math.floor((low + high) / 2);
      if (s.t[mid] <= t) low = mid + 1;
      else high = mid;
    }
    return Math.max(0, low - 1);
  };
  const lerp = (field, i, j, k) => (s[field][j] === null || s[field][i] === null ? s[field][i] : s[field][i] + (s[field][j] - s[field][i]) * k);

  // Phase spans: start time of each phase in order of first appearance.
  const phaseStarts = data.phases.map((phase, p) => ({ phase, t: s.t[s.phase.indexOf(p)] }));
  const events = data.events
    .map((e) => ({ ...e, text: eventText(e) }))
    .filter((e) => e.text);
  const impactWindows = [];
  for (const hit of events.filter((e) => e.type === 'impact' && e.source === 'truth').sort((a, b) => a.t - b.t)) {
    const window = { start: Math.max(0, hit.t - 3), end: Math.min(duration, hit.t + 5) };
    const previous = impactWindows.at(-1);
    if (previous && previous.end >= window.start) previous.end = Math.max(previous.end, window.end);
    else impactWindows.push(window);
  }

  // Camera framing radius: the extent of vehicle and station over a window
  // around each sample, so the view eases out before large motions.
  const STATION_RADIUS = Math.max(...data.station.flatMap((c) => [...c.min_m, ...c.max_m].map(Math.abs)));
  const extent = s.t.map((_, i) => Math.hypot(s.x[i], s.y[i]));
  const bounds = { x: [0, 0], y: [0, 0] };
  const include = (x, y) => {
    bounds.x[0] = Math.min(bounds.x[0], x); bounds.x[1] = Math.max(bounds.x[1], x);
    bounds.y[0] = Math.min(bounds.y[0], y); bounds.y[1] = Math.max(bounds.y[1], y);
  };
  data.station.forEach((component) => { include(...component.min_m); include(...component.max_m); });
  s.t.forEach((_, i) => include(s.x[i], s.y[i]));
  const half = Math.round(15 / dt);
  const frame = extent.map((_, i) => {
    let max = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(count - 1, i + half); j++) max = Math.max(max, extent[j]);
    return max + STATION_RADIUS;
  });

  return {
    data, duration, events, phaseStarts, bounds, impactWindows,
    description: data.description,
    sample(time) {
      const t = clamp(time, 0, duration);
      const i = index(t);
      const j = Math.min(i + 1, count - 1);
      const k = j === i ? 0 : clamp((t - s.t[i]) / (s.t[j] - s.t[i]));
      const n = i;
      let heading = s.heading[i];
      let dh = s.heading[j] - heading;
      if (dh > Math.PI) dh -= 2 * Math.PI;
      if (dh < -Math.PI) dh += 2 * Math.PI;
      heading += dh * k;
      return {
        t,
        x: lerp('x', i, j, k), y: lerp('y', i, j, k), heading,
        fuel: lerp('fuel', i, j, k), alt: lerp('alt', i, j, k), perigee: lerp('perigee', i, j, k),
        clear: s.clear[n],
        phase: data.phases[s.phase[n]], leg: data.legs[s.leg[n]],
        nav: s.nav_x[n] === null ? null : { x: s.nav_x[n], y: s.nav_y[n], age: s.nav_age[n] },
        thrust: Object.fromEntries(data.thrusters.map((name, q) => [name, s.thrust[n][q]])),
        health: Object.fromEntries(data.thrusters.map((name, q) => [name, s.health[n][q]])),
        est: Object.fromEntries(data.thrusters.map((name, q) => [name, s.est[n][q]])),
        frame: frame[i] + (frame[j] - frame[i]) * k,
        range: Math.hypot(lerp('x', i, j, k), lerp('y', i, j, k)),
      };
    },
    // Index of the last sample at or before time, for drawing the flown track.
    sampleIndex: index,
    eventsUntil: (time) => events.filter((e) => e.t <= time),
    adviceAt: (time) => events.findLast((e) => e.type === 'llm_advice' && e.t <= time) ?? null,
    phaseIndex(time) {
      return phaseStarts.findLastIndex((p) => time >= p.t);
    },
  };
}

export function createPlayback(duration, initialTime, initiallyPlaying, speed) {
  let time = clamp(initialTime, 0, duration);
  let playing = initiallyPlaying;
  let previous = null;
  return {
    speed,
    windows: [],
    get effectiveSpeed() { return this.windows.some((w) => time >= w.start && time < w.end) ? Math.min(this.speed, 2) : this.speed; },
    get time() { return Math.min(time, duration); },
    get playing() { return playing; },
    tick(now) {
      if (playing && previous !== null) {
        let seconds = Math.max(0, now - previous) / 1000;
        // Integrate through each boundary: a long frame at 50x must not skip
        // the slower collision sequence. Only presentation time is affected.
        for (const window of this.windows) {
          if (time >= window.end) continue;
          if (time < window.start) {
            const before = Math.min(seconds, (window.start - time) / this.speed);
            time += before * this.speed;
            seconds -= before;
          }
          if (time >= window.start && seconds > 0) {
            const speed = Math.min(this.speed, 2);
            const inside = Math.min(seconds, (window.end - time) / speed);
            time += inside * speed;
            seconds -= inside;
          }
          if (seconds <= 0) break;
        }
        time += seconds * this.speed;
        // Hold the final state briefly, then loop.
        if (time > duration + 3 * this.speed) time = 0;
      }
      previous = now;
      return Math.min(time, duration);
    },
    seek(value) { time = clamp(value, 0, duration); previous = null; },
    setPlaying(value) { playing = value; previous = null; },
    resetFrame() { previous = null; },
  };
}
