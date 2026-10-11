// Assembles the Python Worker in web/.worker/: SingularityReturnHome/worker/entry.py
// at the root, next to the simulator packages and scenario files it imports.
import { cpSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = join(dirname(fileURLToPath(import.meta.url)), '..');
const sim = join(web, '..', 'SingularityReturnHome');
const out = join(web, '.worker');
const PACKAGES = ['singularos_sim', 'intelligence', 'simulation', 'guidance', 'safety', 'navigation', 'telemetry', 'scenarios'];

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(join(sim, 'worker', 'entry.py'), join(out, 'entry.py'));
for (const name of PACKAGES) {
  cpSync(join(sim, name), join(out, name), {
    recursive: true,
    filter: (path) => !path.includes('__pycache__') && !path.endsWith('.pyc') && !path.endsWith('replay_template.html'),
  });
}
console.log(`Bundled Python Worker in ${out}`);
