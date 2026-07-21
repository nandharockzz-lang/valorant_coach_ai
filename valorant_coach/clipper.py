import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from .db import Database


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


def extract_death_clips(
    db: Database,
    match_id: int,
    video_path: Path,
    clips_dir: Path,
    pre_seconds: int = 15,
    duration_seconds: int = 20,
) -> Dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return {
            "available": False,
            "created": 0,
            "message": "ffmpeg is not available on PATH; clip extraction was skipped.",
        }

    if not video_path.exists():
        return {"available": True, "created": 0, "message": "video file is missing."}

    clips_dir.mkdir(parents=True, exist_ok=True)
    created = 0
    errors: List[str] = []
    for death in db.get_deaths(match_id):
        timestamp = death.get("timestamp")
        if timestamp is None:
            continue
        start = max(0, float(timestamp) - pre_seconds)
        output = clips_dir / f"match-{match_id}-death-{death['id']}.mp4"
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.2f}",
            "-i",
            str(video_path),
            "-t",
            str(duration_seconds),
            "-c",
            "copy",
            str(output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0 and output.exists():
            db.update_death_clip(death["id"], str(output.resolve()))
            created += 1
        else:
            errors.append(result.stderr.strip() or f"failed to create {output.name}")

    message = f"created {created} clip(s)."
    if errors:
        message += " Some clips failed: " + "; ".join(errors[:3])
    return {"available": True, "created": created, "message": message}
