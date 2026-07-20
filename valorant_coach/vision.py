import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from .db import Database


FRAME_DIR_NAME = "frames"


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


def suggest_deaths(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
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
    cleaned = db.cleanup_pending_death_suggestions(match_id)
    frames = extract_scan_frames(ffmpeg, video_path, frame_dir, fps=scan_fps(db))
    feedback = db.detector_feedback_summary()
    feedback["sensitivity"] = db.get_setting("detector_sensitivity", "normal")
    suggestions = analyze_frames(frames, db.get_calibration(), feedback)
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
    return {"ok": True, "message": message, "suggestions": saved, "skipped_duplicates": skipped + cleaned}


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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg frame extraction failed")
    return sorted(frame_dir.glob("scan-*.jpg"))


def analyze_frames(
    frames: List[Path],
    calibration: Optional[Dict[str, Dict[str, float]]] = None,
    feedback: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    timeline = build_timeline(frames, calibration)
    return cluster_death_candidates(timeline, adaptive_death_threshold(feedback))


def build_timeline(frames: List[Path], calibration: Optional[Dict[str, Dict[str, float]]] = None) -> List[FrameMetrics]:
    timeline: List[FrameMetrics] = []
    previous: Optional[np.ndarray] = None
    previous_minimap: Optional[np.ndarray] = None
    previous_crosshair: Optional[np.ndarray] = None
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
        timeline.append(compute_metrics(frame, float(index), arr, motion, regions, minimap_motion, crosshair_drift))
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
    frames = extract_keyframe_source(ffmpeg, source, frame_dir, death.get("timestamp") if not clip_path else None)
    timeline = build_timeline(frames, db.get_calibration())
    selected = select_keyframes(timeline)
    gallery = []
    for item in selected:
        stem = f"kf-death-{death_id}-{item['role']}"
        target = frame_dir / f"{stem}.jpg"
        shutil.copyfile(item["path"], target)
        gallery.append(
            {
                "role": item["role"],
                "timestamp": item["timestamp"],
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg keyframe extraction failed")
    return sorted(frame_dir.glob("scan-*.jpg"))


def select_keyframes(timeline: List[FrameMetrics]) -> List[Dict[str, Any]]:
    if not timeline:
        return []
    roles = {
        "pre-contact": min(timeline, key=lambda item: item.pressure_score),
        "first-pressure": first_above(timeline, "pressure_score", 0.45) or max(timeline, key=lambda item: item.pressure_score),
        "peak-motion": max(timeline, key=lambda item: item.motion),
        "death-ui": max(timeline, key=lambda item: item.death_score),
        "post-death": timeline[-1],
    }
    selected = []
    seen = set()
    for role, item in roles.items():
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
                "title": f"Review death R{death.get('round_number') or '?'} @ {format_seconds(death.get('timestamp'))}",
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
