import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRun } from '../src/mission.js';
import { cameraPose } from '../src/visuals/cinematography.js';

const load = (name) => createRun(JSON.parse(readFileSync(new URL(`../public/runs/${name}.json`, import.meta.url))));
const position = (state) => [state.y, state.x, 0];
const distance = (a, b) => Math.hypot(...a.map((value, i) => value - b[i]));

test('cinematic inspection brings the actual vehicle into a close tracking shot', () => {
  const run = load('thruster_fault');
  const state = run.sample(120);
  const pose = cameraPose(run, state, { aspect: 16 / 9 });
  assert.ok(distance(pose.target, position(state)) < 1);
  assert.ok(distance(pose.position, position(state)) < 8);
  assert.equal(pose.title, 'Inspection pass');
});

test('overview keeps the vehicle and station within the framing radius', () => {
  const run = load('orbital_strike_abort');
  const state = run.sample(run.duration);
  const pose = cameraPose(run, state, { mode: 'overview', aspect: 16 / 9 });
  const framingRadius = distance(pose.position, pose.target) * Math.sin(pose.fov * Math.PI / 360);
  assert.ok(framingRadius >= distance(position(state), pose.target));
  assert.ok(framingRadius >= distance([0, -15, 0], pose.target));
});

test('camera transitions do not jump at phase boundaries', () => {
  const run = load('thruster_fault');
  for (const { t } of run.phaseStarts.slice(1)) {
    const before = cameraPose(run, run.sample(t - 0.001), { aspect: 16 / 9 });
    const after = cameraPose(run, run.sample(t + 0.001), { aspect: 16 / 9 });
    assert.ok(distance(before.position, after.position) < 0.1, `camera jumps at ${t}`);
    assert.ok(distance(before.target, after.target) < 0.1, `target jumps at ${t}`);
  }
});

test('overview retains the entire flown orbit-scale path after docking', () => {
  const run = load('orbital_strike_return');
  const pose = cameraPose(run, run.sample(run.duration), { mode: 'overview', aspect: 16 / 9 });
  const radius = distance(pose.position, pose.target) * Math.sin(pose.fov * Math.PI / 360);
  const { x, y } = run.data.samples;
  for (let i = 0; i < x.length; i++) {
    assert.ok(distance([y[i], x[i], 0], pose.target) < radius, `path point ${i} is outside the view`);
  }
});

test('camera frames are reproducible after seeking across the run', () => {
  const run = load('orbital_strike_return');
  const expected = cameraPose(run, run.sample(460), { aspect: 16 / 9 });
  cameraPose(run, run.sample(run.duration), { aspect: 16 / 9 });
  cameraPose(run, run.sample(0), { aspect: 16 / 9 });
  assert.deepEqual(cameraPose(run, run.sample(460), { aspect: 16 / 9 }), expected);
});

test('portrait and reduced-motion views retain finite camera frames', () => {
  for (const name of ['thruster_fault', 'orbital_strike_abort']) {
    const run = load(name);
    for (const time of [0, 160, run.duration]) {
      const pose = cameraPose(run, run.sample(time), { aspect: 390 / 844, reducedMotion: true });
      assert.ok([...pose.position, ...pose.target, pose.fov].every(Number.isFinite));
      assert.ok(distance(pose.position, pose.target) > run.data.vehicle_radius_m * 2);
    }
  }
});
