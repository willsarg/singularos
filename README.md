# SingularOS

A Three.js replay of an autonomous spacecraft recovery simulator. An inspection
craft working around an orbital station detects faults, diagnoses its thrusters,
and returns to dock, holds, or aborts after an impact.

Position, heading, delivered thrust, navigation observations, telemetry, and
events come from exported Python simulator runs. The spacecraft's visual detail
and station depth are illustrative; the physics is planar. This is a simulation,
not flight hardware or a Linux operating system.

## Development

Requires Python 3.11 or newer, Node.js 22.12 or newer, and pnpm. From the
repository root, create the simulator environment before starting the site:

```sh
python3 -m venv SingularityReturnHome/.venv
SingularityReturnHome/.venv/bin/python -m pip install -r SingularityReturnHome/requirements.txt
cd web
pnpm install
pnpm dev
```

Open the local URL printed by Vite. Use Pause, Replay, the timeline, or the
mission-phase buttons to inspect the animation. A URL query such as `?t=18.5`
opens a paused view at that simulator second. Select a run with the scenario
picker or a query such as `?scenario=thruster_degraded&t=533.8`. Reduced-motion
preferences start playback paused; playback can be started explicitly.

Cinematic is the default camera: it opens on the station, tracks the spacecraft,
and transitions into diagnostics, recovery, docking, and descent shots. Overview
keeps the station and flown path in frame. The view buttons switch between them;
`?view=overview` opens Overview directly. Camera motion follows simulator time,
so seeking and paused frames are reproducible. Reduced motion disables camera
drift and impact particles.

`pnpm dev`, `pnpm test`, and `pnpm build` regenerate the ignored files in
`web/public/runs/` before running. `pnpm run export-runs` regenerates them
explicitly after simulator changes while a development server is already open.

## Simulator

`SingularityReturnHome/` contains the headless Python simulator used by the
site, including station inspection and orbit-scale recovery scenarios. See its
[README](SingularityReturnHome/README.md) for simulator and test commands.
`sim_1/` contains the earlier 2D simulator for [MISSION.md](MISSION.md).

## Verification and deployment

```sh
cd web
pnpm test
pnpm build
pnpm exec wrangler deploy --dry-run
```

Run these after the environment setup above. `pnpm preview` serves the
production build locally. `pnpm deploy` exports the runs, builds the site,
and deploys its static assets to the custom domain configured in
`web/wrangler.jsonc`. Cloudflare credentials are required for deployment.

## Implementation

- Three.js renders the spacecraft, station, Earth, starfield, and navigation estimate,
  with material reflections, spacecraft shadows, and HDR bloom on supported GPUs.
- Spacecraft geometry, material detail maps, stars, and the reflection
  environment are generated locally. Small NASA Earth composites are bundled
  with the site; provenance and usage terms are in
  [textures/SOURCES.md](web/public/textures/SOURCES.md).
- `SingularityReturnHome/telemetry/export_web.py` exports sampled truth,
  observations, controller state, events, and the terminal physics record.
- `web/src/mission.js` samples the exported timestamps and manages playback.
- `web/src/visuals/cinematography.js` directs the camera without changing
  spacecraft motion, orientation, dimensions, or telemetry.
- `web/src/scene.js` builds and animates the 3D scene.
- `web/src/visuals/` supplies the layered Earth shaders, detailed spacecraft,
  and procedural material textures. Effects use mission time so seeking and
  paused frames remain reproducible. Texture arrivals refresh paused views.
- Impact particles and the glow below 160 km are illustrative effects, not
  simulated debris or a calculated heating model. Exhaust follows delivered
  thrust from the run.
- `web/src/main.js` connects playback to the accessible HTML controls and log.
- Vite bundles the dependencies into `web/dist`, which is ignored by Git.
- Google Fonts supplies the typefaces, with local font fallbacks.

If WebGL is unavailable, the mission controls and textual telemetry remain
usable. Graphics-context interruptions pause playback; after recovery, playback
can be resumed explicitly. Narrow views use a lower pixel-ratio ceiling.
Devices without floating-point color-buffer support render
without bloom. This does not guarantee a particular frame rate on every device.

## License

MIT. See [LICENSE](LICENSE). Three.js, Vite, and Wrangler retain their respective
package licenses. The deployed assets include runtime notices in
[THIRD_PARTY_LICENSES.txt](web/public/THIRD_PARTY_LICENSES.txt) and Earth imagery
credits in [textures/SOURCES.md](web/public/textures/SOURCES.md).
