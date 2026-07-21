import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from valorant_coach.automation import (
    apply_deterministic_review_fallback,
    budget_local_model_payload,
    local_model_system_prompt,
)
from valorant_coach.vision import build_timeline, local_ai_death_anchor_timestamp, normalize_death_scan_options


def frame(index, relative_second):
    return {
        "index": index,
        "sequence_index": index,
        "relative_second": relative_second,
        "seconds_before_death": max(0.0, -relative_second),
        "caption": f"Frame {index}: {relative_second}s.",
        "metrics": {
            "death_score": 0.8 if relative_second >= -0.5 else 0.1,
            "pressure_score": 0.6 if -2.0 <= relative_second <= 0 else 0.1,
            "crosshair_activity": 0.2 if -3.0 <= relative_second <= 0 else 0.05,
        },
    }


class ClipCoachPipelineTests(unittest.TestCase):
    def test_budget_keeps_representative_clip_timeline(self):
        frames = [frame(index + 1, -6.0 + index * 0.1) for index in range(60)]
        payload = {"prompt": "VALORANT context. " * 900, "keyframes": frames}
        status = {"context_limit": "8192", "image_token_estimate": "900", "purpose": "coach"}

        budgeted, budget = budget_local_model_payload(payload, status, local_model_system_prompt(status), 900, "clip_review")
        sent = budgeted["keyframes"]
        sent_indices = [item["index"] for item in sent]

        self.assertGreaterEqual(len(sent), 3)
        self.assertLess(len(sent), len(frames))
        self.assertIn(1, sent_indices)
        self.assertIn(60, sent_indices)
        self.assertTrue(any(-2.5 <= float(item["relative_second"]) <= 0.35 for item in sent))
        self.assertEqual(budget["sent_frames"], len(sent))
        self.assertTrue(budget["trimmed"])

    def test_deterministic_fallback_does_not_replace_model_review(self):
        result = {
            "summary": "insufficient visual evidence",
            "better_play": "",
            "visible_evidence": [],
            "evidence_timeline": [],
            "confidence": 0.2,
            "status": "completed",
        }
        payload = {
            "keyframes": [frame(1, -1.0), frame(2, -0.2)],
            "segments": [],
            "visual_signals": {
                "status": "completed",
                "confidence": 0.7,
                "first_contact": {"frame": 2, "relative_second": -0.2},
                "death_cue": {"frame": 2, "relative_second": -0.1},
                "crosshair_score": {"risk": "late correction", "summary": "Crosshair late correction; score 48/100."},
                "movement_read": {"risk": "moving during contact", "summary": "Movement moving during contact; contact motion 0.22."},
                "minimap_read": {"risk": "low signal", "summary": "Minimap low signal; 0 pressure-overlap frame(s)."},
                "enemy_visibility_timeline": [{"frame": 2}],
            },
            "ocr_regions": {},
            "privacy": "local-only",
        }

        updated = apply_deterministic_review_fallback(result, payload)

        self.assertEqual(updated["summary"], "insufficient visual evidence")
        self.assertIn("fallback_reason", updated)
        self.assertIn("fallback_support", updated)
        self.assertTrue(updated["fallback_support"]["better_play"])
        self.assertIn("crosshair readiness", updated["fallback_support"]["labels"])
        self.assertTrue(updated["review_diagnostics"]["model_weak"])

    def test_combat_report_marker_anchor_shifts_earlier(self):
        death = {
            "timestamp": 100.0,
            "notes": "Primary detector: combat report appeared but killfeed/player-name confirmation was unavailable.",
            "mistake_labels": ["needs manual review"],
        }

        anchor = local_ai_death_anchor_timestamp(death)

        self.assertEqual(anchor["source"], "combat_report_only_adjusted")
        self.assertEqual(anchor["original_timestamp"], 100.0)
        self.assertLess(anchor["timestamp"], 100.0)

    def test_death_scan_options_accept_range_and_limit(self):
        options = normalize_death_scan_options({"start_seconds": "60", "end_seconds": "120", "limit": "5"})

        self.assertEqual(options["start_seconds"], 60.0)
        self.assertEqual(options["end_seconds"], 120.0)
        self.assertEqual(options["limit"], 5)
        self.assertEqual(options["mode"], "range")

    def test_timeline_timestamp_offset_preserves_vod_time(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.jpg"
            Image.new("RGB", (64, 64), "black").save(path)

            timeline = build_timeline([path], sample_interval=1.0, timestamp_offset=90.0)

        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].timestamp, 90.0)


if __name__ == "__main__":
    unittest.main()
