import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from intelligence.advisor import RecoveryAdvisor, context_text
from intelligence.harness import (
    Proposal, ProposalValidationError, RecoveryHarness, load_schema, schema_problems, semantic_problems,
)
from intelligence.llm import LLMClient, LLMConfig, LLMError
from intelligence.review import review
from singularos_sim.config import load_config
from singularos_sim.runner import run_scenario

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
MODEL = "test-model:latest"


def proposal(**overrides):
    p = {
        "schema_version": 2,
        "mode": "INVESTIGATE",
        "rationale": "Station beacon dropout suggests sensor fault.",
        "requested_checks": ["OBSERVATION_FRESHNESS"],
        "plan": {"action": "MAINTAIN", "parameters": {}},
    }
    p.update(overrides)
    return p


ACCOUNT = "test-account"
TOKEN = "test-token"
CF_MODEL = "@cf/qwen/qwen3.8-27b"


class FakeOllama:
    """Minimal model server: Ollama /api/version, /api/tags, /api/chat;
    OpenAI-compatible /v1/chat/completions; Workers AI
    /accounts/{account}/ai/run/{model} and /ai/models/search. Replies with the
    queued contents in order, repeating the last one."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.auth = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith(f"/accounts/{ACCOUNT}/ai/models/search"):
                    fake.auth.append(self.headers.get("Authorization"))
                    self.send({"success": True, "result": [{"name": CF_MODEL}, {"name": "@cf/qwen/qwq-32b"}]})
                elif self.path == "/health":
                    self.send({"status": "ok"})
                elif self.path == "/api/version":
                    self.send({"version": "0.0.0-test"})
                elif self.path == "/api/tags":
                    self.send({"models": [{"name": MODEL, "model": MODEL}]})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                fake.auth.append(self.headers.get("Authorization"))
                content = fake.replies.pop(0) if len(fake.replies) > 1 else fake.replies[0]
                if self.path.startswith(f"/accounts/{ACCOUNT}/ai/run/"):
                    self.send({"success": True, "errors": [], "result": {"choices": [
                        {"message": {"role": "assistant", "content": content or None},
                         "finish_reason": "stop" if content else "length"}]}})
                elif self.path == "/api/chat":
                    self.send({"model": body["model"], "message": {"role": "assistant", "content": content},
                               "done": True, "done_reason": "stop"})
                elif self.path == "/v1/chat/completions":  # llama.cpp llama-server
                    self.send({"choices": [{"message": {"role": "assistant", "content": content}}]})
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ollama_factory():
    servers = []

    def make(replies):
        s = FakeOllama(replies)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


def client_for(server, **kw):
    return LLMClient(LLMConfig(base_url=server.url, model=MODEL, **{"backend": "ollama", **kw}))


def workers_ai_client(server, **kw):
    return LLMClient(LLMConfig(base_url=server.url, model=CF_MODEL, account_id=ACCOUNT, api_token=TOKEN, **kw))


# ------------------------------------------------------------- configuration


def test_config_from_env_defaults_to_workers_ai():
    c = LLMConfig.from_env({})
    assert (c.backend, c.base_url, c.model, c.think) == (
        "workers-ai", "https://api.cloudflare.com/client/v4", "@cf/qwen/qwen3.8-27b", False)
    assert (c.account_id, c.api_token) == (None, None)
    c = LLMConfig.from_env({"CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"})
    assert (c.account_id, c.api_token) == ("a", "t")
    assert "api_token" not in repr(c)
    ollama = LLMConfig.from_env({"SINGULAROS_LLM_BACKEND": "ollama"})
    assert (ollama.base_url, ollama.model) == ("http://127.0.0.1:11434", "hf.co/unsloth/Qwen3.5-9B-GGUF:Q4_K_M")
    assert LLMConfig.from_env({"SINGULAROS_LLM_THINK": "default"}).think is None
    assert LLMConfig.from_env({"SINGULAROS_LLM_THINK": "true"}).think is True
    openai = LLMConfig.from_env({"SINGULAROS_LLM_BACKEND": "openai"})
    assert (openai.model, openai.base_url) == (None, "http://127.0.0.1:8080")
    c = LLMConfig.from_env({"SINGULAROS_LLM_URL": "http://10.0.2.2:11434", "SINGULAROS_LLM_MODEL": "m",
                            "SINGULAROS_LLM_THINK": "false", "SINGULAROS_LLM_BACKEND": "openai"})
    assert (c.base_url, c.model, c.think, c.backend) == ("http://10.0.2.2:11434", "m", False, "openai")
    with pytest.raises(ValueError):
        LLMConfig.from_env({"SINGULAROS_LLM_BACKEND": "other"})


def test_unreachable_server_reports_unhealthy():
    status = LLMClient(LLMConfig(base_url="http://127.0.0.1:9", model=MODEL, backend="ollama")).health()
    assert status["ok"] is False


# ------------------------------------------------------------- ollama client


def test_ollama_chat_request_uses_native_api_with_schema(ollama_factory):
    server = ollama_factory([json.dumps(proposal())])
    schema = load_schema()
    content = client_for(server, think=False).chat([{"role": "user", "content": "hi"}], schema)
    assert json.loads(content)["mode"] == "INVESTIGATE"
    path, body = server.requests[0]
    assert path == "/api/chat"
    assert body["model"] == MODEL and body["stream"] is False
    assert body["format"] == schema and body["think"] is False
    assert body["options"]["temperature"] == pytest.approx(0.2)


def test_ollama_health_checks_model_is_pulled(ollama_factory):
    server = ollama_factory(["{}"])
    assert client_for(server).health()["ok"]
    assert LLMClient(LLMConfig(base_url=server.url, model="test-model", backend="ollama")).health()["ok"]  # ":latest" implied
    assert LLMClient(LLMConfig(base_url=server.url, model="TEST-MODEL:LATEST", backend="ollama")).health()["ok"]
    missing = LLMClient(LLMConfig(base_url=server.url, model="other", backend="ollama")).health()
    assert not missing["ok"] and "not pulled" in missing["detail"]


def test_ollama_without_model_is_an_error(ollama_factory):
    server = ollama_factory(["{}"])
    harness = RecoveryHarness(LLMClient(LLMConfig(base_url=server.url, model=None, backend="ollama")))
    with pytest.raises(Exception, match="SINGULAROS_LLM_MODEL"):
        harness.propose("context")


def test_openai_backend_for_llama_cpp(ollama_factory):
    server = ollama_factory([json.dumps(proposal())])
    client = LLMClient(LLMConfig(base_url=server.url, model=None, backend="openai"))
    assert client.health()["ok"]
    p = RecoveryHarness(client).propose("context")
    assert p.mode == "INVESTIGATE"
    path, body = server.requests[0]
    assert path == "/v1/chat/completions"
    assert body["response_format"]["json_schema"]["schema"] == load_schema()
    assert "model" not in body  # llama-server serves the model it was started with


# --------------------------------------------------------------- workers ai


def test_workers_ai_request_carries_token_schema_and_thinking_off(ollama_factory):
    server = ollama_factory([json.dumps(proposal())])
    p = RecoveryHarness(workers_ai_client(server)).propose("context")
    assert p.mode == "INVESTIGATE"
    path, body = server.requests[0]
    assert path == f"/accounts/{ACCOUNT}/ai/run/{CF_MODEL}"
    assert server.auth[0] == f"Bearer {TOKEN}"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 1024
    wire = body["response_format"]
    assert wire["type"] == "json_schema"
    assert "uniqueItems" not in json.dumps(wire["json_schema"])
    expected = load_schema()
    del expected["properties"]["requested_checks"]["uniqueItems"]
    assert wire["json_schema"] == expected


def test_workers_ai_duplicate_checks_are_caught_by_the_python_recheck(ollama_factory):
    duplicate = proposal(requested_checks=["OBSERVATION_FRESHNESS", "OBSERVATION_FRESHNESS"])
    server = ollama_factory([json.dumps(duplicate), json.dumps(proposal())])
    p = RecoveryHarness(workers_ai_client(server)).propose("context")
    assert p.requested_checks == ["OBSERVATION_FRESHNESS"]
    assert "unique" in server.requests[1][1]["messages"][-1]["content"]


def test_workers_ai_thinking_on_raises_the_token_budget(ollama_factory):
    server = ollama_factory([json.dumps(proposal())])
    workers_ai_client(server, think=True).chat([{"role": "user", "content": "x"}])
    body = server.requests[0][1]
    assert body["chat_template_kwargs"] == {"enable_thinking": True} and body["max_tokens"] == 8192
    workers_ai_client(server, think=None).chat([{"role": "user", "content": "x"}])
    assert "chat_template_kwargs" not in server.requests[1][1]


def test_workers_ai_empty_reply_is_an_error(ollama_factory):
    server = ollama_factory([""])
    with pytest.raises(LLMError, match="finish_reason length"):
        workers_ai_client(server).chat([{"role": "user", "content": "x"}])


def test_workers_ai_health(ollama_factory):
    server = ollama_factory(["{}"])
    assert workers_ai_client(server).health()["ok"]
    assert server.auth[-1] == f"Bearer {TOKEN}"
    missing = LLMClient(LLMConfig(base_url=server.url)).health()
    assert not missing["ok"] and "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN" in missing["detail"]
    unknown = LLMClient(LLMConfig(base_url=server.url, model="@cf/none/none", account_id=ACCOUNT, api_token=TOKEN))
    assert not unknown.health()["ok"]
    with pytest.raises(LLMError, match="CLOUDFLARE_API_TOKEN"):
        LLMClient(LLMConfig(base_url=server.url, account_id=ACCOUNT)).chat([{"role": "user", "content": "x"}])


# ------------------------------------------------------------------- harness


def test_schema_loads_and_forbids_extra_fields():
    schema = load_schema()
    assert schema["properties"]["schema_version"]["const"] == 2
    assert schema["additionalProperties"] is False
    assert "ORBIT_SAFETY" in schema["properties"]["requested_checks"]["items"]["enum"]


def test_schema_recheck_catches_what_a_server_might_not_enforce():
    schema = load_schema()
    assert schema_problems(proposal(), schema) == []
    bad = proposal(mode="PANIC", extra=1, requested_checks=["FUEL_BUDGET", "FUEL_BUDGET"],
                   plan={"action": "BRAKE", "parameters": {"max_delta_v_mps": 5000}})
    problems = " | ".join(schema_problems(bad, schema))
    for fragment in ("PANIC", "extra", "unique", "out of bounds"):
        assert fragment in problems


@pytest.mark.parametrize("overrides, fragment", [
    ({"mode": "PANIC"}, "PANIC"),
    ({"mode": "DOCKED", "requested_checks": []}, "DOCK_ALIGNMENT"),
    ({"requested_checks": ["FUEL_BUDGET", "FUEL_BUDGET"]}, "unique"),
    ({"rationale": "RECOVERY"}, "rationale"),
    ({"mode": "ABORT"}, "retreat"),
    ({"plan": {"action": "RAISE_PERIGEE", "parameters": {}}, "requested_checks": ["FUEL_BUDGET"]}, "ORBIT_SAFETY"),
    ({"plan": {"action": "BRAKE", "parameters": {}}, "requested_checks": []}, "FUEL_BUDGET"),
    ({"mode": "RETURN", "plan": {"action": "RAISE_PERIGEE", "parameters": {}},
      "requested_checks": ["FUEL_BUDGET", "ORBIT_SAFETY"]}, "mode must be HOLD"),
])
def test_semantic_problems(overrides, fragment):
    assert any(fragment in p for p in semantic_problems(proposal(**overrides)))


def test_semantic_valid_cases():
    assert semantic_problems(proposal()) == []
    assert semantic_problems(proposal(mode="DOCKED", requested_checks=["DOCK_ALIGNMENT", "OBSERVATION_FRESHNESS"],
                                      plan={"action": "DOCK_SEQUENCE", "parameters": {}})) == []
    assert semantic_problems(proposal(mode="ABORT", plan={"action": "SAFE_RETREAT", "parameters": {}})) == []


def test_proposal_properties():
    p = Proposal(raw=proposal())
    assert (p.mode, p.requested_checks, p.plan["action"]) == ("INVESTIGATE", ["OBSERVATION_FRESHNESS"], "MAINTAIN")


def test_harness_retries_with_feedback_then_succeeds(ollama_factory):
    server = ollama_factory(["not json", json.dumps(proposal(rationale="short")), json.dumps(proposal())])
    p = RecoveryHarness(client_for(server)).propose("context")
    assert p.mode == "INVESTIGATE"
    assert len(server.requests) == 3
    feedback = server.requests[2][1]["messages"][-1]["content"]
    assert "failed validation" in feedback and "rationale" in feedback


def test_harness_gives_up_after_retries(ollama_factory):
    server = ollama_factory([json.dumps(proposal(mode="PANIC"))])
    with pytest.raises(ProposalValidationError):
        RecoveryHarness(client_for(server), max_retries=1).propose("context")
    assert len(server.requests) == 2


# ---------------------------------------------------------- review / advisor


def triage_context(**overrides):
    ctx = {
        "t": 160.6, "point": "orbital_triage", "decision": "SAFE_ORBIT", "mode": "HOLD",
        "reasons": ["perigee -104.6 km below safe 200 km"],
        "observed": {"x_m": 0.0, "y_m": 0.0, "vx_m_s": 0.0, "vy_m_s": -150.0, "heading_rad": 0.0, "fuel_kg": 3.1},
        "health_estimate": {"forward": 1.0, "backward": 1.0, "left": 1.0, "right": 1.0},
        "usable": ["forward", "backward", "left", "right"],
        "orbit": {"altitude_km": 400.0, "speed_km_s": 7.52, "eccentricity": 0.0387, "perigee_km": -104.6},
        "safe_perigee_km": 200.0, "docking_check": None, "time_to_reentry_s": 1435.0,
        "available_delta_v_m_s": 144.0, "brake_feasible": False, "safe_orbit_authorized": True,
        "safe_orbit_delta_v_m_s": 92.1,
    }
    ctx.update(overrides)
    return ctx


def test_review_runs_requested_checks_against_planner_numbers():
    ok = Proposal(raw=proposal(mode="HOLD", requested_checks=["FUEL_BUDGET", "ORBIT_SAFETY", "TRAJECTORY_FEASIBILITY"],
                               plan={"action": "RAISE_PERIGEE", "parameters": {"max_delta_v_mps": 95}}))
    r = review(ok, triage_context())
    assert r["failed"] == [] and r["agrees_with_deterministic"]
    brake = Proposal(raw=proposal(mode="INVESTIGATE", requested_checks=["FUEL_BUDGET", "ORBIT_SAFETY"],
                                  plan={"action": "BRAKE", "parameters": {"max_delta_v_mps": 180}}))
    r = review(brake, triage_context())
    assert set(r["failed"]) == {"FUEL_BUDGET", "ORBIT_SAFETY"} and not r["agrees_with_deterministic"]
    collision = Proposal(raw=proposal(requested_checks=["COLLISION_CHECK"]))
    assert review(collision, triage_context())["checks"]["COLLISION_CHECK"]["pass"] is None


def test_advisor_turns_server_failure_into_error_record():
    advisor = RecoveryAdvisor(RecoveryHarness(LLMClient(LLMConfig(base_url="http://127.0.0.1:9", model=MODEL, backend="ollama"))))
    result = advisor(triage_context())
    assert "error" in result and "proposal" not in result


def test_context_text_carries_decision_and_budget():
    text = context_text(triage_context())
    assert "SAFE_ORBIT" in text and "144.0" in text and "1435.0" in text


# ------------------------------------------------------- end to end, sim run


def test_advice_is_logged_and_never_changes_the_run(ollama_factory, tmp_path):
    reply = proposal(mode="HOLD", requested_checks=["FUEL_BUDGET", "ORBIT_SAFETY", "TRAJECTORY_FEASIBILITY"],
                     rationale="Perigee is below the surface; a 92 m/s prograde burn fits the budget.",
                     plan={"action": "RAISE_PERIGEE", "parameters": {"max_delta_v_mps": 95}})
    server = ollama_factory([json.dumps(reply)])
    config = load_config(SCENARIOS / "orbital_strike_safe_orbit.toml")
    plain = run_scenario(config, tmp_path / "plain")
    advised = run_scenario(config, tmp_path / "advised", RecoveryAdvisor(RecoveryHarness(client_for(server))))

    assert advised["outcome"] == plain["outcome"] == "HOLD"
    assert advised["fuel_used_kg"] == plain["fuel_used_kg"]
    assert advised["final_position_m"] == plain["final_position_m"]
    [advice] = advised["llm_advice"]
    assert advice["point"] == "orbital_triage" and advice["decision"] == "SAFE_ORBIT"
    assert advice["review"]["agrees_with_deterministic"] and advice["review"]["failed"] == []
    # The prompt carried controller-side numbers, not truth.
    prompt = server.requests[0][1]["messages"][1]["content"]
    assert "SAFE_ORBIT" in prompt and "effectiveness" not in prompt


def test_unreachable_advisor_does_not_stop_the_run(tmp_path):
    advisor = RecoveryAdvisor(RecoveryHarness(LLMClient(LLMConfig(base_url="http://127.0.0.1:9", model=MODEL, backend="ollama"))))
    summary = run_scenario(load_config(SCENARIOS / "thruster_fault.toml"), tmp_path, advisor)
    assert summary["outcome"] == "DOCKED"
    assert summary["llm_advice"] and all("error" in a for a in summary["llm_advice"])


# ------------------------------------------------------- live server (opt-in)


@pytest.mark.skipif(not os.environ.get("SINGULAROS_LLM_INTEGRATION"),
                    reason="set SINGULAROS_LLM_INTEGRATION=1 (plus SINGULAROS_LLM_URL/MODEL) to test a live server")
def test_live_server_returns_valid_proposal():
    client = LLMClient()
    assert client.health()["ok"], client.health()["detail"]
    advice = RecoveryAdvisor(RecoveryHarness(client))(triage_context())
    assert "proposal" in advice, advice.get("error")


# ------------------------------------------------ async path (Python Worker)


def test_async_harness_retries_like_the_sync_one():
    import asyncio
    replies = ["not json", json.dumps(proposal(rationale="short")), json.dumps(proposal())]
    seen = []

    async def chat(messages, schema):
        seen.append(messages)
        return replies[len(seen) - 1]

    p = asyncio.run(RecoveryHarness(client=object()).propose_async("context", chat))
    assert p.mode == "INVESTIGATE" and len(seen) == 3
    assert "not valid JSON" in seen[1][-1]["content"]


def test_recorded_contexts_match_a_live_advisor(ollama_factory, tmp_path):
    import asyncio
    from intelligence.advisor import ContextRecorder, advise_async
    reply = proposal(mode="HOLD", requested_checks=["FUEL_BUDGET", "ORBIT_SAFETY", "TRAJECTORY_FEASIBILITY"],
                     rationale="Perigee is below the surface; a 92 m/s prograde burn fits the budget.",
                     plan={"action": "RAISE_PERIGEE", "parameters": {"max_delta_v_mps": 95}})
    server = ollama_factory([json.dumps(reply)])
    config = load_config(SCENARIOS / "orbital_strike_safe_orbit.toml")
    live = run_scenario(config, tmp_path / "live", RecoveryAdvisor(RecoveryHarness(client_for(server))))
    recorder = ContextRecorder()
    deferred = run_scenario(config, tmp_path / "deferred", recorder)
    assert deferred["final_position_m"] == live["final_position_m"]
    [ctx] = recorder.contexts

    async def chat(messages, schema):
        return json.dumps(reply)

    event = asyncio.run(advise_async(RecoveryHarness(client=object()), ctx, chat))
    [logged] = live["llm_advice"]
    for key in ("t", "type", "point", "decision", "proposal", "review"):
        assert event[key] == logged[key], key


def test_worker_model_matches_the_client_default():
    entry = (Path(__file__).resolve().parent.parent / "worker" / "entry.py").read_text()
    from intelligence.llm import DEFAULT_MODELS
    assert f'MODEL = "{DEFAULT_MODELS["workers-ai"]}"' in entry
