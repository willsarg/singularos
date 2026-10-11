import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { createImpactEffects, impactEvents, impactEnvelope } from '../src/visuals/impacts.js';

const impact = { type: 'impact', source: 'truth', t: 10, delta_v_m_s: [0, -150], delta_omega_rad_s: 0.05 };
const run = (events = [impact], offset = 0) => ({
  data: { events, vehicle_radius_m: 0.5 },
  sample: (t) => ({ t, x: offset + 2, y: offset + 3 + Math.max(0, t - 10) * -150, heading: 0 }),
});
const quaternion = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(
  new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 0, -1), new THREE.Vector3(0, 1, 0),
));
const snapshot = (effects) => {
  effects.group.updateMatrixWorld(true);
  const result = [];
  effects.group.traverse((object) => result.push({
    name: object.name, visible: object.visible, matrix: object.matrix.toArray(),
    opacity: object.material?.opacity, intensity: object.intensity,
    instances: object.instanceMatrix ? Array.from(object.instanceMatrix.array) : null,
  }));
  return result;
};
const update = (effects, data, time, reducedMotion = false) => {
  const state = data.sample(time);
  effects.update({ time, state, origin: new THREE.Vector3(state.y, state.x, 0), craftQuaternion: quaternion, detail: 1, reducedMotion });
};

test('only recorded truth impacts create visual impact events', () => {
  assert.equal(impactEvents(run([impact, { ...impact, source: 'estimate' }, { ...impact, type: 'thruster_fault' }])).length, 1);
  assert.equal(impactEvents(run([{ ...impact, type: 'thruster_fault' }])).length, 0);
});

test('impact envelope has bounded approach, flash, burst, and persistent damage', () => {
  assert.equal(impactEnvelope(-3).approach, 0);
  assert.ok(impactEnvelope(-0.5).approach > 0);
  assert.equal(impactEnvelope(-0.001).damage, false);
  assert.ok(impactEnvelope(0).flash > 0);
  assert.equal(impactEnvelope(0).damage, true);
  assert.equal(impactEnvelope(8).burst, 0);
  assert.equal(impactEnvelope(8).flash, 0);
  assert.equal(impactEnvelope(1000).damage, true);
});

test('rendered impact and damage are identical after arbitrary seeking', () => {
  const data = run(), effects = createImpactEffects();
  effects.setRun(data);
  update(effects, data, 10.2);
  const expected = snapshot(effects);
  update(effects, data, 200);
  update(effects, data, 0);
  update(effects, data, 10.2);
  assert.deepEqual(snapshot(effects), expected);
  update(effects, data, 9);
  assert.equal(effects.group.getObjectByName('impact-damage-0').visible, false);
  effects.dispose();
});

test('reduced motion hides approach and bursts but preserves a localized damage cue', () => {
  const data = run(), effects = createImpactEffects();
  effects.setRun(data);
  update(effects, data, 10.1, true);
  assert.equal(effects.group.getObjectByName('impact-burst-0').visible, false);
  assert.equal(effects.group.getObjectByName('impact-flash-0').visible, false);
  assert.equal(effects.group.getObjectByName('impact-damage-0').visible, true);
  effects.dispose();
});

test('large orbital coordinates never enter particle instance matrices', () => {
  const data = run([impact], 1e9), effects = createImpactEffects();
  effects.setRun(data);
  update(effects, data, 10.2);
  for (const object of snapshot(effects)) {
    if (object.instances) assert.ok(object.instances.every((value) => Number.isFinite(value) && Math.abs(value) < 100));
  }
  effects.setRun(run([]));
  assert.equal(effects.group.children.length, 0);
  effects.dispose();
});

test('damage follows attitude while illustrative debris inherits only craft translation', () => {
  const data = run(), effects = createImpactEffects();
  effects.setRun(data);
  update(effects, data, 10.1);
  const damage = effects.group.getObjectByName('impact-damage-0');
  const burstAtHit = effects.group.getObjectByName('impact-burst-0').position.clone();
  const local = damage.position.clone().applyQuaternion(quaternion.clone().invert());
  const rotation = quaternion.clone().multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), 1));
  const state = data.sample(10.2);
  effects.update({ time: 10.2, state, origin: new THREE.Vector3(state.y, state.x, 0), craftQuaternion: rotation });
  assert.ok(damage.position.distanceTo(local.applyQuaternion(rotation)) < 1e-10);
  const debris = effects.group.getObjectByName('impact-burst-0');
  assert.ok(debris.position.distanceTo(burstAtHit) < 1e-10, 'co-moving debris should retain the strike direction rather than rotate with the craft');
  effects.dispose();
});

test('impulse direction controls the approach side and bounded emphasis', () => {
  const left = createImpactEffects(), right = createImpactEffects();
  const a = run([{ ...impact, delta_v_m_s: [0, -0.12] }]);
  const b = run([{ ...impact, delta_v_m_s: [0, 300] }]);
  left.setRun(a); right.setRun(b);
  update(left, a, 10); update(right, b, 10);
  assert.ok(left.group.getObjectByName('impact-damage-0').position.x > 0);
  assert.ok(right.group.getObjectByName('impact-damage-0').position.x < 0);
  assert.ok(left.group.getObjectByName('impact-flash-0').scale.x < right.group.getObjectByName('impact-flash-0').scale.x);
  left.dispose(); right.dispose();
});

test('run switches dispose impact geometries, materials, and instance buffers', () => {
  const effects = createImpactEffects();
  effects.setRun(run());
  let geometryDisposals = 0, materialDisposals = 0, instanceDisposals = 0;
  effects.group.traverse((object) => {
    object.geometry?.addEventListener('dispose', () => geometryDisposals++);
    object.material?.addEventListener('dispose', () => materialDisposals++);
    if (object.isInstancedMesh) object.addEventListener('dispose', () => instanceDisposals++);
  });
  effects.setRun(run([]));
  assert.equal(geometryDisposals, 6);
  assert.equal(materialDisposals, 6);
  assert.equal(instanceDisposals, 2);
  assert.equal(effects.group.children.length, 0);
  effects.dispose();
});
