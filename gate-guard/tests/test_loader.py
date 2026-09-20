import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gate_guard.loader import LoaderError, load_instructions, load_scenario

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples"


class TestLoader(unittest.TestCase):
    def test_load_example_scenario(self) -> None:
        sc = load_scenario(EXAMPLE_DIR / "scenario.json")
        self.assertEqual(set(sc.pools), {"upstream", "downstream"})
        self.assertEqual(set(sc.gates), {"G1"})
        self.assertEqual(sc.constraints.min_total_flow, 20.0)
        self.assertEqual(sc.gates["G1"].initial_opening, 0.8)
        self.assertIsInstance(sc.start_time, datetime)

    def test_load_instructions_with_relative_time(self) -> None:
        sc = load_scenario(EXAMPLE_DIR / "scenario.json")
        instructions = load_instructions(EXAMPLE_DIR / "instructions.json", sc)
        self.assertEqual(len(instructions), 6)
        self.assertEqual(
            instructions[0].issued_at,
            datetime(2026, 9, 20, 8, 0, 0),
        )
        self.assertEqual(
            instructions[1].issued_at,
            datetime(2026, 9, 20, 8, 30, 0),
        )

    def test_missing_file_raises_loader_error(self) -> None:
        with self.assertRaises(LoaderError):
            load_scenario("/nonexistent/scenario.json")

    def test_missing_required_field(self) -> None:
        with self.assertRaises(LoaderError):
            load_scenario({"pools": [], "gates": []})

    def test_gate_referencing_unknown_pool_rejected(self) -> None:
        data = {
            "pools": [{"id": "a", "surface_area": 100, "initial_level": 1.0,
                       "bounds": {"min": 0, "max": 2}}],
            "gates": [{"id": "g", "upstream_pool": "a", "downstream_pool": "b",
                       "width": 2, "sill_elevation": 0, "max_opening": 1}],
        }
        with self.assertRaises(LoaderError):
            load_scenario(data)

    def test_inverted_bounds_rejected(self) -> None:
        data = {
            "pools": [{"id": "a", "surface_area": 100, "initial_level": 1.0,
                       "bounds": {"min": 3, "max": 2}}],
            "gates": [{"id": "g", "upstream_pool": "a", "downstream_pool": "a",
                       "width": 2, "sill_elevation": 0, "max_opening": 1}],
        }
        with self.assertRaises(LoaderError):
            load_scenario(data)

    def test_initial_level_out_of_bounds_rejected(self) -> None:
        data = {
            "pools": [{"id": "a", "surface_area": 100, "initial_level": 5.0,
                       "bounds": {"min": 0, "max": 2}}],
            "gates": [],
        }
        with self.assertRaises(LoaderError):
            load_scenario(data)

    def test_relative_time_without_base_rejected(self) -> None:
        data = {"instructions": [
            {"gate_id": "G1", "action": "SET_OPENING",
             "target_opening": 1.0, "issued_at": "+1h"}]}
        with self.assertRaises(LoaderError):
            load_instructions(data, scenario=None)

    def test_round_trip_json_file(self) -> None:
        sc = load_scenario(EXAMPLE_DIR / "scenario.json")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ins.json"
            path.write_text(json.dumps({"instructions": [
                {"gate_id": "G1", "action": "SET_FLOW",
                 "target_flow": 30.0, "issued_at": "2026-09-20T08:00:00"}
            ]}), encoding="utf-8")
            loaded = load_instructions(path, sc)
        self.assertEqual(loaded[0].target_flow, 30.0)


if __name__ == "__main__":
    unittest.main()
