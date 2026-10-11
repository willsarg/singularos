import json

import pytest

from telemetry.export_web import FIELDS, main
from tests.test_llm import FakeOllama, MODEL, proposal


def test_export_matches_summary(tmp_path):
    main(["--out", str(tmp_path), "thruster_fault", "orbital_strike_abort"])
    index = json.loads((tmp_path / "index.json").read_text())
    assert [e["name"] for e in index] == ["thruster_fault", "orbital_strike_abort"]

    data = json.loads((tmp_path / "thruster_fault.json").read_text())
    s = data["samples"]
    assert set(s) == set(FIELDS)
    assert len({len(v) for v in s.values()}) == 1
    assert s["t"][-1] == data["summary"]["sim_time_s"]
    assert data["phases"][s["phase"][-1]] == data["summary"]["final_phase"]
    assert abs(s["fuel"][-1] - data["summary"]["fuel_remaining_kg"]) < 1e-3
    fault = next(e for e in data["events"] if e["type"] == "thruster_fault")
    assert fault["source"] == "truth"
    i = s["t"].index(next(t for t in s["t"] if t > fault["t"]))
    assert s["health"][i][data["thrusters"].index(fault["thruster"])] == 0

    abort = json.loads((tmp_path / "orbital_strike_abort.json").read_text())
    # The reentry happens after the last controller tick; it must still be exported.
    assert any(e["type"] == "reentry" and e["source"] == "truth" for e in abort["events"])
    assert abort["samples"]["t"][-1] == abort["summary"]["sim_time_s"]
    assert max(e["t"] for e in abort["events"]) <= abort["samples"]["t"][-1]
    assert abs(abort["samples"]["x"][-1] - abort["summary"]["final_position_m"][0]) < 0.01
    assert abs(abort["samples"]["y"][-1] - abort["summary"]["final_position_m"][1]) < 0.01


def test_llm_export_records_advice_without_changing_flight(tmp_path, monkeypatch):
    from telemetry import export_web
    monkeypatch.setattr(export_web, "DEFAULT_RUNS_DIR", tmp_path / "logs")
    server = FakeOllama([json.dumps(proposal())])
    monkeypatch.setenv("SINGULAROS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("SINGULAROS_LLM_URL", server.url)
    monkeypatch.setenv("SINGULAROS_LLM_MODEL", MODEL)
    try:
        main(["--out", str(tmp_path / "offline"), "thruster_fault"])
        main(["--llm", "--out", str(tmp_path / "llm"), "thruster_fault"])
        offline = json.loads((tmp_path / "offline/thruster_fault.json").read_text())
        llm = json.loads((tmp_path / "llm/thruster_fault.json").read_text())
        assert llm["advisor"] == {"enabled": True, "backend": "ollama", "model": MODEL, "recorded": True}
        assert llm["samples"] == offline["samples"]
        advice = [e for e in llm["events"] if e["type"] == "llm_advice"]
        assert advice and advice[0]["proposal"]["rationale"] == proposal()["rationale"]
        assert "review" in advice[0]
        assert any(path == "/api/chat" for path, _ in server.requests)
    finally:
        server.close()


def test_requested_llm_export_reports_unavailable_server(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SINGULAROS_LLM_BACKEND", "ollama")
    monkeypatch.setenv("SINGULAROS_LLM_URL", "http://127.0.0.1:9")
    with pytest.raises(SystemExit) as error:
        main(["--llm", "--out", str(tmp_path), "thruster_fault"])
    assert error.value.code == 2
    assert "advisor unavailable" in capsys.readouterr().err.lower()
    assert not (tmp_path / "index.json").exists()
