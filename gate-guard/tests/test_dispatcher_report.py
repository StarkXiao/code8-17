import json
import unittest
from datetime import datetime

from gate_guard.dispatcher import BlockedInstructionError, GateDispatcher
from gate_guard.models import GateAction, Instruction, Severity
from gate_guard.report import render_json, render_text

from tests._factories import build_scenario

T0 = datetime(2026, 9, 20, 8, 0, 0)


class TestDispatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = build_scenario()
        self.dispatcher = GateDispatcher(self.scenario)

    def test_valid_instruction_dispatched_and_state_changes(self) -> None:
        ins = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=1.0, duration_hours=1.0, hold_hours=2.0,
        )
        result = self.dispatcher.submit(ins)
        self.assertTrue(result.dispatched)
        self.assertEqual(self.dispatcher.state.openings["G1"], 1.0)
        self.assertEqual(len(self.dispatcher.history), 1)

    def test_conflict_blocked_and_state_untouched(self) -> None:
        ins = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=9.0,  # 超上限
        )
        result = self.dispatcher.submit(ins)
        self.assertTrue(result.blocked)
        self.assertEqual(self.dispatcher.state.openings["G1"], 0.8)

    def test_raise_on_block(self) -> None:
        ins = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=9.0,
        )
        with self.assertRaises(BlockedInstructionError) as ctx:
            self.dispatcher.submit(ins, raise_on_block=True)
        self.assertFalse(ctx.exception.verdict.accepted)

    def test_batch_mixed_dispatch(self) -> None:
        good = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=1.0, duration_hours=1.0, hold_hours=2.0,
        )
        bad = Instruction(
            gate_id="G9", action=GateAction.SET_OPENING,
            issued_at=datetime(2026, 9, 20, 8, 30), target_opening=1.0,
        )
        results = self.dispatcher.submit_batch([bad, good])  # 乱序输入，按时间排序
        self.assertEqual([r.dispatched for r in results], [True, False])
        # 闸门状态最终反映被放行的那条
        self.assertEqual(self.dispatcher.state.openings["G1"], 1.0)

    def test_stop_on_block(self) -> None:
        bad = Instruction(
            gate_id="G9", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=1.0,
        )
        good = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING,
            issued_at=datetime(2026, 9, 20, 9, 0),
            target_opening=1.0, duration_hours=1.0, hold_hours=2.0,
        )
        results = self.dispatcher.submit_batch([bad, good], stop_on_block=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].blocked)


class TestReport(unittest.TestCase):
    def test_text_report_contains_verdict_summary(self) -> None:
        scenario = build_scenario()
        dispatcher = GateDispatcher(scenario)
        good = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=1.0, duration_hours=1.0, hold_hours=2.0,
        )
        bad = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=9.0,
        )
        results = dispatcher.submit_batch([good, bad])
        text = render_text(results)
        self.assertIn("通过", text)
        self.assertIn("阻断", text)
        self.assertIn("OPENING_OUT_OF_RANGE", text)
        self.assertIn("汇总：共 2 条", text)

    def test_json_report_parseable(self) -> None:
        scenario = build_scenario()
        dispatcher = GateDispatcher(scenario)
        ins = Instruction(
            gate_id="G1", action=GateAction.SET_OPENING, issued_at=T0,
            target_opening=9.0,
        )
        result = dispatcher.submit(ins)
        payload = json.loads(render_json([result]))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["blocked"], 1)
        self.assertFalse(payload["verdicts"][0]["accepted"])
        self.assertFalse(payload["verdicts"][0]["dispatched"])
        self.assertEqual(
            payload["verdicts"][0]["violations"][0]["severity"],
            Severity.ERROR.value,
        )


if __name__ == "__main__":
    unittest.main()
