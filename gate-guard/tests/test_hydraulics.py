import math
import unittest

from gate_guard.models import Gate


def make_gate(**kw) -> Gate:
    defaults = dict(
        id="G", name="G", upstream_pool="u", downstream_pool="d",
        width=8.0, sill_elevation=4.0, max_opening=3.0,
        discharge_coef=0.62, weir_coef=0.385, gate_to_weir_ratio=0.65,
    )
    defaults.update(kw)
    return Gate(**defaults)


class TestHydraulics(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = make_gate()

    def test_zero_or_negative_opening_is_zero_flow(self) -> None:
        from gate_guard.hydraulics import gate_discharge
        self.assertEqual(gate_discharge(self.gate, 6.5, 4.2, 0.0), 0.0)

    def test_free_orifice_discharge(self) -> None:
        # 自由孔流：Q = μ b e sqrt(2g(H1-e))，H1=2.5, e=0.5
        from gate_guard.hydraulics import gate_discharge
        q = gate_discharge(self.gate, 6.5, 4.2, 0.5)
        expected = 0.62 * 8 * 0.5 * math.sqrt(2 * 9.81 * (2.5 - 0.5))
        self.assertAlmostEqual(q, expected, places=6)
        self.assertGreater(q, 0)

    def test_submerged_orifice_when_tailwater_over_gate_lip(self) -> None:
        # H2=0.6 > e=0.4：淹没孔流，作用水头 H1-H2=1.9
        from gate_guard.hydraulics import gate_discharge
        q = gate_discharge(self.gate, 6.5, 4.6, 0.4)
        expected = 0.62 * 8 * 0.4 * math.sqrt(2 * 9.81 * 1.9)
        self.assertAlmostEqual(q, expected, places=6)

    def test_weir_flow_when_large_relative_opening(self) -> None:
        # e=2.0 >= 0.65*H1=1.625 → 堰流；下游水位 3.5 < 堰顶 4.0 → 自由出流 C_s=1
        from gate_guard.hydraulics import gate_discharge
        q = gate_discharge(self.gate, 6.5, 3.5, 2.0)
        expected = 0.385 * 8 * math.sqrt(2 * 9.81) * 2.5 ** 1.5
        self.assertAlmostEqual(q, expected, places=6)

    def test_submerged_weir_factor_reduces_flow(self) -> None:
        from gate_guard.hydraulics import gate_discharge
        q_free = gate_discharge(self.gate, 6.5, 3.5, 2.0)
        q_sub = gate_discharge(self.gate, 6.5, 6.0, 2.0)
        self.assertLess(q_sub, q_free)

    def test_reverse_flow_is_negative_and_blocked_in_verifier(self) -> None:
        from gate_guard.hydraulics import gate_discharge
        q = gate_discharge(self.gate, 4.2, 6.5, 0.8)
        self.assertLess(q, 0)

    def test_discharge_monotone_in_opening(self) -> None:
        from gate_guard.hydraulics import gate_discharge
        levels = [(6.5, 4.2), (6.0, 4.5), (7.0, 5.0)]
        for lu, ld in levels:
            qs = [gate_discharge(self.gate, lu, ld, e / 100) for e in range(0, 301, 10)]
            for a, b in zip(qs, qs[1:]):
                self.assertGreaterEqual(b, a - 1e-9)

    def test_opening_for_flow_inversion_hits_target(self) -> None:
        from gate_guard.hydraulics import gate_discharge, opening_for_flow
        # 取孔流单调段内的目标流量
        for target in (10.0, 20.0, 30.0):
            e, q, ok = opening_for_flow(self.gate, 6.5, 4.2, target)
            self.assertTrue(ok)
            self.assertAlmostEqual(q, target, places=4)
            self.assertLessEqual(e, self.gate.max_opening)

    def test_opening_for_flow_infeasible_beyond_capacity(self) -> None:
        from gate_guard.hydraulics import opening_for_flow
        e, q, ok = opening_for_flow(self.gate, 6.5, 4.2, 500.0)
        self.assertFalse(ok)
        self.assertEqual(e, self.gate.max_opening)

    def test_nonpositive_flow_means_gate_closed(self) -> None:
        from gate_guard.hydraulics import opening_for_flow
        e, q, ok = opening_for_flow(self.gate, 6.5, 4.2, 0.0)
        self.assertEqual((e, q, ok), (0.0, 0.0, True))


if __name__ == "__main__":
    unittest.main()
