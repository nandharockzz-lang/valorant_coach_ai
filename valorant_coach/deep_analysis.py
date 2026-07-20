import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .db import Database
from .vision import build_timeline, extract_scan_frames, ffmpeg_path


def analyze_hud(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    frames = sample_match_frames(db, match_id, work_dir / "hud", fps="1/10")
    if isinstance(frames, dict):
        return frames
    calibration = db.get_calibration()
    observations = []
    for frame in frames[:12]:
        img = Image.open(frame).convert("RGB")
        arr = np.asarray(img).astype(np.float32) / 255.0
        observations.append(hud_frame_metrics(arr, calibration))
    summary = summarize_hud(observations)
    result = {
        "kind": "hud",
        "summary": summary,
        "observations": observations[:8],
        "confidence": 0.45 if observations else 0.0,
    }
    db.save_structured_analysis(match_id, "hud", result)
    return {"ok": True, "message": "HUD sampled locally.", "analysis": result}


def analyze_minimap(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    frames = sample_match_frames(db, match_id, work_dir / "minimap", fps="1/5")
    if isinstance(frames, dict):
        return frames
    calibration = db.get_calibration()
    observations = []
    previous = None
    for frame in frames[:24]:
        img = Image.open(frame).convert("RGB")
        arr = np.asarray(img).astype(np.float32) / 255.0
        minimap = crop_region(arr, calibration["minimap"])
        motion = float(np.mean(np.abs(minimap - previous)) * 2.5) if previous is not None else 0.0
        previous = minimap
        observations.append(
            {
                "map_activity": round(float(minimap.std()), 3),
                "rotation_motion": round(motion, 3),
                "bright_marker_density": round(float((minimap.mean(axis=2) > 0.62).mean()), 3),
            }
        )
    result = {
        "kind": "minimap_v2",
        "summary": summarize_minimap(observations),
        "interpretation": interpret_minimap(observations),
        "spacing_read": minimap_spacing_read(observations),
        "observations": observations[:10],
        "confidence": 0.40 if observations else 0.0,
    }
    db.save_structured_analysis(match_id, "minimap", result)
    return {"ok": True, "message": "Minimap sampled locally.", "analysis": result}


def analyze_ocr(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    tesseract = tesseract_path()
    if not tesseract:
        return {
            "ok": False,
            "message": "Tesseract OCR is not installed or not on PATH.",
            "analysis": {"engine": "tesseract", "available": False},
        }
    frames = sample_match_frames(db, match_id, work_dir / "ocr", fps="1/15")
    if isinstance(frames, dict):
        return frames
    calibration = db.get_calibration()
    crop_dir = work_dir / "ocr" / f"match-{match_id}" / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    regions = ["hud_top", "killfeed", "combat_report"]
    reads = []
    for idx, frame in enumerate(frames[:8]):
        image = Image.open(frame).convert("RGB")
        arr = np.asarray(image).astype(np.float32) / 255.0
        for region_name in regions:
            crop_arr = crop_region(arr, calibration[region_name])
            crop_image = Image.fromarray(np.clip(crop_arr * 255, 0, 255).astype(np.uint8))
            crop_path = crop_dir / f"{idx:03d}-{region_name}.png"
            crop_image.save(crop_path)
            text = run_tesseract(tesseract, crop_path)
            if text:
                reads.append({"frame": idx, "region": region_name, "text": text})
    result = {
        "kind": "ocr",
        "summary": f"OCR completed with {len(reads)} non-empty text read(s).",
        "engine": "tesseract",
        "reads": reads,
        "timeline_events": ocr_timeline_events(reads),
        "confidence": 0.50 if reads else 0.10,
    }
    db.save_structured_analysis(match_id, "ocr", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def infer_rounds_from_scoreboard(db: Database, match_id: int, work_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for scoreboard round detection.", "analysis": None}
    tesseract = tesseract_path()
    if not tesseract:
        return {
            "ok": False,
            "message": "Tesseract OCR is required to read the top scoreboard.",
            "analysis": {"engine": "tesseract", "available": False},
        }

    deaths = [
        death
        for death in db.get_deaths(match_id)
        if death.get("timestamp") is not None
    ]
    frame_dir = work_dir / "scoreboard-rounds" / f"match-{match_id}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    results = []
    updated = 0
    for death in deaths:
        read = read_scoreboard_round_consensus(tesseract, ffmpeg, video_path, frame_dir, death)
        existing_round = int(death.get("round_number") or 0)
        read_round = int(read.get("round_number") or 0)
        can_update_missing = read_round and not existing_round
        can_correct_existing = read_round and existing_round and existing_round != read_round and read.get("overwrite_safe")
        if can_update_missing or can_correct_existing:
            db.update_death_round_number(int(death["id"]), int(read["round_number"]))
            updated += 1
        results.append({"death_id": death["id"], "timestamp": death["timestamp"], "existing_round_number": existing_round or None, **read})

    analysis = {
        "kind": "scoreboard_rounds",
        "summary": f"Read scoreboard scores for {len(results)} death marker(s); updated {updated} round number(s).",
        "updated": updated,
        "checked": len(results),
        "reads": results,
        "confidence": round(
            sum(float(item.get("confidence") or 0) for item in results) / len(results),
            2,
        )
        if results
        else 0.0,
    }
    db.save_structured_analysis(match_id, "scoreboard_rounds", analysis)
    return {"ok": True, "message": analysis["summary"], "analysis": analysis}


def analyze_gameplay(db: Database, death_id: int) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        raise ValueError(f"Unknown death id: {death_id}")
    vision = db.get_latest_clip_analysis(death_id)
    labels = death.get("mistake_labels") or []
    hypotheses: List[str] = []
    confidence = float(death.get("confidence") or 0)

    if vision:
        observations = " ".join(vision.get("observations") or []).lower()
        metrics = vision.get("metrics") or {}
        confidence = max(confidence, float(vision.get("confidence") or 0))
        if float(metrics.get("peak_motion") or 0) > 0.28:
            hypotheses.append("Likely contact or rapid reposition happened shortly before the death.")
        if float(metrics.get("average_crosshair_activity") or 0) > 0.10:
            hypotheses.append("Crosshair region was unstable/busy; check pre-aim and target correction.")
        if "killfeed" in observations or "red-tinted" in observations:
            hypotheses.append("HUD activity suggests the marker is near combat/death timing.")

    if "dry peek" in labels:
        hypotheses.append("Existing labels indicate a first-contact risk: verify whether utility or a trade was available.")
    if "exposed to multiple angles" in labels:
        hypotheses.append("Existing labels indicate possible angle-isolation failure.")
    if not hypotheses:
        hypotheses.append("Not enough visual or label evidence for a strong gameplay hypothesis yet.")

    result = {
        "kind": "gameplay",
        "death_id": death_id,
        "summary": "Local gameplay hypotheses generated from labels and visual reads.",
        "hypotheses": hypotheses,
        "confidence": round(min(0.95, confidence), 2),
    }
    db.save_death_analysis(death_id, "gameplay", result)
    return {"ok": True, "message": "Gameplay hypotheses generated locally.", "analysis": result}


def ai_review_status(db: Database, death_id: int) -> Dict[str, Any]:
    mode = db.get_setting("ai_review_mode", "disabled")
    provider = db.get_setting("ai_review_provider", "")
    if mode != "enabled":
        return {
            "ok": False,
            "message": "AI clip review is disabled. Enable it explicitly before any online or local model review.",
            "analysis": {
                "mode": mode,
                "provider": provider,
                "privacy": "No clip upload or model call was performed.",
            },
        }
    return {
        "ok": False,
        "message": "AI review is configured but no provider adapter has been enabled in this build.",
        "analysis": {"mode": mode, "provider": provider},
    }


def sample_match_frames(db: Database, match_id: int, work_dir: Path, fps: str) -> Any:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    video_path = Path(match["video_path"])
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "analysis": None}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required for frame analysis.", "analysis": None}
    frame_dir = work_dir / f"match-{match_id}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("sample-*.jpg"):
        old.unlink()
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
        str(frame_dir / "sample-%06d.jpg"),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"ok": False, "message": result.stderr.strip() or "ffmpeg frame extraction failed.", "analysis": None}
    return sorted(frame_dir.glob("sample-*.jpg"))


def hud_frame_metrics(arr: np.ndarray, calibration: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    top = crop_region(arr, calibration["hud_top"])
    bottom = crop_region(arr, calibration["hud_bottom"])
    top_right = crop_region(arr, calibration["killfeed"])
    return {
        "top_hud_contrast": round(float(top.std()), 3),
        "bottom_hud_darkness": round(1.0 - float(bottom.mean()), 3),
        "killfeed_red_density": round(red_score(top_right), 3),
    }


def summarize_hud(items: List[Dict[str, float]]) -> str:
    if not items:
        return "No HUD frames were available."
    avg_red = sum(item["killfeed_red_density"] for item in items) / len(items)
    avg_bottom = sum(item["bottom_hud_darkness"] for item in items) / len(items)
    return f"HUD sample complete. Killfeed red density averages {avg_red:.2f}; lower HUD darkness averages {avg_bottom:.2f}."


def summarize_minimap(items: List[Dict[str, float]]) -> str:
    if not items:
        return "No minimap frames were available."
    motion = sum(item["rotation_motion"] for item in items) / len(items)
    density = sum(item["bright_marker_density"] for item in items) / len(items)
    return f"Minimap sample complete. Average rotation motion {motion:.2f}; marker density {density:.2f}."


def interpret_minimap(items: List[Dict[str, float]]) -> List[str]:
    if not items:
        return ["No minimap frames were available for interpretation."]
    avg_motion = sum(item["rotation_motion"] for item in items) / len(items)
    avg_density = sum(item["bright_marker_density"] for item in items) / len(items)
    reads = []
    if avg_motion > 0.16:
        reads.append("High minimap motion suggests frequent rotation/reposition timing; compare deaths against rotate calls.")
    else:
        reads.append("Minimap motion was modest in sampled frames; deaths may be more duel/positioning driven than rotation driven.")
    if avg_density < 0.05:
        reads.append("Low bright marker density can indicate poor minimap readability in this capture; recalibrate or review frame quality.")
    elif avg_density > 0.16:
        reads.append("High marker density gives enough signal to review teammate spacing and isolation patterns.")
    return reads


def minimap_spacing_read(items: List[Dict[str, float]]) -> Dict[str, Any]:
    if not items:
        return {"risk": "unknown", "reason": "No minimap observations were available."}
    avg_motion = sum(item["rotation_motion"] for item in items) / len(items)
    avg_density = sum(item["bright_marker_density"] for item in items) / len(items)
    activity = sum(item["map_activity"] for item in items) / len(items)
    if avg_density < 0.04:
        return {"risk": "low-confidence", "reason": "The minimap crop is too sparse or low contrast for teammate-spacing reads."}
    if avg_motion > 0.18 and activity > 0.20:
        return {"risk": "rotation-timing", "reason": "High minimap motion with readable markers suggests rotation timing should be checked around deaths."}
    if avg_density > 0.15:
        return {"risk": "spacing-review", "reason": "Marker density is high enough to review whether deaths happened isolated from teammate support."}
    return {"risk": "moderate", "reason": "Minimap signal is usable but not strong enough for a sharp spacing conclusion."}


def extract_single_frame(ffmpeg: str, video_path: Path, timestamp: float, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{timestamp:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:-1",
        "-q:v",
        "3",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and output_path.exists()


def read_scoreboard_round_consensus(
    tesseract: str,
    ffmpeg: str,
    video_path: Path,
    work_dir: Path,
    death: Dict[str, Any],
) -> Dict[str, Any]:
    death_id = int(death["id"])
    death_ts = float(death["timestamp"])
    offsets = (-8.0, -5.0, -2.0, 0.0, 2.0)
    reads = []
    for offset in offsets:
        timestamp = max(0.0, death_ts + offset)
        frame_path = work_dir / f"death-{death_id}-{int(offset * 10):+04d}.jpg"
        extracted = extract_single_frame(ffmpeg, video_path, timestamp, frame_path)
        if not extracted:
            reads.append({"sample_ts": timestamp, "status": "frame_failed"})
            continue
        read = read_scoreboard_round(tesseract, frame_path, work_dir, death_id, sample_tag=f"{int(offset * 10):+04d}")
        reads.append({"sample_ts": timestamp, **read})

    candidates: Dict[int, List[Dict[str, Any]]] = {}
    for read in reads:
        round_number = read.get("round_number")
        if round_number:
            candidates.setdefault(int(round_number), []).append(read)

    if not candidates:
        return {
            "status": "unreadable_consensus",
            "round_number": None,
            "confidence": 0.0,
            "samples": reads,
        }

    round_number, winning_reads = max(candidates.items(), key=lambda item: (len(item[1]), sum(float(row.get("confidence") or 0) for row in item[1])))
    support = len(winning_reads)
    avg_confidence = sum(float(row.get("confidence") or 0) for row in winning_reads) / max(1, support)
    strong = support >= 2 and avg_confidence >= 0.70
    overwrite_safe = support >= 3 and avg_confidence >= 0.84
    return {
        "status": "consensus_read" if strong else "low_confidence_consensus",
        "round_number": round_number if strong else None,
        "candidate_round_number": round_number,
        "support": support,
        "overwrite_safe": overwrite_safe,
        "confidence": round(avg_confidence if strong else min(avg_confidence, 0.45), 2),
        "samples": reads,
    }


def read_scoreboard_round(tesseract: str, frame_path: Path, work_dir: Path, death_id: int, sample_tag: str = "") -> Dict[str, Any]:
    image = Image.open(frame_path).convert("RGB")
    crops = scoreboard_score_crops(image)
    reads: Dict[str, List[Dict[str, Any]]] = {"left": [], "right": []}
    for key, variants in crops.items():
        for variant, crop_img in variants.items():
            for preprocess_name, prepared in preprocess_score_crops(crop_img).items():
                tag = f"{sample_tag}-" if sample_tag else ""
                crop_path = work_dir / f"death-{death_id}-{tag}{key}-{variant}-{preprocess_name}.png"
                prepared.save(crop_path)
                text = run_tesseract_digits(tesseract, crop_path)
                value = parse_score_digit(text)
                reads[key].append({"variant": f"{variant}:{preprocess_name}", "text": text, "score": value, "crop": str(crop_path)})
    left_read = best_score_read(reads["left"])
    right_read = best_score_read(reads["right"])
    left = left_read.get("score")
    right = right_read.get("score")
    if left is None or right is None:
        return {
            "status": "unreadable",
            "left_score": left,
            "right_score": right,
            "round_number": None,
            "confidence": 0.0,
            "raw": {"left": left_read, "right": right_read, "all": reads},
        }
    round_number = int(left) + int(right) + 1
    valid = 1 <= round_number <= 30 and 0 <= int(left) <= 14 and 0 <= int(right) <= 14
    confidence = min(float(left_read.get("confidence") or 0), float(right_read.get("confidence") or 0))
    return {
        "status": "read" if valid else "out_of_range",
        "left_score": int(left),
        "right_score": int(right),
        "round_number": round_number if valid else None,
        "confidence": confidence if valid else 0.25,
        "raw": {"left": left_read, "right": right_read, "all": reads},
    }


def best_score_read(reads: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [item for item in reads if item.get("score") is not None]
    if not valid:
        return {"score": None, "confidence": 0.0, "reads": reads}
    counts: Dict[int, int] = {}
    for item in valid:
        score = int(item["score"])
        counts[score] = counts.get(score, 0) + 1
    score, count = max(counts.items(), key=lambda item: item[1])
    representative = next(item for item in valid if int(item["score"]) == score)
    confidence = min(0.92, 0.52 + (count * 0.16))
    return {**representative, "score": score, "confidence": round(confidence, 2), "support": count, "reads": reads}


def scoreboard_score_crops(image: Image.Image) -> Dict[str, Dict[str, Image.Image]]:
    width, height = image.size
    # VALORANT's top scoreboard is centered. These crops target the two score numbers
    # beside the round timer and use variants because HUD scale differs by capture.
    boxes = {
        "left": {
            "tight": (0.425, 0.015, 0.485, 0.095),
            "wide": (0.405, 0.010, 0.492, 0.105),
            "lower": (0.420, 0.030, 0.490, 0.120),
            "tall": (0.412, 0.000, 0.492, 0.130),
        },
        "right": {
            "tight": (0.515, 0.015, 0.575, 0.095),
            "wide": (0.508, 0.010, 0.595, 0.105),
            "lower": (0.510, 0.030, 0.580, 0.120),
            "tall": (0.508, 0.000, 0.588, 0.130),
        },
    }
    return {
        key: {
            variant: image.crop(
                (
                    int(width * left),
                    int(height * top),
                    int(width * right),
                    int(height * bottom),
                )
            )
            for variant, (left, top, right, bottom) in variants.items()
        }
        for key, variants in boxes.items()
    }


def preprocess_score_crops(image: Image.Image) -> Dict[str, Image.Image]:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = gray.resize((gray.width * 5, gray.height * 5))
    gray = gray.filter(ImageFilter.SHARPEN)
    return {
        "binary-low": gray.point(lambda value: 255 if value > 125 else 0),
        "binary-mid": gray.point(lambda value: 255 if value > 145 else 0),
        "binary-high": gray.point(lambda value: 255 if value > 165 else 0),
        "contrast": gray,
    }


def run_tesseract_digits(tesseract: str, image_path: Path) -> str:
    cmd = [
        tesseract,
        str(image_path),
        "stdout",
        "--psm",
        "7",
        "-c",
        "tessedit_char_whitelist=0123456789",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return "".join(result.stdout.split())


def parse_score_digit(text: str) -> Optional[int]:
    match = re.search(r"\d{1,2}", text or "")
    if not match:
        return None
    value = int(match.group(0))
    return value if 0 <= value <= 14 else None


def ocr_timeline_events(reads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events = []
    for item in reads:
        text = str(item.get("text") or "").strip()
        lower = text.lower()
        kind = "hud_text"
        if any(token in lower for token in ("combat", "killed", "damage", "head")):
            kind = "combat_report"
        elif ":" in text or "round" in lower:
            kind = "round_hud"
        events.append({"frame": item.get("frame"), "region": item.get("region"), "kind": kind, "text": text})
    return events


def crop(arr: np.ndarray, top: float, bottom: float, left: float, right: float) -> np.ndarray:
    h, w, _ = arr.shape
    return arr[int(h * top) : int(h * bottom), int(w * left) : int(w * right), :]


def crop_region(arr: np.ndarray, region: Dict[str, float]) -> np.ndarray:
    x = float(region["x"])
    y = float(region["y"])
    w = float(region["w"])
    h = float(region["h"])
    return crop(arr, y, min(1.0, y + h), x, min(1.0, x + w))


def red_score(region: np.ndarray) -> float:
    red = region[:, :, 0]
    green = region[:, :, 1]
    blue = region[:, :, 2]
    return float(np.clip(red - np.maximum(green, blue), 0, 1).mean() * 4.0)


def tesseract_path() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    candidates = [
        root / "tools" / "tesseract" / "tesseract.exe",
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def run_tesseract(tesseract: str, image_path: Path) -> str:
    cmd = [tesseract, str(image_path), "stdout", "--psm", "6"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return " ".join(result.stdout.split())
