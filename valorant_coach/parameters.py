import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .clipper import ffmpeg_path
from .db import Database, normalize_compare_value
from .deep_analysis import extract_single_frame, read_scoreboard_round, tesseract_path


DEFAULT_PARAMETER_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "round_score_left",
        "label": "Left Team Score",
        "description": "Score number to the left of the round timer.",
        "source_region": "hud_top",
        "extractor_type": "score_digit",
        "config": {"side": "left", "valid_min": 0, "valid_max": 14},
        "dependencies": [],
    },
    {
        "key": "round_score_right",
        "label": "Right Team Score",
        "description": "Score number to the right of the round timer.",
        "source_region": "hud_top",
        "extractor_type": "score_digit",
        "config": {"side": "right", "valid_min": 0, "valid_max": 14},
        "dependencies": [],
    },
    {
        "key": "round_number",
        "label": "Round Number",
        "description": "Derived as left score plus right score plus one.",
        "source_region": "hud_top",
        "extractor_type": "derived_sum_plus_one",
        "config": {"valid_min": 1, "valid_max": 30},
        "dependencies": ["round_score_left", "round_score_right"],
    },
    {
        "key": "round_timer",
        "label": "Round Timer",
        "description": "Top-center round timer, parsed separately from score digits.",
        "source_region": "hud_top",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"\b([0-1]?\d:[0-5]\d)\b",
            "group": 1,
            "value_type": "string",
            "sub_region": {"x": 0.36, "y": 0.0, "w": 0.28, "h": 0.95},
            "psm": "7",
        },
        "dependencies": [],
    },
    {
        "key": "health",
        "label": "Health",
        "description": "Bottom HUD health value.",
        "source_region": "hud_bottom",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"\b(100|[1-9]\d?)\b",
            "group": 1,
            "value_type": "integer",
            "valid_min": 1,
            "valid_max": 100,
            "sub_region": {"x": 0.0, "y": 0.35, "w": 0.28, "h": 0.45},
            "psm": "7",
        },
        "dependencies": [],
    },
    {
        "key": "ammo_magazine",
        "label": "Ammo Magazine",
        "description": "Current bullets in magazine from bottom HUD ammo text.",
        "source_region": "hud_bottom",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"\b(\d{1,2})\s*/\s*(\d{1,3})\b",
            "group": 1,
            "value_type": "integer",
            "valid_min": 0,
            "valid_max": 100,
            "sub_region": {"x": 0.62, "y": 0.20, "w": 0.36, "h": 0.55},
            "psm": "7",
        },
        "dependencies": [],
    },
    {
        "key": "ammo_reserve",
        "label": "Ammo Reserve",
        "description": "Reserve bullets from bottom HUD ammo text.",
        "source_region": "hud_bottom",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"\b(\d{1,2})\s*/\s*(\d{1,3})\b",
            "group": 2,
            "value_type": "integer",
            "valid_min": 0,
            "valid_max": 300,
            "sub_region": {"x": 0.62, "y": 0.20, "w": 0.36, "h": 0.55},
            "psm": "7",
        },
        "dependencies": [],
    },
    {
        "key": "weapon",
        "label": "Weapon",
        "description": "Weapon name if OCR can read bottom HUD text.",
        "source_region": "hud_bottom",
        "extractor_type": "ocr_vocabulary",
        "config": {
            "terms": [
                "vandal",
                "phantom",
                "operator",
                "outlaw",
                "sheriff",
                "ghost",
                "spectre",
                "guardian",
                "bulldog",
                "marshal",
                "odin",
                "ares",
                "judge",
                "bucky",
                "classic",
                "frenzy",
                "shorty",
                "stinger",
            ],
            "sub_region": {"x": 0.28, "y": 0.08, "w": 0.48, "h": 0.65},
            "psm": "6",
        },
        "dependencies": [],
    },
    {
        "key": "combat_report_damage_dealt",
        "label": "Damage Dealt",
        "description": "Damage dealt number from the post-death combat report.",
        "source_region": "combat_report",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"(?:dealt|outgoing|damage)[^\d]{0,12}(\d{1,3})",
            "group": 1,
            "value_type": "integer",
            "valid_min": 0,
            "valid_max": 999,
            "sub_region": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.65},
            "psm": "6",
        },
        "dependencies": [],
    },
    {
        "key": "combat_report_damage_taken",
        "label": "Damage Received",
        "description": "Damage received number from the post-death combat report.",
        "source_region": "combat_report",
        "extractor_type": "ocr_regex",
        "config": {
            "regex": r"(?:received|incoming|taken)[^\d]{0,12}(\d{1,3})",
            "group": 1,
            "value_type": "integer",
            "valid_min": 0,
            "valid_max": 999,
            "sub_region": {"x": 0.0, "y": 0.20, "w": 1.0, "h": 0.80},
            "psm": "6",
        },
        "dependencies": [],
    },
    {
        "key": "killfeed_player_death",
        "label": "Player In Killfeed",
        "description": "Whether the configured in-game name is visible in the top-right killfeed.",
        "source_region": "killfeed",
        "extractor_type": "ocr_contains",
        "config": {"setting_key": "player_name", "default": "SicaJR", "sub_region": {"x": 0, "y": 0, "w": 1, "h": 1}, "psm": "6"},
        "dependencies": [],
    },
]


def ensure_default_parameters(db: Database) -> None:
    db.seed_parameter_definitions(DEFAULT_PARAMETER_DEFINITIONS)


def parameter_dashboard(db: Database) -> Dict[str, Any]:
    ensure_default_parameters(db)
    definitions = db.list_parameter_definitions()
    labels = db.list_parameter_labels(limit=5000)
    latest_reads = latest_reads_by_key(db.latest_parameter_reads(limit=1000))
    label_stats = labels_by_key(labels)
    rows = []
    trained = 0
    for definition in definitions:
        key = definition["parameter_key"]
        stats = label_stats.get(key, {"labels": 0, "checked": 0, "correct": 0})
        accuracy = round(stats["correct"] / stats["checked"], 2) if stats["checked"] else None
        if stats["labels"] >= 5 and (accuracy is None or accuracy >= 0.75):
            trained += 1
        rows.append(
            {
                **definition,
                "latest_read": latest_reads.get(key),
                "label_count": stats["labels"],
                "checked_count": stats["checked"],
                "accuracy": accuracy,
                "status": parameter_status(definition, stats, latest_reads.get(key), accuracy),
            }
        )
    readiness = round((trained / max(1, len(definitions))) * 100)
    return {
        "ok": True,
        "summary": f"{readiness}% parameter trainer readiness across {len(definitions)} signal(s).",
        "readiness_percent": readiness,
        "parameter_count": len(definitions),
        "trained_count": trained,
        "parameters": rows,
        "gaps": parameter_gaps(rows),
    }


def list_parameters(db: Database) -> Dict[str, Any]:
    ensure_default_parameters(db)
    return {"ok": True, "parameters": db.list_parameter_definitions()}


def update_parameter_definition(db: Database, parameter_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_default_parameters(db)
    parsed = dict(payload)
    if isinstance(parsed.get("config"), str):
        parsed["config"] = json.loads(parsed["config"] or "{}")
    if isinstance(parsed.get("dependencies"), str):
        parsed["dependencies"] = json.loads(parsed["dependencies"] or "[]")
    updated = db.upsert_parameter_definition(parameter_key, parsed)
    return {"ok": True, "parameter": updated}


def save_parameter_label(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_default_parameters(db)
    label_id = db.save_parameter_label(payload)
    return {"ok": True, "label_id": label_id, "dashboard": parameter_dashboard(db)}


def extract_match_parameters(db: Database, match_id: int, work_dir: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_default_parameters(db)
    match = db.get_match(match_id)
    if not match:
        return {"ok": False, "message": "match not found"}
    video_path = Path(match.get("video_path") or "")
    if not video_path.exists():
        return {"ok": False, "message": "Video file is missing.", "status": "missing_video"}
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {"ok": False, "message": "ffmpeg is required to capture a trainer frame.", "status": "missing_tool"}
    timestamp = parse_timestamp(payload.get("timestamp"), 60.0)
    frame_dir = work_dir / "parameter-trainer" / f"match-{match_id}"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frame_id = f"param-m{match_id}-{int(timestamp * 10):08d}"
    frame_path = frame_dir / f"{frame_id}.jpg"
    if not extract_single_frame(ffmpeg, video_path, timestamp, frame_path):
        return {"ok": False, "message": "Could not extract trainer frame.", "status": "frame_failed"}

    definitions = [item for item in db.list_parameter_definitions() if item.get("enabled")]
    image = Image.open(frame_path).convert("RGB")
    tesseract = tesseract_path()
    calibration = db.get_calibration()
    cache: Dict[str, Any] = {}
    reads: Dict[str, Dict[str, Any]] = {}
    for definition in definitions:
        read = extract_parameter(
            db=db,
            definition=definition,
            image=image,
            frame_path=frame_path,
            frame_dir=frame_dir,
            frame_id=frame_id,
            match_id=match_id,
            timestamp=timestamp,
            tesseract=tesseract,
            calibration=calibration,
            prior_reads=reads,
            cache=cache,
        )
        read_id = db.save_parameter_read(read)
        read["id"] = read_id
        reads[definition["parameter_key"]] = read
    return {
        "ok": True,
        "message": f"Extracted {len(reads)} parameter(s) at {format_timestamp(timestamp)}.",
        "timestamp": timestamp,
        "frame_id": frame_id,
        "frame": {"frame_id": frame_id, "timestamp": timestamp},
        "reads": list(reads.values()),
        "by_key": reads,
        "dashboard": parameter_dashboard(db),
    }


def extract_parameter(
    db: Database,
    definition: Dict[str, Any],
    image: Image.Image,
    frame_path: Path,
    frame_dir: Path,
    frame_id: str,
    match_id: int,
    timestamp: float,
    tesseract: Optional[str],
    calibration: Dict[str, Dict[str, float]],
    prior_reads: Dict[str, Dict[str, Any]],
    cache: Dict[str, Any],
) -> Dict[str, Any]:
    extractor = definition.get("extractor_type") or ""
    key = definition["parameter_key"]
    base = {
        "parameter_key": key,
        "match_id": match_id,
        "timestamp": timestamp,
        "frame_id": frame_id,
        "value": None,
        "raw_text": "",
        "confidence": 0.0,
        "status": "unknown",
        "evidence": {
            "source_region": definition.get("source_region") or "",
            "extractor_type": extractor,
            "dependencies": definition.get("dependencies") or [],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    if extractor == "score_digit":
        return merge_parameter_read(base, extract_score_digit(definition, tesseract, frame_path, frame_dir, frame_id, cache))
    if extractor == "derived_sum_plus_one":
        return merge_parameter_read(base, extract_sum_plus_one(definition, prior_reads))
    if extractor == "ocr_regex":
        return merge_parameter_read(base, extract_ocr_regex(definition, image, frame_dir, frame_id, tesseract, calibration))
    if extractor == "ocr_vocabulary":
        return merge_parameter_read(base, extract_ocr_vocabulary(definition, image, frame_dir, frame_id, tesseract, calibration))
    if extractor == "ocr_contains":
        return merge_parameter_read(base, extract_ocr_contains(db, definition, image, frame_dir, frame_id, tesseract, calibration))
    base["status"] = "unsupported_extractor"
    return base


def merge_parameter_read(base: Dict[str, Any], extracted: Dict[str, Any]) -> Dict[str, Any]:
    merged = {**base, **extracted}
    merged["evidence"] = {**(base.get("evidence") or {}), **(extracted.get("evidence") or {})}
    return merged


def extract_score_digit(
    definition: Dict[str, Any],
    tesseract: Optional[str],
    frame_path: Path,
    frame_dir: Path,
    frame_id: str,
    cache: Dict[str, Any],
) -> Dict[str, Any]:
    if not tesseract:
        return {"status": "missing_tool", "raw_text": "", "evidence": {"message": "Tesseract is not installed."}}
    if "scoreboard_round" not in cache:
        cache["scoreboard_round"] = read_scoreboard_round(tesseract, frame_path, frame_dir, 0, sample_tag=frame_id)
    score_read = cache["scoreboard_round"]
    side = (definition.get("config") or {}).get("side") or "left"
    value = score_read.get(f"{side}_score")
    raw_side = ((score_read.get("raw") or {}).get(side) or {})
    raw_text = str(raw_side.get("text") or "")
    confidence = float(score_read.get("confidence") or 0)
    status = "read" if value is not None and score_read.get("status") == "read" else score_read.get("status") or "unreadable"
    return {
        "value": value,
        "raw_text": raw_text,
        "confidence": confidence if value is not None else 0.0,
        "status": status,
        "evidence": {
            "score_read": score_read,
            "message": "Score digit read uses top scoreboard variants and stores all OCR crop attempts.",
        },
    }


def extract_sum_plus_one(definition: Dict[str, Any], prior_reads: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    deps = definition.get("dependencies") or []
    values = []
    for dep in deps:
        value = (prior_reads.get(dep) or {}).get("value")
        try:
            values.append(int(value))
        except (TypeError, ValueError):
            return {
                "value": None,
                "raw_text": "",
                "confidence": 0.0,
                "status": "missing_dependency",
                "evidence": {"dependency_values": {dep: (prior_reads.get(dep) or {}).get("value") for dep in deps}},
            }
    value = sum(values) + 1
    config = definition.get("config") or {}
    valid = int(config.get("valid_min", 1)) <= value <= int(config.get("valid_max", 30))
    dep_conf = [float((prior_reads.get(dep) or {}).get("confidence") or 0) for dep in deps]
    return {
        "value": value if valid else None,
        "raw_text": " + ".join(str(item) for item in values) + " + 1",
        "confidence": round(min(dep_conf or [0.0]), 2) if valid else 0.0,
        "status": "derived" if valid else "out_of_range",
        "evidence": {"dependency_values": {dep: (prior_reads.get(dep) or {}).get("value") for dep in deps}, "formula": "sum(dependencies) + 1"},
    }


def extract_ocr_regex(
    definition: Dict[str, Any],
    image: Image.Image,
    frame_dir: Path,
    frame_id: str,
    tesseract: Optional[str],
    calibration: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    text, evidence, status = run_parameter_ocr(definition, image, frame_dir, frame_id, tesseract, calibration)
    if status != "readable":
        return {"value": None, "raw_text": text, "confidence": 0.0, "status": status, "evidence": evidence}
    config = definition.get("config") or {}
    match = re.search(str(config.get("regex") or ""), text, flags=re.IGNORECASE)
    if not match:
        return {"value": None, "raw_text": text, "confidence": 0.15 if text else 0.0, "status": "unmatched", "evidence": evidence}
    group = int(config.get("group") or 1)
    value: Any = match.group(group)
    if config.get("value_type") == "integer":
        value = int(value)
        if not within_range(value, config):
            return {"value": None, "raw_text": text, "confidence": 0.25, "status": "out_of_range", "evidence": evidence}
    return {"value": value, "raw_text": text, "confidence": 0.72, "status": "read", "evidence": evidence}


def extract_ocr_vocabulary(
    definition: Dict[str, Any],
    image: Image.Image,
    frame_dir: Path,
    frame_id: str,
    tesseract: Optional[str],
    calibration: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    text, evidence, status = run_parameter_ocr(definition, image, frame_dir, frame_id, tesseract, calibration)
    lower = text.lower()
    for term in (definition.get("config") or {}).get("terms") or []:
        if str(term).lower() in lower:
            return {"value": str(term).title(), "raw_text": text, "confidence": 0.68, "status": "read", "evidence": evidence}
    return {"value": None, "raw_text": text, "confidence": 0.12 if text else 0.0, "status": status if status != "readable" else "unmatched", "evidence": evidence}


def extract_ocr_contains(
    db: Database,
    definition: Dict[str, Any],
    image: Image.Image,
    frame_dir: Path,
    frame_id: str,
    tesseract: Optional[str],
    calibration: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    text, evidence, status = run_parameter_ocr(definition, image, frame_dir, frame_id, tesseract, calibration)
    config = definition.get("config") or {}
    target = db.get_setting(str(config.get("setting_key") or ""), str(config.get("default") or "")) or ""
    found = bool(target and normalize_compare_value(target) in normalize_compare_value(text))
    return {
        "value": "visible" if found else None,
        "raw_text": text,
        "confidence": 0.74 if found else (0.1 if text else 0.0),
        "status": "read" if found else (status if status != "readable" else "not_found"),
        "evidence": {**evidence, "target": target},
    }


def run_parameter_ocr(
    definition: Dict[str, Any],
    image: Image.Image,
    frame_dir: Path,
    frame_id: str,
    tesseract: Optional[str],
    calibration: Dict[str, Dict[str, float]],
) -> Tuple[str, Dict[str, Any], str]:
    region_name = definition.get("source_region") or ""
    if not tesseract:
        return "", {"message": "Tesseract is not installed.", "source_region": region_name}, "missing_tool"
    region = calibration.get(region_name)
    if not region:
        return "", {"message": "Missing calibration region.", "source_region": region_name}, "missing_calibration"
    config = definition.get("config") or {}
    crop, box = crop_parameter_region(image, region, config.get("sub_region") or {})
    safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "-", definition["parameter_key"])
    crop_id = f"{frame_id}-{safe_key}"
    crop_path = frame_dir / f"{crop_id}.jpg"
    prep_path = frame_dir / f"{crop_id}-ocr.png"
    crop.save(crop_path, quality=90)
    preprocess_ocr_crop(crop).save(prep_path)
    text = run_tesseract_text(tesseract, prep_path, psm=str(config.get("psm") or "6")).strip()
    evidence = {
        "source_region": region_name,
        "sub_region": config.get("sub_region") or {},
        "box": box,
        "frame_id": crop_id,
        "crop_path": str(crop_path),
        "prepared_path": str(prep_path),
    }
    return text, evidence, "readable" if text else "unreadable"


def crop_parameter_region(image: Image.Image, region: Dict[str, float], sub_region: Dict[str, Any]) -> Tuple[Image.Image, Dict[str, float]]:
    width, height = image.size
    rx = float(region.get("x") or 0)
    ry = float(region.get("y") or 0)
    rw = float(region.get("w") or 1)
    rh = float(region.get("h") or 1)
    sx = float(sub_region.get("x", 0) if isinstance(sub_region, dict) else 0)
    sy = float(sub_region.get("y", 0) if isinstance(sub_region, dict) else 0)
    sw = float(sub_region.get("w", 1) if isinstance(sub_region, dict) else 1)
    sh = float(sub_region.get("h", 1) if isinstance(sub_region, dict) else 1)
    box = {
        "x": clamp(rx + rw * sx),
        "y": clamp(ry + rh * sy),
        "w": clamp(rw * sw),
        "h": clamp(rh * sh),
    }
    left = int(width * box["x"])
    top = int(height * box["y"])
    right = max(left + 1, int(width * min(1.0, box["x"] + box["w"])))
    bottom = max(top + 1, int(height * min(1.0, box["y"] + box["h"])))
    return image.crop((left, top, right, bottom)), box


def preprocess_ocr_crop(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = gray.resize((max(1, gray.width * 3), max(1, gray.height * 3)))
    gray = gray.filter(ImageFilter.SHARPEN)
    arr = np.asarray(gray)
    threshold = max(90, int(arr.mean() + arr.std() * 0.25))
    return gray.point(lambda px: 255 if px >= threshold else 0)


def run_tesseract_text(tesseract: str, image_path: Path, psm: str = "6") -> str:
    cmd = [tesseract, str(image_path), "stdout", "--psm", str(psm)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return ""
    return " ".join(result.stdout.split())


def labels_by_key(labels: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    stats: Dict[str, Dict[str, int]] = {}
    for label in labels:
        key = str(label.get("parameter_key") or "")
        row = stats.setdefault(key, {"labels": 0, "checked": 0, "correct": 0})
        row["labels"] += 1
        if label.get("was_correct") is not None:
            row["checked"] += 1
            if int(label.get("was_correct") or 0):
                row["correct"] += 1
    return stats


def latest_reads_by_key(reads: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for read in reads:
        latest.setdefault(str(read.get("parameter_key") or ""), read)
    return latest


def parameter_status(definition: Dict[str, Any], stats: Dict[str, int], latest_read: Optional[Dict[str, Any]], accuracy: Optional[float]) -> str:
    if not definition.get("enabled"):
        return "disabled"
    if stats.get("labels", 0) < 3:
        return "needs_labels"
    if accuracy is not None and accuracy < 0.65:
        return "needs_rule_tuning"
    if not latest_read:
        return "needs_live_read"
    if float(latest_read.get("confidence") or 0) < 0.5:
        return "low_confidence"
    return "usable"


def parameter_gaps(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    gaps = []
    for row in rows:
        if row["status"] != "usable":
            gaps.append(
                {
                    "parameter_key": row["parameter_key"],
                    "label": row["label"],
                    "status": row["status"],
                    "detail": f"{row.get('label_count', 0)} label(s), accuracy {row.get('accuracy') if row.get('accuracy') is not None else 'n/a'}.",
                }
            )
    return gaps[:8]


def within_range(value: int, config: Dict[str, Any]) -> bool:
    low = config.get("valid_min")
    high = config.get("valid_max")
    if low is not None and value < int(low):
        return False
    if high is not None and value > int(high):
        return False
    return True


def parse_timestamp(value: Any, default: float) -> float:
    try:
        number = float(value)
        return max(0.0, number)
    except (TypeError, ValueError):
        return default


def format_timestamp(value: float) -> str:
    minutes = int(value // 60)
    seconds = int(value % 60)
    return f"{minutes}:{seconds:02d}"


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
