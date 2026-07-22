import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from valorant_coach.db import Database
from valorant_coach.parameters import (
    extract_sum_plus_one,
    list_parameters,
    parameter_dashboard,
    save_parameter_label,
)


class ParameterTrainerTests(unittest.TestCase):
    def test_default_parameters_are_seeded(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")

            payload = list_parameters(db)

            keys = {item["parameter_key"] for item in payload["parameters"]}
            self.assertIn("round_score_left", keys)
            self.assertIn("round_score_right", keys)
            self.assertIn("round_number", keys)
            self.assertIn("combat_report_damage_taken", keys)

    def test_round_number_is_derived_from_score_dependencies(self):
        read = extract_sum_plus_one(
            {"dependencies": ["round_score_left", "round_score_right"], "config": {"valid_min": 1, "valid_max": 30}},
            {
                "round_score_left": {"value": 3, "confidence": 0.82},
                "round_score_right": {"value": 5, "confidence": 0.76},
            },
        )

        self.assertEqual(read["value"], 9)
        self.assertEqual(read["status"], "derived")
        self.assertEqual(read["confidence"], 0.76)
        self.assertEqual(read["evidence"]["formula"], "sum(dependencies) + 1")

    def test_label_feedback_updates_dashboard_accuracy(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            list_parameters(db)
            read_id = db.save_parameter_read(
                {
                    "parameter_key": "round_score_left",
                    "match_id": 1,
                    "timestamp": 12.0,
                    "frame_id": "frame-a",
                    "value": "3",
                    "raw_text": "3",
                    "confidence": 0.8,
                    "evidence": {},
                    "status": "read",
                }
            )

            result = save_parameter_label(
                db,
                {
                    "parameter_key": "round_score_left",
                    "match_id": 1,
                    "timestamp": 12.0,
                    "frame_id": "frame-a",
                    "read_id": read_id,
                    "expected_value": "3",
                },
            )

            row = next(item for item in result["dashboard"]["parameters"] if item["parameter_key"] == "round_score_left")
            self.assertEqual(row["label_count"], 1)
            self.assertEqual(row["checked_count"], 1)
            self.assertEqual(row["accuracy"], 1.0)

    def test_dashboard_reports_training_gaps_for_unlabeled_parameters(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")

            dashboard = parameter_dashboard(db)

            self.assertTrue(dashboard["ok"])
            self.assertEqual(dashboard["readiness_percent"], 0)
            self.assertTrue(any(gap["parameter_key"] == "round_number" for gap in dashboard["gaps"]))


if __name__ == "__main__":
    unittest.main()
