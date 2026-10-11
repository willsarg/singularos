import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import sqrt, isfinite
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from simulation.orbit import MU, EARTH_RADIUS, orbital_status
from safety.emergency import assess
from guidance.recovery import plan_recovery
from safety.validator import validate_independently
from navigation.deadline import time_to_altitude
from singularos_sim.config import load_config, with_seed
from singularos_sim.replay import build_replay
from singularos_sim.runner import DEFAULT_RUNS_DIR, run_scenario

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
LAUNCHER = Path(__file__).resolve().parent / "launcher.html"
REPLAY_TEMPLATE = Path(__file__).resolve().parents[1] / "singularos_sim" / "replay_template.html"
_copilot = None
TRACK_PERIOD_S = 1.0


def mission_snapshot(disturbance_m_s=150):
    radius = EARTH_RADIUS + 400
    position = (radius, 0, 0)
    velocity = (0, sqrt(MU / radius) - disturbance_m_s / 1000, 0)

    emergency = assess(position, velocity, communications=False)
    plan = plan_recovery(position, velocity)
    safety = validate_independently(position, velocity, plan)

    return {
        "mode": "SIMULATION",
        "disturbance_m_s": disturbance_m_s,
        "initial_orbit": orbital_status(position, velocity),
        "hazards": emergency["hazards"],
        "time_to_120km_seconds": time_to_altitude(position, velocity),
        "recovery_plan": plan,
        "safety_verification": safety,
        "recovery_feasible": safety["authorized"],
    }


def scenario_names():
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.toml"))


def list_scenarios():
    out = []
    for name in scenario_names():
        config = load_config(SCENARIOS_DIR / f"{name}.toml")
        out.append({"name": name, "expected_outcome": config["scenario"]["expected_outcome"],
                    "expected_outcome_copilot": config["scenario"]["expected_outcome_copilot"],
                    "faults": config["faults"],
                    "advisories": [{"id": a["id"], "text": a["text"]} for a in config["advisories"]]})
    return out


def copilot_agent():
    """The LLM copilot (created once), or None if the model server is unavailable."""
    global _copilot
    if _copilot is None:
        from intelligence.copilot import CopilotAgent
        from intelligence.llm import LLMClient

        client = LLMClient()
        if client.health()["ok"]:
            _copilot = CopilotAgent(client)
    return _copilot


def copilot_status():
    from intelligence.llm import LLMClient

    client = LLMClient()
    status = client.health()
    return {"ok": status["ok"], "detail": status["detail"], "model": client.config.model}


def run_dir(name, seed, copilot=False):
    return DEFAULT_RUNS_DIR / f"{name}-{'copilot-' if copilot else ''}seed{seed}"


def ensure_run(name, seed, copilot=False, force=False):
    """Run the scenario unless a finished run for it already exists (or force)."""
    config = with_seed(load_config(SCENARIOS_DIR / f"{name}.toml"), seed)
    out = run_dir(name, config["scenario"]["seed"], copilot)
    if force or not (out / "summary.json").exists():
        agent = copilot_agent() if copilot else None
        if copilot and agent is None:
            raise ValueError("AI copilot unavailable: start `ollama serve` and pull the model")
        run_scenario(config, out, copilot=agent)
        build_replay(out)
    elif not (out / "replay.html").exists() or (out / "replay.html").stat().st_mtime < REPLAY_TEMPLATE.stat().st_mtime:
        build_replay(out)  # missing, or built from an older replay page
    return out


def run_report(out):
    """Summary, a track sampled every TRACK_PERIOD_S, and controller events."""
    summary = json.loads((out / "summary.json").read_text())
    summary.pop("out_dir", None)
    track, events, next_t = [], [], 0.0
    with open(out / "log.jsonl") as f:
        for line in f:
            rec = json.loads(line)
            ctrl = rec.get("controller")
            if ctrl is None:
                continue
            events += [{"t": rec["t"], **e} for e in ctrl.get("events", [])]
            if rec["t"] + 1e-9 >= next_t:
                tr = rec["truth"]
                track.append({"t": round(rec["t"], 2), "x_m": round(tr["x_m"], 2), "y_m": round(tr["y_m"], 2),
                              "phase": ctrl["phase"], "fuel_kg": round(tr["fuel_kg"], 4),
                              "altitude_km": tr["orbit"]["altitude_km"], "perigee_km": tr["orbit"]["perigee_km"]})
                next_t += TRACK_PERIOD_S
    return {"summary": summary, "track": track, "events": events}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def scenario_args(self, query):
        name = query.get("scenario", [""])[0]
        if name not in scenario_names():
            raise ValueError(f"Unknown scenario: {name!r}")
        raw = query.get("seed", [None])[0]
        seed = None if raw is None else int(raw)
        if seed is not None and not 0 <= seed <= 1_000_000:
            raise ValueError("Seed must be between 0 and 1000000")
        flag = lambda k: query.get(k, ["0"])[0] in ("1", "true", "yes")
        return name, seed, flag("copilot"), flag("force")

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            html = LAUNCHER.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if url.path == "/api/scenarios":
            self.send_json(list_scenarios())
            return
        if url.path == "/api/copilot":
            self.send_json(copilot_status())
            return
        if url.path in ("/api/run", "/api/replay"):
            try:
                name, seed, copilot, force = self.scenario_args(query)
                out = ensure_run(name, seed, copilot, force and url.path == "/api/run")
            except ValueError as error:
                self.send_error(400, str(error))
                return
            if url.path == "/api/run":
                self.send_json(run_report(out))
                return
            html = (out / "replay.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if url.path != "/api/mission":
            self.send_error(404)
            return

        try:
            raw = parse_qs(url.query).get("disturbance", ["150"])[0]
            disturbance = float(raw)
            if not isfinite(disturbance) or not 0 <= disturbance <= 300:
                raise ValueError("Disturbance must be between 0 and 300 m/s")
        except ValueError as error:
            self.send_error(400, str(error))
            return

        self.send_json(mission_snapshot(disturbance))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Scenario launcher: http://127.0.0.1:8000  (API: /api/scenarios, /api/run, /api/replay)", flush=True)
    server.serve_forever()
