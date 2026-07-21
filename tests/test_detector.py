import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from valorant_coach.db import Database
from valorant_coach.detector import (
    detector_status,
    export_detector_dataset,
    save_detector_annotation,
    xyxy_to_norm,
    yolo_label_line,
)


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


if __name__ == "__main__":
    unittest.main()
