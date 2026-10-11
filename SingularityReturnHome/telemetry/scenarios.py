"""Scenario files shipped with the simulator."""

from pathlib import Path

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


def scenario_names():
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.toml"))
