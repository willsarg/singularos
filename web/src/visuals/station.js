import * as THREE from 'three';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

// Component footprints use the simulator's metres. Surface equipment projects
// into orbit-normal depth; the orbital-plane silhouettes stay inside each box.
export function createStation(components, textures) {
  const station = new THREE.Group();
  station.name = 'Orbital station';
  const materials = {
    hull: new THREE.MeshStandardMaterial({ map: textures.hull, color: 0xe7e5df, metalness: 0.48, roughness: 0.38 }),
    foil: new THREE.MeshStandardMaterial({ color: 0xd5a453, metalness: 0.88, roughness: 0.42, bumpMap: textures.foil, bumpScale: 0.045 }),
    silver: new THREE.MeshStandardMaterial({ color: 0xbcc5cd, metalness: 0.85, roughness: 0.25 }),
    dark: new THREE.MeshStandardMaterial({ color: 0x15212b, metalness: 0.65, roughness: 0.46 }),
    solar: new THREE.MeshPhysicalMaterial({ map: textures.solar, color: 0xcedfff, metalness: 0.6, roughness: 0.28, clearcoat: 0.8, clearcoatRoughness: 0.2 }),
    radiator: new THREE.MeshStandardMaterial({ color: 0xdfe6ea, metalness: 0.2, roughness: 0.55 }),
    port: new THREE.MeshBasicMaterial({ color: new THREE.Color(0.18, 1.8, 1.55) }),
  };
  for (const component of components) {
    const [x0, y0] = component.min_m;
    const [x1, y1] = component.max_m;
    const width = y1 - y0, height = x1 - x0;
    const unit = Math.min(width, height);
    const group = new THREE.Group();
    group.name = component.name;
    group.userData.component = component.name;
    group.position.set((y0 + y1) / 2, (x0 + x1) / 2, 0);
    const parts = new Map(Object.keys(materials).map((name) => [name, []]));
    const transform = new THREE.Object3D();
    const add = (geometry, material, position = [0, 0, 0], rotation = [0, 0, 0], scale = [1, 1, 1]) => {
      transform.position.set(...position);
      transform.rotation.set(...rotation);
      transform.scale.set(...scale);
      transform.updateMatrix();
      const prepared = geometry.index ? geometry.toNonIndexed() : geometry.clone();
      geometry.dispose();
      parts.get(material).push(prepared.applyMatrix4(transform.matrix));
    };
    const box = (size, material, position, rotation) => add(new THREE.BoxGeometry(...size), material, position, rotation);
    const cylinder = (radius, length, material, position, rotation = [0, 0, 0], open = false) =>
      add(new THREE.CylinderGeometry(radius, radius, length, 48, 1, open), material, position, rotation);
    const ring = (radius, thickness, material, position, rotation = [Math.PI / 2, 0, 0]) =>
      add(new THREE.TorusGeometry(radius, thickness, 6, 48), material, position, rotation);
    const beam = (start, end, radius, material = 'silver') => {
      const a = new THREE.Vector3(...start), b = new THREE.Vector3(...end);
      const direction = b.clone().sub(a);
      const geometry = new THREE.CylinderGeometry(radius, radius, direction.length(), 8);
      geometry.applyQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize()));
      add(geometry, material, a.add(b).multiplyScalar(0.5).toArray());
    };
    if (component.type === 'hull') {
      const radius = width / 2;
      cylinder(radius, height, 'hull');
      // Pressure-shell bands, welded stringers, and recessed end hardware.
      for (let i = 1; i < 6; i++) {
        const y = -height / 2 + height * i / 6;
        ring(radius - unit * 0.008, unit * 0.008, 'silver', [0, y, 0]);
        for (let j = 0; j < 16; j++) {
          const angle = j * Math.PI / 8;
          add(new THREE.SphereGeometry(unit * 0.009, 6, 4), 'dark',
            [Math.sin(angle) * (radius - unit * 0.009), y, Math.cos(angle) * radius]);
        }
      }
      for (let i = 0; i < 12; i++) {
        const angle = i * Math.PI / 6;
        const r = radius - unit * 0.012;
        beam([Math.sin(angle) * r, -height * 0.47, Math.cos(angle) * r],
          [Math.sin(angle) * r, height * 0.47, Math.cos(angle) * r], unit * 0.012);
      }
      for (const y of [-height * 0.46, height * 0.46]) {
        cylinder(radius * 0.84, unit * 0.03, 'foil', [0, y, 0]);
        ring(radius * 0.84, unit * 0.012, 'silver', [0, y, 0]);
      }
      // Distinct equipment decks on both depth faces remain in the hull's XY box.
      for (const side of [-1, 1]) {
        for (let row = 0; row < 3; row++) {
          const y = height * (-0.28 + row * 0.28);
          box([width * 0.3, height * 0.17, unit * 0.08], 'dark', [0, y, side * radius]);
          box([width * 0.27, height * 0.15, unit * 0.085], row === 1 ? 'foil' : 'hull', [0, y, side * (radius + unit * 0.025)]);
          for (const x of [-width * 0.124, width * 0.124]) {
            beam([x, y - height * 0.064, side * (radius + unit * 0.08)],
              [x, y + height * 0.064, side * (radius + unit * 0.08)], unit * 0.008);
          }
        }
        for (const x of [-width * 0.26, width * 0.26]) {
          beam([x, -height * 0.4, side * radius * 0.9], [x, height * 0.4, side * radius * 0.9], unit * 0.022, 'foil');
          for (let i = 0; i < 9; i++) box([unit * 0.1, unit * 0.045, unit * 0.06], 'silver', [x, height * (-0.38 + i * 0.095), side * radius * 0.9]);
        }
        // Optical navigation instruments with sunshades and glass-like interiors.
        for (const x of [-width * 0.31, width * 0.31]) {
          const z = side * radius * 0.82;
          box([unit * 0.13, unit * 0.13, unit * 0.17], 'foil', [x, height * 0.38, z]);
          cylinder(unit * 0.05, unit * 0.18, 'silver', [x, height * 0.38, z + side * unit * 0.12], [Math.PI / 2, 0, 0], true);
          cylinder(unit * 0.046, unit * 0.01, 'dark', [x, height * 0.38, z + side * unit * 0.19], [Math.PI / 2, 0, 0]);
        }
      }
    } else if (component.type === 'docking_port') {
      const radius = width / 2;
      cylinder(radius, height, 'foil', [0, 0, 0], [0, 0, 0], true);
      cylinder(radius * 0.78, height * 0.9, 'dark', [0, -height * 0.025, 0], [0, 0, 0], true);
      cylinder(radius * 0.77, height * 0.05, 'dark', [0, -height * 0.45, 0]);
      for (const y of [-height * 0.4, height * 0.08, height * 0.43]) {
        ring(radius - unit * 0.04, unit * 0.04, 'silver', [0, y, 0]);
        ring(radius * 0.79, unit * 0.027, 'silver', [0, y, 0]);
      }
      // Guide petals and locking jaws make the aperture legible at approach.
      for (let i = 0; i < 12; i++) {
        const angle = i * Math.PI / 6;
        const x = Math.sin(angle) * radius * 0.87, z = Math.cos(angle) * radius * 0.87;
        box([unit * 0.09, height * 0.18, unit * 0.13], 'dark', [x, height * 0.36, z], [0, angle, 0]);
        box([unit * 0.045, height * 0.12, unit * 0.15], 'silver', [x, height * 0.35, z], [0, angle, 0]);
        add(new THREE.SphereGeometry(unit * 0.025, 8, 6), 'silver', [x, -height * 0.12, z]);
        if (i % 3 === 0) box([unit * 0.055, height * 0.025, unit * 0.035], 'port', [x, height * 0.45, z], [0, angle, 0]);
      }
      ring(radius * 0.775, unit * 0.012, 'port', [0, height * 0.47, 0]);
    } else if (component.type === 'solar_array') {
      const rim = unit * 0.016;
      box([width, height, 0.12], 'dark', [0, 0, 0]);
      const columns = 4, rows = 5;
      const tileWidth = (width - rim * 2) / columns, tileHeight = (height - rim * 2) / rows;
      for (let col = 0; col < columns; col++) for (let row = 0; row < rows; row++) {
        const x = -width / 2 + rim + tileWidth * (col + 0.5);
        const y = -height / 2 + rim + tileHeight * (row + 0.5);
        box([tileWidth - rim * 0.25, tileHeight - rim * 0.25, 0.035], 'solar', [x, y, 0.08]);
      }
      for (const y of [-height / 2 + rim / 2, height / 2 - rim / 2]) box([width, rim, 0.2], 'silver', [0, y, 0]);
      for (const x of [-width / 2 + rim / 2, width / 2 - rim / 2]) box([rim, height - rim * 2, 0.2], 'silver', [x, 0, 0]);
      for (let col = 1; col < columns; col++) box([rim * 0.3, height - rim * 2, 0.15], 'silver', [-width / 2 + rim + col * tileWidth, 0, 0.04]);
      // Back structure and cable raceways add parallax without growing XY.
      for (let row = 0; row < rows; row++) {
        const y = -height / 2 + rim + tileHeight * (row + 0.5);
        beam([-width * 0.48, y - tileHeight * 0.4, -0.15], [width * 0.48, y + tileHeight * 0.4, -0.15], rim * 0.2);
        box([width * 0.96, rim * 0.55, 0.17], 'silver', [0, y, -0.14]);
      }
      box([rim * 0.6, height * 0.96, 0.12], 'foil', [0, 0, -0.26]);
    } else if (component.type === 'truss') {
      const rod = unit * 0.055;
      const x = width / 2 - rod, y = height / 2 - rod, z = unit * 0.42;
      for (const sy of [-1, 1]) for (const sz of [-1, 1]) beam([-x, sy * y, sz * z], [x, sy * y, sz * z], rod);
      const bays = Math.max(2, Math.ceil(width / unit));
      for (let bay = 0; bay <= bays; bay++) {
        const bx = -x + 2 * x * bay / bays;
        for (const sz of [-1, 1]) beam([bx, -y, sz * z], [bx, y, sz * z], rod * 0.6);
        for (const sy of [-1, 1]) beam([bx, sy * y, -z], [bx, sy * y, z], rod * 0.6);
        if (bay < bays) {
          const next = -x + 2 * x * (bay + 1) / bays;
          for (const sz of [-1, 1]) beam([bx, -y, sz * z], [next, y, sz * z], rod * 0.65);
          for (const sy of [-1, 1]) beam([bx, sy * y, -z], [next, sy * y, z], rod * 0.65);
        }
      }
      beam([-x, 0, 0], [x, 0, 0], rod * 1.25, 'foil');
      for (const sx of [-1, 1]) box([rod * 1.7, height, unit], 'dark', [sx * (width / 2 - rod), 0, 0]);
    } else if (component.type === 'radiator') {
      box([width, height, 0.18], 'dark', [0, 0, 0]);
      const rim = unit * 0.045;
      const panels = 6;
      for (let i = 0; i < panels; i++) {
        const x = -width / 2 + width * (i + 0.5) / panels;
        box([width / panels - rim * 0.6, height - rim * 2, 0.08], 'radiator', [x, 0, 0.13]);
      }
      for (const y of [-height / 2 + rim / 2, height / 2 - rim / 2]) box([width, rim, 0.24], 'silver', [0, y, 0]);
      for (let i = 0; i < 48; i++) {
        const x = -width * 0.49 + width * 0.98 * (i + 0.5) / 48;
        box([unit * 0.009, height - rim * 2, 0.015], 'silver', [x, 0, 0.178]);
      }
      beam([-width * 0.48, -height * 0.36, -0.18], [width * 0.48, -height * 0.36, -0.18], unit * 0.035);
      beam([-width * 0.48, height * 0.36, -0.18], [width * 0.48, height * 0.36, -0.18], unit * 0.035);
    } else {
      box([width, height, unit], 'foil');
    }
    // Keep a component group for selection and measured bounds; merge all of
    // its static pieces by material instead of drawing every fastener or tile.
    for (const [name, geometries] of parts) {
      if (!geometries.length) continue;
      const geometry = mergeGeometries(geometries);
      geometries.forEach((part) => part.dispose());
      const mesh = new THREE.Mesh(geometry, materials[name]);
      mesh.name = `${component.type} ${name}`;
      mesh.castShadow = mesh.receiveShadow = true;
      mesh.userData.component = component.name;
      group.add(mesh);
    }
    station.add(group);
  }
  return station;
}
