import * as THREE from 'three';
import { impactEvents, impactEnvelope } from './impact-timing.js';
export { impactEvents, impactEnvelope } from './impact-timing.js';

const UP = new THREE.Vector3(0, 0, 1);
const DUMMY = new THREE.Object3D();
const SPARK_COUNT = 40, FRAGMENT_COUNT = 24;

function randomSequence(seed) {
  let value = seed >>> 0;
  return () => { value = (1664525 * value + 1013904223) >>> 0; return value / 4294967296; };
}
function attitude(heading) {
  const c = Math.cos(heading), s = Math.sin(heading);
  return new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(
    new THREE.Vector3(c, -s, 0), new THREE.Vector3(0, 0, -1), new THREE.Vector3(s, c, 0),
  ));
}
const scenePosition = (state) => new THREE.Vector3(state.y, state.x, 0);

function glowMaterial() {
  return new THREE.ShaderMaterial({
    uniforms: { opacity: { value: 0 } },
    vertexShader: 'varying vec2 coord; void main(){coord=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
    fragmentShader: `uniform float opacity; varying vec2 coord;
      void main(){float r=length(coord-0.5)*2.0;float halo=exp(-r*r*6.0);
        gl_FragColor=vec4(mix(vec3(4.0,0.62,0.08),vec3(7.0,5.0,2.6),exp(-r*r*25.0)),halo*opacity*smoothstep(1.0,0.6,r));}`,
    transparent: true, depthWrite: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending,
  });
}
function scorchMaterial() {
  return new THREE.ShaderMaterial({
    vertexShader: 'varying vec2 coord; void main(){coord=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
    fragmentShader: `varying vec2 coord; void main(){vec2 p=(coord-0.5)*2.0;float a=atan(p.y,p.x);
      float r=length(p)*(1.0+0.09*sin(a*7.0)+0.06*cos(a*13.0));
      float core=1.0-smoothstep(0.1,0.44,r);float rim=exp(-pow((r-0.5)*9.0,2.0));
      vec3 color=mix(vec3(0.04,0.027,0.016),vec3(0.29,0.12,0.035),rim*0.7)*(1.0-core*0.6);
      gl_FragColor=vec4(color,(1.0-smoothstep(0.6,1.0,r))*0.95);}`,
    transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2,
    side: THREE.DoubleSide,
  });
}

// GPU buffers are allocated when a run is selected, never per animation frame.
// group lives in the scene's station frame; origin is the current floating origin
// in that same frame. Debris uses small relative offsets and inherits craft
// translation for readability; it is an illustration, not exported physics.
export function createImpactEffects() {
  const group = new THREE.Group();
  group.name = 'recorded-impact-effects';
  let run, entries = [];
  const release = () => {
    const geometries = new Set(), materials = new Set();
    group.traverse((object) => {
      if (object.geometry) geometries.add(object.geometry);
      if (object.material) materials.add(object.material);
      if (object.isInstancedMesh) object.dispose();
    });
    geometries.forEach((geometry) => geometry.dispose());
    materials.forEach((material) => material.dispose());
    group.clear();
    entries = [];
  };
  return {
    group,
    setRun(next) {
      release();
      run = next;
      const scale = run.data.vehicle_radius_m;
      entries = impactEvents(run).map((event, index) => {
        const atHit = run.sample(event.t), rotation = attitude(atHit.heading);
        const dv = event.delta_v_m_s ?? [0, 0];
        // Bounded visual emphasis, not an estimate of energy or physical damage.
        const strength = 0.55 + 0.7 * Math.min(1, Math.log1p(Math.hypot(...dv)) / Math.log(151));
        const incoming = new THREE.Vector3(dv[1] ?? 0, dv[0] ?? 0, 0);
        if (incoming.lengthSq() < 1e-12) incoming.set(-1, 0, 0);
        incoming.normalize();
        const normal = incoming.clone().negate().applyQuaternion(rotation.clone().invert());
        // A contact patch on the bus or nose; its position is an illustration,
        // since telemetry provides neither a measured strike location nor size.
        const radial = Math.hypot(normal.x, normal.y);
        const contact = radial > 0.3
          ? new THREE.Vector3(normal.x / radial * 0.425, normal.y / radial * 0.425, 0.12)
          : new THREE.Vector3(normal.x * 0.12, normal.y * 0.12, normal.z > 0 ? 0.83 : -0.65);
        const surfaceNormal = radial > 0.3 ? new THREE.Vector3(normal.x / radial, normal.y / radial, 0) : new THREE.Vector3(0, 0, Math.sign(normal.z));
        contact.multiplyScalar(scale);
        const worldNormal = surfaceNormal.clone().applyQuaternion(rotation);
        const strikeOffset = contact.clone().applyQuaternion(rotation);
        const damage = new THREE.Mesh(new THREE.CircleGeometry(scale * 0.22 * strength, 32), scorchMaterial());
        damage.name = `impact-damage-${index}`;
        const damageNormal = new THREE.Quaternion().setFromUnitVectors(UP, surfaceNormal);
        const burst = new THREE.Group();
        burst.name = `impact-burst-${index}`;
        const fragments = new THREE.InstancedMesh(new THREE.IcosahedronGeometry(1, 0), new THREE.MeshStandardMaterial({ color: 0x766e60, roughness: 0.8, metalness: 0.65, transparent: true }), FRAGMENT_COUNT);
        const sparks = new THREE.InstancedMesh(new THREE.CylinderGeometry(1, 1, 1, 4), new THREE.MeshBasicMaterial({ color: new THREE.Color(4.0, 1.55, 0.24), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending }), SPARK_COUNT);
        fragments.frustumCulled = sparks.frustumCulled = false;
        fragments.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        sparks.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
        burst.add(fragments, sparks);
        const flash = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), glowMaterial());
        flash.name = `impact-flash-${index}`;
        const light = new THREE.PointLight(0xffce81, 0, scale * 8, 2);
        const meteor = new THREE.Mesh(new THREE.IcosahedronGeometry(scale * 0.065 * strength, 1), new THREE.MeshStandardMaterial({ color: 0x8c857b, roughness: 0.95, emissive: 0x603314 }));
        const trail = new THREE.Mesh(new THREE.CylinderGeometry(scale * 0.025, 0, scale * 1.5, 8), new THREE.MeshBasicMaterial({ color: 0xffb65a, transparent: true, opacity: 0.45, depthWrite: false, blending: THREE.AdditiveBlending }));
        trail.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), incoming.clone().negate());
        group.add(damage, burst, flash, light, meteor, trail);
        const random = randomSequence(139 + index * 101 + Math.round(event.t * 100));
        const particles = Array.from({ length: SPARK_COUNT }, () => ({
          // The impulse gives a directional cue, never a measured fragment speed.
          velocity: worldNormal.clone().multiplyScalar(0.5 + random() * 1.5).add(new THREE.Vector3(random() - 0.5, random() - 0.5, random() - 0.5)).multiplyScalar(scale * strength * 2),
          size: scale * strength * (0.018 + random() * 0.035),
          spin: new THREE.Vector3(random() * 8, random() * 8, random() * 8),
          phase: random() * Math.PI * 2,
        }));
        return { event, scale, strength, contact, strikeOffset, incoming, damageNormal, damage, burst, fragments, sparks, flash, light, meteor, trail, particles };
      });
      group.children.forEach((object) => { object.visible = false; });
    },
    update({ time, state, origin = scenePosition(state), craftQuaternion = attitude(state.heading), camera, detail = 1, reducedMotion = false }) {
      if (!run) return;
      group.position.copy(origin);
      const craftPosition = scenePosition(state).sub(origin);
      for (const entry of entries) {
        const { event, scale, contact, strikeOffset, incoming, damage, burst, fragments, sparks, flash, light, meteor, trail, particles } = entry;
        const age = time - event.t, envelope = impactEnvelope(age);
        const visible = !reducedMotion && detail > 0.15;
        const currentContact = contact.clone().applyQuaternion(craftQuaternion).add(craftPosition);
        damage.visible = envelope.damage;
        damage.position.copy(currentContact);
        damage.quaternion.copy(craftQuaternion).multiply(entry.damageNormal);
        flash.visible = visible && envelope.flash > 0;
        flash.position.copy(currentContact);
        flash.scale.setScalar(scale * entry.strength * (1.1 + age * 2));
        flash.quaternion.copy(camera?.quaternion ?? new THREE.Quaternion());
        flash.material.uniforms.opacity.value = envelope.flash;
        light.visible = flash.visible;
        light.position.copy(currentContact);
        light.intensity = visible ? envelope.flash * scale * scale * entry.strength * 55 : 0;
        meteor.visible = trail.visible = visible && envelope.approach > 0;
        // Approach is a short illustrative lead-in, attached to the real craft
        // before the exact event timestamp. It does not alter simulator state.
        meteor.position.copy(currentContact).addScaledVector(incoming, age * scale * 6);
        meteor.rotation.set(time * 2, time * 3, time);
        trail.position.copy(meteor.position).addScaledVector(incoming, -scale * 0.75);
        trail.material.opacity = envelope.approach * 0.4;
        burst.visible = visible && envelope.burst > 0;
        // Inherit translation to keep the illustrative ejecta visible during
        // orbital-scale impulses, but never rotate the debris with the hull.
        burst.position.copy(craftPosition).add(strikeOffset);
        fragments.material.opacity = envelope.burst;
        sparks.material.opacity = envelope.sparks;
        sparks.visible = envelope.sparks > 0;
        // Skip hidden pools; all poses are computed from age on re-entry.
        if (burst.visible) {
          particles.forEach((particle, i) => {
            DUMMY.position.copy(particle.velocity).multiplyScalar(age * 0.4);
            DUMMY.rotation.set(particle.phase + particle.spin.x * age, particle.spin.y * age, particle.spin.z * age);
            DUMMY.scale.set(particle.size, particle.size * 0.5, particle.size * 1.4);
            DUMMY.updateMatrix();
            if (i < FRAGMENT_COUNT) fragments.setMatrixAt(i, DUMMY.matrix);
            DUMMY.position.copy(particle.velocity).multiplyScalar(age);
            DUMMY.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), particle.velocity.clone().normalize());
            DUMMY.scale.set(scale * 0.008, Math.min(scale * 0.8, particle.velocity.length() * 0.15), scale * 0.008);
            DUMMY.updateMatrix();
            sparks.setMatrixAt(i, DUMMY.matrix);
          });
          fragments.instanceMatrix.needsUpdate = sparks.instanceMatrix.needsUpdate = true;
        }
      }
    },
    dispose() { release(); run = null; group.removeFromParent(); },
  };
}
