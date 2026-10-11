import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { canvasTexture, randomGenerator, surfaceTextures } from './visuals/textures.js';
import { createEarth } from './visuals/earth.js';
import { createSpacecraft } from './visuals/spacecraft.js';
import { cameraPose } from './visuals/cinematography.js';

// Units are metres in the station frame of the simulator. Scene axes: +X is
// along-track (sim +y), +Y is radial, away from Earth (sim +x), +Z is the
// orbit normal. The sim is planar, so everything it moves stays at Z = 0.
const CYAN = 0x79f4ef;
const AMBER = 0xe8c788;
const v = (x, y, z) => new THREE.Vector3(x, y, z);
const toScene = (x, y) => v(y, x, 0);
// Earth is drawn in its own pass, shrunk about the camera by EARTH_SCALE:
// apparent size and direction are exact, depth stays in range.
const EARTH_SCALE = 1e-3;

function buildStation(components, textures) {
  const station = new THREE.Group();
  const materials = {
    hull: new THREE.MeshStandardMaterial({ map: textures.hull, color: 0xe7e5df, metalness: 0.48, roughness: 0.38 }),
    foil: new THREE.MeshStandardMaterial({ color: 0xd5a453, metalness: 0.88, roughness: 0.42, bumpMap: textures.foil, bumpScale: 0.045 }),
    silver: new THREE.MeshStandardMaterial({ color: 0xbcc5cd, metalness: 0.85, roughness: 0.25 }),
    solar: new THREE.MeshPhysicalMaterial({ map: textures.solar, color: 0xcedfff, metalness: 0.6, roughness: 0.28, clearcoat: 0.8, clearcoatRoughness: 0.2 }),
    radiator: new THREE.MeshStandardMaterial({ color: 0xdfe6ea, metalness: 0.2, roughness: 0.55 }),
    port: new THREE.MeshBasicMaterial({ color: new THREE.Color(0.18, 1.8, 1.55) }),
  };
  for (const c of components) {
    // Footprint in the orbital plane is the sim's rectangle exactly; depth
    // along the orbit normal is not simulated and is chosen for looks.
    const [x0, y0] = c.min_m;
    const [x1, y1] = c.max_m;
    const width = y1 - y0; // along-track
    const height = x1 - x0; // radial
    const centre = toScene((x0 + x1) / 2, (y0 + y1) / 2);
    let mesh;
    if (c.type === 'hull') {
      mesh = new THREE.Mesh(new THREE.CylinderGeometry(width / 2, width / 2, height, 64), materials.hull);
      const bands = new THREE.Group();
      for (let i = 1; i < 4; i++) {
        const band = new THREE.Mesh(new THREE.TorusGeometry(width / 2, 0.06, 8, 64), materials.silver);
        band.rotation.x = Math.PI / 2;
        band.position.y = -height / 2 + (height * i) / 4;
        bands.add(band);
      }
      mesh.add(bands);
      const bolts = new THREE.InstancedMesh(new THREE.SphereGeometry(0.045, 8, 6), materials.silver, 36);
      const fastener = new THREE.Object3D();
      for (let i = 0; i < 36; i++) {
        const angle = (i % 12) / 12 * Math.PI * 2;
        fastener.position.set(Math.sin(angle) * (width / 2 + 0.015), -height / 2 + height * (Math.floor(i / 12) + 1) / 4, Math.cos(angle) * (width / 2 + 0.015));
        fastener.updateMatrix();
        bolts.setMatrixAt(i, fastener.matrix);
      }
      mesh.add(bolts);
    } else if (c.type === 'docking_port') {
      mesh = new THREE.Mesh(new THREE.CylinderGeometry(width / 2, width / 2, height, 48), materials.foil);
      const ring = new THREE.Mesh(new THREE.TorusGeometry(width / 2, 0.05, 8, 48), materials.port);
      ring.rotation.x = Math.PI / 2;
      ring.position.y = height / 2;
      mesh.add(ring);
    } else if (c.type === 'solar_array') {
      mesh = new THREE.Mesh(new RoundedBoxGeometry(width, height, 0.12, 2, 0.04), materials.solar);
      const frame = new THREE.Mesh(new THREE.BoxGeometry(width + 0.1, 0.08, 0.16), materials.silver);
      for (const dy of [-height / 2, height / 2]) {
        const edge = frame.clone();
        edge.position.y = dy;
        mesh.add(edge);
      }
      for (const dx of [-width / 2, width / 2]) {
        const edge = new THREE.Mesh(new THREE.BoxGeometry(0.08, height, 0.16), materials.silver);
        edge.position.x = dx;
        mesh.add(edge);
      }
    } else if (c.type === 'radiator') {
      mesh = new THREE.Mesh(new RoundedBoxGeometry(width, height, 0.2, 2, 0.06), materials.radiator);
      const seams = new THREE.InstancedMesh(new THREE.BoxGeometry(0.025, height * 0.9, 0.015), materials.silver, 24);
      const seam = new THREE.Object3D();
      for (let i = 0; i < 24; i++) {
        seam.position.set(-width / 2 + width * (i + 0.5) / 24, 0, 0.11);
        seam.updateMatrix();
        seams.setMatrixAt(i, seam.matrix);
      }
      mesh.add(seams);
    } else {
      mesh = new THREE.Mesh(new RoundedBoxGeometry(width, height, Math.min(width, height), 2, 0.08), materials.foil);
    }
    mesh.position.copy(centre);
    mesh.traverse((part) => { if (part.isMesh) part.castShadow = part.receiveShadow = true; });
    mesh.userData.component = c.name;
    station.add(mesh);
  }
  return station;
}

function ringSet(color, opacity) {
  const group = new THREE.Group();
  for (let axis = 0; axis < 3; axis++) {
    const circle = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(Array.from({ length: 96 }, (_, i) => {
      const a = i / 96 * Math.PI * 2;
      return v(Math.cos(a), Math.sin(a), 0);
    })), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    if (axis === 1) circle.rotation.x = Math.PI / 2;
    if (axis === 2) circle.rotation.y = Math.PI / 2;
    group.add(circle);
  }
  return group;
}

export function createScene(canvas, { invalidate = () => {}, onTextureError = () => {}, reducedMotion = false } = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  renderer.autoClear = false;

  const textures = surfaceTextures();
  for (const texture of [textures.solar, textures.hull, textures.foil]) {
    texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
  }
  const sunDirection = v(-0.55, 0.62, 0.56).normalize();

  // Background pass: stars and a true-scale Earth below the station's orbit.
  const background = new THREE.Scene();
  background.background = new THREE.Color(0x020409);
  const backgroundCamera = new THREE.PerspectiveCamera(37, 1, 1, 4e4);
  let planet = null;
  let earthPivot = null;
  let earthCentre = v(0, 0, 0);
  const rand = randomGenerator(42);
  const starPositions = [], starColors = [];
  for (let i = 0; i < 2400; i++) {
    const phi = rand() * Math.PI * 2;
    const z = rand() * 2 - 1;
    const k = Math.sqrt(1 - z * z);
    starPositions.push(3e4 * k * Math.cos(phi), 3e4 * z, 3e4 * k * Math.sin(phi));
    const brightness = 0.25 + rand() * 0.6;
    starColors.push(brightness * 0.72, brightness * 0.9, brightness);
  }
  const starGeometry = new THREE.BufferGeometry();
  starGeometry.setAttribute('position', new THREE.Float32BufferAttribute(starPositions, 3));
  starGeometry.setAttribute('color', new THREE.Float32BufferAttribute(starColors, 3));
  const starSprite = canvasTexture(64, 64, (ctx) => {
    const glow = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    glow.addColorStop(0, '#ffffff'); glow.addColorStop(0.12, '#ffffff');
    glow.addColorStop(0.35, 'rgba(210,225,255,0.25)'); glow.addColorStop(1, 'rgba(200,220,255,0)');
    ctx.fillStyle = glow; ctx.fillRect(0, 0, 64, 64);
  });
  const stars = new THREE.Points(starGeometry, new THREE.PointsMaterial({
    size: 2.6, vertexColors: true, sizeAttenuation: false, map: starSprite,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  background.add(stars);

  // Foreground pass: station, vehicle, tracks.
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(37, 1, 0.1, 1e6);
  scene.add(new THREE.AmbientLight(0x6a8bb5, 0.13));
  const sun = new THREE.DirectionalLight(0xfff2dc, 3.1);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.bias = -0.00015;
  sun.shadow.normalBias = 0.02;
  scene.add(sun, sun.target);
  const earthshine = new THREE.DirectionalLight(0x789bcf, 0.6);
  earthshine.position.set(0, -1, 0.2);
  scene.add(earthshine, earthshine.target);
  const rim = new THREE.DirectionalLight(0x75b9e9, 1.2);
  scene.add(rim, rim.target);

  const reflection = canvasTexture(512, 256, (ctx, width, height) => {
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, '#72839a');
    gradient.addColorStop(0.4, '#121b29');
    gradient.addColorStop(0.7, '#243f60');
    gradient.addColorStop(1, '#080e18');
    ctx.fillStyle = gradient; ctx.fillRect(0, 0, width, height);
    const glow = ctx.createRadialGradient(130, 70, 0, 130, 70, 75);
    glow.addColorStop(0, '#ffffff'); glow.addColorStop(0.1, '#e7d5b5'); glow.addColorStop(1, 'rgba(150,165,190,0)');
    ctx.fillStyle = glow; ctx.fillRect(55, 0, 150, 145);
  });
  reflection.mapping = THREE.EquirectangularReflectionMapping;
  const pmrem = new THREE.PMREMGenerator(renderer);
  let environment;
  function restoreEnvironment() {
    environment?.dispose();
    environment = pmrem.fromEquirectangular(reflection);
    scene.environment = environment.texture;
    scene.environmentIntensity = 0.65;
  }
  restoreEnvironment();

  let composer, bloom;
  const backgroundPass = new RenderPass(background, backgroundCamera);
  const foregroundPass = new RenderPass(scene, camera);
  foregroundPass.clear = false;
  foregroundPass.clearDepth = true;
  if (renderer.extensions.has('EXT_color_buffer_float')) {
    // Avoid multisampled half-float targets: some WebGL drivers expose float
    // color buffers but resolve these targets to black. Pixel ratio supplies AA.
    const target = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType });
    composer = new EffectComposer(renderer, target);
    composer.addPass(backgroundPass);
    composer.addPass(foregroundPass);
    bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.32, 0.5, 1.15);
    composer.addPass(bloom);
    composer.addPass(new OutputPass());
  }

  const vehicle = createSpacecraft(textures, { wings: false });
  const { craft } = vehicle;
  scene.add(craft);
  let station = null;
  let run = null;
  let flown = null;
  let viewMode = 'cinematic';
  const impactGeometry = new THREE.BufferGeometry();
  const impactPositions = new Float32Array(96 * 3);
  impactGeometry.setAttribute('position', new THREE.BufferAttribute(impactPositions, 3));
  const impactRandom = randomGenerator(114);
  const debris = Array.from({ length: 96 }, () => ({
    angle: impactRandom() * Math.PI * 2, speed: 0.2 + impactRandom() * 0.5,
    depth: impactRandom() * 0.8 - 0.4,
  }));
  const impact = new THREE.Points(impactGeometry, new THREE.PointsMaterial({
    color: 0xffd7a4, size: 0.018, map: starSprite, transparent: true,
    depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  impact.frustumCulled = false;
  scene.add(impact);
  const estimate = new THREE.Mesh(new THREE.OctahedronGeometry(1), new THREE.MeshBasicMaterial({ color: AMBER, wireframe: true, transparent: true }));
  const estimateRings = ringSet(AMBER, 0.55);
  const dock = ringSet(CYAN, 0.35);
  scene.add(estimate, estimateRings, dock);

  let viewWidth = 0, viewHeight = 0, narrow = false;
  const labels = {
    earth: document.querySelector('.earth-label'),
    craft: document.querySelector('.craft-label'),
    station: document.querySelector('.station-label'),
  };
  const projected = v(0, 0, 0);
  const placeLabel = (element, position, cam, visible = true, offsetY = 0) => {
    if (!element) return;
    projected.copy(position);
    if (cam === camera) projected.add(scene.position);
    projected.project(cam);
    const x = (projected.x + 1) * viewWidth / 2;
    const y = (1 - projected.y) * viewHeight / 2 + offsetY;
    const onScreen = projected.z > -1 && projected.z < 1 && x > 20 && x < viewWidth - 140 && y > 80 && y < viewHeight - 200;
    element.style.transform = `translate(${x}px, ${y}px)`;
    element.style.opacity = visible && onScreen ? '1' : '0';
  };

  function setRun(next) {
    run = next;
    const { data } = run;
    if (station) {
      scene.remove(station);
      const geometries = new Set(), materials = new Set();
      station.traverse((part) => {
        if (part.isMesh) { geometries.add(part.geometry); materials.add(part.material); }
        if (part.isInstancedMesh) part.dispose();
      });
      geometries.forEach((geometry) => geometry.dispose());
      materials.forEach((material) => material.dispose());
    }
    station = buildStation(data.station, textures);
    scene.add(station);
    // The wingless model spans about 2 units; match the sim's vehicle disc.
    craft.scale.setScalar(data.vehicle_radius_m);
    estimate.scale.setScalar(data.vehicle_radius_m * 0.25);
    dock.scale.setScalar(data.vehicle_radius_m * 1.5);
    dock.position.copy(toScene(0.2, 0));

    if (!planet) {
      // Thin shells at true scale: clouds about 10 km up, the visible
      // atmosphere about 80 km.
      planet = createEarth(data.orbit.earth_radius_m * EARTH_SCALE, sunDirection, renderer, invalidate, onTextureError,
        { cloudHeight: 10e3 / data.orbit.earth_radius_m, airHeight: 80e3 / data.orbit.earth_radius_m });
      earthPivot = new THREE.Group();
      earthPivot.add(planet.earth);
      planet.earth.rotation.set(0.35, 2.6, 0);
      background.add(earthPivot);
    }
    earthCentre = v(0, -data.orbit.station_radius_m, 0);

    if (flown) { scene.remove(flown); flown.geometry.dispose(); flown.material.dispose(); }
    const s = data.samples;
    const positions = new Float32Array(s.t.length * 3);
    s.t.forEach((_, i) => {
      positions[i * 3] = s.y[i];
      positions[i * 3 + 1] = s.x[i];
    });
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    flown = new THREE.Line(geometry, new THREE.LineBasicMaterial({ color: CYAN, transparent: true, opacity: 0.75 }));
    flown.frustumCulled = false;
    scene.add(flown);
  }

  function resize() {
    const bounds = canvas.parentElement.getBoundingClientRect();
    viewWidth = bounds.width;
    viewHeight = bounds.height;
    narrow = viewWidth < 760;
    for (const cam of [camera, backgroundCamera]) {
      cam.aspect = viewWidth / viewHeight;
      cam.fov = narrow ? 52 : 37;
      cam.updateProjectionMatrix();
    }
    const pixelRatio = Math.min(window.devicePixelRatio || 1, narrow ? 1.25 : 1.75);
    renderer.setPixelRatio(pixelRatio);
    renderer.setSize(viewWidth, viewHeight, false);
    if (composer) {
      composer.setPixelRatio(pixelRatio);
      composer.setSize(viewWidth, viewHeight);
      bloom.strength = narrow ? 0.22 : 0.32;
    }
    sun.shadow.mapSize.set(narrow ? 1024 : 2048, narrow ? 1024 : 2048);
    if (sun.shadow.map && sun.shadow.map.width !== sun.shadow.mapSize.x) {
      sun.shadow.map.dispose(); sun.shadow.map = null;
    }
  }

  const nose = v(0, 0, 0), left = v(0, 0, 0), up = v(0, 0, 0), basis = new THREE.Matrix4();
  function render(time) {
    if (!run) return;
    const state = run.sample(time);
    const position = toScene(state.x, state.y);
    craft.position.copy(position);
    // Body +x (nose) and body +y (left) in the station frame, mapped to the
    // model's +z and +x.
    const c = Math.cos(state.heading), s = Math.sin(state.heading);
    nose.copy(toScene(c, s));
    left.copy(toScene(-s, c));
    up.crossVectors(nose, left);
    craft.quaternion.setFromRotationMatrix(basis.makeBasis(left, up, nose));
    vehicle.update(time, state.thrust, state.alt < 160);
    flown.geometry.setDrawRange(0, run.sampleIndex(time) + 1);

    estimate.visible = estimateRings.visible = state.nav !== null;
    if (state.nav) {
      estimate.position.copy(toScene(state.nav.x, state.nav.y));
      estimateRings.position.copy(estimate.position);
      // 3-sigma position uncertainty of the navigation sensor.
      estimateRings.scale.setScalar(Math.max(3 * run.data.nav_sigma_m, 0.02));
      estimate.rotation.set(time * 0.4, time * 0.6, 0);
    }

    const shot = cameraPose(run, state, { mode: viewMode, aspect: viewWidth / viewHeight, reducedMotion });
    const targetWorld = v(...shot.target);
    const worldCamera = v(...shot.position);
    const distance = worldCamera.distanceTo(targetWorld);
    // Keep close-up geometry near the GPU origin even hundreds of kilometres
    // from the station. Exported positions and the Earth pass remain unchanged.
    scene.position.copy(position).negate();
    camera.position.copy(worldCamera).sub(position);
    const target = targetWorld.clone().sub(position);
    camera.fov = shot.fov;
    camera.near = Math.max(0.02, distance * 0.001);
    camera.far = Math.max(500, distance * 50);
    camera.setViewOffset(viewWidth, viewHeight, narrow ? 0 : viewWidth * 0.07, narrow ? viewHeight * 0.08 : viewHeight * 0.025, viewWidth, viewHeight);
    camera.updateProjectionMatrix();
    camera.lookAt(target);
    backgroundCamera.fov = camera.fov;
    backgroundCamera.copy(camera);
    backgroundCamera.near = 1;
    backgroundCamera.far = 4e4;
    backgroundCamera.updateProjectionMatrix();
    flown.material.opacity = viewMode === 'overview' ? 0.75 : 0.75 * (1 - shot.detail) ** 2;
    estimate.material.opacity = 0.55 * (1 - shot.detail * 0.8);
    estimateRings.children.forEach((ring) => { ring.material.opacity = 0.55 * (1 - shot.detail * 0.8); });
    dock.children.forEach((ring) => { ring.material.opacity = 0.35 * (1 - shot.detail * 0.85); });

    const hit = run.data.events.find((event) => event.type === 'impact' && time >= event.t && time < event.t + 8);
    impact.visible = Boolean(hit) && !reducedMotion;
    if (hit) {
      const origin = run.sample(hit.t);
      const age = (time - hit.t) / 8;
      impact.position.copy(toScene(origin.x, origin.y));
      impact.material.opacity = (1 - age) ** 2;
      debris.forEach((particle, i) => {
        const travel = age * particle.speed * run.data.vehicle_radius_m * 6;
        impactPositions[i * 3] = Math.cos(particle.angle) * travel;
        impactPositions[i * 3 + 1] = Math.sin(particle.angle) * travel;
        impactPositions[i * 3 + 2] = particle.depth * travel;
      });
      impactGeometry.attributes.position.needsUpdate = true;
    }

    // The Earth turns under the station once per orbit: the frame co-rotates.
    const n = 2 * Math.PI / run.data.orbit.period_s;
    earthPivot.rotation.z = -n * time;
    backgroundCamera.quaternion.copy(camera.quaternion);
    backgroundCamera.position.set(0, 0, 0);
    earthPivot.position.copy(earthCentre).sub(worldCamera).multiplyScalar(EARTH_SCALE);

    const radius = viewMode === 'overview' ? Math.max(24, distance * Math.sin(shot.fov * Math.PI / 360)) : Math.max(5, distance * 0.7);
    const shadowsOn = radius < 200;
    sun.castShadow = shadowsOn;
    sun.target.position.copy(targetWorld);
    sun.position.copy(targetWorld).addScaledVector(sunDirection, radius * 4);
    Object.assign(sun.shadow.camera, { left: -radius, right: radius, top: radius, bottom: -radius, near: radius, far: radius * 8 });
    sun.shadow.camera.updateProjectionMatrix();
    rim.target.position.copy(position);
    rim.position.copy(position).add(v(-5, 2, -4));

    if (composer) composer.render();
    else {
      renderer.clear();
      renderer.render(background, backgroundCamera);
      renderer.clearDepth();
      renderer.render(scene, camera);
    }
    const tiny = radius > 400;
    placeLabel(labels.craft, position, camera, !narrow && shot.detail < 0.4, 28);
    placeLabel(labels.station, toScene(-7, 0), camera, !narrow && tiny, 0);
    // Earth label on the horizon ahead of the camera.
    const re = run.data.orbit.earth_radius_m;
    const radial = worldCamera.clone().sub(earthCentre);
    const r = radial.length();
    radial.divideScalar(r);
    // Swung left of centre so the label clears the station.
    const ahead = camera.getWorldDirection(v(0, 0, 0)).projectOnPlane(radial).normalize().applyAxisAngle(radial, 0.45);
    const dip = Math.acos(Math.min(1, re / r));
    const horizon = ahead.multiplyScalar(Math.cos(dip)).addScaledVector(radial, -Math.sin(dip))
      .multiplyScalar(Math.sqrt(Math.max(0, r * r - re * re)) * EARTH_SCALE);
    placeLabel(labels.earth, horizon, backgroundCamera, viewMode === 'overview', 12);
    return shot;
  }

  resize();
  return {
    render, resize, setRun, restore: restoreEnvironment,
    setViewMode(mode) { viewMode = mode === 'overview' ? 'overview' : 'cinematic'; },
    setReducedMotion(value) { reducedMotion = value; },
  };
}
