import './style.css';
import { createScene } from './scene.js';
import {
  DEFAULT_SCENARIO, SPEEDS, autoSpeed, createPlayback, createRun, formatDistance, formatMET, parseFrozenTime, phaseLabel,
} from './mission.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const runsUrl = `${import.meta.env.BASE_URL}${import.meta.env.MODE === 'demo' ? 'runs-llm/' : 'runs/'}`;
let scene;
let run = null;
let playback = null;
let available = false;
let contextLost = false;
let request = null;
let lastLog = '';
let phaseButtons = [];
let scenarioRequest = 0;
let viewMode = params.get('view') === 'overview' ? 'overview' : 'cinematic';
let adviceRun = null;
let lastAdvice;

function updateAdvice(time) {
  const metadata = run.data.advisor;
  $('advice-card').hidden = !metadata?.enabled;
  if (!metadata?.enabled) return;
  const advice = run.adviceAt(time);
  if (adviceRun === run && lastAdvice === advice) return;
  adviceRun = run;
  lastAdvice = advice;
  $('advice-backend').textContent = metadata.backend === 'ollama' ? 'OLLAMA' : 'MODEL';
  $('advice-model').textContent = metadata.model?.split('/').at(-1) || 'Configured model';
  $('advice-model').title = metadata.model || '';
  $('advice-details').hidden = !advice || !!advice.error;
  $('advice-checks').replaceChildren();
  $('advice-time').textContent = advice ? `${formatMET(advice.t)} · ${advice.point.replaceAll('_', ' ')}` : 'ADVISORY ONLY';
  if (!advice) {
    $('advice-proposal').textContent = run.events.some((e) => e.type === 'llm_advice')
      ? 'Awaiting recovery decision' : 'No advice requested for this run';
    $('advice-comparison').textContent = 'The flight controller executes the mission.';
    return;
  }
  if (advice.error) {
    $('advice-proposal').textContent = 'Advice unavailable';
    $('advice-comparison').textContent = 'The flight controller continued independently.';
    return;
  }
  $('advice-proposal').textContent = `${advice.proposal.mode} / ${advice.proposal.plan.action.replaceAll('_', ' ')}`;
  $('advice-comparison').textContent = `Mode ${advice.review.agrees_with_deterministic ? 'matches' : 'differs from'} controller: ${advice.review.deterministic_mode}`;
  $('advice-rationale').textContent = advice.proposal.rationale;
  $('advice-checks').replaceChildren(...Object.entries(advice.review.checks).map(([name, check]) => {
    const li = document.createElement('li');
    const result = check.pass === true ? 'PASS' : check.pass === false ? 'FAIL' : 'NOT EVALUATED';
    li.dataset.result = result;
    li.textContent = `${name.replaceAll('_', ' ')}: ${result}. ${check.detail}`;
    return li;
  }));
}

const CAPTIONS = {
  DEPART: 'Leaving the dock for the inspection loop.',
  INSPECT: 'Flying the inspection loop around the station.',
  INVESTIGATE: 'Something is wrong. Braking and testing each thruster.',
  RETURN: 'Returning to the station with the thrusters that work.',
  APPROACH: 'Final approach to the docking port.',
  DOCKED: 'Docked.',
  HOLD: 'Holding: return is not safe with what is left.',
  ABORT: 'Abort: no feasible recovery.',
};

function updateUI(time) {
  if (!run) return;
  const state = run.sample(time);
  const summary = run.data.summary;
  updateAdvice(time);
  $('met').textContent = formatMET(time);
  $('range').textContent = formatDistance(state.range);
  $('fuel').textContent = `${state.fuel.toFixed(2)} kg`;
  $('perigee').textContent = `${Math.round(state.perigee).toLocaleString('en-US')} km`;
  $('perigee').classList.toggle('locked', state.perigee >= 200);
  $('altitude').textContent = `${state.alt.toFixed(1)} km`;
  $('phase').textContent = phaseLabel(state.phase).toUpperCase();
  $('leg').textContent = (state.leg || '-').toUpperCase();
  $('clearance').textContent = formatDistance(Math.max(0, state.clear));
  $('scene-caption').textContent = CAPTIONS[state.phase] ?? '';
  $('thrusters').replaceChildren(...run.data.thrusters.flatMap((name) => {
    const dt = document.createElement('dt');
    dt.textContent = name;
    const dd = document.createElement('dd');
    const truth = document.createElement('span');
    truth.textContent = `${Math.round(state.health[name] * 100)}%`;
    if (state.health[name] === 0) truth.className = 'failed';
    dd.append(truth, ` / ${Math.round(state.est[name] * 100)}%`);
    dd.classList.toggle('firing', state.thrust[name] > 0.01);
    return [dt, dd];
  }));

  const logs = run.eventsUntil(time).slice(-3);
  const logKey = logs.map((e) => `${e.t}${e.type}`).join(',');
  if (logKey !== lastLog) {
    lastLog = logKey;
    $('log').replaceChildren(...logs.map((e) => {
      const li = document.createElement('li');
      const timestamp = document.createElement('time');
      timestamp.textContent = formatMET(e.t);
      const source = document.createElement('span');
      source.className = `source ${e.source}`;
      source.textContent = e.type === 'llm_advice' ? 'MODEL' : e.source === 'truth' ? 'TRUTH' : 'CONTROLLER';
      timestamp.append(source);
      li.append(timestamp, document.createTextNode(e.text));
      return li;
    }));
  }

  const ended = time >= run.duration;
  const pass = summary.outcome === summary.expected_outcome && !summary.collision;
  $('outcome').textContent = ended ? `OUTCOME ${summary.outcome}` : 'IN FLIGHT';
  $('outcome').className = ended ? (pass ? 'pass' : 'fail') : '';
  $('expected').textContent = `EXPECTED ${summary.expected_outcome}`;

  const phase = run.phaseIndex(time);
  phaseButtons.forEach((button, index) => {
    const active = index === phase;
    button.classList.toggle('active', active);
    button.classList.toggle('done', index < phase);
    if (active) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  $('timeline').value = time;
  $('timeline').setAttribute('aria-valuetext', `${phaseLabel(state.phase)}, ${formatMET(time)}`);
  $('timeline').style.setProperty('--progress', `${time / run.duration * 100}%`);
  $('elapsed').textContent = `${formatMET(time)} / ${formatMET(run.duration).slice(2)}`;
  $('play').setAttribute('aria-label', playback.playing ? 'Pause mission' : 'Play mission');
  $('play-text').textContent = playback.playing ? 'Pause' : 'Play';
  $('play-icon').textContent = playback.playing ? 'Ⅱ' : '▷';
  $('playback-status').textContent = contextLost ? '3D VIEW INTERRUPTED' : playback.playing ? `PLAYING AT ${playback.speed}x` : 'PAUSED';
}

function render() {
  if (!playback) return;
  if (available && !contextLost) {
    const shot = scene.render(playback.time);
    if (shot) $('shot-title').textContent = shot.title;
  }
  updateUI(playback.time);
}

function setViewMode(mode) {
  viewMode = mode;
  document.body.dataset.view = mode;
  document.querySelectorAll('[data-view-mode]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.viewMode === mode));
  });
  if (available) scene.setViewMode(mode);
  const url = new URL(location.href);
  url.searchParams.set('view', mode);
  history.replaceState(null, '', url);
  render();
}

function schedule() {
  if (request === null && playback?.playing && !document.hidden && !contextLost) request = requestAnimationFrame(frame);
}

function frame(now) {
  request = null;
  if (document.hidden || contextLost || !playback.playing) return;
  playback.tick(now);
  render();
  schedule();
}

function setPlaying(playing) {
  if (contextLost || !playback) return;
  playback.setPlaying(playing);
  if (request !== null) cancelAnimationFrame(request);
  request = null;
  render();
  schedule();
}

async function loadScenario(name, frozen = null) {
  const requestId = ++scenarioRequest;
  let data;
  try {
    const response = await fetch(`${runsUrl}${name}.json`);
    if (!response.ok) throw new Error(`Run ${name}: HTTP ${response.status}`);
    data = await response.json();
  } catch (error) {
    if (requestId !== scenarioRequest) return;
    throw error;
  }
  if (requestId !== scenarioRequest) return;
  run = createRun(data);
  const speed = autoSpeed(run.duration);
  const time = frozen === null ? 0 : parseFrozenTime(frozen, run.duration) ?? 0;
  const wasPlaying = playback ? playback.playing : frozen === null && !reduceMotion.matches;
  playback = createPlayback(run.duration, time, wasPlaying, speed);
  lastLog = '';
  if (available) scene.setRun(run);

  const summary = run.data.summary;
  $('scenario-name').textContent = name.replaceAll('_', ' ').toUpperCase();
  $('scenario-faults').textContent = run.data.faults.toUpperCase();
  $('description').textContent = `${run.data.faults === 'no faults' ? 'No faults injected.' : `Fault: ${run.data.faults}.`} `
    + `Expected outcome: ${summary.expected_outcome.toLowerCase()}. Outcome: ${summary.outcome.toLowerCase()} at ${formatMET(summary.outcome_t ?? run.duration)}.`
    + (summary.reentry ? ` Reentry at ${formatMET(summary.reentry.t)}.` : '');
  $('timeline').max = run.duration;
  $('speed').replaceChildren(...[...new Set([...SPEEDS, speed])].sort((a, b) => a - b).map((value) => {
    const option = new Option(`${value}x`, value);
    option.selected = value === speed;
    return option;
  }));
  document.documentElement.style.setProperty('--phase-count', run.phaseStarts.length);
  $('phases').replaceChildren(...run.phaseStarts.map(({ phase, t }, i) => {
    const li = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    const number = document.createElement('span');
    number.textContent = String(i + 1).padStart(2, '0');
    button.append(number, phaseLabel(phase));
    button.addEventListener('click', () => {
      playback.seek(t);
      setPlaying(false);
    });
    li.append(button);
    return li;
  }));
  phaseButtons = [...$('phases').querySelectorAll('button')];
  const url = new URL(location.href);
  url.searchParams.set('scenario', name);
  history.replaceState(null, '', url);
  render();
  schedule();
}

$('play').addEventListener('click', () => setPlaying(!playback.playing));
$('replay').addEventListener('click', () => {
  playback.seek(0);
  setPlaying(true);
});
$('timeline').addEventListener('input', (event) => {
  playback.seek(Number(event.target.value));
  setPlaying(false);
});
$('scenario').addEventListener('change', (event) => {
  loadScenario(event.target.value).catch(showLoadError);
});
$('speed').addEventListener('change', (event) => {
  playback.speed = Number(event.target.value);
  playback.resetFrame();
  render();
});
document.querySelectorAll('[data-view-mode]').forEach((button) => {
  button.addEventListener('click', () => setViewMode(button.dataset.viewMode));
});

window.addEventListener('resize', () => {
  if (available && !contextLost) scene.resize();
  render();
});
document.addEventListener('visibilitychange', () => {
  playback?.resetFrame();
  if (document.hidden && request !== null) {
    cancelAnimationFrame(request);
    request = null;
  } else schedule();
});
reduceMotion.addEventListener('change', () => {
  if (available) scene.setReducedMotion(reduceMotion.matches);
  if (reduceMotion.matches) setPlaying(false);
  render();
});

function showFallback(message) {
  $('render-status').textContent = message;
  $('render-status').hidden = false;
  document.body.classList.add('render-fallback');
}

function showLoadError(error) {
  console.warn(error);
  $('render-status').textContent = `The simulator run could not be loaded (${error.message}). Reload to retry.`;
  $('render-status').hidden = false;
}

$('scene').addEventListener('webglcontextlost', (event) => {
  event.preventDefault();
  contextLost = true;
  playback?.setPlaying(false);
  if (request !== null) cancelAnimationFrame(request);
  request = null;
  showFallback('The 3D view was interrupted. Playback is paused while graphics recover.');
  $('play').disabled = $('replay').disabled = true;
  if (playback) updateUI(playback.time);
});
$('scene').addEventListener('webglcontextrestored', () => {
  // Draw on the next frame, after Three.js has handled the restoration event
  // and rebuilt its GL state. Paused playback still needs this single redraw.
  requestAnimationFrame(() => {
    contextLost = false;
    $('render-status').hidden = true;
    document.body.classList.remove('render-fallback');
    $('play').disabled = $('replay').disabled = false;
    scene.restore();
    scene.resize();
    render();
  });
});

try {
  scene = createScene($('scene'), {
    reducedMotion: reduceMotion.matches,
    invalidate: () => { if (available && !contextLost) render(); },
    onTextureError: () => {
      $('render-status').textContent = 'Some planet textures could not load. The replay remains available; reload to retry.';
      $('render-status').hidden = false;
    },
  });
  available = true;
} catch (error) {
  console.warn('3D scene unavailable:', error.message);
  showFallback('The 3D view is unavailable on this device. You can still play the run and read the telemetry.');
}
setViewMode(viewMode);

(async () => {
  const response = await fetch(`${runsUrl}index.json`);
  if (!response.ok) throw new Error(`Run index: HTTP ${response.status}`);
  const index = await response.json();
  const requested = params.get('scenario');
  const name = index.some((entry) => entry.name === requested) ? requested : DEFAULT_SCENARIO;
  $('scenario').replaceChildren(...index.map((entry) => {
    const option = new Option(`${entry.name.replaceAll('_', ' ')} (${entry.faults})`, entry.name);
    option.selected = entry.name === name;
    return option;
  }));
  await loadScenario(name, params.get('t'));
})().catch(showLoadError);
