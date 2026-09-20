"""测试用最小场景构造工具。"""

from __future__ import annotations

from datetime import datetime

from gate_guard.models import (
    Gate,
    GateStatus,
    LevelBounds,
    Pool,
    Scenario,
    SystemConstraints,
)


def build_scenario(**overrides) -> Scenario:
    pools = {
        "up": Pool(
            id="up", name="上游",
            bounds=LevelBounds(min=6.0, max=6.8),
            surface_area=2_000_000.0,
            initial_level=6.5,
            min_level_rate=0.5, max_level_rate=0.3,
        ),
        "down": Pool(
            id="down", name="下游",
            bounds=LevelBounds(min=3.8, max=5.0),
            surface_area=500_000.0,
            initial_level=4.2,
            min_level_rate=0.6, max_level_rate=0.4,
        ),
    }
    gates = {
        "G1": Gate(
            id="G1", name="1号闸",
            upstream_pool="up", downstream_pool="down",
            width=8.0, sill_elevation=4.0, max_opening=3.0,
            open_rate=0.5, close_rate=0.5, initial_opening=0.8,
        ),
        "G2": Gate(
            id="G2", name="2号闸(检修)",
            upstream_pool="up", downstream_pool="down",
            width=5.0, sill_elevation=4.0, max_opening=2.0,
            status=GateStatus.MAINTENANCE,
        ),
    }
    constraints = SystemConstraints(
        min_head_diff=0.3, max_head_diff=3.5,
        min_total_flow=20.0, warning_margin=0.1,
    )
    scenario = Scenario(
        pools=pools, gates=gates, constraints=constraints,
        start_time=datetime(2026, 9, 20, 8, 0, 0),
        step_hours=0.25, default_horizon_hours=3.0,
    )
    for key, value in overrides.items():
        setattr(scenario, key, value)
    return scenario
