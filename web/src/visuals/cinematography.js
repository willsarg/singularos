import { clamp } from '../mission.js';

const ease = (value) => { const k = clamp(value); return k * k * (3 - 2 * k); };
const mix = (a, b, k) => a + (b - a) * k;
const blend = (a, b, k) => a.map((value, i) => mix(value, b[i], k));
const direction = (angle, elevation) => {
  const vector = [Math.sin(angle), elevation, Math.cos(angle)];
  const length = Math.hypot(...vector);
  return vector.map((value) => value / length);
};

// Camera choreography changes only the view. Every pose is a pure function of
// exported mission time, so a paused frame and a sought frame are identical.
export function cameraPose(run, state, { mode = 'cinematic', aspect = 16 / 9, reducedMotion = false } = {}) {
  const portrait = aspect < 1;
  const fov = portrait ? 48 : 39;
  const halfFov = Math.min(fov * Math.PI / 360, Math.atan(Math.tan(fov * Math.PI / 360) * aspect));
  const vehicle = [state.y, state.x, 0];
  const radius = Math.max(portrait ? 30 : 24, state.frame * 1.1);
  const overviewTarget = [vehicle[0] * 0.5 - (portrait ? 0 : radius * 0.12), vehicle[1] * 0.5, 0];
  const overviewDistance = radius / Math.sin(halfFov);
  const wideDirection = direction(0.18, 0.34);
  const overview = {
    position: overviewTarget.map((value, i) => value + wideDirection[i] * overviewDistance),
    target: overviewTarget, fov, detail: 0, title: 'Orbital overview',
  };
  if (mode === 'overview') {
    const { x, y } = run.bounds;
    const target = [(y[0] + y[1]) / 2, (x[0] + x[1]) / 2, 0];
    const fullRadius = Math.max(24, Math.hypot(y[1] - y[0], x[1] - x[0]) * 0.5) * 1.18;
    const distance = fullRadius / Math.sin(halfFov);
    return { ...overview, target, position: target.map((value, i) => value + wideDirection[i] * distance) };
  }

  const phaseIndex = Math.max(0, run.phaseIndex(state.t));
  const phase = run.phaseStarts[phaseIndex];
  const end = run.phaseStarts[phaseIndex + 1]?.t ?? run.duration;
  const scale = run.data.vehicle_radius_m;
  const drift = reducedMotion ? 0 : Math.sin(state.t / Math.max(30, run.duration * 0.18)) * 0.18;
  const recipe = (entry, finish) => {
    const progress = clamp((state.t - entry.t) / Math.max(1, finish - entry.t));
    let angle = 0.5, elevation = 0.28, distance = scale * 8, title = 'Inspection pass', wide = 0;
    let target = [vehicle[0], vehicle[1] - scale * 0.12, 0];
    switch (entry.phase) {
      case 'DEPART':
        title = 'Leaving the station';
        wide = 1 - ease((state.t - entry.t) / Math.min(26, Math.max(1, finish - entry.t)));
        angle = -0.3;
        break;
      case 'INSPECT':
        angle = 0.65 + drift;
        distance = scale * 8;
        break;
      case 'INVESTIGATE':
        title = 'Thruster diagnostics';
        angle = -0.85 + drift;
        distance = scale * 7;
        elevation = 0.18;
        break;
      case 'RETURN':
        title = Object.values(state.thrust).some((thrust) => thrust > 0.01) ? 'Recovery burn' : 'Return flight';
        angle = 0.9 + drift;
        distance = scale * 8;
        wide = reducedMotion ? 0 : Math.max(0, 1 - Math.abs(progress - 0.52) / 0.13) ** 2 * 0.8;
        if (wide > 0.4) title = 'Return corridor';
        break;
      case 'APPROACH':
      case 'DOCKED':
        title = entry.phase === 'DOCKED' ? 'Docking complete' : 'Docking approach';
        angle = -0.45;
        elevation = 0.35;
        distance = Math.max(scale * 8, state.range * 1.6);
        target = [vehicle[0] * 0.8, vehicle[1] * 0.8 - scale * 0.2, 0];
        break;
      case 'HOLD':
        title = 'Safe hold';
        angle = 0.8 + drift;
        distance = scale * 10;
        break;
      case 'ABORT':
        title = state.alt < 160 ? 'Atmospheric descent' : 'Unpowered coast';
        angle = -0.5 + drift;
        elevation = 0.15;
        distance = scale * (state.alt < 160 ? 12 : 9);
        break;
    }
    // Leave more breathing room around the vehicle and its exhaust.
    distance *= (portrait ? 1.3 : 1) * 1.25;
    const view = direction(angle, elevation);
    const closePosition = target.map((value, i) => value + view[i] * distance);
    return {
      position: blend(closePosition, overview.position, wide),
      target: blend(target, overview.target, wide), fov, detail: 1 - wide, title,
    };
  };
  const current = recipe(phase, end);
  if (phaseIndex === 0) return current;
  const previous = recipe(run.phaseStarts[phaseIndex - 1], phase.t);
  const transition = ease((state.t - phase.t) / Math.min(18, Math.max(1, (end - phase.t) * 0.2)));
  return {
    ...current,
    position: blend(previous.position, current.position, transition),
    target: blend(previous.target, current.target, transition),
    detail: mix(previous.detail, current.detail, transition),
  };
}
