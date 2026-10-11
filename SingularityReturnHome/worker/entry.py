"""Cloudflare Python Worker for the 3D site (singularos.willsarg.com).

Serves the built site from static assets and runs the simulator per request:

  GET /api/scenarios                     scenario names, expected outcomes, faults
  GET /api/run?scenario=NAME[&seed=N]    runs the scenario; playback data as
                                         written by telemetry.export_web, plus
                                         "seed". Without seed, a random seed.
  GET /api/advice?scenario=NAME&seed=N   re-runs the same scenario and seed,
                                         asks Workers AI for a recovery proposal
                                         at each decision point (AI binding,
                                         model intelligence.llm.DEFAULT_MODELS
                                         ["workers-ai"]) and returns the
                                         llm_advice events. Advice never changes
                                         the run, so these belong to the run
                                         returned by /api/run.

Both simulator routes are rate limited per client address (RUN_LIMITER,
ADVICE_LIMITER bindings). The bundle is assembled by web/scripts/bundle-worker.mjs.
"""

import random
import tempfile
from urllib.parse import parse_qs, urlparse

from js import Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from telemetry.scenarios import SCENARIOS_DIR, scenario_names

# The simulator and advisor modules are imported per request, not at module
# load: Worker startup is limited to 1 s.
MODEL = "@cf/qwen/qwen3.8-27b"  # intelligence.llm.DEFAULT_MODELS["workers-ai"]
MAX_SEED = 1_000_000
ADVISOR = {"enabled": True, "backend": "workers-ai", "model": MODEL, "recorded": False, "live": True}


def js(value):
    return to_js(value, dict_converter=Object.fromEntries)


def error(status, message):
    return Response.json({"error": message}, status=status)


def scenario_list():
    from singularos_sim.config import load_config
    from singularos_sim.replay import describe_faults

    out = []
    for name in scenario_names():
        config = load_config(SCENARIOS_DIR / f"{name}.toml")
        out.append({"name": name, "expected_outcome": config["scenario"]["expected_outcome"],
                    "faults": describe_faults(config["faults"])})
    return out


def parse_run(query, seed_required):
    name = query.get("scenario", [""])[0]
    if name not in scenario_names():
        raise ValueError(f"unknown scenario {name!r}")
    raw = query.get("seed", [None])[0]
    if raw is None:
        if seed_required:
            raise ValueError("seed is required")
        return name, random.randint(1, MAX_SEED)
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_SEED:
        raise ValueError(f"seed must be an integer from 1 to {MAX_SEED}")
    return name, int(raw)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        if not url.path.startswith("/api/"):
            return await self.env.ASSETS.fetch(request)
        if request.method != "GET":
            return error(405, "GET only")
        query = parse_qs(url.query)
        if url.path == "/api/scenarios":
            return Response.json(scenario_list())
        if url.path not in ("/api/run", "/api/advice"):
            return error(404, "not found")
        try:
            name, seed = parse_run(query, seed_required=url.path == "/api/advice")
        except ValueError as e:
            return error(400, str(e))
        limiter = self.env.RUN_LIMITER if url.path == "/api/run" else self.env.ADVICE_LIMITER
        client = request.headers.get("CF-Connecting-IP") or "unknown"
        if not (await limiter.limit(js({"key": f"{url.path}:{client}"}))).success:
            return error(429, "rate limited; try again in a minute")
        if url.path == "/api/run":
            return self.run(name, seed)
        return await self.advice(name, seed)

    def run(self, name, seed):
        from telemetry.export_web import run_data

        with tempfile.TemporaryDirectory() as tmp:
            data = run_data(name, seed, advisor_metadata=ADVISOR, run=tmp)
        data["seed"] = seed
        return Response.json(data)

    async def advice(self, name, seed):
        from intelligence.advisor import ContextRecorder, advise_async
        from intelligence.harness import RecoveryHarness
        from intelligence.llm import LLMConfig, LLMError, workers_ai_content, workers_ai_payload
        from singularos_sim.config import load_config, with_seed
        from singularos_sim.runner import run_scenario

        config = LLMConfig()  # Workers AI defaults; thinking off
        recorder = ContextRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            run_scenario(with_seed(load_config(SCENARIOS_DIR / f"{name}.toml"), seed), tmp, advisor=recorder)

        async def chat(messages, schema):
            try:
                result = await self.env.AI.run(MODEL, js(workers_ai_payload(config, messages, schema)))
            except Exception as e:  # JsException from the binding
                raise LLMError(f"workers-ai: {e}") from e
            return workers_ai_content(result.to_py())

        harness = RecoveryHarness()  # its own client is unused; replies come from chat
        events = [await advise_async(harness, ctx, chat) for ctx in recorder.contexts]
        return Response.json({"scenario": name, "seed": seed, "advisor": ADVISOR,
                              "events": [{**e, "source": "controller"} for e in events]})
