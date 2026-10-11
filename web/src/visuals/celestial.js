import * as THREE from 'three';

// Nominal radii/distances from NASA Moon Facts and Sun Facts. The directions
// are an illustrative sky composition, not a date-dependent ephemeris.
export const MOON_RADIUS_M = 1.7374e6;
export const MOON_DISTANCE_M = 384.4e6;
export const SUN_RADIUS_M = 696e6;
export const SUN_DISTANCE_M = 149.6e9;
export const SUN_DIRECTION = new THREE.Vector3(-0.46, 0.04, -0.887).normalize();
const MOON_DIRECTION = new THREE.Vector3(0.72, 0.03, -0.694).normalize();
const MOON_SCALE = 5e-5;
const SKY_DISTANCE = 30000;

export function moonPosition(earthCentre, worldCamera) {
  return MOON_DIRECTION.clone().multiplyScalar(MOON_DISTANCE_M)
    .add(earthCentre).sub(worldCamera).multiplyScalar(MOON_SCALE);
}

export function createCelestialBackdrop(moonTexture) {
  const group = new THREE.Group();
  const moon = new THREE.Mesh(new THREE.SphereGeometry(MOON_RADIUS_M * MOON_SCALE, 64, 32), new THREE.ShaderMaterial({
    uniforms: { surface: { value: moonTexture }, sunDirection: { value: SUN_DIRECTION } },
    vertexShader: `varying vec2 coord; varying vec3 normalWorld;
      void main() {
        coord = uv; normalWorld = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }`,
    fragmentShader: `uniform sampler2D surface; uniform vec3 sunDirection;
      varying vec2 coord; varying vec3 normalWorld;
      void main() {
        float light = max(dot(normalize(normalWorld), sunDirection), 0.0);
        vec3 albedo = texture2D(surface, coord).rgb;
        gl_FragColor = vec4(albedo * (0.012 + light * 0.95), 1.0);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }`,
  }));
  moon.rotation.set(0.2, 1.5, -0.1);
  group.add(moon);

  const sunRadius = SKY_DISTANCE * SUN_RADIUS_M / SUN_DISTANCE_M;
  const sun = new THREE.Mesh(new THREE.PlaneGeometry(sunRadius * 10, sunRadius * 10), new THREE.ShaderMaterial({
    uniforms: { time: { value: 0 } },
    vertexShader: `varying vec2 coord;
      void main() { coord = uv * 2.0 - 1.0; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `uniform float time; varying vec2 coord;
      void main() {
        float r = length(coord) * 5.0;
        float disc = 1.0 - smoothstep(0.97, 1.02, r);
        float corona = exp(-max(r - 1.0, 0.0) * 2.4) * 0.22 * (1.0 - disc);
        float limb = sqrt(max(0.0, 1.0 - r*r));
        float granules = 0.97 + 0.03 * sin(coord.x * 650.0 + sin(coord.y * 170.0 + time * 0.03));
        vec3 color = vec3(5.0, 3.7, 2.1) * (0.65 + 0.35 * limb) * granules;
        gl_FragColor = vec4(color, disc + corona);
      }`,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  }));
  sun.position.copy(SUN_DIRECTION).multiplyScalar(SKY_DISTANCE);
  group.add(sun);
  return {
    group, moon, sun,
    update(time, earthCentre, worldCamera, camera) {
      moon.position.copy(moonPosition(earthCentre, worldCamera));
      sun.quaternion.copy(camera.quaternion);
      sun.material.uniforms.time.value = time;
    },
  };
}
