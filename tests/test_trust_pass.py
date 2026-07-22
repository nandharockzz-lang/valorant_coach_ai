import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from valorant_coach.automation import death_evidence, normalize_ocr_region_name, ocr_health_check, ocr_health_region_metadata
from valorant_coach.db import Database
from valorant_coach.reports import build_report


class TrustPassTests(unittest.TestCase):
    def test_confirmed_death_blocks_nearby_suggestion(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "match.mp4"), "now", "imported")
            db.create_death(match_id, None, 100.0, ["needs manual review"], "", 1.0)

            suggestion_id = db.create_death_suggestion(match_id, 103.0, "near death", 0.7, None)

            self.assertIsNone(suggestion_id)

    def test_rejected_suggestion_blocks_duplicate_suggestion(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "match.mp4"), "now", "imported")
            first_id = db.create_death_suggestion(match_id, 50.0, "candidate", 0.6, None)
            self.assertIsNotNone(first_id)
            db.update_death_suggestion_status(int(first_id), "rejected")

            duplicate_id = db.create_death_suggestion(match_id, 53.0, "duplicate", 0.9, None)

            self.assertIsNone(duplicate_id)

    def test_clear_pending_suggestions_preserves_reviewed_history(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "match.mp4"), "now", "imported")
            pending = db.create_death_suggestion(match_id, 10.0, "pending", 0.5, None)
            accepted = db.create_death_suggestion(match_id, 40.0, "accepted", 0.8, None)
            rejected = db.create_death_suggestion(match_id, 80.0, "rejected", 0.4, None)
            db.update_death_suggestion_status(int(accepted), "accepted")
            db.update_death_suggestion_status(int(rejected), "rejected")

            cleared = db.clear_pending_death_suggestions(match_id)
            remaining = db.list_death_suggestions(match_id)

            self.assertEqual(cleared, 1)
            self.assertEqual([item["id"] for item in remaining], [accepted, rejected])
            self.assertNotIn(pending, [item["id"] for item in remaining])

    def test_death_evidence_reports_gaps_for_unanalyzed_marker(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "match.mp4"), "now", "imported")
            death_id = db.create_death(match_id, None, 25.0, ["needs manual review"], "manual", 1.0)

            evidence = death_evidence(db, death_id)

            self.assertTrue(evidence["ok"])
            self.assertEqual(evidence["marker"]["status"], "confirmed")
            self.assertTrue(any("No keyframes" in gap for gap in evidence["gaps"]))
            self.assertTrue(any("No local AI" in gap for gap in evidence["gaps"]))

    def test_report_explains_unknown_round_reason(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "match.mp4"), "now", "imported")
            db.create_death(match_id, None, None, ["needs manual review"], "", 1.0)

            report = build_report(db, match_id)

            self.assertIn("round_unknown_reason", report["deaths"][0])
            self.assertIn("no timestamp", report["deaths"][0]["round_unknown_reason"])

    def test_ocr_health_reports_missing_video(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            match_id = db.upsert_match(str(Path(tmp) / "missing.mp4"), "now", "imported")

            result = ocr_health_check(db, match_id, Path(tmp) / "deep", {})

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "missing_video")

    def test_ocr_region_aliases_are_user_friendly(self):
        self.assertEqual(normalize_ocr_region_name("Top Score"), "hud_top")
        self.assertEqual(normalize_ocr_region_name("bottom-hud"), "hud_bottom")
        self.assertEqual(ocr_health_region_metadata("combat_report")["label"], "Combat Report")

    def test_reset_calibration_restores_defaults(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "coach.sqlite3")
            original = db.get_calibration()["killfeed"]
            db.save_calibration({"killfeed": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}})
            self.assertNotEqual(db.get_calibration()["killfeed"], original)

            db.reset_calibration(["killfeed"])

            self.assertEqual(db.get_calibration()["killfeed"], original)


if __name__ == "__main__":
    unittest.main()
