# SingularOS

A Three.js replay of an autonomous spacecraft recovery simulator. An inspection
craft working around an orbital station detects faults, diagnoses its thrusters,
and returns to dock, holds, or aborts after an impact.

Position, heading, delivered thrust, navigation observations, telemetry, and
events come from the Python simulator. On singularos.willsarg.com it runs on
Cloudflare: the site's Python Worker runs the selected scenario with a new
random seed on each load and asks Workers AI (`@cf/qwen/qwen3.8-27b`) for
advisory recovery proposals at the run's decision points. The spacecraft's visual detail
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
pnpm run dev:worker   # the Worker with the built site, http://127.0.0.1:8787
pnpm dev              # in a second terminal: Vite with /api proxied to the Worker
```

`pnpm run dev:worker` signs in to Cloudflare through Wrangler: the Workers AI
binding always runs remotely, also in local development. `pnpm run dev:static`
plays exported runs without a Worker or Cloudflare account.

Open the local URL printed by Vite. Use Pause, Replay, the timeline, or the
mission-phase buttons to inspect the animation. A URL query such as `?t=18.5`
opens a paused view at that simulator second. Select a run with the scenario
picker or a query such as `?scenario=thruster_degraded&t=533.8`. The seed of
each live run is shown next to the pickers and added to the URL (`&seed=N`),
so a link reproduces the run; New run starts the scenario with a new seed. Reduced-motion
preferences start playback paused; playback can be started explicitly.

Cinematic is the default camera: it opens on the station, tracks the spacecraft,
and transitions into diagnostics, recovery, docking, and descent shots. Overview
keeps the station and flown path in frame. The view buttons switch between them;
`?view=overview` opens Overview directly. Camera motion follows simulator time,
so seeking and paused frames are reproducible. Reduced motion disables camera
drift and impact particles.

Scenario highlight buttons jump to an impact, fault, or descent. Cinematic
playback caps the selected speed at 2× from three seconds before a recorded
strike until five seconds after it, then resumes the selected speed. Overview
and reduced-motion playback keep the selected constant speed. Seeking and
paused scenes use mission time throughout.

`pnpm dev`, `pnpm test`, and `pnpm build` regenerate the ignored files in
`web/public/runs/` before running. `pnpm run export-runs` regenerates them
explicitly after simulator changes while a development server is already open.

For a demo with recorded model advice from Cloudflare Workers AI
(`@cf/qwen/qwen3.8-27b`; see the
[simulator's LLM settings](SingularityReturnHome/README.md#llm-advisor)), run
from `web/` with a Workers AI API token:

```sh
CLOUDFLARE_ACCOUNT_ID=<account id> CLOUDFLARE_API_TOKEN=<token> pnpm run build:demo
pnpm preview
```

The demo build generates separate runs in `web/public/runs-llm/` and displays
the model's proposal, rationale, and numerical check results at each decision
point. The model runs during export; browser playback uses the recorded replies.
The deterministic controller retains actuator authority. Missing server or
model stops the export; individual advice failures appear in the replay while
the controller continues. Normal development, tests, and builds remain offline
and do not overwrite the demo recordings. To rebuild the UI using existing
recordings, run `pnpm exec vite build --mode demo`.

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
- The spacecraft has thermal blanket seams, cooling ribs, service piping,
  instruments, and thruster bells. Station components have structural trusses,
  segmented solar panels, service decks, and docking locks. Static details are
  merged by material; station footprints match the simulator's rectangles.
- A procedural crescent Moon and Sun join the Earth backdrop. Their nominal
  angular sizes use [NASA Moon facts](https://science.nasa.gov/moon/facts/) and
  [Sun data](https://radiojove.gsfc.nasa.gov/education/sun/basics/material/sunfacts.htm).
  Sky directions are illustrative, without a date-dependent ephemeris.
- Recorded truth impacts trigger an incoming meteoroid, a brief contact flash,
  directional sparks, tumbling fragments, and persistent scorch marks. Object
  size, contact location, and co-moving fragments are illustrations; the
  simulator exports impulse and spin rather than debris or damage physics.
  Thruster faults without a recorded impact do not produce a meteor strike.
  Reduced motion suppresses moving impact effects and the flash. The glow below
  160 km illustrates reentry, rather than calculated heating. Exhaust follows
  delivered thrust from the run.
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
