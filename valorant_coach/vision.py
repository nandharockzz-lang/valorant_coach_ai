import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .db import Database


FRAME_DIR_NAME = "frames"
DEFAULT_PLAYER_NAME = "SicaJR"


@dataclass
class FrameMetrics:
    path: Path
    timestamp: float
    brightness: float
    contrast: float
    center_dark: float
    center_red: float
    killfeed_red: float
    bottom_dark: float
    motion: float
    crosshair_activity: float
    crosshair_drift: float
    minimap_activity: float
    minimap_motion: float
    combat_report_score: float
    death_score: float
    pressure_score: float
    reason: str


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    local = root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    candidates = [
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def suggest_deaths(
    db: Database,
    match_id: int,
    work_dir: Path,
    update: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "suggestions": []}

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {
            "ok": False,
            "message": "ffmpeg is required for automatic video scanning. Install ffmpeg and make sure it is on PATH.",
            "suggestions": [],
        }

    frame_dir = work_dir / FRAME_DIR_NAME / f"match-{match_id}"
    db.log("info", "death_detector", f"Find Deaths started for match #{match_id}")
    progress(update, "Find Deaths: cleaning old pending duplicates.", 3)
    cleaned = db.cleanup_pending_death_suggestions(match_id)
    fps = death_scan_fps(db)
    progress(update, f"Find Deaths: extracting scan frames at {fps} FPS.", 8)
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps=fps)
    progress(update, f"Find Deaths: extracted {len(frames)} frame(s); building visual timeline.", 24)
    feedback = db.detector_feedback_summary()
    feedback["sensitivity"] = db.get_setting("detector_sensitivity", "normal")
    player_name = str(db.get_setting("player_name", DEFAULT_PLAYER_NAME) or DEFAULT_PLAYER_NAME).strip()
    ocr_available = bool(tesseract_path())
    max_ocr_frames = death_scan_max_ocr_frames(db)
    suggestions = analyze_frames(
        frames,
        db.get_calibration(),
        feedback,
        player_name=player_name,
        evidence_dir=frame_dir / "evidence",
        fps=fps,
        update=update,
        max_ocr_frames=max_ocr_frames,
    )
    progress(update, f"Find Deaths: saving {len(suggestions)} candidate marker(s).", 90)
    saved = []
    skipped = 0
    seen_ids = set()
    for item in suggestions:
        suggestion_id = db.create_death_suggestion(
            match_id=match_id,
            timestamp=item["timestamp"],
            reason=item["reason"],
            confidence=item["confidence"],
            frame_path=item.get("frame_path"),
        )
        if suggestion_id is None or suggestion_id in seen_ids:
            skipped += 1
            continue
        seen_ids.add(suggestion_id)
        item["id"] = suggestion_id
        saved.append(item)
    cleaned += db.cleanup_pending_death_suggestions(match_id)
    saved = [item for item in saved if db.get_death_suggestion(int(item["id"]))]
    message = f"Found {len(saved)} new suggested death marker(s)."
    if skipped or cleaned:
        message += f" Skipped/cleaned {skipped + cleaned} duplicate or already-reviewed candidate(s)."
    detector = death_detector_summary(saved, player_name, ocr_available)
    if detector["warning"]:
        message += f" {detector['warning']}"
    else:
        message += f" {detector['message']}"
    db.log(
        "info",
        "death_detector",
        f"Find Deaths completed for match #{match_id}",
        {"saved": len(saved), "skipped_or_cleaned": skipped + cleaned, "detector": detector},
    )
    return {
        "ok": True,
        "message": message,
        "suggestions": saved,
        "skipped_duplicates": skipped + cleaned,
        "detector": detector,
    }


def extract_scan_frames(ffmpeg: str, video_path: Path, frame_dir: Path, fps: str = "1") -> List[Path]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("scan-*.jpg"):
        old.unlink()
    output_pattern = str(frame_dir / "scan-%06d.jpg")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps},scale=640:-1",
        "-q:v",
        "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg frame extraction failed")
    return sorted(frame_dir.glob("scan-*.jpg"))


def sample_interval_seconds(fps: str) -> float:
    value = str(fps or "1").strip()
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            numerator = float(left)
            denominator = float(right)
            if numerator > 0 and denominator > 0:
                return denominator / numerator
        except ValueError:
            return 1.0
    try:
        number = float(value)
    except ValueError:
        return 1.0
    if number <= 0:
        return 1.0
    return 1.0 / number


def analyze_frames(
    frames: List[Path],
    calibration: Optional[Dict[str, Dict[str, float]]] = None,
    feedback: Optional[Dict[str, Any]] = None,
    player_name: str = DEFAULT_PLAYER_NAME,
    evidence_dir: Optional[Path] = None,
    fps: str = "1",
    update: Optional[Callable[[str, int], None]] = None,
    max_ocr_frames: int = 180,
) -> List[Dict[str, Any]]:
    sample_interval = sample_interval_seconds(fps)
    timeline = build_timeline(
        frames,
        calibration,
        sample_interval=sample_interval,
        update=update,
        progress_start=24,
        progress_end=52,
    )
    player_deaths = detect_player_deaths_from_hud(
        timeline,
        calibration or default_calibration(),
        player_name,
        evidence_dir,
        update=update,
        max_ocr_frames=max_ocr_frames,
        progress_start=54,
        progress_end=84,
    )
    progress(update, "Find Deaths: running fallback visual grouping.", 86)
    generic = cluster_death_candidates(timeline, adaptive_death_threshold(feedback))
    return merge_primary_and_fallback_death_candidates(player_deaths, generic)


def build_timeline(
    frames: List[Path],
    calibration: Optional[Dict[str, Dict[str, float]]] = None,
    sample_interval: float = 1.0,
    update: Optional[Callable[[str, int], None]] = None,
    progress_start: int = 0,
    progress_end: int = 0,
) -> List[FrameMetrics]:
    timeline: List[FrameMetrics] = []
    previous: Optional[np.ndarray] = None
    previous_minimap: Optional[np.ndarray] = None
    previous_crosshair: Optional[np.ndarray] = None
    total = max(1, len(frames))
    last_progress = -1
    for index, frame in enumerate(frames):
        arr = load_frame(frame)
        motion = frame_motion(previous, arr) if previous is not None else 0.0
        regions = calibration or default_calibration()
        minimap = crop_region(arr, regions["minimap"])
        crosshair = crop_region(arr, regions["crosshair"])
        minimap_motion = frame_motion(previous_minimap, minimap) if previous_minimap is not None else 0.0
        crosshair_drift = frame_motion(previous_crosshair, crosshair) if previous_crosshair is not None else 0.0
        previous = arr
        previous_minimap = minimap
        previous_crosshair = crosshair
        timeline.append(compute_metrics(frame, float(index) * sample_interval, arr, motion, regions, minimap_motion, crosshair_drift))
        if update and progress_end > progress_start:
            percent = progress_start + int(((index + 1) / total) * (progress_end - progress_start))
            if percent != last_progress and (index == 0 or index + 1 == total or (index + 1) % 25 == 0):
                last_progress = percent
                progress(update, f"Find Deaths: building visual timeline ({index + 1}/{total}).", percent)
    return timeline


def load_frame(frame: Path) -> np.ndarray:
    image = Image.open(frame).convert("RGB")
    return np.asarray(image).astype(np.float32) / 255.0


def compute_metrics(
    frame: Path,
    timestamp: float,
    arr: np.ndarray,
    motion: float,
    calibration: Optional[Dict[str, Dict[str, float]]] = None,
    minimap_motion: float = 0.0,
    crosshair_drift: float = 0.0,
) -> FrameMetrics:
    regions = calibration or default_calibration()
    center = crop_region(arr, {"x": 0.22, "y": 0.32, "w": 0.56, "h": 0.46})
    top_right = crop_region(arr, regions["killfeed"])
    bottom = crop_region(arr, regions["hud_bottom"])
    crosshair = crop_region(arr, regions["crosshair"])
    minimap = crop_region(arr, regions["minimap"])
    combat_report = crop_region(arr, regions["combat_report"])

    brightness = float(arr.mean())
    contrast = float(arr.std())
    center_dark = 1.0 - float(center.mean())
    center_red = red_score(center)
    killfeed_red = red_score(top_right)
    bottom_dark = 1.0 - float(bottom.mean())
    crosshair_activity = float(crosshair.std())
    minimap_activity = float(minimap.std())
    combat_report_score = score_combat_report(combat_report)
    death_score = score_death(center_dark, center_red, killfeed_red, bottom_dark, contrast, motion, combat_report_score)
    pressure_score = score_pressure(killfeed_red, motion, crosshair_activity, contrast, minimap_motion)
    reason = build_reason(
        center_dark,
        center_red,
        killfeed_red,
        bottom_dark,
        motion,
        crosshair_activity,
        combat_report_score,
    )

    return FrameMetrics(
        path=frame,
        timestamp=timestamp,
        brightness=brightness,
        contrast=contrast,
        center_dark=center_dark,
        center_red=center_red,
        killfeed_red=killfeed_red,
        bottom_dark=bottom_dark,
        motion=motion,
        crosshair_activity=crosshair_activity,
        crosshair_drift=crosshair_drift,
        minimap_activity=minimap_activity,
        minimap_motion=minimap_motion,
        combat_report_score=combat_report_score,
        death_score=death_score,
        pressure_score=pressure_score,
        reason=reason,
    )


def crop(arr: np.ndarray, top: float, bottom: float, left: float, right: float) -> np.ndarray:
    h, w, _ = arr.shape
    return arr[int(h * top) : int(h * bottom), int(w * left) : int(w * right), :]


def crop_region(arr: np.ndarray, region: Dict[str, float]) -> np.ndarray:
    x = max(0.0, min(1.0, float(region.get("x", 0))))
    y = max(0.0, min(1.0, float(region.get("y", 0))))
    w = max(0.001, min(1.0 - x, float(region.get("w", 0.1))))
    h = max(0.001, min(1.0 - y, float(region.get("h", 0.1))))
    return crop(arr, y, y + h, x, x + w)


def frame_motion(previous: np.ndarray, current: np.ndarray) -> float:
    if previous.shape != current.shape:
        return 0.0
    return float(np.mean(np.abs(current - previous)) * 2.5)


def score_death(
    center_dark: float,
    center_red: float,
    killfeed_red: float,
    bottom_dark: float,
    contrast: float,
    motion: float,
    combat_report_score: float = 0.0,
) -> float:
    return float(
        center_dark * 0.28
        + center_red * 0.22
        + killfeed_red * 0.16
        + bottom_dark * 0.14
        + combat_report_score * 0.16
        + min(contrast * 1.7, 1.0) * 0.08
        + min(motion, 1.0) * 0.12
    )


def score_pressure(
    killfeed_red: float,
    motion: float,
    crosshair_activity: float,
    contrast: float,
    minimap_motion: float = 0.0,
) -> float:
    return float(
        killfeed_red * 0.25
        + min(motion, 1.0) * 0.35
        + min(crosshair_activity * 5.0, 1.0) * 0.25
        + min(contrast * 1.7, 1.0) * 0.15
        + min(minimap_motion, 1.0) * 0.10
    )


def score_combat_report(region: np.ndarray) -> float:
    darkness = 1.0 - float(region.mean())
    contrast = float(region.std())
    red = red_score(region)
    return float(min(1.0, darkness * 0.35 + contrast * 2.0 + red * 0.30))


def build_reason(
    center_dark: float,
    center_red: float,
    killfeed_red: float,
    bottom_dark: float,
    motion: float,
    crosshair_activity: float,
    combat_report_score: float = 0.0,
) -> str:
    reasons = []
    if motion > 0.22:
        reasons.append("large visual transition")
    if center_dark > 0.48:
        reasons.append("dark center overlay")
    if center_red > 0.20:
        reasons.append("red-tinted center UI")
    if killfeed_red > 0.18:
        reasons.append("killfeed-like red activity")
    if bottom_dark > 0.45:
        reasons.append("dark lower HUD transition")
    if combat_report_score > 0.42:
        reasons.append("combat-report-like panel activity")
    if crosshair_activity > 0.12:
        reasons.append("busy crosshair region")
    if not reasons:
        reasons.append("visual transition candidate")
    return ", ".join(reasons)


def adaptive_death_threshold(feedback: Optional[Dict[str, Any]]) -> float:
    base = 0.56
    if not feedback:
        return base
    sensitivity = str(feedback.get("sensitivity") or "normal")
    if sensitivity == "high":
        base -= 0.05
    elif sensitivity == "low":
        base += 0.05
    return max(0.48, min(0.68, base + float(feedback.get("threshold_adjustment") or 0)))


def cluster_death_candidates(metrics: List[FrameMetrics], threshold: float = 0.56) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    active: List[FrameMetrics] = []

    for current in metrics:
        if current.death_score >= threshold and (
            current.motion >= 0.09
            or current.killfeed_red >= 0.16
            or current.center_red >= 0.18
            or current.combat_report_score >= 0.42
        ):
            active.append(current)
            continue
        if active:
            candidates.append(best_candidate(active))
            active = []
    if active:
        candidates.append(best_candidate(active))

    filtered: List[Dict[str, Any]] = []
    last_ts = -999.0
    for candidate in candidates:
        if candidate["timestamp"] - last_ts >= 8:
            filtered.append(candidate)
            last_ts = candidate["timestamp"]
        elif filtered and candidate["confidence"] > filtered[-1]["confidence"]:
            filtered[-1] = candidate
            last_ts = candidate["timestamp"]
    return filtered


def detect_player_deaths_from_hud(
    timeline: List[FrameMetrics],
    calibration: Dict[str, Dict[str, float]],
    player_name: str,
    evidence_dir: Optional[Path] = None,
    update: Optional[Callable[[str, int], None]] = None,
    max_ocr_frames: int = 180,
    progress_start: int = 54,
    progress_end: int = 84,
) -> List[Dict[str, Any]]:
    player_name = (player_name or DEFAULT_PLAYER_NAME).strip()
    if not timeline or not player_name:
        return []
    tesseract = tesseract_path()
    evidence_dir = evidence_dir or (timeline[0].path.parent / "evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_hits: List[Dict[str, Any]] = []
    if tesseract:
        ocr_items = select_ocr_death_frames(timeline, max_ocr_frames=max_ocr_frames)
        progress(
            update,
            f"Find Deaths: OCR will inspect {len(ocr_items)} likely HUD frame(s), capped at {max_ocr_frames}.",
            progress_start,
        )
    else:
        ocr_items = []
        progress(update, "Find Deaths: Tesseract missing; using visual combat-report onset and fallback.", progress_start)
    total = max(1, len(ocr_items))
    last_progress = -1
    combat_ocr_samples: List[Dict[str, Any]] = []
    for index, item in enumerate(ocr_items):
        arr = load_frame(item.path)
        killfeed_crop = crop_region(arr, calibration.get("killfeed") or default_calibration()["killfeed"])
        combat_crop = crop_region(arr, calibration.get("combat_report") or default_calibration()["combat_report"])
        killfeed_text = ""
        combat_text = ""
        name_score = 0.0
        if tesseract:
            killfeed_path = save_ocr_crop(killfeed_crop, evidence_dir / f"{item.path.stem}-killfeed.png")
            combat_path = save_ocr_crop(combat_crop, evidence_dir / f"{item.path.stem}-combat-report.png")
            killfeed_text = run_tesseract_text(tesseract, killfeed_path, psm="6")
            combat_text = run_tesseract_text(tesseract, combat_path, psm="6")
            name_score = fuzzy_contains_player_name(killfeed_text, player_name)
        else:
            killfeed_path = ""
            combat_path = ""
        combat_text_score = combat_report_text_score(combat_text)
        combat_score = max(float(item.combat_report_score or 0), combat_text_score)
        killfeed_visual = max(float(item.killfeed_red or 0), red_or_blue_score(killfeed_crop))
        combat_ocr_samples.append(
            {
                "timestamp": item.timestamp,
                "combat_score": combat_score,
                "combat_text_score": combat_text_score,
                "killfeed_activity": killfeed_visual,
                "killfeed_text": killfeed_text[:220],
                "combat_report_text": combat_text[:220],
                "killfeed_crop": str(killfeed_path) if killfeed_path else "",
                "combat_report_crop": str(combat_path) if combat_path else "",
            }
        )
        if name_score >= 0.72 and combat_score >= 0.34:
            confidence = min(0.98, 0.55 + name_score * 0.25 + combat_score * 0.18 + min(killfeed_visual, 0.25))
            raw_hits.append(
                {
                    "timestamp": item.timestamp,
                    "confidence": round(confidence, 2),
                    "reason": (
                        f"Primary detector: killfeed OCR matched player '{player_name}' "
                        f"(score {name_score:.2f}) and combat report appeared (score {combat_score:.2f}). "
                        f"Killfeed text: {short_evidence(killfeed_text)}"
                    ),
                    "frame_path": str(item.path.resolve()),
                    "metrics": {
                        "detector": "player_name_killfeed_and_combat_report",
                        "player_name": player_name,
                        "name_match_score": round(name_score, 3),
                        "combat_report_score": round(combat_score, 3),
                        "killfeed_activity": round(killfeed_visual, 3),
                        "killfeed_text": killfeed_text[:220],
                        "combat_report_text": combat_text[:220],
                        "killfeed_crop": str(killfeed_path) if killfeed_path else "",
                        "combat_report_crop": str(combat_path) if combat_path else "",
                    },
                }
            )
        if update and progress_end > progress_start:
            percent = progress_start + int(((index + 1) / total) * (progress_end - progress_start))
            if percent != last_progress and (index == 0 or index + 1 == total or (index + 1) % 10 == 0):
                last_progress = percent
                progress(update, f"Find Deaths: OCR HUD pass ({index + 1}/{total}).", percent)
    raw_hits.extend(combat_report_onset_hits(timeline, combat_ocr_samples, player_name))
    return cluster_player_death_hits(raw_hits)


def combat_report_confirms_death(combat_score: float, text_score: float, item: FrameMetrics) -> bool:
    visual = float(item.combat_report_score or 0)
    death_score = float(item.death_score or 0)
    if text_score >= 0.45 and combat_score >= 0.34:
        return True
    if visual >= 0.58 and death_score >= 0.42:
        return True
    if visual >= 0.66:
        return True
    return False


def combat_report_onset_hits(
    timeline: List[FrameMetrics],
    ocr_samples: List[Dict[str, Any]],
    player_name: str,
    absent_seconds: float = 6.0,
    cooldown_seconds: float = 20.0,
) -> List[Dict[str, Any]]:
    if not timeline:
        return []
    samples = sorted(ocr_samples, key=lambda row: float(row.get("timestamp") or 0.0))
    hits: List[Dict[str, Any]] = []
    panel_active = False
    low_since: Optional[float] = None
    last_emit = -9999.0
    for item in sorted(timeline, key=lambda row: row.timestamp):
        sample = nearest_ocr_sample(samples, item.timestamp)
        text_score = float((sample or {}).get("combat_text_score") or 0.0)
        combat_score = max(float(item.combat_report_score or 0.0), text_score)
        present = combat_report_confirms_death(combat_score, text_score, item)
        absent = combat_report_is_absent(item, text_score)
        timestamp = float(item.timestamp)
        if present and not panel_active:
            if timestamp - last_emit >= cooldown_seconds:
                hits.append(combat_report_onset_candidate(item, sample, player_name, combat_score, text_score))
                last_emit = timestamp
            panel_active = True
            low_since = None
            continue
        if panel_active:
            if absent:
                if low_since is None:
                    low_since = timestamp
                elif timestamp - low_since >= absent_seconds:
                    panel_active = False
                    low_since = None
            else:
                low_since = None
    return hits


def combat_report_is_absent(item: FrameMetrics, text_score: float) -> bool:
    return float(item.combat_report_score or 0.0) <= 0.24 and float(item.death_score or 0.0) <= 0.35 and text_score < 0.25


def nearest_ocr_sample(samples: List[Dict[str, Any]], timestamp: float, window_seconds: float = 4.0) -> Optional[Dict[str, Any]]:
    if not samples:
        return None
    best = min(samples, key=lambda row: abs(float(row.get("timestamp") or 0.0) - float(timestamp)))
    if abs(float(best.get("timestamp") or 0.0) - float(timestamp)) <= window_seconds:
        return best
    return None


def combat_report_onset_candidate(
    item: FrameMetrics,
    sample: Optional[Dict[str, Any]],
    player_name: str,
    combat_score: float,
    text_score: float,
) -> Dict[str, Any]:
    sample = sample or {}
    confidence = min(0.84, 0.47 + combat_score * 0.22 + text_score * 0.18)
    text = str(sample.get("combat_report_text") or "")
    return {
        "timestamp": item.timestamp,
        "confidence": round(confidence, 2),
        "reason": (
            "Primary detector: combat report appeared but killfeed/player-name confirmation was unavailable. "
            f"Combat score {combat_score:.2f}; OCR text: {short_evidence(text)}"
        ),
        "frame_path": str(item.path.resolve()),
        "metrics": {
            "detector": "combat_report_only",
            "player_name": player_name,
            "name_match_score": 0.0,
            "combat_report_score": round(combat_score, 3),
            "combat_report_text_score": round(text_score, 3),
            "killfeed_activity": round(float(sample.get("killfeed_activity") or 0.0), 3),
            "killfeed_text": str(sample.get("killfeed_text") or "")[:220],
            "combat_report_text": text[:220],
            "killfeed_crop": str(sample.get("killfeed_crop") or ""),
            "combat_report_crop": str(sample.get("combat_report_crop") or ""),
        },
    }


def select_ocr_death_frames(timeline: List[FrameMetrics], max_ocr_frames: int = 180) -> List[FrameMetrics]:
    limit = max(30, min(600, int(max_ocr_frames or 180)))
    scored: List[tuple[float, FrameMetrics]] = []
    for item in timeline:
        score = max(
            float(item.killfeed_red or 0) * 1.45,
            float(item.combat_report_score or 0) * 1.35,
            float(item.death_score or 0),
            float(item.center_red or 0) * 0.75,
        )
        if (
            float(item.killfeed_red or 0) >= 0.04
            or float(item.combat_report_score or 0) >= 0.10
            or float(item.death_score or 0) >= 0.50
            or float(item.center_red or 0) >= 0.16
        ):
            scored.append((score, item))
    if not scored:
        return []
    selected = [item for _, item in sorted(scored, key=lambda row: row[0], reverse=True)[:limit]]
    return sorted(selected, key=lambda item: item.timestamp)


def cluster_player_death_hits(hits: List[Dict[str, Any]], gap_seconds: float = 5.0) -> List[Dict[str, Any]]:
    if not hits:
        return []
    hits = sorted(hits, key=lambda item: item["timestamp"])
    groups: List[List[Dict[str, Any]]] = []
    current = [hits[0]]
    for hit in hits[1:]:
        if float(hit["timestamp"]) - float(current[-1]["timestamp"]) <= gap_seconds:
            current.append(hit)
        else:
            groups.append(current)
            current = [hit]
    groups.append(current)
    return [max(group, key=lambda item: float(item.get("confidence") or 0)) for group in groups]


def merge_primary_and_fallback_death_candidates(primary: List[Dict[str, Any]], fallback: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = list(primary)
    for candidate in fallback:
        if any(abs(float(candidate["timestamp"]) - float(existing["timestamp"])) <= 6.0 for existing in merged):
            continue
        weaker = dict(candidate)
        weaker["confidence"] = min(float(weaker.get("confidence") or 0), 0.72)
        weaker["reason"] = "Fallback detector: " + str(weaker.get("reason") or "")
        merged.append(weaker)
    return sorted(merged, key=lambda item: item["timestamp"])


def death_detector_summary(suggestions: List[Dict[str, Any]], player_name: str, ocr_available: bool) -> Dict[str, Any]:
    primary = 0
    combat_only = 0
    fallback = 0
    for item in suggestions:
        metrics = item.get("metrics") or {}
        detector = str(metrics.get("detector") or "")
        reason = str(item.get("reason") or "")
        if detector == "player_name_killfeed_and_combat_report":
            primary += 1
        elif detector == "combat_report_only":
            combat_only += 1
        elif reason.startswith("Primary detector:"):
            primary += 1
        elif reason.startswith("Fallback detector:"):
            fallback += 1
    warning = ""
    if not ocr_available and combat_only == 0:
        warning = "Player-name killfeed detection is unavailable because Tesseract OCR is not installed; only the fallback visual detector ran."
    elif primary == 0 and combat_only == 0:
        warning = f"Player-name killfeed OCR ran for '{player_name}' but found no confirmed killfeed, combat-report-only, or fallback deaths."
    message = (
        f"VALORANT HUD detector used player '{player_name}': {primary} killfeed-confirmed hit(s), "
        f"{combat_only} combat-report-only hit(s), "
        f"{fallback} fallback visual hit(s)."
    )
    return {
        "player_name": player_name,
        "ocr_available": ocr_available,
        "primary_hits": primary,
        "combat_report_only_hits": combat_only,
        "fallback_hits": fallback,
        "warning": warning,
        "message": message,
    }


def save_ocr_crop(region: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(np.clip(region * 255, 0, 255).astype(np.uint8))
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = gray.resize((max(1, gray.width * 3), max(1, gray.height * 3)))
    gray = gray.filter(ImageFilter.SHARPEN)
    gray.save(path)
    return path


def tesseract_path() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    for candidate in (
        root / "tools" / "tesseract" / "tesseract.exe",
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return ""


def run_tesseract_text(tesseract: str, image_path: Path, psm: str = "6", timeout_seconds: float = 1.5) -> str:
    cmd = [tesseract, str(image_path), "stdout", "--psm", psm]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return ""
    return " ".join(result.stdout.split())


def fuzzy_contains_player_name(text: str, player_name: str) -> float:
    target = normalize_ocr_name(player_name)
    value = normalize_ocr_name(text)
    if not target or not value:
        return 0.0
    if target in value:
        return 1.0
    best = SequenceMatcher(None, target, value).ratio()
    window = max(len(target), 3)
    for index in range(0, max(1, len(value) - window + 1)):
        chunk = value[index : index + window]
        best = max(best, SequenceMatcher(None, target, chunk).ratio())
    return round(best, 3)


def normalize_ocr_name(value: str) -> str:
    text = str(value or "").lower()
    text = text.replace("|", "i").replace("1", "i").replace("0", "o").replace("5", "s")
    return "".join(ch for ch in text if ch.isalnum())


def combat_report_text_score(text: str) -> float:
    lower = str(text or "").lower()
    score = 0.0
    if any(token in lower for token in ("damage", "received", "dealt", "combat", "report")):
        score += 0.35
    if any(token in lower for token in ("head", "body", "leg")):
        score += 0.20
    if any(ch.isdigit() for ch in lower):
        score += 0.22
    return min(1.0, score)


def red_or_blue_score(region: np.ndarray) -> float:
    if region.size == 0:
        return 0.0
    red = region[:, :, 0]
    green = region[:, :, 1]
    blue = region[:, :, 2]
    red_mask = (red > 0.40) & (red > green * 1.20) & (red > blue * 1.15)
    blue_mask = (blue > 0.40) & (blue > red * 1.15) & (blue > green * 1.05)
    return float(min(1.0, (float(red_mask.mean()) + float(blue_mask.mean())) * 8.0))


def short_evidence(text: str, limit: int = 140) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact or "unreadable"
    return compact[: limit - 3] + "..."


def best_candidate(group: List[FrameMetrics]) -> Dict[str, Any]:
    best = max(group, key=lambda item: item.death_score)
    return {
        "timestamp": best.timestamp,
        "confidence": round(min(0.95, best.death_score), 2),
        "reason": best.reason,
        "frame_path": str(best.path.resolve()),
        "metrics": {
            "death_score": round(best.death_score, 3),
            "combat_report_score": round(best.combat_report_score, 3),
            "killfeed_red": round(best.killfeed_red, 3),
            "motion": round(best.motion, 3),
            "crosshair_activity": round(best.crosshair_activity, 3),
        },
    }


def death_likelihood(frame: Path) -> tuple:
    metrics = compute_metrics(frame, 0.0, load_frame(frame), 0.0)
    return metrics.death_score, metrics.reason


def reconstruct_rounds(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "rounds": []}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for round timeline reconstruction.", "rounds": []}

    frame_dir = work_dir / "rounds" / f"match-{match_id}"
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps="1/5")
    timeline = build_timeline(frames, db.get_calibration())
    boundaries = detect_round_boundaries(timeline)
    rounds = []
    for index, start_ts in enumerate(boundaries, start=1):
        end_ts = boundaries[index] - 1 if index < len(boundaries) else None
        rounds.append(
            {
                "round_number": index,
                "start_ts": round(start_ts, 1),
                "end_ts": round(end_ts, 1) if end_ts is not None else None,
                "outcome": "",
                "side": "",
            }
        )
    if rounds:
        db.replace_rounds(match_id, rounds)
    result = {
        "kind": "round_timeline",
        "summary": f"Reconstructed {len(rounds)} likely round boundary marker(s) from HUD/scene transitions.",
        "rounds": rounds,
        "confidence": 0.35 if rounds else 0.0,
    }
    db.save_structured_analysis(match_id, "round_timeline", result)
    return {"ok": True, "message": result["summary"], "rounds": rounds, "analysis": result}


def detect_round_boundaries(timeline: List[FrameMetrics]) -> List[float]:
    if not timeline:
        return []
    boundaries = [0.0]
    last = 0.0
    for item in timeline:
        ts = item.timestamp * 5.0
        transition = item.motion > 0.20 and item.bottom_dark < 0.55
        quiet_reset = item.pressure_score < 0.20 and item.crosshair_activity < 0.08
        if ts - last >= 45 and (transition or quiet_reset):
            boundaries.append(ts)
            last = ts
    return boundaries[:30]


def analyze_match_events(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for death-event detector v2.", "analysis": None}

    frame_dir = work_dir / "events-v2" / f"match-{match_id}"
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps=scan_fps(db))
    timeline = build_timeline(frames, db.get_calibration())
    feedback = db.detector_feedback_summary()
    feedback["sensitivity"] = db.get_setting("detector_sensitivity", "normal")
    candidates = cluster_death_candidates(timeline, adaptive_death_threshold(feedback))
    pressure = [
        {
            "timestamp": item.timestamp,
            "pressure_score": round(item.pressure_score, 3),
            "reason": item.reason,
        }
        for item in sorted(timeline, key=lambda row: row.pressure_score, reverse=True)[:8]
    ]
    result = {
        "kind": "death_events_v2",
        "summary": f"Detector v2 found {len(candidates)} death/combat transition candidate(s).",
        "candidates": candidates,
        "pressure_windows": sorted(pressure, key=lambda row: row["timestamp"]),
        "confidence": round(min(0.90, max([item["confidence"] for item in candidates] or [0.0])), 2),
    }
    db.save_structured_analysis(match_id, "death_events_v2", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def score_crosshair_match(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for crosshair placement scoring.", "analysis": None}

    frame_dir = work_dir / "crosshair" / f"match-{match_id}"
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps=crosshair_fps(db))
    timeline = build_timeline(frames, db.get_calibration())
    if not timeline:
        return {"ok": False, "message": "No frames were available for crosshair scoring.", "analysis": None}
    avg_activity = float(np.mean([item.crosshair_activity for item in timeline]))
    avg_drift = float(np.mean([item.crosshair_drift for item in timeline]))
    avg_center_red = float(np.mean([item.center_red for item in timeline]))
    avg_pressure = float(np.mean([item.pressure_score for item in timeline]))
    unstable = sum(1 for item in timeline if item.crosshair_activity > 0.11 or item.crosshair_drift > 0.10)
    correction_load = avg_activity * 4.0 + avg_drift * 2.0 + avg_pressure * 0.20
    stability = max(0.0, 1.0 - correction_load)
    result = {
        "kind": "crosshair_score_v2",
        "summary": crosshair_summary(stability, unstable, len(timeline)),
        "score": round(stability * 100),
        "interpretation": crosshair_interpretation(stability, avg_activity, avg_drift, avg_center_red, avg_pressure),
        "metrics": {
            "average_crosshair_activity": round(avg_activity, 3),
            "average_crosshair_drift": round(avg_drift, 3),
            "average_center_red": round(avg_center_red, 3),
            "average_pressure_score": round(avg_pressure, 3),
            "correction_load": round(correction_load, 3),
            "unstable_frames": unstable,
            "sampled_frames": len(timeline),
        },
        "confidence": 0.42,
    }
    db.save_structured_analysis(match_id, "crosshair", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def scan_full_vod_coach_moments(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for full-VOD coaching.", "analysis": None}

    frame_dir = work_dir / "full-vod-coach" / f"match-{match_id}"
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps=full_vod_fps(db))
    timeline = build_timeline(frames, db.get_calibration())
    moments = detect_coach_moments(timeline, match)
    result = {
        "kind": "full_vod_coach_moments",
        "summary": f"Found {len(moments)} full-VOD coaching moment(s) from movement, crosshair, pressure, and minimap signals.",
        "moments": moments,
        "ranked_focus": rank_moment_focus(moments),
        "sampled_frames": len(timeline),
        "confidence": round(min(0.75, 0.25 + len(moments) * 0.05), 2) if moments else 0.0,
    }
    db.save_structured_analysis(match_id, "full_vod_coach_moments", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def full_vod_fps(db: Database) -> str:
    rate = str(db.get_setting("frame_sample_rate", "standard") or "standard")
    return {"light": "1/3", "standard": "1", "dense": "2"}.get(rate, "1")


def detect_coach_moments(timeline: List[FrameMetrics], match: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    for index, item in enumerate(timeline):
        previous = timeline[index - 1] if index > 0 else None
        next_item = timeline[index + 1] if index + 1 < len(timeline) else None
        turn_score = min(1.0, item.motion * 2.6 + item.crosshair_drift * 3.2)
        correction_score = min(1.0, item.crosshair_activity * 5.0 + item.crosshair_drift * 2.0)
        pressure = min(1.0, item.pressure_score + item.killfeed_red * 0.3 + item.center_red * 0.2)
        minimap_change = min(1.0, item.minimap_motion * 3.0 + item.minimap_activity * 0.7)

        if turn_score >= 0.50 and correction_score >= 0.42:
            raw.append(
                coach_moment(
                    item,
                    "crosshair_turn_drift",
                    "Crosshair drift during a turn",
                    "Your view turns quickly and the crosshair region changes heavily. In VALORANT this often means the crosshair is chasing the angle instead of arriving pre-placed.",
                    "During rotations and clears, move the crosshair to the next likely head-height angle before the body fully commits.",
                    turn_score * 0.55 + correction_score * 0.45,
                    match,
                    previous,
                    next_item,
                )
            )
        if pressure >= 0.55 and correction_score >= 0.45:
            raw.append(
                coach_moment(
                    item,
                    "panic_correction_under_pressure",
                    "High correction load during contact",
                    "Combat-pressure signals overlap with a busy crosshair region. This is a candidate for rushed target correction or fighting before pre-aim was ready.",
                    "Pause this moment and check whether the crosshair was already on the likely contact point before the opponent appeared.",
                    pressure * 0.50 + correction_score * 0.50,
                    match,
                    previous,
                    next_item,
                )
            )
        if minimap_change >= 0.45 and pressure >= 0.42:
            raw.append(
                coach_moment(
                    item,
                    "minimap_pressure_missed",
                    "Possible minimap timing cue",
                    "The minimap region changes while the screen also shows pressure. This is a candidate for missed map information before the fight or rotate.",
                    "Check the minimap two seconds before this timestamp and decide if the safer play was hold, rotate, or wait for support.",
                    minimap_change * 0.45 + pressure * 0.55,
                    match,
                    previous,
                    next_item,
                )
            )
        if previous and previous.pressure_score >= 0.48 and item.motion >= 0.18 and item.crosshair_drift >= 0.10:
            raw.append(
                coach_moment(
                    item,
                    "poor_reset_after_contact",
                    "Messy reset after contact",
                    "The frames immediately after pressure show movement and crosshair drift. This can indicate repeeking, panic movement, or not resetting the fight cleanly.",
                    "After first contact, break line of sight or change the fight condition before taking the next duel.",
                    min(1.0, previous.pressure_score * 0.4 + item.motion * 1.5 + item.crosshair_drift * 2.0),
                    match,
                    previous,
                    next_item,
                )
            )
    return cluster_coach_moments(raw)


def coach_moment(
    item: FrameMetrics,
    label: str,
    title: str,
    reason: str,
    better_play: str,
    score: float,
    match: Dict[str, Any],
    previous: Optional[FrameMetrics],
    next_item: Optional[FrameMetrics],
) -> Dict[str, Any]:
    confidence = round(max(0.35, min(0.92, score)), 2)
    return {
        "timestamp": item.timestamp,
        "label": label,
        "title": title,
        "reason": reason,
        "better_play": better_play,
        "priority": int(confidence * 100),
        "confidence": confidence,
        "frame_path": str(item.path.resolve()),
        "context_frame_paths": [
            str(frame.path.resolve())
            for frame in (previous, item, next_item)
            if frame is not None
        ],
        "valorant_context": {
            "map": match.get("map") or "unknown",
            "agent": match.get("agent") or "unknown",
            "role_hint": role_hint(str(match.get("agent") or "")),
        },
        "metrics": {
            "motion": round(item.motion, 3),
            "crosshair_activity": round(item.crosshair_activity, 3),
            "crosshair_drift": round(item.crosshair_drift, 3),
            "pressure_score": round(item.pressure_score, 3),
            "minimap_motion": round(item.minimap_motion, 3),
            "killfeed_red": round(item.killfeed_red, 3),
        },
    }


def cluster_coach_moments(items: List[Dict[str, Any]], gap_seconds: float = 10.0, limit: int = 18) -> List[Dict[str, Any]]:
    if not items:
        return []
    items = sorted(items, key=lambda row: (row["timestamp"], -row["priority"]))
    clustered: List[Dict[str, Any]] = []
    for item in items:
        if not clustered or item["timestamp"] - clustered[-1]["timestamp"] > gap_seconds:
            clustered.append(item)
            continue
        current = clustered[-1]
        if item["priority"] > current["priority"]:
            clustered[-1] = item
        elif item["label"] != current["label"]:
            current.setdefault("secondary_labels", []).append(item["label"])
            current["reason"] = f"{current['reason']} Also flagged: {item['title'].lower()}."
            current["priority"] = min(100, current["priority"] + 3)
    return sorted(clustered, key=lambda row: row["priority"], reverse=True)[:limit]


def rank_moment_focus(moments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, Dict[str, Any]] = {}
    for moment in moments:
        label = str(moment.get("label") or "review")
        row = counts.setdefault(label, {"label": label, "count": 0, "priority": 0})
        row["count"] += 1
        row["priority"] = max(row["priority"], int(moment.get("priority") or 0))
    return sorted(counts.values(), key=lambda row: (row["count"], row["priority"]), reverse=True)


def role_hint(agent: str) -> str:
    agent = agent.lower()
    if agent in {"jett", "raze", "reyna", "phoenix", "neon", "yoru", "iso"}:
        return "duelist: first-contact spacing, pre-aim, escape route, and trade timing matter heavily"
    if agent in {"omen", "brimstone", "viper", "astra", "harbor", "clove"}:
        return "controller: smoke timing, map control, and supported rotates matter heavily"
    if agent in {"sova", "fade", "breach", "skye", "kayo", "gekko"}:
        return "initiator: info utility before contact and teammate timing matter heavily"
    if agent in {"cypher", "killjoy", "sage", "chamber", "deadlock", "vyse"}:
        return "sentinel: info discipline, anchor positioning, and safe re-peeks matter heavily"
    return "unknown role: focus on crosshair placement, trade spacing, utility timing, and map awareness"


def crosshair_summary(stability: float, unstable: int, total: int) -> str:
    if stability >= 0.70:
        return f"Crosshair region looked relatively stable across {total} sampled frames."
    if unstable > total * 0.35:
        return f"Crosshair region was unstable in {unstable}/{total} sampled frames; review pre-aim and target correction."
    return f"Crosshair stability was mixed across {total} sampled frames."


def crosshair_interpretation(
    stability: float,
    activity: float,
    drift: float,
    center_red: float,
    pressure: float,
) -> List[str]:
    reads = []
    if stability < 0.55:
        reads.append("High correction load: review whether your crosshair was already on the next likely head-height angle.")
    if activity > 0.10:
        reads.append("Crosshair crop had high visual activity, which often means rushed target correction during contact.")
    if drift > 0.09:
        reads.append("Crosshair crop changed sharply between samples; check panic flicks or movement while clearing.")
    if center_red > 0.14 or pressure > 0.45:
        reads.append("The unstable samples overlap with combat-pressure signals, so prioritize pre-aim before the duel starts.")
    if not reads:
        reads.append("No strong crosshair-placement warning from sampled frames.")
    return reads


def build_keyframe_gallery(db: Database, death_id: int, work_dir: Path) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    clip_path = death.get("clip_path")
    if clip_path and Path(clip_path).exists():
        source = Path(clip_path)
    else:
        match = db.get_match(int(death["match_id"]))
        if not match or death.get("timestamp") is None:
            return {"ok": False, "message": "A clip or death timestamp is required for keyframe extraction.", "analysis": None}
        source = Path(match["video_path"])
    if not source.exists():
        return {"ok": False, "message": "Video source is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for keyframe extraction.", "analysis": None}

    frame_dir = work_dir / "keyframes" / f"death-{death_id}"
    source_timestamp = death.get("timestamp") if not clip_path else None
    frames = extract_keyframe_source(ffmpeg, source, frame_dir, source_timestamp)
    timeline = build_timeline(frames, db.get_calibration())
    selected = select_keyframes(timeline)
    gallery = []
    for item in selected:
        stem = f"kf-death-{death_id}-{item['role']}"
        target = frame_dir / f"{stem}.jpg"
        shutil.copyfile(item["path"], target)
        relative_seconds = round(float(item["timestamp"]) / 2.0, 2)
        actual_timestamp = None
        if source_timestamp is not None:
            actual_timestamp = round(max(0.0, float(source_timestamp) - 8.0) + relative_seconds, 2)
        gallery.append(
            {
                "role": item["role"],
                "timestamp": actual_timestamp if actual_timestamp is not None else relative_seconds,
                "relative_second": relative_seconds,
                "frame_id": stem,
                "metrics": item["metrics"],
                "reason": item["reason"],
            }
        )
    result = {
        "kind": "keyframes",
        "death_id": death_id,
        "summary": f"Selected {len(gallery)} keyframe(s) around this death.",
        "frames": gallery,
        "confidence": 0.45 if gallery else 0.0,
    }
    db.save_death_analysis(death_id, "keyframes", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def build_local_ai_review_sequence(
    db: Database,
    death_id: int,
    work_dir: Path,
    mode: str = "contact",
    fps_override: Any = None,
    window_seconds: Any = None,
) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for local AI sequence extraction.", "analysis": None}

    profile = local_ai_sequence_profile(mode, fps_override, window_seconds)
    frame_dir = work_dir / "local-ai-sequences" / f"death-{death_id}"
    sequence = []
    source_kind = ""
    marker_quality: Dict[str, Any] = {}

    for segment_index, segment in enumerate(profile["segments"], start=1):
        source_info = local_ai_sequence_source(db, death, float(segment["start_before"]), float(segment["duration"]))
        if not source_info:
            return {
                "ok": False,
                "message": "A full VOD death timestamp or extracted clip is required for local AI sequence review.",
                "analysis": None,
            }
        source = source_info["source"]
        if not source.exists():
            return {"ok": False, "message": "Video source is missing.", "analysis": None}
        source_kind = source_info["kind"]
        marker_quality = source_info.get("marker_quality") or marker_quality
        segment_dir = frame_dir / f"segment-{segment_index}-{segment['label']}"
        frames = extract_sequence_frames(
            ffmpeg,
            source,
            segment_dir,
            float(source_info["start"]),
            float(source_info["duration"]),
            int(segment["fps"]),
            int(segment.get("width") or 576),
        )
        if not frames:
            continue
        timeline = build_timeline(frames, db.get_calibration())
        for item in timeline:
            sequence.append(sequence_frame_payload(item, segment, source_info, len(sequence) + 1))

    if not sequence:
        return {"ok": False, "message": "No frames could be extracted for the selected local AI review mode.", "analysis": None}

    for item in sequence:
        index = int(item["sequence_index"])
        stem = f"localai-death-{death_id}-{index:02d}"
        target = frame_dir / f"{stem}.jpg"
        shutil.copyfile(Path(str(item.pop("path"))), target)
        item["frame_id"] = stem

    result = {
        "kind": "local_ai_sequence",
        "death_id": death_id,
        "summary": f"Prepared {len(sequence)} ordered frame(s) using {profile['label']} mode.",
        "frames": sequence,
        "mode": profile["id"],
        "mode_label": profile["label"],
        "source": source_kind,
        "marker_quality": marker_quality,
        "confidence": 0.65 if sequence else 0.0,
    }
    db.save_death_analysis(death_id, "local_ai_sequence", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def local_ai_sequence_profile(mode: str, fps_override: Any = None, window_seconds: Any = None) -> Dict[str, Any]:
    mode = str(mode or "contact").strip().lower()
    window = normalize_review_window(window_seconds)
    profiles = {
        "context": {
            "id": "context",
            "label": "Context: final 10s at 2 FPS",
            "limit": 24,
            "segments": [{"label": "context", "start_before": 10.0, "duration": 10.0, "fps": 2, "width": 576}],
        },
        "contact": {
            "id": "contact",
            "label": "Contact: final 5s at 5 FPS",
            "limit": 30,
            "segments": [{"label": "contact", "start_before": 5.0, "duration": 5.0, "fps": 5, "width": 576}],
        },
        "burst": {
            "id": "burst",
            "label": "Burst: final 5s at 10 FPS, batched",
            "limit": 60,
            "segments": [{"label": "burst-contact", "start_before": 5.0, "duration": 5.0, "fps": 10, "width": 448}],
        },
        "hybrid": {
            "id": "hybrid",
            "label": "Hybrid: context 5s at 2 FPS + contact 5s at 5 FPS",
            "limit": 40,
            "segments": [
                {"label": "setup-context", "start_before": 10.0, "duration": 5.0, "fps": 2, "width": 576},
                {"label": "contact", "start_before": 5.0, "duration": 5.0, "fps": 5, "width": 576},
            ],
        },
        "adaptive": {
            "id": "adaptive",
            "label": f"Adaptive: final {window}s with dense contact",
            "limit": int((max(0, window - 5) * 2) + (min(window, 5) * 8)) + 8,
            "segments": [
                {"label": "setup-context", "start_before": float(window), "duration": max(0.5, float(window) - 5.0), "fps": 2, "width": 512},
                {"label": "dense-contact", "start_before": min(float(window), 5.0), "duration": min(float(window), 5.0), "fps": 8, "width": 512},
            ],
        },
    }
    profile = profiles.get(mode) or profiles["contact"]
    result = {
        "id": profile["id"],
        "label": profile["label"],
        "limit": profile["limit"],
        "segments": [dict(segment) for segment in profile["segments"]],
    }
    override = normalize_review_fps(fps_override)
    if override:
        for segment in result["segments"]:
            if result["id"] in {"hybrid", "adaptive"} and "contact" not in str(segment.get("label") or ""):
                continue
            segment["fps"] = override
        result["limit"] = int(sum(float(segment["duration"]) * int(segment["fps"]) for segment in result["segments"])) + 6
        result["label"] = f"{result['label']} with {override} FPS override"
    result["window_seconds"] = window
    return result


def normalize_review_fps(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return max(1, min(20, number))


def normalize_review_window(value: Any) -> int:
    if value is None or value == "":
        return 10
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 10
    return max(5, min(20, number))


def sequence_frame_payload(
    item: FrameMetrics,
    segment: Dict[str, Any],
    source_info: Dict[str, Any],
    index: int,
) -> Dict[str, Any]:
    fps = float(segment["fps"])
    segment_elapsed = float(item.timestamp) / fps
    relative_second = round(float(segment["start_before"]) * -1.0 + segment_elapsed, 2)
    seconds_before_death = round(max(0.0, float(segment["start_before"]) - segment_elapsed), 2)
    timestamp = None
    if source_info.get("vod_timestamp_start") is not None:
        timestamp = round(float(source_info["vod_timestamp_start"]) + segment_elapsed, 2)
    return {
        "role": str(segment["label"]),
        "sequence_index": index,
        "path": str(item.path),
        "timestamp": timestamp if timestamp is not None else segment_elapsed,
        "relative_second": relative_second,
        "seconds_before_death": seconds_before_death,
        "fps": int(segment["fps"]),
        "reason": item.reason,
        "metrics": {
            "death_score": round(item.death_score, 3),
            "pressure_score": round(item.pressure_score, 3),
            "motion": round(item.motion, 3),
            "crosshair_activity": round(item.crosshair_activity, 3),
            "crosshair_drift": round(item.crosshair_drift, 3),
            "minimap_motion": round(item.minimap_motion, 3),
        },
    }


def local_ai_sequence_source(db: Database, death: Dict[str, Any], start_before: float, duration: float) -> Optional[Dict[str, Any]]:
    timestamp = death.get("timestamp")
    match = db.get_match(int(death["match_id"])) if death.get("match_id") else None
    if match and timestamp is not None:
        video_path = Path(match["video_path"])
        if video_path.exists():
            anchor = local_ai_death_anchor_timestamp(death)
            desired_start = float(anchor["timestamp"]) - start_before
            desired_end = desired_start + duration
            start = max(0.0, desired_start)
            end = min(float(anchor["timestamp"]), desired_end)
            return {
                "kind": "full-vod",
                "source": video_path,
                "start": start,
                "duration": max(0.5, end - start),
                "vod_timestamp_start": start,
                "marker_quality": anchor,
            }
    clip_path = death.get("clip_path")
    if clip_path and Path(clip_path).exists():
        return {
            "kind": "clip",
            "source": Path(clip_path),
            "start": max(0.0, 15.0 - start_before),
            "duration": duration,
            "vod_timestamp_start": None,
            "marker_quality": local_ai_death_anchor_timestamp(death),
        }
    return None


def local_ai_death_anchor_timestamp(death: Dict[str, Any]) -> Dict[str, Any]:
    timestamp = float(death.get("timestamp") or 0.0)
    notes = str(death.get("notes") or "").lower()
    labels = " ".join(str(item).lower() for item in death.get("mistake_labels") or [])
    combat_report_only = "combat_report_only" in notes or "combat report appeared" in notes or "combat-report" in notes or "combat report" in labels
    if combat_report_only:
        offset = 1.75
        return {
            "timestamp": max(0.0, timestamp - offset),
            "original_timestamp": timestamp,
            "anchor_offset_seconds": offset,
            "source": "combat_report_only_adjusted",
            "warning": "Combat-report-only marker may be post-death; Clip Coach shifted the review anchor earlier.",
        }
    return {
        "timestamp": timestamp,
        "original_timestamp": timestamp,
        "anchor_offset_seconds": 0.0,
        "source": "death_timestamp",
        "warning": "",
    }


def extract_sequence_frames(ffmpeg: str, source: Path, frame_dir: Path, start: float, duration: float, fps: int, width: int = 576) -> List[Path]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("sequence-*.jpg"):
        old.unlink()
    for old in frame_dir.glob("localai-death-*.jpg"):
        old.unlink()
    output_pattern = str(frame_dir / "sequence-%06d.jpg")
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, start):.2f}",
        "-i",
        str(source),
        "-t",
        f"{max(0.5, duration):.2f}",
        "-vf",
        f"fps={fps},scale={width}:-1",
        "-q:v",
        "5",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg local AI sequence extraction failed")
    return sorted(frame_dir.glob("sequence-*.jpg"))


def extract_keyframe_source(ffmpeg: str, source: Path, frame_dir: Path, timestamp: Optional[float]) -> List[Path]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("scan-*.jpg"):
        old.unlink()
    output_pattern = str(frame_dir / "scan-%06d.jpg")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if timestamp is not None:
        cmd.extend(["-ss", f"{max(0, float(timestamp) - 8):.2f}"])
    cmd.extend(["-i", str(source)])
    if timestamp is not None:
        cmd.extend(["-t", "16"])
    cmd.extend(["-vf", "fps=2,scale=640:-1", "-q:v", "4", output_pattern])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg keyframe extraction failed")
    return sorted(frame_dir.glob("scan-*.jpg"))


def select_keyframes(timeline: List[FrameMetrics]) -> List[Dict[str, Any]]:
    if not timeline:
        return []

    death_peak = max(timeline, key=lambda item: item.death_score)
    pressure_peak = max(timeline, key=lambda item: item.pressure_score)
    anchor = pressure_peak if pressure_peak.pressure_score >= 0.35 else death_peak
    anchor_second = frame_second(anchor)
    death_second = frame_second(death_peak)
    pressure_threshold = max(0.28, pressure_peak.pressure_score * 0.72)

    roles = [
        ("setup", nearest_second(timeline, max(0.0, anchor_second - 4.0))),
        ("pre-contact", nearest_second(timeline, max(0.0, anchor_second - 2.0))),
        (
            "first-pressure",
            first_above_before(timeline, "pressure_score", pressure_threshold, death_peak.timestamp)
            or nearest_second(timeline, max(0.0, anchor_second - 0.5)),
        ),
        ("peak-pressure", pressure_peak),
        (
            "crosshair-correction",
            max(window_around(timeline, anchor_second, before=1.5, after=1.5), key=lambda item: item.crosshair_drift + item.motion),
        ),
        ("death-result", death_peak),
        ("aftermath", nearest_second(timeline, min(frame_second(timeline[-1]), death_second + 1.5))),
    ]

    selected = []
    seen = set()
    for role, item in roles:
        if item.path in seen:
            continue
        seen.add(item.path)
        selected.append(
            {
                "role": role,
                "path": item.path,
                "timestamp": item.timestamp,
                "reason": item.reason,
                "metrics": {
                    "death_score": round(item.death_score, 3),
                    "pressure_score": round(item.pressure_score, 3),
                    "motion": round(item.motion, 3),
                    "crosshair_activity": round(item.crosshair_activity, 3),
                    "minimap_motion": round(item.minimap_motion, 3),
                },
            }
        )
    return selected


def frame_second(item: FrameMetrics) -> float:
    return float(item.timestamp) / 2.0


def nearest_second(timeline: List[FrameMetrics], second: float) -> FrameMetrics:
    return min(timeline, key=lambda item: abs(frame_second(item) - second))


def window_around(timeline: List[FrameMetrics], center_second: float, before: float, after: float) -> List[FrameMetrics]:
    window = [item for item in timeline if center_second - before <= frame_second(item) <= center_second + after]
    return window or timeline


def first_above_before(timeline: List[FrameMetrics], field: str, threshold: float, max_timestamp: float) -> Optional[FrameMetrics]:
    for item in timeline:
        if item.timestamp <= max_timestamp and float(getattr(item, field)) >= threshold:
            return item
    return None


def first_above(timeline: List[FrameMetrics], field: str, threshold: float) -> Optional[FrameMetrics]:
    for item in timeline:
        if float(getattr(item, field)) >= threshold:
            return item
    return None


def build_review_queue(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    rounds = db.get_rounds(match_id)
    deaths = db.get_deaths(match_id)
    suggestions = db.get_death_suggestions(match_id)
    analyses = {
        name: db.get_latest_structured_analysis("match", match_id, name)
        for name in ("death_events_v2", "crosshair", "minimap", "ocr", "round_timeline", "full_vod_coach")
    }
    items: List[Dict[str, Any]] = []
    for death in deaths:
        items.append(
            {
                "kind": "death",
                "priority": death_priority(death),
                "timestamp": death.get("timestamp"),
                "round_phase": round_phase(rounds, death.get("timestamp")),
                "title": f"Review death {death_round_label(death)} @ {format_seconds(death.get('timestamp'))}",
                "reason": ", ".join(death.get("mistake_labels") or []) or "marked death",
                "action": "Generate clip understanding and compare against the current focus.",
            }
        )
    for suggestion in suggestions:
        items.append(
            {
                "kind": "suggested_death",
                "priority": round(float(suggestion.get("confidence") or 0) * 100),
                "timestamp": suggestion.get("timestamp"),
                "round_phase": round_phase(rounds, suggestion.get("timestamp")),
                "title": f"Verify suggested death @ {format_seconds(suggestion.get('timestamp'))}",
                "reason": suggestion.get("reason") or "detector candidate",
                "action": "Accept if this is a real death; reject to train the detector threshold.",
            }
        )
    event_payload = (analyses.get("death_events_v2") or {}).get("payload") or {}
    for candidate in (event_payload.get("candidates") or [])[:5]:
        items.append(
            {
                "kind": "event_v2",
                "priority": round(float(candidate.get("confidence") or 0) * 95),
                "timestamp": candidate.get("timestamp"),
                "round_phase": round_phase(rounds, candidate.get("timestamp")),
                "title": f"Inspect detector v2 event @ {format_seconds(candidate.get('timestamp'))}",
                "reason": candidate.get("reason") or "high local event score",
                "action": "Jump to the VOD and confirm whether this is a death, risky duel, or transition.",
            }
        )
    crosshair_payload = (analyses.get("crosshair") or {}).get("payload") or {}
    if int(crosshair_payload.get("score") or 100) < 65:
        items.append(
            {
                "kind": "crosshair",
                "priority": 72,
                "timestamp": None,
                "round_phase": "match-wide",
                "title": "Review crosshair stability",
                "reason": crosshair_payload.get("summary") or "crosshair score below target",
                "action": "Inspect key deaths for pre-aim and correction load.",
            }
        )
    full_vod_payload = (analyses.get("full_vod_coach") or {}).get("payload") or {}
    for moment in (full_vod_payload.get("moments") or [])[:6]:
        items.append(
            {
                "kind": "coach_moment",
                "priority": int(moment.get("priority") or 0),
                "timestamp": moment.get("timestamp"),
                "round_phase": round_phase(rounds, moment.get("timestamp")),
                "title": moment.get("title") or "Full VOD coach moment",
                "reason": moment.get("reason") or moment.get("label") or "full VOD signal",
                "action": moment.get("better_play") or "Review this timestamp before death review.",
            }
        )
    items = sorted(items, key=lambda item: item["priority"], reverse=True)[:12]
    result = {
        "kind": "review_queue",
        "summary": f"Built {len(items)} high-value review item(s).",
        "items": items,
        "confidence": 0.5 if items else 0.0,
    }
    db.save_structured_analysis(match_id, "review_queue", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def death_priority(death: Dict[str, Any]) -> int:
    labels = death.get("mistake_labels") or []
    base = 60 + int(float(death.get("confidence") or 0) * 20)
    if "dry peek" in labels or "crosshair too low/wide" in labels:
        base += 10
    if death.get("understanding"):
        base += 4
    return min(100, base)


def round_phase(rounds: List[Dict[str, Any]], timestamp: Any) -> str:
    if timestamp is None:
        return "unknown"
    ts = float(timestamp)
    for item in rounds:
        start = float(item.get("start_ts") or 0)
        end = item.get("end_ts")
        if end is not None and not (start <= ts <= float(end)):
            continue
        elapsed = ts - start
        if elapsed < 25:
            return "early round"
        if elapsed < 65:
            return "mid round"
        return "late round"
    return "unknown"


def format_seconds(value: Any) -> str:
    if value is None:
        return "unknown"
    seconds = int(float(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def death_round_label(death: Dict[str, Any]) -> str:
    return f"Round {death.get('round_number')}" if death.get("round_number") else "Round unknown"


def progress(update: Optional[Callable[[str, int], None]], message: str, percent: int) -> None:
    if update:
        update(message, max(0, min(100, int(percent))))


def death_scan_fps(db: Database) -> str:
    rate = str(db.get_setting("frame_sample_rate", "standard") or "standard")
    return {"light": "1/4", "standard": "1/2", "dense": "1"}.get(rate, "1/2")


def death_scan_max_ocr_frames(db: Database) -> int:
    raw = db.get_setting("death_scan_max_ocr_frames", "180")
    try:
        value = int(float(str(raw or "180")))
    except ValueError:
        value = 180
    return max(30, min(600, value))


def scan_fps(db: Database) -> str:
    rate = str(db.get_setting("frame_sample_rate", "standard") or "standard")
    return {"light": "1/3", "standard": "1", "dense": "2"}.get(rate, "1")


def crosshair_fps(db: Database) -> str:
    rate = str(db.get_setting("frame_sample_rate", "standard") or "standard")
    return {"light": "1/5", "standard": "1/3", "dense": "1"}.get(rate, "1/3")


def understand_clip(db: Database, death_id: int) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    result = describe_clip(db, death_id)
    if not result.get("ok") or not result.get("description"):
        return result
    description = result["description"]
    metrics = description.get("metrics") or {}
    labels = description.get("suggested_labels") or []
    understanding = {
        "kind": "clip_understanding",
        "death_id": death_id,
        "summary": build_clip_understanding_summary(description),
        "timeline": {
            "peak_second": metrics.get("peak_second"),
            "peak_motion": metrics.get("peak_motion"),
            "late_death_ui_score": metrics.get("late_death_ui_score"),
        },
        "minimap_read": minimap_read_from_metrics(metrics),
        "crosshair_read": crosshair_read_from_metrics(metrics),
        "suggested_labels": labels,
        "confidence": description.get("confidence", 0),
    }
    db.save_death_analysis(death_id, "clip_understanding", understanding)
    return {"ok": True, "message": "Clip understanding generated locally.", "analysis": understanding}


def build_clip_understanding_summary(description: Dict[str, Any]) -> str:
    labels = description.get("suggested_labels") or []
    label_text = ", ".join(labels) if labels else "manual review"
    return f"Local clip pipeline read: {description.get('summary', '')} Suggested focus: {label_text}."


def minimap_read_from_metrics(metrics: Dict[str, Any]) -> str:
    if float(metrics.get("peak_motion") or 0) > 0.28:
        return "High motion near the event; check whether the fight happened during a rotate, swing, or panic reposition."
    return "No strong minimap-specific conclusion from the clip sample."


def crosshair_read_from_metrics(metrics: Dict[str, Any]) -> str:
    activity = float(metrics.get("average_crosshair_activity") or 0)
    if activity > 0.10:
        return "Crosshair region was busy; review whether pre-aim reduced correction before the duel."
    return "Crosshair region looked comparatively stable in sampled frames."


def default_calibration() -> Dict[str, Dict[str, float]]:
    return {
        "hud_top": {"x": 0.20, "y": 0.00, "w": 0.60, "h": 0.14},
        "hud_bottom": {"x": 0.20, "y": 0.78, "w": 0.60, "h": 0.22},
        "killfeed": {"x": 0.54, "y": 0.00, "w": 0.46, "h": 0.24},
        "minimap": {"x": 0.02, "y": 0.02, "w": 0.20, "h": 0.28},
        "crosshair": {"x": 0.45, "y": 0.45, "w": 0.10, "h": 0.10},
        "combat_report": {"x": 0.68, "y": 0.22, "w": 0.28, "h": 0.50},
    }


def red_score(region: np.ndarray) -> float:
    red = region[:, :, 0]
    green = region[:, :, 1]
    blue = region[:, :, 2]
    dominance = red - np.maximum(green, blue)
    return float(np.clip(dominance, 0, 1).mean() * 4.0)


def describe_clip(db: Database, death_id: int) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    clip_path = death.get("clip_path")
    if not clip_path or not Path(clip_path).exists():
        return {
            "ok": False,
            "message": "No extracted clip exists for this death. Install ffmpeg and run Extract Clips first.",
            "description": None,
        }
    result = describe_video_file(Path(clip_path), death_id=death_id, calibration=db.get_calibration())
    if result.get("ok") and result.get("description"):
        analysis = result["description"]
        analysis_id = db.save_clip_analysis(analysis)
        analysis["id"] = analysis_id
    return result


def describe_video_file(
    video_path: Path,
    death_id: Optional[int] = None,
    calibration: Optional[Dict[str, Dict[str, float]]] = None,
) -> Dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for clip frame analysis.", "description": None}
    frame_dir = video_path.parent / "vision" / video_path.stem
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir)
    if not frames:
        return {"ok": False, "message": "No frames could be extracted from the clip.", "description": None}

    timeline = build_timeline(frames, calibration)
    scores = [item.death_score for item in timeline]
    pressure = [item.pressure_score for item in timeline]
    motion = [item.motion for item in timeline]
    crosshair = [item.crosshair_activity for item in timeline]
    peak_index = int(np.argmax(np.asarray(scores)))
    early = float(np.mean(scores[: max(1, len(scores) // 3)]))
    late = float(np.mean(scores[-max(1, len(scores) // 3) :]))
    peak = float(scores[peak_index])
    observations = clip_observations(timeline, early, late, peak)
    suggested_labels = suggested_labels_from_observations(observations)
    description = {
        "death_id": death_id,
        "frame_count": len(frames),
        "summary": clip_read(early, late, peak),
        "observations": observations,
        "suggested_labels": suggested_labels,
        "confidence": round(min(0.95, max(peak, late)), 2),
        "metrics": {
            "peak_second": peak_index,
            "peak_death_ui_score": round(peak, 2),
            "early_pressure_score": round(float(np.mean(pressure[: max(1, len(pressure) // 3)])), 2),
            "late_death_ui_score": round(late, 2),
            "average_motion": round(float(np.mean(motion)), 2),
            "peak_motion": round(float(max(motion) if motion else 0), 2),
            "average_crosshair_activity": round(float(np.mean(crosshair)), 2),
        },
        "peak_second": peak_index,
        "peak_death_ui_score": round(peak, 2),
        "early_pressure_score": round(early, 2),
        "late_death_ui_score": round(late, 2),
        "read": clip_read(early, late, peak),
    }
    return {"ok": True, "message": "Clip analyzed locally from sampled frames.", "description": description}


def clip_read(early: float, late: float, peak: float) -> str:
    if peak > 0.68 and late > early + 0.15:
        return "The clip appears to transition into a death/combat-report style screen after contact."
    if peak > 0.58:
        return "The clip has a strong visual transition near the suspected death moment, but should be verified manually."
    return "The sampled frames do not show a strong death-screen signature; review the marker manually."


def clip_observations(timeline: List[FrameMetrics], early: float, late: float, peak: float) -> List[str]:
    if not timeline:
        return ["No frames were available for local visual analysis."]
    observations: List[str] = []
    max_motion = max(item.motion for item in timeline)
    max_killfeed = max(item.killfeed_red for item in timeline)
    max_center_red = max(item.center_red for item in timeline)
    avg_crosshair = float(np.mean([item.crosshair_activity for item in timeline]))
    max_pressure = max(item.pressure_score for item in timeline)

    if peak > 0.68 and late > early + 0.15:
        observations.append("Strong death/combat-report transition appears near the end of the clip.")
    elif peak > 0.58:
        observations.append("Possible death-screen transition detected; verify manually.")
    else:
        observations.append("No strong death-screen transition was detected.")

    if max_motion > 0.28:
        observations.append("Large visual motion/scene change occurs before the suspected death.")
    if max_killfeed > 0.20:
        observations.append("Top-right HUD region shows red activity consistent with killfeed/combat events.")
    if max_center_red > 0.20:
        observations.append("Center screen shows red-tinted UI or damage/death-style overlay.")
    if avg_crosshair > 0.10:
        observations.append("Crosshair region is visually busy, which can indicate active fighting or rapid target correction.")
    if max_pressure > 0.62:
        observations.append("Pre-death frames show high visual pressure based on motion, contrast, and HUD activity.")
    return observations


def suggested_labels_from_observations(observations: List[str]) -> List[str]:
    text = " ".join(observations).lower()
    labels: List[str] = []
    if "high visual pressure" in text or "large visual motion" in text:
        labels.append("poor reposition after contact")
    if "killfeed" in text or "red-tinted" in text:
        labels.append("dry peek")
    if "crosshair region is visually busy" in text:
        labels.append("crosshair too low/wide")
    return labels[:3]
