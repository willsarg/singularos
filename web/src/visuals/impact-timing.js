import { clamp } from '../mission.js';

const smooth = (value) => { const k = clamp(value); return k * k * (3 - 2 * k); };

// The exporter records impulse and spin, not a meteor trajectory or hull damage.
// These bounded visual envelopes illustrate the recorded event only.
export function impactEvents(run) {
  return (run?.data.events ?? []).filter((event) => event.type === 'impact' && event.source === 'truth' && Number.isFinite(event.t));
}

export function impactEnvelope(age) {
  return {
    approach: age > -2.5 && age < 0 ? smooth((age + 2.5) / 0.5) : 0,
    flash: age >= 0 && age < 0.28 ? (1 - age / 0.28) ** 3 : 0,
    burst: age >= 0 && age < 4.8 ? (1 - age / 4.8) ** 2 : 0,
    sparks: age >= 0 && age < 1.6 ? (1 - age / 1.6) ** 2 : 0,
    damage: age >= 0,
    focus: age <= -3 || age >= 6 ? 0 : smooth((age + 3) / 3) * (1 - smooth((age - 2) / 4)),
  };
}
