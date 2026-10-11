import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { autoSpeed, createPlayback, createRun, eventText, formatDistance, formatMET, parseFrozenTime } from '../src/mission.js';

const runsDir = new URL('../public/runs/', import.meta.url);
const load = (name) => JSON.parse(readFileSync(new URL(`${name}.json`, runsDir)));
const index = JSON.parse(readFileSync(new URL('index.json', runsDir)));

test('frozen links accept finite seconds, clamp to the run, and reject invalid values', () => {
  assert.equal(parseFrozenTime(null, 500), null);
  assert.equal(parseFrozenTime('', 500), null);
  assert.equal(parseFrozenTime('Infinity', 500), null);
  assert.equal(parseFrozenTime('18junk', 500), null);
  assert.equal(parseFrozenTime('-9', 500), 0);
  assert.equal(parseFrozenTime('900', 500), 500);
  assert.equal(parseFrozenTime('18.5', 500), 18.5);
});

test('formatting', () => {
  assert.equal(formatMET(0), 'T+00:00');
  assert.equal(formatMET(555.2), 'T+09:15');
  assert.equal(formatMET(3725), 'T+1:02:05');
  assert.equal(formatDistance(0.43), '0.4 m');
  assert.equal(formatDistance(42.4), '42 m');
  assert.equal(formatDistance(2500), '2.5 km');
  assert.equal(formatDistance(280400), '280 km');
});

test('the index lists every exported run and each has a text for every event', () => {
  const files = readdirSync(runsDir).filter((f) => f.endsWith('.json') && f !== 'index.json').map((f) => f.slice(0, -5));
  assert.deepEqual(index.map((e) => e.name).sort(), files.sort());
  for (const { name } of index) {
    const data = load(name);
    for (const e of data.events) assert.ok(eventText(e) !== null || e.type === 'llm_advice', `${name}: no text for ${e.type}`);
  }
});

test('samples match the exported truth and interpolate between them', () => {
  const data = load('thruster_fault');
  const run = createRun(data);
  const s = data.samples;
  const i = 400;
  const state = run.sample(s.t[i]);
  assert.equal(state.x, s.x[i]);
  assert.equal(state.y, s.y[i]);
  assert.equal(state.phase, data.phases[s.phase[i]]);
  const mid = run.sample((s.t[i] + s.t[i + 1]) / 2);
  assert.ok(Math.abs(mid.x - (s.x[i] + s.x[i + 1]) / 2) < 1e-9);
  assert.equal(run.duration, data.summary.sim_time_s);
});

test('the run reproduces the summary: fault, phases and final state', () => {
  const run = createRun(load('thruster_fault'));
  assert.deepEqual(run.phaseStarts.map((p) => p.phase), ['DEPART', 'INSPECT', 'INVESTIGATE', 'RETURN', 'APPROACH', 'DOCKED']);
  const fault = run.events.find((e) => e.type === 'thruster_fault');
  assert.equal(fault.source, 'truth');
  assert.equal(run.sample(fault.t - 1).health.left, 1);
  assert.equal(run.sample(fault.t + 1).health.left, 0);
  const end = run.sample(run.duration);
  assert.equal(end.phase, 'DOCKED');
  assert.ok(end.range < 0.5);
  assert.ok(Math.abs(end.fuel - run.data.summary.fuel_remaining_kg) < 0.01);
});

test('the abort run descends to reentry', () => {
  const run = createRun(load('orbital_strike_abort'));
  const end = run.sample(run.duration);
  assert.equal(end.phase, 'ABORT');
  assert.ok(end.alt < 120.5, `final altitude ${end.alt}`);
  assert.ok(run.events.some((e) => e.type === 'reentry' && e.source === 'truth'));
});

test('every run reaches its exported final phase and complete flown track', () => {
  for (const { name } of index) {
    const data = load(name);
    const run = createRun(data);
    const end = run.sample(run.duration);
    assert.equal(end.phase, data.summary.final_phase, name);
    assert.equal(run.sampleIndex(run.duration), data.samples.t.length - 1, name);
  }
});

test('discrete state changes at its sample timestamp, never before it', () => {
  const data = load('thruster_fault');
  const s = data.samples;
  const transition = s.phase.findIndex((phase, i) => i > 0 && phase !== s.phase[i - 1]);
  const run = createRun(data);
  const at = s.t[transition];
  assert.equal(run.sample(at - 0.01).phase, data.phases[s.phase[transition - 1]]);
  assert.equal(run.sample(at).phase, data.phases[s.phase[transition]]);
  assert.equal(run.sampleIndex(at), transition);
});

test('reentry is reachable at the end of abort playback', () => {
  const run = createRun(load('orbital_strike_abort'));
  assert.ok(run.eventsUntil(run.duration).some((event) => event.type === 'reentry'));
});

test('recorded model advice is revealed only after its decision time', () => {
  const data = load('thruster_fault');
  data.events.push({ t: 190, type: 'llm_advice', source: 'controller', point: 'recovery_plan',
    proposal: { mode: 'RETURN', plan: { action: 'RETURN_LOOP' }, rationale: 'Three thrusters remain available.' },
    review: { deterministic_mode: 'RETURN', agrees_with_deterministic: true, checks: {}, failed: [] } });
  const run = createRun(data);
  assert.equal(run.adviceAt(189), null);
  assert.equal(run.adviceAt(190).proposal.mode, 'RETURN');
  assert.equal(run.adviceAt(191).proposal.rationale, 'Three thrusters remain available.');
});

test('auto speed plays each run in about a minute', () => {
  for (const { name, sim_time_s: duration } of index) {
    const loop = duration / autoSpeed(duration);
    assert.ok(loop <= 90, `${name}: ${loop.toFixed(0)} s`);
  }
});

test('playback advances at the chosen speed and loops after the end', () => {
  const playback = createPlayback(100, 0, true, 10);
  playback.tick(0);
  playback.tick(1000);
  assert.equal(playback.time, 10);
  playback.tick(10000);
  assert.equal(playback.time, 100);
  playback.tick(14000);
  assert.equal(playback.time, 0);
});

test('cinematic playback cannot skip a strike window at high speed or after a long frame', () => {
  const playback = createPlayback(500, 150, true, 50);
  playback.windows = [{ start: 157, end: 165 }];
  playback.tick(0);
  playback.tick(1000);
  assert.ok(Math.abs(playback.time - 158.72) < 1e-9);
  playback.seek(150);
  playback.tick(0);
  playback.tick(6000);
  assert.ok(Math.abs(playback.time - 258) < 1e-9);
});

test('cinematic windows affect presentation speed only and support seeking backwards', () => {
  const playback = createPlayback(500, 160, true, 1);
  playback.windows = [{ start: 157, end: 165 }];
  playback.tick(0); playback.tick(1000);
  assert.equal(playback.time, 161);
  playback.speed = 50;
  playback.seek(160);
  playback.tick(0); playback.tick(1000);
  assert.equal(playback.time, 162);
  playback.windows = [];
  playback.tick(2000);
  assert.equal(playback.time, 212);
});
