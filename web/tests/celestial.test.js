import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { createCelestialBackdrop, moonPosition, MOON_RADIUS_M, MOON_DISTANCE_M, SUN_RADIUS_M, SUN_DISTANCE_M } from '../src/visuals/celestial.js';

test('Moon and Sun keep their nominal angular sizes in the background pass', () => {
  const sky = createCelestialBackdrop(new THREE.Texture());
  sky.update(10, new THREE.Vector3(), new THREE.Vector3(), new THREE.PerspectiveCamera());
  assert.ok(Math.abs(sky.moon.geometry.parameters.radius / sky.moon.position.length() - MOON_RADIUS_M / MOON_DISTANCE_M) < 1e-12);
  assert.ok(Math.abs(sky.sun.geometry.parameters.width / 10 / sky.sun.position.length() - SUN_RADIUS_M / SUN_DISTANCE_M) < 1e-12);
});

test('Moon parallax is stable and finite across orbit-scale camera positions', () => {
  const earth = new THREE.Vector3(0, -6.77e6, 0);
  const camera = new THREE.Vector3(280000, -250000, 25);
  const position = moonPosition(earth, camera);
  assert.ok(position.toArray().every(Number.isFinite));
  assert.deepEqual(position.toArray(), moonPosition(earth, camera).toArray());
  const displacement = moonPosition(earth, new THREE.Vector3()).sub(position);
  assert.ok(Math.abs(displacement.length() - camera.length() * 5e-5) < 1e-9);
});
