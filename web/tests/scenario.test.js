import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as mission from '../src/mission.js';

// Exercise the page's loader with controlled network completion order. The
// DOM boundary is stubbed because main.js also imports CSS and creates WebGL.
function pageLoader() {
  const nodes = new Map();
  const element = () => ({
    style: { setProperty() {} }, append() {}, addEventListener() {}, setAttribute() {},
    replaceChildren() {}, querySelectorAll() { return []; },
  });
  const $ = (id) => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  const pending = new Map();
  let url = 'http://localhost/';
  const context = vm.createContext({
    ...mission, $, live: false, runsUrl: 'runs/', available: false, run: null, playback: null,
    lastLog: '', phaseButtons: [], scenarioRequest: 0, reduceMotion: { matches: true },
    URL, Option: function () {}, location: { get href() { return url; } },
    history: { replaceState(_state, _title, value) { url = String(value); } },
    document: { createElement: element, documentElement: element() },
    render() {}, schedule() {},
    fetch: (uri) => new Promise((resolve, reject) => pending.set(uri, { resolve, reject })),
  });
  const source = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  vm.runInContext(source.slice(source.indexOf('async function responseError'), source.indexOf("$('play').addEventListener")), context);
  return {
    load: (name) => context.loadScenario(name),
    complete(name) {
      const data = JSON.parse(readFileSync(new URL(`../public/runs/${name}.json`, import.meta.url)));
      pending.get(`runs/${name}.json`).resolve({ ok: true, json: async () => data });
    },
    fail: (name) => pending.get(`runs/${name}.json`).reject(new Error('Network unavailable')),
    get scenario() { return context.run?.data.scenario; },
    get url() { return url; },
  };
}

test('an older scenario response cannot replace the latest selection', async () => {
  const page = pageLoader();
  const older = page.load('nominal');
  const newer = page.load('thruster_fault');
  page.complete('thruster_fault');
  await newer;
  page.complete('nominal');
  await older;
  assert.equal(page.scenario, 'thruster_fault');
  assert.equal(new URL(page.url).searchParams.get('scenario'), 'thruster_fault');
});

test('an obsolete scenario failure does not report an error for the current run', async () => {
  const page = pageLoader();
  const older = page.load('nominal');
  const settled = older.then(() => null, (error) => error);
  const newer = page.load('thruster_fault');
  page.complete('thruster_fault');
  await newer;
  page.fail('nominal');
  assert.equal(await settled, null);
  assert.equal(page.scenario, 'thruster_fault');
});

test('a current scenario failure is reported', async () => {
  const page = pageLoader();
  const loading = page.load('nominal');
  const failure = assert.rejects(loading, /Network unavailable/);
  page.fail('nominal');
  await failure;
});
