import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db import Database


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
MAP_NAMES = [
    "abyss",
    "ascent",
    "bind",
    "breeze",
    "fracture",
    "haven",
    "icebox",
    "lotus",
    "pearl",
    "split",
    "sunset",
]
AGENT_NAMES = [
    "astra",
    "breach",
    "brimstone",
    "chamber",
    "clove",
    "cypher",
    "deadlock",
    "fade",
    "gekko",
    "harbor",
    "iso",
    "jett",
    "kayo",
    "killjoy",
    "neon",
    "omen",
    "phoenix",
    "raze",
    "reyna",
    "sage",
    "skye",
    "sova",
    "viper",
    "vyse",
    "yoru",
]


def infer_name_token(video_path: Path, candidates: List[str]) -> Optional[str]:
    name = re.sub(r"[_\-]+", " ", video_path.stem.lower())
    for candidate in candidates:
        if re.search(rf"\b{re.escape(candidate)}\b", name):
            return candidate.title() if candidate != "kayo" else "KAY/O"
    return None


def sidecar_path(video_path: Path) -> Path:
    return video_path.with_suffix(".events.json")


def scan_recording_folder(folder: Path) -> List[Path]:
    if not folder.exists() or not folder.is_dir():
        return []
    videos: List[Path] = []
    for root, _, files in os.walk(folder):
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(path)
    return sorted(videos, key=lambda item: item.stat().st_mtime, reverse=True)


def import_video(db: Database, video_path: Path) -> int:
    video_path = video_path.resolve()
    stat = video_path.stat()
    started_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    return db.upsert_match(str(video_path), started_at, "queued")


def analyze_match(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")

    video_path = Path(match["video_path"])
    db.update_match(match_id, status="analyzing")

    detected_map = infer_name_token(video_path, MAP_NAMES)
    detected_agent = infer_name_token(video_path, AGENT_NAMES)
    rounds: List[Dict[str, Any]] = []
    deaths: List[Dict[str, Any]] = []
    status = "needs_review"

    sidecar = sidecar_path(video_path)
    if sidecar.exists():
        with sidecar.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        detected_map = payload.get("map") or detected_map
        detected_agent = payload.get("agent") or detected_agent
        rounds = normalize_rounds(payload.get("rounds") or [])
        deaths = normalize_deaths(payload.get("deaths") or [])
        status = "analyzed"

    if not rounds:
        rounds = []
    if not deaths:
        deaths = [
            {
                "round_number": None,
                "timestamp": None,
                "labels": ["needs manual review"],
                "confidence": 0,
                "notes": "No event sidecar was found. Add an .events.json file or mark deaths manually after watching the VOD.",
            }
        ]

    db.replace_rounds(match_id, rounds)
    db.replace_deaths(match_id, deaths)
    db.update_match(
        match_id,
        map=detected_map,
        agent=detected_agent,
        duration=None,
        status=status,
    )
    return {"match_id": match_id, "status": status, "rounds": len(rounds), "deaths": len(deaths)}


def normalize_rounds(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for index, item in enumerate(items, start=1):
        normalized.append(
            {
                "round_number": item.get("round_number") or index,
                "start_ts": item.get("start_ts"),
                "end_ts": item.get("end_ts"),
                "outcome": item.get("outcome"),
                "side": item.get("side"),
            }
        )
    return normalized


def normalize_deaths(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for item in items:
        labels = item.get("labels") or item.get("mistake_labels") or []
        if isinstance(labels, str):
            labels = [labels]
        normalized.append(
            {
                "round_number": item.get("round_number"),
                "timestamp": item.get("timestamp"),
                "clip_path": item.get("clip_path"),
                "labels": [str(label).strip().lower() for label in labels if str(label).strip()],
                "confidence": float(item.get("confidence") or 0),
                "notes": item.get("notes") or "",
            }
        )
    return normalized

