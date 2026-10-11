import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

function plume(length, radius, tint) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius * 0.08, length, 32, 12, true), new THREE.ShaderMaterial({
    uniforms: { time: { value: 0 }, tint: { value: new THREE.Color(tint) }, wisps: { value: 0 } },
    vertexShader: `varying vec2 coord; void main() {
      coord = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }`,
    fragmentShader: `uniform float time, wisps; uniform vec3 tint; varying vec2 coord;
      void main() {
        float axial = coord.y;
        float bands = 0.72 + 0.28 * sin(axial * 42.0 - time * 17.0);
        float pulse = 0.9 + 0.1 * sin(time * 41.0 + coord.x * 12.0);
        float strands = pow(0.5 + 0.5 * sin(coord.x * 56.55 + sin(axial * 11.0 - time * 1.4)), 18.0);
        float alpha = pow(axial, 0.65) * bands * pulse * 0.55 * mix(1.0, strands, wisps);
        gl_FragColor = vec4(tint * (1.3 + axial * 1.7), alpha);
      }`,
    transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending,
  }));
  mesh.position.y = -length / 2;
  return mesh;
}

// A group of plumes whose nozzle sits at origin and whose exhaust points along
// direction; scaling the group's y axis shortens the exhaust.
function jet(origin, direction, plumes) {
  const group = new THREE.Group();
  group.position.set(...origin);
  group.quaternion.setFromUnitVectors(new THREE.Vector3(0, -1, 0), new THREE.Vector3(...direction));
  for (const [length, radius, tint] of plumes) group.add(plume(length, radius, tint));
  return group;
}

export function createSpacecraft(textures, { wings = true } = {}) {
  const craft = new THREE.Group();
  const materials = {
    hull: new THREE.MeshStandardMaterial({ map: textures.hull, color: 0xe7e5df, metalness: 0.48, roughness: 0.38 }),
    silver: new THREE.MeshStandardMaterial({ color: 0xbcc5cd, metalness: 0.85, roughness: 0.25, side: THREE.DoubleSide }),
    dark: new THREE.MeshStandardMaterial({ color: 0x17202d, metalness: 0.68, roughness: 0.4 }),
    foil: new THREE.MeshStandardMaterial({ color: 0xd5a453, metalness: 0.88, roughness: 0.42, bumpMap: textures.foil, bumpScale: 0.045 }),
    solar: new THREE.MeshPhysicalMaterial({ map: textures.solar, color: 0xcedfff, metalness: 0.6, roughness: 0.28, clearcoat: 0.8, clearcoatRoughness: 0.2 }),
    glass: new THREE.MeshPhysicalMaterial({ color: 0x102c40, metalness: 0.65, roughness: 0.08, clearcoat: 1 }),
    indicator: new THREE.MeshBasicMaterial({ color: new THREE.Color(0.18, 1.8, 1.55) }),
  };
  const parts = new Map(Object.keys(materials).map((name) => [name, []]));
  const transform = new THREE.Object3D();
  const add = (geometry, material, position = [0, 0, 0], rotation = [0, 0, 0]) => {
    transform.position.set(...position);
    transform.rotation.set(...rotation);
    transform.updateMatrix();
    // Merge static details by material: hundreds of parts need only seven draws.
    const prepared = geometry.index ? geometry.toNonIndexed() : geometry.clone();
    geometry.dispose();
    parts.get(material).push(prepared.applyMatrix4(transform.matrix));
  };
  const box = (size, material, position, rotation) => {
    const geometry = Math.min(...size) > 0.08
      ? new RoundedBoxGeometry(...size, 2, Math.min(...size) * 0.12)
      : new THREE.BoxGeometry(...size);
    add(geometry, material, position, rotation);
  };
  const cylinder = (top, bottom, length, material, position, rotation = [Math.PI / 2, 0, 0], segments = 48) =>
    add(new THREE.CylinderGeometry(top, bottom, length, segments), material, position, rotation);
  const ring = (radius, thickness, material, position, rotation) =>
    add(new THREE.TorusGeometry(radius, thickness, 8, 48), material, position, rotation);
  const pipe = (start, end, radius, material = 'silver') => {
    const a = new THREE.Vector3(...start), b = new THREE.Vector3(...end);
    const direction = b.clone().sub(a);
    const geometry = new THREE.CylinderGeometry(radius, radius, direction.length(), 8);
    geometry.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()));
    add(geometry, material, a.add(b).multiplyScalar(0.5).toArray());
  };

  cylinder(0.42, 0.42, 0.92, 'foil', [0, 0, -0.18]);
  // Overlapping thermal blankets expose physical seams under grazing light.
  for (let row = 0; row < 3; row++) for (let panel = 0; panel < 12; panel++) {
    const angle = panel * Math.PI / 6;
    add(new THREE.CylinderGeometry(0.426, 0.426, 0.286, 8, 1, true, angle + 0.018, Math.PI / 6 - 0.036),
      'foil', [0, 0, -0.48 + row * 0.3], [Math.PI / 2, 0, 0]);
    const seamAngle = angle + Math.PI / 12;
    for (const dz of [-0.12, 0.12]) {
      add(new THREE.SphereGeometry(0.011, 6, 4), 'dark',
        [Math.sin(seamAngle) * 0.432, Math.cos(seamAngle) * 0.432, -0.48 + row * 0.3 + dz]);
    }
  }
  cylinder(0.43, 0.45, 0.22, 'dark', [0, 0, -0.65]);
  cylinder(0.39, 0.42, 0.26, 'hull', [0, 0, 0.39]);
  const capsuleProfile = [[0.39, 0], [0.39, 0.08], [0.35, 0.2], [0.28, 0.31], [0.14, 0.4], [0.035, 0.42]];
  add(new THREE.LatheGeometry(capsuleProfile.map(([x, y]) => new THREE.Vector2(x, y)), 64), 'hull', [0, 0, 0.51], [Math.PI / 2, 0, 0]);
  cylinder(0.17, 0.17, 0.08, 'dark', [0, 0, 0.88]);
  ring(0.175, 0.018, 'silver', [0, 0, 0.93]);
  cylinder(0.12, 0.12, 0.012, 'glass', [0, 0, 0.933]);
  for (let i = 0; i < 8; i++) {
    const angle = i * Math.PI / 4;
    const x = Math.sin(angle), y = Math.cos(angle);
    box([0.035, 0.075, 0.022], 'dark', [x * 0.149, y * 0.149, 0.916], [0, 0, -angle]);
    add(new THREE.SphereGeometry(0.01, 6, 4), 'silver', [x * 0.158, y * 0.158, 0.937]);
    // Capsule panel joints follow the taper towards the optical docking head.
    pipe([x * 0.379, y * 0.379, 0.57], [x * 0.271, y * 0.271, 0.8], 0.006, 'dark');
  }
  for (const z of [-0.6, -0.38, 0.05, 0.27, 0.52]) ring(0.423, 0.018, 'silver', [0, 0, z]);
  for (let i = 0; i < 12; i++) {
    const a = i / 12 * Math.PI * 2;
    box([0.025, 0.025, 0.91], 'silver', [Math.sin(a) * 0.425, Math.cos(a) * 0.425, -0.18]);
    for (const z of [-0.59, 0.28]) add(new THREE.SphereGeometry(0.022, 8, 6), 'dark', [Math.sin(a) * 0.438, Math.cos(a) * 0.438, z]);
  }
  // Radiator fins, avionics housings, and optical instruments.
  for (const side of [-1, 1]) {
    box([0.12, 0.53, 0.62], 'dark', [side * 0.43, 0, -0.15]);
    for (let i = 0; i < 10; i++) box([0.14, 0.032, 0.6], 'silver', [side * 0.49, -0.23 + i * 0.05, -0.15]);
    box([0.21, 0.16, 0.24], 'foil', [side * 0.24, 0.39, 0.15]);
    cylinder(0.065, 0.065, 0.17, 'dark', [side * 0.24, 0.43, 0.35]);
    cylinder(0.048, 0.048, 0.01, 'glass', [side * 0.24, 0.43, 0.44]);
    box([0.035, 0.025, 0.025], 'indicator', [side * 0.2, 0.468, 0.24]);
    // Exposed service loops, mounting feet, and sunshaded star trackers.
    for (const y of [-0.17, 0.17]) {
      pipe([side * 0.44, y, -0.47], [side * 0.53, y, -0.4], 0.012, 'foil');
      pipe([side * 0.53, y, -0.4], [side * 0.53, y, 0.12], 0.012, 'foil');
      pipe([side * 0.53, y, 0.12], [side * 0.44, y, 0.18], 0.012, 'foil');
      for (const z of [-0.35, -0.1, 0.1]) box([0.042, 0.046, 0.032], 'dark', [side * 0.53, y, z]);
    }
    cylinder(0.07, 0.07, 0.08, 'silver', [side * 0.24, 0.43, 0.413]);
    cylinder(0.061, 0.061, 0.045, 'dark', [side * 0.24, 0.43, 0.439]);
    for (const dx of [-0.048, 0.048]) box([0.018, 0.022, 0.07], 'silver', [side * 0.24 + dx, 0.39, 0.405]);
    // Hinged booms and three independently framed panels per wing.
    for (let section = 0; wings && section < 3; section++) {
      if (section === 0) {
        cylinder(0.055, 0.055, 0.5, 'silver', [side * 0.58, 0, -0.12], [0, 0, Math.PI / 2]);
        cylinder(0.09, 0.09, 0.13, 'dark', [side * 0.83, 0, -0.12], [0, 0, Math.PI / 2]);
      }
      const x = side * (1.15 + section * 0.59);
      box([0.575, 0.98, 0.04], 'silver', [x, 0, -0.12]);
      box([0.541, 0.941, 0.047], 'solar', [x, 0, -0.12]);
      for (const y of [-0.47, 0.47]) box([0.575, 0.018, 0.052], 'silver', [x, y, -0.12]);
      for (const dx of [-0.278, 0.278]) box([0.016, 0.98, 0.052], 'silver', [x + dx, 0, -0.12]);
    }
    // Small attitude-control nozzles around the aft structure.
    for (const y of [-0.28, 0.28]) {
      box([0.12, 0.13, 0.12], 'foil', [side * 0.39, y, -0.54]);
      cylinder(0.035, 0.065, 0.1, 'dark', [side * 0.45, y, -0.58], [0, 0, side * Math.PI / 2]);
    }
    // The lateral delivered-thrust plumes originate at these central bells.
    box([0.09, 0.17, 0.16], 'foil', [side * 0.405, 0, -0.56]);
    cylinder(0.066, 0.03, 0.1, 'dark', [side * 0.45, 0, -0.56], [0, 0, -side * Math.PI / 2], 24);
    ring(0.061, 0.007, 'silver', [side * 0.495, 0, -0.56], [0, Math.PI / 2, 0]);
    pipe([side * 0.39, -0.24, -0.55], [side * 0.39, 0.24, -0.55], 0.011);
  }
  const engineProfile = [[0.095, 0.12], [0.1, 0.04], [0.13, -0.02], [0.2, -0.11], [0.245, -0.22]];
  add(new THREE.LatheGeometry(engineProfile.map(([x, y]) => new THREE.Vector2(x, y)), 48), 'dark', [0, 0, -0.66], [Math.PI / 2, 0, 0]);
  ring(0.243, 0.014, 'silver', [0, 0, -0.88]);
  for (let i = 0; i < 3; i++) ring(0.17 + i * 0.026, 0.008, 'silver', [0, 0, -0.77 - i * 0.04]);
  // Regenerative cooling ribs and feed lines around the main engine skirt.
  for (let i = 0; i < 24; i++) {
    const angle = i * Math.PI / 12;
    const x = Math.sin(angle), y = Math.cos(angle);
    pipe([x * 0.105, y * 0.105, -0.63], [x * 0.142, y * 0.142, -0.73], 0.006);
    pipe([x * 0.142, y * 0.142, -0.73], [x * 0.237, y * 0.237, -0.867], 0.006);
    if (i % 4 === 0) pipe([x * 0.36, y * 0.36, -0.56], [x * 0.13, y * 0.13, -0.73], 0.014);
  }
  cylinder(0.085, 0.085, 0.09, 'silver', [0, 0, -0.65]);
  ring(0.087, 0.009, 'foil', [0, 0, -0.69]);
  // Communication dish, feed, and two instrument antennae.
  cylinder(0.025, 0.045, 0.27, 'silver', [0, 0.5, -0.19], [0, 0, 0]);
  const dishProfile = [[0.01, -0.045], [0.07, -0.04], [0.14, -0.01], [0.22, 0.04], [0.27, 0.085]];
  add(new THREE.LatheGeometry(dishProfile.map(([x, y]) => new THREE.Vector2(x, y)), 48), 'silver', [0, 0.66, -0.19]);
  cylinder(0.009, 0.009, 0.24, 'silver', [0, 0.76, -0.19], [0, 0, 0]);
  add(new THREE.SphereGeometry(0.03, 12, 8), 'dark', [0, 0.89, -0.19]);
  for (let i = 0; i < 3; i++) {
    const angle = i * Math.PI * 2 / 3;
    pipe([Math.sin(angle) * 0.23, 0.725, -0.19 + Math.cos(angle) * 0.23], [0, 0.86, -0.19], 0.007);
  }
  for (const side of [-1, 1]) {
    cylinder(0.009, 0.014, 0.66, 'silver', [side * 0.3, 0.67, -0.42], [0, 0, side * -0.2]);
    add(new THREE.SphereGeometry(0.017, 8, 6), 'indicator', [side * 0.365, 0.99, -0.42]);
  }
  for (const [name, geometries] of parts) {
    if (!geometries.length) continue;
    const geometry = mergeGeometries(geometries);
    for (const part of geometries) part.dispose();
    const mesh = new THREE.Mesh(geometry, materials[name]);
    mesh.name = `Spacecraft ${name}`;
    mesh.castShadow = mesh.receiveShadow = true;
    craft.add(mesh);
  }

  // One jet per sim thruster. Model +z is the nose (body +x) and model +x is
  // the body's left side (body +y); each jet exhausts opposite its push.
  const jets = {
    forward: jet([0, 0, -0.87], [0, 0, -1], [[2.2, 0.23, 0x51a9ff], [1.4, 0.09, 0xd4eaff]]),
    backward: jet([0, 0, 0.94], [0, 0, 1], [[0.9, 0.1, 0x51a9ff], [0.6, 0.04, 0xd4eaff]]),
    left: jet([-0.5, 0, -0.56], [-1, 0, 0], [[0.9, 0.1, 0x51a9ff], [0.6, 0.04, 0xd4eaff]]),
    right: jet([0.5, 0, -0.56], [1, 0, 0], [[0.9, 0.1, 0x51a9ff], [0.6, 0.04, 0xd4eaff]]),
  };
  for (const [name, group] of Object.entries(jets)) group.name = `${name} exhaust`;
  craft.add(...Object.values(jets));
  const plumes = Object.values(jets).flatMap((group) => group.children);
  const engineLight = new THREE.PointLight(0x75baff, 0, 3, 2);
  engineLight.position.z = -1.1;
  craft.add(engineLight);
  const heat = new THREE.Mesh(new THREE.SphereGeometry(0.5, 32, 24), new THREE.ShaderMaterial({
    uniforms: { time: { value: 0 } },
    vertexShader: `varying vec3 localPosition, viewNormal, viewPosition;
      void main() {
        localPosition = position;
        vec4 p = modelViewMatrix * vec4(position, 1.0);
        viewPosition = p.xyz; viewNormal = normalize(normalMatrix * normal);
        gl_Position = projectionMatrix * p;
      }`,
    fragmentShader: `uniform float time; varying vec3 localPosition, viewNormal, viewPosition;
      void main() {
        float front = smoothstep(-0.18, 0.3, localPosition.z);
        float rim = pow(1.0 - abs(dot(normalize(viewNormal), normalize(-viewPosition))), 2.4);
        float ripple = 0.8 + 0.2 * sin(localPosition.y * 40.0 - time * 23.0);
        gl_FragColor = vec4(mix(vec3(3.0, 0.08, 0.01), vec3(3.5, 0.8, 0.12), front), front * rim * ripple * 0.5);
      }`,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  heat.scale.set(1.05, 1.05, 1.1);
  heat.position.z = 0.42;
  craft.add(heat);
  const plasma = jet([0, 0, 0.92], [0, 0, -1], [[2.0, 0.46, 0xff6224]]);
  plasma.children[0].material.uniforms.wisps.value = 1;
  const heatLight = new THREE.PointLight(0xff6426, 0, 2, 2);
  heatLight.position.z = 0.75;
  craft.add(plasma, heatLight);
  return {
    craft,
    // thrust: delivered thrust per thruster as a fraction of its maximum.
    update(time, thrust, reentry) {
      for (const [name, group] of Object.entries(jets)) {
        const level = thrust[name] ?? 0;
        group.visible = level > 0.01;
        group.scale.set(0.6 + 0.4 * level, (0.35 + 0.65 * level) * (1 + 0.045 * Math.sin(time * 73)), 0.6 + 0.4 * level);
      }
      for (const mesh of plumes) mesh.material.uniforms.time.value = time;
      engineLight.intensity = 2.3 * (thrust.forward ?? 0);
      heat.visible = reentry;
      heat.material.uniforms.time.value = time;
      plasma.visible = reentry;
      plasma.children[0].material.uniforms.time.value = time;
      heatLight.intensity = reentry ? 1.5 : 0;
    },
  };
}
