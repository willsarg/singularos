import json

from telemetry.export_web import FIELDS, main


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
