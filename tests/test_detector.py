import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from valorant_coach.db import Database
from valorant_coach.detector import (
    build_detector_candidates,
    detector_status,
    export_detector_dataset,
    list_detector_candidates,
    save_detector_annotation,
    xyxy_to_norm,
    yolo_label_line,
)
from valorant_coach.signals import signal_registry


class DetectorTests(unittest.TestCase):
    def test_yolo_label_line_uses_center_coordinates(self):
        line = yolo_label_line(1, {"x": 0.40, "y": 0.20, "w": 0.10, "h": 0.30})

        self.assertEqual(line, "1 0.450000 0.350000 0.100000 0.300000")

    def test_xyxy_to_norm_converts_absolute_box(self):
        bbox = xyxy_to_norm([100, 50, 300, 250], 1000, 500)

        self.assertEqual(bbox, {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.4})

    def test_save_annotation_and_export_dataset(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "coach.sqlite3")
            match_id = db.upsert_match(str(root / "match.mp4"), "now", "imported")
            death_id = db.create_death(match_id, None, 12.0, ["test"], "", 0.8)
            frame_dir = root / "vision" / "frames"
            frame_dir.mkdir(parents=True)
            frame = frame_dir / "frame-a.jpg"
            Image.new("RGB", (100, 100), "black").save(frame)

            saved = save_detector_annotation(
                db,
                death_id,
                {
                    "frame_id": "frame-a",
                    "label": "enemy_head",
                    "bbox_norm": {"x": 0.4, "y": 0.2, "w": 0.1, "h": 0.2},
                },
            )
            exported = export_detector_dataset(db, root)

            self.assertTrue(saved["ok"])
            self.assertTrue(exported["ok"])
            self.assertEqual(exported["boxes"], 1)
            label_file = root / "detector_dataset" / "labels" / "train" / "frame-a.txt"
            self.assertTrue(label_file.exists())
            self.assertEqual(label_file.read_text(encoding="utf-8").strip(), "1 0.450000 0.300000 0.100000 0.200000")

    def test_status_reports_unconfigured_detector(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "coach.sqlite3")

            status = detector_status(db, root)

            self.assertFalse(status["configured"])
            self.assertIn("annotations", status)

    def test_candidate_queue_uses_saved_keyframes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "coach.sqlite3")
            match_id = db.upsert_match(str(root / "match.mp4"), "now", "imported")
            death_id = db.create_death(match_id, None, 20.0, ["test"], "", 0.8)
            frame_dir = root / "vision" / "frames"
            frame_dir.mkdir(parents=True)
            Image.new("RGB", (100, 100), "black").save(frame_dir / "candidate-a.jpg")
            db.save_death_analysis(
                death_id,
                "keyframes",
                {
                    "frames": [
                        {
                            "frame_id": "candidate-a",
                            "sequence_index": 1,
                            "relative_second": -1.2,
                            "seconds_before_death": 1.2,
                            "role": "contact",
                            "timestamp": 18.8,
                        }
                    ]
                },
            )

            built = build_detector_candidates(db, root, {"match_id": match_id})
            listed = list_detector_candidates(db, match_id)

            self.assertTrue(built["ok"])
            self.assertEqual(built["count"], 1)
            self.assertEqual(listed["candidates"][0]["frame_id"], "candidate-a")
            self.assertEqual(listed["candidates"][0]["status"], "needs_label")

    def test_signal_registry_defines_claim_boundaries(self):
        registry = signal_registry()

        self.assertGreater(len(registry["signals"]), 5)
        for item in registry["signals"]:
            self.assertTrue(item["meaning"])
            self.assertTrue(item["not_meaning"])
            self.assertTrue(item["source_type"])
            self.assertTrue(item["allowed_claims"])
            self.assertTrue(item["forbidden_claims"])
        self.assertIn("contact_proxy", registry["by_id"])
        self.assertIn("Do not say confirmed enemy detected.", registry["by_id"]["contact_proxy"]["forbidden_claims"])


if __name__ == "__main__":
    unittest.main()
