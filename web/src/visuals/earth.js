import * as THREE from 'three';

const vertexShader = `
  varying vec2 texCoord;
  varying vec3 worldNormal;
  varying vec3 worldPosition;
  void main() {
    texCoord = uv;
    worldNormal = normalize(mat3(modelMatrix) * normal);
    vec4 p = modelMatrix * vec4(position, 1.0);
    worldPosition = p.xyz;
    gl_Position = projectionMatrix * viewMatrix * p;
  }
`;

function placeholder(r, g, b) {
  const texture = new THREE.DataTexture(new Uint8Array([r, g, b, 255]), 1, 1);
  texture.needsUpdate = true;
  return texture;
}

// cloudHeight and airHeight are shell heights as fractions of the radius.
export function createEarth(radius, sunDirection, renderer, invalidate, onTextureError, { cloudHeight = 0.009, airHeight = 0.035 } = {}) {
  const earth = new THREE.Group();
  const uniforms = {
    dayMap: { value: placeholder(12, 40, 68) },
    nightMap: { value: placeholder(0, 0, 0) },
    cloudMap: { value: placeholder(0, 0, 0) },
    sunDirection: { value: sunDirection },
    cloudOffset: { value: 0 },
  };
  const globe = new THREE.Mesh(new THREE.SphereGeometry(radius, 128, 64), new THREE.ShaderMaterial({
    uniforms, vertexShader,
    fragmentShader: `
      uniform sampler2D dayMap, nightMap, cloudMap;
      uniform vec3 sunDirection;
      uniform float cloudOffset;
      varying vec2 texCoord;
      varying vec3 worldNormal, worldPosition;
      void main() {
        vec3 n = normalize(worldNormal);
        vec3 viewDirection = normalize(cameraPosition - worldPosition);
        float sunlight = dot(n, sunDirection);
        float day = smoothstep(-0.12, 0.22, sunlight);
        vec3 surface = texture2D(dayMap, texCoord).rgb;
        float clouds = texture2D(cloudMap, vec2(texCoord.x + cloudOffset + 0.002, texCoord.y)).r;
        vec3 color = surface * (0.022 + day * (0.18 + max(sunlight, 0.0) * 1.15));
        color *= 1.0 - clouds * day * 0.22;
        vec3 lights = texture2D(nightMap, texCoord).rgb;
        float luminance = dot(lights, vec3(0.2126, 0.7152, 0.0722));
        color += vec3(1.0, 0.58, 0.24) * pow(luminance, 1.4) * (1.0 - day) * 1.9;
        float ocean = smoothstep(0.02, 0.12, surface.b - surface.r) * (1.0 - clouds);
        vec3 halfDirection = normalize(sunDirection + viewDirection);
        color += vec3(0.62, 0.78, 1.0) * pow(max(dot(n, halfDirection), 0.0), 90.0) * ocean * day * 0.65;
        float fresnel = pow(1.0 - max(dot(n, viewDirection), 0.0), 4.0);
        color += vec3(0.035, 0.19, 0.42) * fresnel * day;
        gl_FragColor = vec4(color, 1.0);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }
    `,
  }));
  earth.add(globe);
  const clouds = new THREE.Mesh(new THREE.SphereGeometry(radius * (1 + cloudHeight), 96, 48), new THREE.ShaderMaterial({
    uniforms, vertexShader, transparent: true, depthWrite: false,
    fragmentShader: `
      uniform sampler2D cloudMap;
      uniform vec3 sunDirection;
      uniform float cloudOffset;
      varying vec2 texCoord;
      varying vec3 worldNormal, worldPosition;
      void main() {
        float density = texture2D(cloudMap, vec2(texCoord.x + cloudOffset, texCoord.y)).r;
        float sun = dot(normalize(worldNormal), sunDirection);
        vec3 color = mix(vec3(0.06, 0.12, 0.2), vec3(1.2, 1.22, 1.25), smoothstep(-0.15, 0.6, sun));
        gl_FragColor = vec4(color, smoothstep(0.12, 0.85, density) * 0.86);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }
    `,
  }));
  earth.add(clouds);
  const air = new THREE.Mesh(new THREE.SphereGeometry(radius * (1 + airHeight), 96, 48), new THREE.ShaderMaterial({
    uniforms, vertexShader, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    fragmentShader: `
      uniform vec3 sunDirection;
      varying vec3 worldNormal, worldPosition;
      void main() {
        vec3 n = normalize(worldNormal);
        float edge = pow(1.0 - max(dot(n, normalize(cameraPosition - worldPosition)), 0.0), 3.5);
        float sun = dot(n, sunDirection);
        float day = smoothstep(-0.35, 0.35, sun);
        vec3 tint = mix(vec3(0.9, 0.23, 0.07), vec3(0.09, 0.4, 1.0), smoothstep(-0.1, 0.25, sun));
        gl_FragColor = vec4(tint * 1.3, edge * day * 0.65);
        #include <tonemapping_fragment>
        #include <colorspace_fragment>
      }
    `,
  }));
  earth.add(air);

  const manager = new THREE.LoadingManager(invalidate);
  const loader = new THREE.TextureLoader(manager);
  const anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
  for (const [uniform, filename, color] of [
    ['dayMap', 'earth-day.png', true], ['nightMap', 'earth-night.png', true], ['cloudMap', 'earth-clouds.jpg', false],
  ]) {
    loader.load(`${import.meta.env.BASE_URL}textures/${filename}`, (texture) => {
      texture.wrapS = THREE.RepeatWrapping;
      texture.anisotropy = anisotropy;
      if (color) texture.colorSpace = THREE.SRGBColorSpace;
      uniforms[uniform].value.dispose();
      uniforms[uniform].value = texture;
      invalidate();
    }, undefined, onTextureError);
  }
  return {
    earth,
    update(time) {
      earth.rotation.y = 2.6 + time * 0.008;
      uniforms.cloudOffset.value = time * 0.0007;
    },
  };
}
