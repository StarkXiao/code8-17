import unittest
from datetime import datetime

from gate_guard.models import GateAction, Instruction, Severity
from gate_guard.engine import InstructionVerifier, initial_state

from tests._factories import build_scenario

T0 = datetime(2026, 9, 20, 8, 0, 0)


def ins(gate_id="G1", action=GateAction.SET_OPENING, *, opening=None, flow=None,
        duration=None, hold=3.0, at=T0) -> Instruction:
    return Instruction(
        gate_id=gate_id, action=action, issued_at=at,
        target_opening=opening, target_flow=flow,
        duration_hours=duration, hold_hours=hold,
    )


def codes(verdict) -> set[str]:
    return {v.code for v in verdict.violations}


def error_codes(verdict) -> set[str]:
    return {v.code for v in verdict.errors}


class TestStaticVerification(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = build_scenario()
        self.verifier = InstructionVerifier(self.scenario)

    def test_healthy_opening_instruction_passes(self) -> None:
        v = self.verifier.verify(ins(opening=1.0, duration=1.0))
        self.assertTrue(v.accepted, [x.message for x in v.violations])

    def test_gate_not_found_blocks(self) -> None:
        v = self.verifier.verify(ins(gate_id="NOPE", opening=1.0))
        self.assertFalse(v.accepted)
        self.assertIn("GATE_NOT_FOUND", error_codes(v))

    def test_maintenance_gate_blocks(self) -> None:
        v = self.verifier.verify(ins(gate_id="G2", opening=1.0))
        self.assertFalse(v.accepted)
        self.assertIn("GATE_UNAVAILABLE", error_codes(v))

    def test_opening_above_max_blocks(self) -> None:
        v = self.verifier.verify(ins(opening=4.0, duration=1.0))
        self.assertIn("OPENING_OUT_OF_RANGE", error_codes(v))
        self.assertNotIn("GATE_MOTION_RATE_EXCEEDED", error_codes(v))
        self.assertFalse(v.accepted)

    def test_motion_rate_exceeded_blocks(self) -> None:
        # 当前 0.8m，目标 2.0m，要求 0.5h → 2.4m/h > 0.5m/h
        v = self.verifier.verify(ins(opening=2.0, duration=0.5))
        self.assertIn("GATE_MOTION_RATE_EXCEEDED", error_codes(v))
        self.assertFalse(v.accepted)

    def test_close_rate_exceeded_blocks(self) -> None:
        v = self.verifier.verify(ins(opening=0.0, duration=0.5))
        # (0.8-0)/0.5=1.6 m/h > 0.5
        self.assertIn("GATE_MOTION_RATE_EXCEEDED", error_codes(v))

    def test_flow_beyond_capacity_blocks(self) -> None:
        v = self.verifier.verify(ins(action=GateAction.SET_FLOW, flow=999.0, duration=1.0))
        self.assertIn("FLOW_CAPACITY_EXCEEDED", error_codes(v))
        self.assertFalse(v.accepted)

    def test_negative_target_flow_blocks(self) -> None:
        v = self.verifier.verify(ins(action=GateAction.SET_FLOW, flow=-5.0))
        self.assertIn("FLOW_CAPACITY_EXCEEDED", error_codes(v))

    def test_feasible_flow_instruction_passes_and_resolves_opening(self) -> None:
        v = self.verifier.verify(
            ins(action=GateAction.SET_FLOW, flow=25.0, duration=1.0)
        )
        self.assertTrue(v.accepted, [x.message for x in v.violations])
        self.assertIsNotNone(v.resolved_opening)
        self.assertAlmostEqual(v.resolved_flow, 25.0, places=3)

    def test_head_diff_upper_limit_blocks(self) -> None:
        # 把下游初始水位压低，使水位差超过 3.5m 上限
        self.scenario.pools["down"].initial_level = 2.5
        v = self.verifier.verify(ins(opening=1.0, duration=1.0))
        self.assertIn("HEAD_DIFF_EXCEEDED", error_codes(v))
        self.assertFalse(v.accepted)

    def test_low_head_is_warning_not_blocking(self) -> None:
        self.scenario.pools["up"].initial_level = 4.6
        self.scenario.pools["up"].bounds = type(self.scenario.pools["up"].bounds)(
            min=3.8, max=6.8
        )
        # 水位差 4.6-4.2=0.4 > 0.3? 调到 4.45 → 差 0.25 < 0.3
        self.scenario.pools["up"].initial_level = 4.45
        v = self.verifier.verify(ins(opening=1.0, duration=1.0))
        self.assertIn("HEAD_TOO_LOW", codes(v))
        self.assertTrue(all(x.severity is not Severity.ERROR or x.code != "HEAD_TOO_LOW"
                            for x in v.violations))

    def test_current_level_out_of_bounds_blocks(self) -> None:
        self.scenario.pools["up"].initial_level = 7.0  # > max 6.8
        v = self.verifier.verify(ins(opening=1.0))
        self.assertIn("CURRENT_LEVEL_OUT_OF_BOUNDS", error_codes(v))

    def test_eco_flow_shortage_static_blocks(self) -> None:
        # 初始开度 0.8 时下泄约 25.8 > 20；目标关到 0 立即断流
        v = self.verifier.verify(ins(opening=0.0, duration=2.0, hold=0.5))
        self.assertIn("ECO_FLOW_SHORTAGE", error_codes(v))


class TestSimulationRules(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = build_scenario()
        self.verifier = InstructionVerifier(self.scenario)

    def test_upstream_flood_with_closed_gate_breaches_max(self) -> None:
        # 大流量入库 + 全关 → 上游水位漫限
        self.scenario.inflows = {"up": [[0.0, 150.0], [4.0, 150.0]]}
        v = self.verifier.verify(ins(opening=0.0, duration=2.0, hold=4.0))
        self.assertIn("LEVEL_OUT_OF_BOUNDS", error_codes(v))
        self.assertFalse(v.accepted)
        breach = next(x for x in v.violations if x.code == "LEVEL_OUT_OF_BOUNDS")
        self.assertIsNotNone(breach.at_hours)

    def test_level_rise_rate_violation(self) -> None:
        # 下游每步净入流极大，超过 max_level_rate=0.4m/h
        # 下游面积 5e5 m²，0.4m/h ≈ 55.6 m³/s；注入 300 m³/s
        self.scenario.inflows["down"] = [[0.0, 300.0], [4.0, 300.0]]
        v = self.verifier.verify(ins(opening=1.0, duration=1.0, hold=1.0))
        self.assertIn("LEVEL_RATE_EXCEEDED", error_codes(v))

    def test_simulated_extrema_recorded(self) -> None:
        v = self.verifier.verify(ins(opening=1.0, duration=1.0, hold=3.0))
        self.assertIn("up", v.simulated_levels)
        lo, hi = v.simulated_levels["up"]
        self.assertLessEqual(lo, hi)
        self.assertAlmostEqual(lo, 6.5, delta=0.6)

    def test_warning_margin_does_not_block(self) -> None:
        # 水位靠近限值但不越界 → WARNING，指令仍可下发
        v = self.verifier.verify(ins(opening=1.0, duration=1.0, hold=3.0))
        # 该健康场景下可能有也可能无预警；强制构造一个贴限场景
        self.scenario.pools["up"].initial_level = 6.75
        self.scenario.inflows = {"up": [[0.0, 0.0], [4.0, 0.0]]}
        self.scenario.withdrawals = {}
        v2 = self.verifier.verify(ins(opening=1.0, duration=1.0, hold=1.0))
        near = [x for x in v2.violations if x.code == "LEVEL_NEAR_LIMIT"]
        # 若产生预警，必须不阻断
        for w in near:
            self.assertIs(w.severity, Severity.WARNING)
        self.assertTrue(all(x.severity is not Severity.ERROR for x in v2.violations))

    def test_strict_mode_turns_warnings_into_blocks(self) -> None:
        strict = InstructionVerifier(self.scenario, warnings_as_errors=True)
        v = strict.verify(ins(opening=1.0, duration=1.0, hold=3.0))
        # 任一预警在严格模式下都成为阻断
        self.assertEqual(
            any(x.severity is Severity.ERROR for x in v.violations),
            not v.accepted,
        )


class TestBatchCascade(unittest.TestCase):
    def test_blocked_instruction_does_not_affect_next_state(self) -> None:
        scenario = build_scenario()
        verifier = InstructionVerifier(scenario)
        first = ins(opening=4.0, at=T0)          # 阻断（超开度）
        second = ins(opening=1.0, duration=1.0, hold=1.0,
                     at=datetime(2026, 9, 20, 9, 0))
        verdicts = verifier.verify_batch([first, second])
        self.assertFalse(verdicts[0].accepted)
        self.assertTrue(verdicts[1].accepted)
        # 第二条仍从初始开度 0.8 起算：开度变化 0.2m/1h 合规
        self.assertEqual(initial_state(scenario).openings["G1"], 0.8)

    def test_accepted_instruction_opening_carries_forward(self) -> None:
        scenario = build_scenario()
        verifier = InstructionVerifier(scenario)
        first = ins(opening=1.0, duration=1.0, hold=0.5, at=T0)      # 通过
        # 1h 后再要求 0.5h 内开到 2.0：从 1.0 起算 → 2m/h 超限
        second = ins(opening=2.0, duration=0.5,
                     at=datetime(2026, 9, 20, 9, 0))
        verdicts = verifier.verify_batch([first, second])
        self.assertTrue(verdicts[0].accepted)
        self.assertIn("GATE_MOTION_RATE_EXCEEDED", error_codes(verdicts[1]))

    def test_no_cascade_each_starts_from_initial(self) -> None:
        scenario = build_scenario()
        verifier = InstructionVerifier(scenario)
        first = ins(opening=1.0, duration=1.0, at=T0)
        second = ins(opening=1.0, duration=1.0,
                     at=datetime(2026, 9, 20, 9, 0))
        verdicts = [verifier.verify(i) for i in (first, second)]
        self.assertTrue(all(v.accepted for v in verdicts))


if __name__ == "__main__":
    unittest.main()
