import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as THREE from 'three';
import { createSpacecraft } from '../src/visuals/spacecraft.js';
import { createStation } from '../src/visuals/station.js';

const textures = { hull: null, foil: null, solar: null };
const components = JSON.parse(readFileSync(new URL('../public/runs/thruster_fault.json', import.meta.url))).station;
const meshes = (object) => {
  const found = [];
  object.traverse((part) => { if (part.isMesh) found.push(part); });
  return found;
};
const dispose = (object) => {
  const geometries = new Set(), materials = new Set();
  for (const mesh of meshes(object)) { geometries.add(mesh.geometry); materials.add(mesh.material); }
  geometries.forEach((geometry) => geometry.dispose());
  materials.forEach((material) => material.dispose());
};

test('detailed station components remain within the simulator planar bounds', () => {
  // Different sizes catch details that accidentally use fixed planar offsets.
  for (const scale of [0.25, 1, 3]) {
    const scaled = components.map((component) => ({
      ...component,
      min_m: component.min_m.map((value) => value * scale),
      max_m: component.max_m.map((value) => value * scale),
    }));
    const station = createStation(scaled, textures);
    assert.equal(station.children.length, scaled.length);
    for (const component of scaled) {
      const group = station.children.find((part) => part.userData.component === component.name);
      assert.ok(group, `${component.name} retains its selection metadata`);
      const bounds = new THREE.Box3().setFromObject(group);
      assert.ok(bounds.min.x >= component.min_m[1] - 1e-5, `${component.name} exceeds min along-track`);
      assert.ok(bounds.max.x <= component.max_m[1] + 1e-5, `${component.name} exceeds max along-track`);
      assert.ok(bounds.min.y >= component.min_m[0] - 1e-5, `${component.name} exceeds min radial`);
      assert.ok(bounds.max.y <= component.max_m[0] + 1e-5, `${component.name} exceeds max radial`);
      assert.ok(bounds.max.x - bounds.min.x >= (component.max_m[1] - component.min_m[1]) * 0.97);
      assert.ok(bounds.max.y - bounds.min.y >= (component.max_m[0] - component.min_m[0]) * 0.97);
    }
    dispose(station);
  }
});

test('station detail uses a bounded number of material batches and finite geometry', () => {
  const station = createStation(components, textures);
  assert.ok(meshes(station).length <= 28, 'fasteners, panels, and truss bays are batched');
  let triangles = 0;
  for (const mesh of meshes(station)) {
    for (const coordinate of mesh.geometry.attributes.position.array) assert.ok(Number.isFinite(coordinate));
    triangles += (mesh.geometry.index?.count ?? mesh.geometry.attributes.position.count) / 3;
    assert.ok(mesh.castShadow && mesh.receiveShadow);
  }
  assert.ok(triangles < 30000, 'station detail stays inside the triangle budget');
  dispose(station);
});

test('wingless vehicle detail preserves its scale and batches its static hardware', () => {
  const { craft, update } = createSpacecraft(textures, { wings: false });
  update(0, {}, false);
  const hardware = craft.children.filter((part) => part.isMesh && !part.material.isShaderMaterial);
  assert.ok(hardware.length <= 7);
  const bounds = new THREE.Box3();
  for (const mesh of hardware) {
    mesh.geometry.computeBoundingBox();
    bounds.union(mesh.geometry.boundingBox);
  }
  assert.ok(bounds.min.x >= -0.57 && bounds.max.x <= 0.57);
  assert.ok(bounds.min.z >= -0.9 && bounds.max.z <= 0.95);
  assert.ok(bounds.max.y <= 1.01);
  const winged = createSpacecraft(textures);
  const wingBounds = new THREE.Box3().setFromObject(winged.craft);
  assert.ok(wingBounds.max.x > 2.6 && wingBounds.min.x < -2.6);
  dispose(craft);
  dispose(winged.craft);
});

test('delivered thruster fractions remain isolated and point opposite their pushes', () => {
  const { craft, update } = createSpacecraft(textures, { wings: false });
  const directions = { forward: [0, 0, -1], backward: [0, 0, 1], left: [-1, 0, 0], right: [1, 0, 0] };
  const groups = Object.fromEntries(Object.keys(directions).map((name) => [name, craft.getObjectByName(`${name} exhaust`)]));
  for (const active of Object.keys(directions)) {
    update(1.25, { [active]: 0.6 }, false);
    for (const [name, group] of Object.entries(groups)) {
      assert.equal(group.visible, name === active);
      const actual = new THREE.Vector3(0, -1, 0).applyQuaternion(group.quaternion);
      assert.ok(actual.distanceTo(new THREE.Vector3(...directions[name])) < 1e-6);
      assert.equal(group.children[0].material.uniforms.time.value, 1.25);
    }
  }
  update(2, {}, true);
  for (const group of Object.values(groups)) assert.equal(group.visible, false);
  assert.ok(craft.children.some((part) => part.isMesh && part.material.isShaderMaterial && part.visible));
  dispose(craft);
});
