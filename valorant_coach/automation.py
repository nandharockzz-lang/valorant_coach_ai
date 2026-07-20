import json
import csv
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .advice import generate_advice
from .analyzer import analyze_match, import_video, scan_recording_folder
from .clipper import extract_death_clips
from .coach import build_coach_dashboard
from .db import Database
from .deep_analysis import analyze_hud, analyze_minimap, analyze_ocr
from .reports import build_report, write_markdown_report
from .clipper import ffmpeg_path
from .deep_analysis import tesseract_path
from .vision import (
    analyze_match_events,
    build_keyframe_gallery,
    build_review_queue,
    reconstruct_rounds,
    score_crosshair_match,
    suggest_deaths,
    understand_clip,
)


PLAYBOOKS = {
    "Ascent:Jett": {
        "summary": "Take first contact only with a planned escape or teammate timing.",
        "rules": [
            "Before mid fights, name your escape route before swinging.",
            "Use dash/updraft as a planned reset, not a panic button.",
            "Avoid wide dry exposure into top mid, cat, or lane without trade spacing.",
        ],
        "drills": [
            "Custom route: clear A main, mid, and B main while calling the next angle before it appears.",
            "Deathmatch: after every first bullet burst, reposition before re-peeking.",
        ],
    },
    "Bind:Raze": {
        "summary": "Use utility to clear tight corners before committing satchel movement.",
        "rules": [
            "Boom bot or nade common cubbies before taking space.",
            "Do not satchel into two uncleared angles.",
            "Pair explosive entries with teammate trade timing.",
        ],
        "drills": ["Custom route: clear Hookah, Lamps, and Showers with utility-first routing."],
    },
    "Haven:Omen": {
        "summary": "Win timing through smoke discipline and supported rotations.",
        "rules": [
            "Smoke before crossing exposed links.",
            "Avoid solo late rotations through known contact lanes.",
            "Use paranoia to change the fight before re-challenging.",
        ],
        "drills": ["Review three rounds and pause at first contact to choose anchor, shade, or rotate."],
    },
    "Sunset:Cypher": {
        "summary": "Anchor information patiently and avoid unsupported re-peeks after utility contact.",
        "rules": ["Play off trips/camera instead of dry contact.", "After first reveal, reposition or wait for support."],
        "drills": ["Review defensive deaths and mark whether your utility or body took first contact."],
    },
    "Lotus:Omen": {
        "summary": "Use smoke/paranoia timing to isolate multi-lane pressure before rotating.",
        "rules": ["Smoke the lane that creates the second angle.", "Avoid rotating through broken map control alone."],
        "drills": ["Pause every rotate death and identify the last safe smoke timing."],
    },
    "Icebox:Sage": {
        "summary": "Prioritize trade spacing and wall value before taking vertical fights.",
        "rules": ["Do not heal/wall after overexposing.", "Use wall to create a safer plant or isolate a vertical angle."],
        "drills": ["Review plant-round deaths and write whether wall changed the fight condition."],
    },
}


class JobManager:
    def __init__(self, db: Optional[Database] = None) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[int, Dict[str, Any]] = {}
        self._next_id = 1
        self.db = db
        if self.db:
            for job in self.db.list_jobs(200):
                if job.get("status") in {"queued", "running"}:
                    self.db.update_job(job["id"], status="failed", message="Interrupted by app restart.", error="app restart")

    def start(self, name: str, target: Callable[[Callable[[str, int], None]], Dict[str, Any]]) -> int:
        if self.db:
            job_id = self.db.create_job(name)
            self.db.log("info", "jobs", f"Queued job #{job_id}: {name}")
        else:
            with self._lock:
                job_id = self._next_id
                self._next_id += 1
                self._jobs[job_id] = {
                    "id": job_id,
                    "name": name,
                    "status": "queued",
                    "progress": 0,
                    "message": "Queued.",
                    "result": None,
                    "error": None,
                    "cancel_requested": False,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }

        def update(message: str, progress: int) -> None:
            if self.cancel_requested(job_id):
                raise RuntimeError("Job cancelled.")
            self.update(job_id, status="running", message=message, progress=progress)

        def runner() -> None:
            while self.db and self.running_count() >= int(self.db.get_setting("max_concurrent_jobs", "1") or 1):
                if self.cancel_requested(job_id):
                    self.update(job_id, status="cancelled", message="Cancelled before start.", progress=0)
                    return
                time.sleep(0.5)
            self.update(job_id, status="running", message="Started.", progress=1)
            try:
                result = target(update)
                self.update(job_id, status="complete", message="Complete.", progress=100, result=result)
                if self.db:
                    self.db.log("info", "jobs", f"Completed job #{job_id}: {name}")
            except Exception as exc:
                status = "cancelled" if "cancelled" in str(exc).lower() else "failed"
                self.update(job_id, status=status, message=str(exc), error=str(exc))
                if self.db:
                    self.db.log("error", "jobs", f"Failed job #{job_id}: {exc}", {"job": name})

        threading.Thread(target=runner, daemon=True).start()
        return job_id

    def update(self, job_id: int, **fields: Any) -> None:
        if self.db:
            self.db.update_job(job_id, **fields)
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.update(fields)
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def list(self) -> List[Dict[str, Any]]:
        if self.db:
            return self.db.list_jobs()
        with self._lock:
            return list(sorted((dict(job) for job in self._jobs.values()), key=lambda item: item["id"], reverse=True))

    def cancel(self, job_id: int) -> None:
        if self.db:
            self.db.request_cancel_job(job_id)
            self.db.log("warning", "jobs", f"Cancel requested for job #{job_id}")
            return
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id]["cancel_requested"] = True

    def cancel_requested(self, job_id: int) -> bool:
        if self.db:
            job = self.db.get_job(job_id)
            return bool((job or {}).get("cancel_requested"))
        with self._lock:
            return bool((self._jobs.get(job_id) or {}).get("cancel_requested"))

    def running_count(self) -> int:
        jobs = self.list()
        return sum(1 for job in jobs if job.get("status") == "running")


class RecordingWatcher:
    def __init__(self, jobs: JobManager) -> None:
        self.jobs = jobs
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(
        self,
        db: Database,
        dirs: Dict[str, Path],
        interval_seconds: int = 20,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    if setting_bool(db, "auto_import", False):
                        scan_and_maybe_analyze(db, dirs, self.jobs)
                except Exception as exc:
                    db.set_setting("watcher_last_error", str(exc))
                self._stop.wait(interval_seconds)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        return {"running": bool(self._thread and self._thread.is_alive()) and not self._stop.is_set()}


def setting_bool(db: Database, key: str, default: bool = False) -> bool:
    value = db.get_setting(key, "true" if default else "false")
    return str(value).lower() in {"1", "true", "yes", "on", "enabled"}


def scan_and_maybe_analyze(db: Database, dirs: Dict[str, Path], jobs: JobManager) -> Dict[str, Any]:
    folder = Path(db.get_setting("recording_dir", "") or "")
    videos = scan_recording_folder(folder)
    imported = []
    known = {item["video_path"]: item["id"] for item in db.list_matches()}
    for video in videos:
        resolved = str(video.resolve())
        if resolved in known:
            continue
        if not file_is_stable(video):
            db.log("info", "watcher", f"Skipping active recording until file size is stable: {video}")
            continue
        match_id = import_video(db, video)
        db.log("info", "watcher", f"Imported recording: {video}", {"match_id": match_id})
        imported.append(match_id)
        if setting_bool(db, "auto_analysis", False):
            jobs.start(f"Auto analyze match #{match_id}", lambda update, mid=match_id: run_match_pipeline(db, mid, dirs, update))
    return {"found": len(videos), "imported": imported}


def file_is_stable(path: Path, wait_seconds: float = 1.0) -> bool:
    if not path.exists() or not path.is_file():
        return False
    first = path.stat().st_size
    time.sleep(wait_seconds)
    return path.exists() and path.stat().st_size == first


def run_match_pipeline(
    db: Database,
    match_id: int,
    dirs: Dict[str, Path],
    update: Callable[[str, int], None],
) -> Dict[str, Any]:
    update("Analyzing sidecar/manual metadata.", 5)
    result: Dict[str, Any] = {"match_id": match_id, "steps": []}
    result["steps"].append({"analyze": analyze_match(db, match_id)})

    steps = [
        ("events_v2", 16, lambda: analyze_match_events(db, match_id, dirs["vision"])),
        ("rounds", 26, lambda: reconstruct_rounds(db, match_id, dirs["vision"])),
        ("hud", 38, lambda: analyze_hud(db, match_id, dirs["deep"])),
        ("minimap", 48, lambda: analyze_minimap(db, match_id, dirs["deep"])),
        ("crosshair", 58, lambda: score_crosshair_match(db, match_id, dirs["vision"])),
        ("ocr", 68, lambda: analyze_ocr(db, match_id, dirs["deep"])),
        ("suggest_deaths", 76, lambda: suggest_deaths(db, match_id, dirs["vision"])),
        ("clips", 84, lambda: extract_clips_for_match(db, match_id, dirs["clips"])),
        ("death_batch", 92, lambda: run_death_batch(db, match_id, dirs)),
        ("review_queue", 96, lambda: build_review_queue(db, match_id)),
    ]
    for name, progress, fn in steps:
        analysis_key = {
            "events_v2": "death_events_v2",
            "rounds": "round_timeline",
            "hud": "hud",
            "minimap": "minimap",
            "crosshair": "crosshair",
            "ocr": "ocr",
            "review_queue": "review_queue",
        }.get(name)
        if setting_bool(db, "skip_completed_analysis", True) and analysis_key and db.get_latest_structured_analysis("match", match_id, analysis_key):
            result["steps"].append({name: {"ok": True, "message": "Skipped existing analysis."}})
            continue
        update(f"Running {name}.", progress)
        try:
            result["steps"].append({name: fn()})
        except Exception as exc:
            result["steps"].append({name: {"ok": False, "message": str(exc)}})

    write_markdown_report(db, dirs["reports"], match_id)
    return result


def extract_clips_for_match(db: Database, match_id: int, clips_dir: Path) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    return extract_death_clips(db, match_id, Path(match["video_path"]), clips_dir)


def run_death_batch(db: Database, match_id: int, dirs: Dict[str, Path]) -> Dict[str, Any]:
    deaths = db.get_deaths(match_id)
    outputs = []
    for death in deaths:
        death_id = int(death["id"])
        outputs.append({"death_id": death_id, "keyframes": build_keyframe_gallery(db, death_id, dirs["vision"])})
        outputs.append({"death_id": death_id, "understanding": understand_clip(db, death_id)})
    return {"ok": True, "processed": len(deaths), "outputs": outputs}


def storage_stats(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    return {
        "data": folder_size(paths["data"]),
        "clips": folder_size(paths["clips"]),
        "reports": folder_size(paths["reports"]),
        "vision": folder_size(paths["vision"]),
        "deep": folder_size(paths["deep"]),
        "database": paths["data"].joinpath("coach.sqlite3").stat().st_size if paths["data"].joinpath("coach.sqlite3").exists() else 0,
        "matches": len(db.list_matches()),
    }


def folder_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def cleanup_storage(paths: Dict[str, Path], targets: List[str]) -> Dict[str, Any]:
    allowed = {"clips": paths["clips"], "reports": paths["reports"], "vision": paths["vision"], "deep": paths["deep"]}
    removed = {}
    for name in targets:
        path = allowed.get(name)
        if not path:
            continue
        size = folder_size(path)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        removed[name] = size
    return {"ok": True, "removed": removed}


def apply_retention(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    days = int(db.get_setting("storage_cleanup_days", "30") or 30)
    cutoff = time.time() - (days * 86400)
    removed: Dict[str, int] = {}
    for name in ("clips", "reports", "vision", "deep"):
        root = paths[name]
        count = 0
        if root.exists():
            for item in root.rglob("*"):
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink()
                    count += 1
        removed[name] = count
    db.log("info", "storage", "Applied retention policy.", {"days": days, "removed": removed})
    return {"ok": True, "days": days, "removed": removed}


def backup_database(paths: Dict[str, Path]) -> Dict[str, Any]:
    source = paths["data"] / "coach.sqlite3"
    backup_dir = paths["data"] / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"coach-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    if not source.exists():
        return {"ok": False, "message": "Database file does not exist."}
    shutil.copy2(source, target)
    return {"ok": True, "path": str(target)}


def restore_database(paths: Dict[str, Path], backup_path: str) -> Dict[str, Any]:
    source = Path(backup_path).resolve()
    backup_root = (paths["data"] / "backups").resolve()
    if not str(source).startswith(str(backup_root)) or not source.exists():
        return {"ok": False, "message": "Backup must exist under the data/backups folder."}
    target = paths["data"] / "coach.sqlite3"
    shutil.copy2(source, target)
    return {"ok": True, "message": "Database restored. Restart the app to reload all state."}


def list_backups(paths: Dict[str, Path]) -> Dict[str, Any]:
    backup_dir = paths["data"] / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    items = [
        {"path": str(item), "size": item.stat().st_size, "created_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds")}
        for item in sorted(backup_dir.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    ]
    return {"backups": items}


def export_memory(db: Database) -> Dict[str, Any]:
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "profile": db.get_profile(),
        "coach": build_coach_dashboard(db),
        "trends": db.build_trends(),
        "detector_feedback": db.detector_feedback_summary(),
        "analyses": db.list_structured_analyses(limit=200),
    }


def import_memory(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = payload.get("profile") or {}
    agents = profile.get("main_agents") or []
    if isinstance(agents, str):
        agents = [item.strip() for item in agents.split(",") if item.strip()]
    db.save_profile(
        rank=str(profile.get("rank") or ""),
        main_agents=list(agents),
        target_style=str(profile.get("target_style") or ""),
        notes=str(profile.get("notes") or ""),
    )
    return {"ok": True, "message": "Profile memory imported. Historical analyses are kept read-only in the export file."}


def analytics_dashboard(db: Database) -> Dict[str, Any]:
    trends = db.build_trends()
    analyses = db.list_structured_analyses("match", limit=200)
    crosshair = [
        {"created_at": item["created_at"], "score": (item.get("payload") or {}).get("score")}
        for item in analyses
        if item.get("analysis_type") == "crosshair"
    ]
    detector = db.detector_feedback_summary()
    return {
        "trends": trends,
        "crosshair_scores": [item for item in crosshair if item["score"] is not None],
        "detector": detector,
        "summary": {
            "matches": len(trends.get("matches") or []),
            "top_mistake": next(iter((trends.get("labels") or {}).keys()), ""),
            "maps": trends.get("by_map") or {},
            "agents": trends.get("by_agent") or {},
        },
    }


def tool_status() -> Dict[str, Any]:
    ffmpeg = ffmpeg_path()
    tesseract = tesseract_path()
    pyinstaller = shutil.which("pyinstaller")
    return {
        "ffmpeg": {"available": bool(ffmpeg), "path": ffmpeg, "install": "Install ffmpeg and add ffmpeg.exe to PATH or tools/ffmpeg/bin."},
        "tesseract": {"available": bool(tesseract), "path": tesseract, "install": "Install Tesseract OCR or place tesseract.exe under tools/tesseract."},
        "pyinstaller": {"available": bool(pyinstaller), "path": pyinstaller or "", "install": "Run: python -m pip install pyinstaller"},
    }


def search_deaths(db: Database, query: Dict[str, Any]) -> Dict[str, Any]:
    label = str(query.get("label") or "").lower()
    map_name = str(query.get("map") or "").lower()
    agent = str(query.get("agent") or "").lower()
    phase = str(query.get("phase") or "").lower()
    results = []
    for match in db.list_matches():
        if map_name and map_name not in str(match.get("map") or "").lower():
            continue
        if agent and agent not in str(match.get("agent") or "").lower():
            continue
        report = build_report(db, int(match["id"]))
        for death in report["deaths"]:
            labels = [str(item).lower() for item in death.get("mistake_labels") or []]
            if label and not any(label in item for item in labels):
                continue
            if phase and phase not in str(death.get("round_phase") or "").lower():
                continue
            results.append({"match": report["match"], "death": death})
    return {"results": results, "count": len(results)}


def export_report(db: Database, match_id: int, fmt: str) -> Dict[str, Any]:
    report = build_report(db, match_id)
    if fmt == "json":
        return {"ok": True, "format": "json", "content": json.dumps(report, indent=2, default=str)}
    if fmt == "html":
        deaths = "".join(
            f"<li>R{death.get('round_number') or '?'} @ {death.get('timestamp')}: {', '.join(death.get('mistake_labels') or [])}</li>"
            for death in report["deaths"]
        )
        html = f"""<!doctype html><html><head><meta charset='utf-8'><title>Match {match_id}</title></head>
<body><h1>VALORANT Match Report #{match_id}</h1><p>{report['match'].get('map')} / {report['match'].get('agent')}</p><ul>{deaths}</ul></body></html>"""
        return {"ok": True, "format": "html", "content": html}
    return {"ok": False, "message": "format must be html or json"}


def import_stats(db: Database, path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "message": "stats file does not exist"}
    imported = 0
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("matches", [])
    else:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    for row in rows:
        video_path = str(row.get("video_path") or row.get("vod") or f"manual-stats-{imported + 1}")
        match_id = db.upsert_match(video_path, str(row.get("started_at") or datetime.now().isoformat(timespec="seconds")), "stats_import")
        db.update_match(match_id, map=row.get("map"), agent=row.get("agent"), status="stats_import")
        imported += 1
    return {"ok": True, "imported": imported}


APP_VERSION = "0.8.0-local"


def app_version(db: Database) -> Dict[str, Any]:
    return {
        "version": APP_VERSION,
        "build": "local-dev",
        "schema": db.schema_info(),
        "changelog": [
            "Persistent jobs, logs, backups, retention, exports, and automation.",
            "Advanced search, playbook editing, correction review, privacy audit, provider registry.",
        ],
    }


def provider_registry() -> Dict[str, Any]:
    return {
        "ocr": [{"id": "tesseract", "status": "available-if-installed", "privacy": "local"}],
        "frame_extractors": [{"id": "ffmpeg", "status": "available-if-installed", "privacy": "local"}],
        "playbooks": [{"id": "local-json", "status": "enabled", "privacy": "local"}],
        "analytics": [{"id": "sqlite-local", "status": "enabled", "privacy": "local"}],
        "ai_review": [{"id": "none", "status": "disabled", "privacy": "no upload"}],
    }


def playbooks(db: Optional[Database] = None, match: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(PLAYBOOKS)
    if db:
        merged.update(db.list_playbooks())
    if not match:
        return {"playbooks": merged}
    key = f"{match.get('map') or ''}:{match.get('agent') or ''}"
    return {"key": key, "playbook": merged.get(key) or generic_playbook(match), "playbooks": merged}


def save_playbook(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    key = str(payload.get("key") or "").strip()
    if not key:
        return {"ok": False, "message": "key is required"}
    playbook = {
        "summary": str(payload.get("summary") or ""),
        "rules": normalize_text_list(payload.get("rules") or []),
        "drills": normalize_text_list(payload.get("drills") or []),
    }
    db.save_playbook(key, playbook)
    db.log("info", "playbooks", f"Saved playbook {key}")
    return {"ok": True, "key": key, "playbook": playbook}


def delete_playbook(db: Database, key: str) -> Dict[str, Any]:
    db.delete_playbook(key)
    db.log("warning", "playbooks", f"Deleted playbook {key}")
    return {"ok": True}


def normalize_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        normalized = value.replace(",", "\n")
        return [item.strip() for item in normalized.splitlines() if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def generic_playbook(match: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "summary": f"Generic discipline plan for {match.get('map') or 'unknown map'} / {match.get('agent') or 'unknown agent'}.",
        "rules": [
            "Before first contact, identify trade, utility, or escape.",
            "After contact, change the fight condition before re-peeking.",
            "Use the minimap before rotating through exposed space.",
        ],
        "drills": ["Review five deaths and write whether the issue was angle, timing, utility, or spacing."],
    }


def save_manual_correction(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    subject_type = str(payload.get("subject_type") or "death")
    subject_id = int(payload.get("subject_id") or 0)
    correction_type = str(payload.get("correction_type") or "manual")
    data = payload.get("data") or {}
    result = {
        "kind": "manual_correction",
        "correction_type": correction_type,
        "data": data,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if subject_type == "match":
        correction_id = db.save_structured_analysis(subject_id, f"correction_{correction_type}", result)
    else:
        correction_id = db.save_death_analysis(subject_id, f"correction_{correction_type}", result)
    return {"ok": True, "correction_id": correction_id, "correction": result}


def list_corrections(db: Database) -> Dict[str, Any]:
    analyses = db.list_structured_analyses(limit=200)
    items = [item for item in analyses if str(item.get("analysis_type") or "").startswith("correction_")]
    return {"corrections": items, "count": len(items)}


def apply_correction(db: Database, correction_id: int) -> Dict[str, Any]:
    corrections = list_corrections(db)["corrections"]
    correction = next((item for item in corrections if int(item["id"]) == correction_id), None)
    if not correction:
        return {"ok": False, "message": "correction not found"}
    payload = correction.get("payload") or {}
    data = payload.get("data") or {}
    if correction["subject_type"] == "death" and payload.get("correction_type") == "round_phase":
        db.save_death_analysis(
            int(correction["subject_id"]),
            "applied_round_phase",
            {"kind": "applied_correction", "phase": data.get("phase"), "note": data.get("note")},
        )
    db.log("info", "corrections", f"Applied correction #{correction_id}")
    return {"ok": True, "correction": correction}


def privacy_audit(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    stats = storage_stats(paths, db)
    return {
        "privacy_mode": db.get_setting("privacy_mode", "local-only"),
        "network_uploads": "disabled",
        "local_files": stats,
        "data_categories": [
            "recording paths",
            "death labels",
            "local frame metrics",
            "optional clips/keyframes",
            "coach profile and feedback",
            "logs and job history",
        ],
        "delete_controls": ["storage cleanup", "retention", "manual database backup/delete"],
    }


def advanced_search(db: Database, query: Dict[str, Any]) -> Dict[str, Any]:
    base = search_deaths(db, query)
    text = str(query.get("text") or "").lower()
    confidence_min = float(query.get("confidence_min") or 0)
    clip_state = str(query.get("clip") or "")
    rows = []
    for item in base["results"]:
        death = item["death"]
        haystack = " ".join(
            [
                str(death.get("notes") or ""),
                " ".join(death.get("mistake_labels") or []),
                str(item["match"].get("map") or ""),
                str(item["match"].get("agent") or ""),
            ]
        ).lower()
        if text and text not in haystack:
            continue
        if float(death.get("confidence") or 0) < confidence_min:
            continue
        has_clip = bool(death.get("clip_path"))
        if clip_state == "with_clip" and not has_clip:
            continue
        if clip_state == "without_clip" and has_clip:
            continue
        rows.append(item)
    return {"results": rows, "count": len(rows)}


def evaluation_benchmark(db: Database) -> Dict[str, Any]:
    matches = db.list_matches()
    total_deaths = 0
    total_suggestions = 0
    accepted = 0
    rejected = 0
    pending = 0
    labeled_matches = 0
    per_match = []
    for match in matches:
        match_id = int(match["id"])
        deaths = db.get_deaths(match_id)
        suggestions = db.get_death_suggestions(match_id)
        suggestion_rows = _all_suggestions_for_match(db, match_id)
        if deaths:
            labeled_matches += 1
        total_deaths += len(deaths)
        total_suggestions += len(suggestion_rows)
        accepted += sum(1 for item in suggestion_rows if item.get("status") == "accepted")
        rejected += sum(1 for item in suggestion_rows if item.get("status") == "rejected")
        pending += sum(1 for item in suggestion_rows if item.get("status") == "pending")
        per_match.append(
            {
                "match_id": match_id,
                "map": match.get("map") or "unknown",
                "agent": match.get("agent") or "unknown",
                "deaths": len(deaths),
                "suggestions": len(suggestion_rows),
                "accepted": sum(1 for item in suggestion_rows if item.get("status") == "accepted"),
                "rejected": sum(1 for item in suggestion_rows if item.get("status") == "rejected"),
                "pending": sum(1 for item in suggestion_rows if item.get("status") == "pending"),
            }
        )
    labeled = accepted + rejected
    precision = round(accepted / labeled, 3) if labeled else None
    coverage = round(accepted / total_deaths, 3) if total_deaths else None
    result = {
        "kind": "evaluation_benchmark",
        "summary": benchmark_summary(precision, coverage, total_deaths, total_suggestions),
        "metrics": {
            "matches": len(matches),
            "labeled_matches": labeled_matches,
            "marked_deaths": total_deaths,
            "suggestions": total_suggestions,
            "accepted": accepted,
            "rejected": rejected,
            "pending": pending,
            "precision_proxy": precision,
            "coverage_proxy": coverage,
        },
        "per_match": per_match[:50],
        "notes": [
            "Precision proxy uses accepted/(accepted+rejected) death suggestions.",
            "Coverage proxy uses accepted suggestions divided by manually marked deaths.",
            "For true precision/recall, annotate false negatives in the clip annotation workflow.",
        ],
    }
    db.save_structured_analysis(0, "evaluation_benchmark", result)
    return result


def _all_suggestions_for_match(db: Database, match_id: int) -> List[Dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM death_suggestions WHERE match_id = ? ORDER BY timestamp, id", (match_id,)).fetchall()
    return [dict(row) for row in rows]


def benchmark_summary(precision: Any, coverage: Any, deaths: int, suggestions: int) -> str:
    if precision is None:
        return f"Benchmark needs accepted/rejected suggestions. Current set has {deaths} marked death(s) and {suggestions} suggestion(s)."
    return f"Detector benchmark proxy: precision {precision}, coverage {coverage}; based on {deaths} marked death(s)."


def save_clip_annotation(db: Database, death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    annotation = {
        "kind": "clip_annotation",
        "death_id": death_id,
        "mistake_start": optional_float(payload.get("mistake_start")),
        "first_contact": optional_float(payload.get("first_contact")),
        "death_moment": optional_float(payload.get("death_moment")),
        "better_decision": str(payload.get("better_decision") or "").strip(),
        "notes": str(payload.get("notes") or "").strip(),
        "labels": normalize_text_list(payload.get("labels") or []),
        "created_by": "local-user",
    }
    analysis_id = db.save_death_analysis(death_id, "clip_annotation", annotation)
    db.log("info", "annotations", f"Saved clip annotation #{analysis_id} for death #{death_id}")
    return {"ok": True, "id": analysis_id, "annotation": annotation}


def list_annotations(db: Database) -> Dict[str, Any]:
    items = [
        item for item in db.list_structured_analyses("death", limit=300)
        if item.get("analysis_type") == "clip_annotation"
    ]
    return {"annotations": items, "count": len(items)}


def coach_dashboard_v2(db: Database) -> Dict[str, Any]:
    base = build_coach_dashboard(db)
    trends = db.build_trends()
    analyses = db.list_structured_analyses(limit=300)
    feedback = db.get_feedback_summary()
    weights = weighted_personal_profile(trends, analyses, feedback)
    focus = weights[0]["label"] if weights else "death review discipline"
    skill_scores = skill_ratings(trends, analyses)
    weekly = weekly_focus_plan(focus, weights, skill_scores)
    base["coach_v2"] = {
        "weighted_profile": weights,
        "skill_scores": skill_scores,
        "weekly_focus": weekly,
        "memory_strength": min(100, len(analyses) * 2 + sum((trends.get("labels") or {}).values()) * 5),
    }
    return base


def weighted_personal_profile(trends: Dict[str, Any], analyses: List[Dict[str, Any]], feedback: Dict[str, Any]) -> List[Dict[str, Any]]:
    weights: Dict[str, float] = {}
    for label, count in (trends.get("labels") or {}).items():
        weights[label] = weights.get(label, 0.0) + float(count) * 10.0
    for item in analyses:
        payload = item.get("payload") or {}
        for label in payload.get("suggested_labels") or []:
            weights[label] = weights.get(label, 0.0) + 3.0
        if item.get("analysis_type") == "clip_annotation":
            for label in payload.get("labels") or []:
                weights[label] = weights.get(label, 0.0) + 8.0
    accepted = int(feedback.get("accepted") or 0)
    rejected = int(feedback.get("rejected") or 0)
    scale = 1.0 + min(0.25, accepted * 0.02) - min(0.15, rejected * 0.01)
    rows = [{"label": label, "weight": round(score * scale, 1)} for label, score in weights.items()]
    return sorted(rows, key=lambda item: item["weight"], reverse=True)[:10]


def skill_ratings(trends: Dict[str, Any], analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
    labels = trends.get("labels") or {}
    ratings = {
        "fight_discipline": 80 - labels.get("dry peek", 0) * 8 - labels.get("repeated same-angle fight", 0) * 7,
        "crosshair": 80 - labels.get("crosshair too low/wide", 0) * 10,
        "positioning": 80 - labels.get("exposed to multiple angles", 0) * 8 - labels.get("isolated from team", 0) * 7,
        "utility": 80 - labels.get("utility unused before taking space", 0) * 10,
        "map_awareness": 80 - labels.get("late rotation / bad timing", 0) * 9,
    }
    for item in analyses:
        payload = item.get("payload") or {}
        if item.get("analysis_type") == "crosshair" and payload.get("score") is not None:
            ratings["crosshair"] = round((ratings["crosshair"] + int(payload["score"])) / 2)
        if item.get("analysis_type") == "minimap":
            summary = str(payload.get("summary") or "").lower()
            if "risk" in summary or "late" in summary:
                ratings["map_awareness"] -= 5
    return {key: max(0, min(100, int(value))) for key, value in ratings.items()}


def weekly_focus_plan(focus: str, weights: List[Dict[str, Any]], skill_scores: Dict[str, int]) -> Dict[str, Any]:
    weakest = sorted(skill_scores.items(), key=lambda item: item[1])[0] if skill_scores else ("review", 0)
    return {
        "primary_focus": focus,
        "weakest_skill": weakest[0],
        "target": "Review 10 annotated deaths and reduce the primary focus by one death per match.",
        "drills": [
            f"Before every ranked queue, say the rule for '{focus}' out loud once.",
            "Annotate mistake start, first contact, death moment, and better decision for 5 deaths.",
            f"Run one custom/deathmatch block aimed at {weakest[0].replace('_', ' ')}.",
        ],
        "top_weights": weights[:3],
    }


def reconstruct_round_story(db: Database, match_id: int) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        return {"ok": False, "message": "match not found"}
    rounds = db.get_rounds(match_id)
    deaths = db.get_deaths(match_id)
    if not rounds and deaths:
        rounds = infer_rounds_from_deaths(deaths)
    stories = []
    for index, round_row in enumerate(rounds or [{"round_number": 1, "start_ts": 0, "end_ts": None}], start=1):
        start = float(round_row.get("start_ts") or 0)
        end_value = round_row.get("end_ts")
        end = float(end_value) if end_value is not None else None
        round_deaths = [death for death in deaths if death.get("timestamp") is not None and death_in_round(float(death["timestamp"]), start, end)]
        events = []
        for death in round_deaths:
            elapsed = float(death["timestamp"]) - start
            events.append(
                {
                    "type": "death",
                    "timestamp": death.get("timestamp"),
                    "phase": phase_from_elapsed(elapsed),
                    "labels": death.get("mistake_labels") or [],
                    "read": story_read_for_death(death),
                }
            )
        stories.append(
            {
                "round_number": round_row.get("round_number") or index,
                "start_ts": start,
                "end_ts": end,
                "side": round_row.get("side") or "unknown",
                "outcome": round_row.get("outcome") or "unknown",
                "events": events,
                "summary": round_story_summary(events),
            }
        )
    result = {
        "kind": "round_story_v2",
        "summary": f"Built story reconstruction for {len(stories)} round(s).",
        "rounds": stories,
        "confidence": 0.45 if rounds else 0.25,
    }
    db.save_structured_analysis(match_id, "round_story_v2", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def infer_rounds_from_deaths(deaths: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rounds = []
    seen = sorted({int(death.get("round_number") or 1) for death in deaths})
    for round_number in seen:
        timestamps = [float(death["timestamp"]) for death in deaths if int(death.get("round_number") or 1) == round_number and death.get("timestamp") is not None]
        start = max(0.0, min(timestamps) - 65) if timestamps else 0.0
        end = max(timestamps) + 20 if timestamps else None
        rounds.append({"round_number": round_number, "start_ts": start, "end_ts": end, "outcome": "", "side": ""})
    return rounds


def death_in_round(ts: float, start: float, end: Any) -> bool:
    if end is None:
        return ts >= start
    return start <= ts <= float(end)


def phase_from_elapsed(elapsed: float) -> str:
    if elapsed < 25:
        return "setup / first contact"
    if elapsed < 65:
        return "mid-round pressure"
    return "late-round conversion"


def story_read_for_death(death: Dict[str, Any]) -> str:
    labels = death.get("mistake_labels") or []
    if "dry peek" in labels:
        return "Death likely came from taking contact without enough utility, trade timing, or info."
    if "late rotation / bad timing" in labels:
        return "Death likely came during a timing or rotation decision."
    if "crosshair too low/wide" in labels:
        return "Death likely included a mechanical readiness issue before first damage."
    return death.get("notes") or "Marked death needs manual story annotation."


def round_story_summary(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "No marked death events in this round."
    labels = []
    for event in events:
        labels.extend(event.get("labels") or [])
    top = labels[0] if labels else "unlabeled death"
    return f"{len(events)} marked death event(s); first pattern: {top}."


def local_ai_status(db: Database) -> Dict[str, Any]:
    command = str(db.get_setting("local_ai_command", "") or "").strip()
    enabled = bool(command)
    return {
        "enabled": enabled,
        "privacy": "local process only; no network upload by this app",
        "command": command,
        "status": "configured" if enabled else "disabled",
        "expected_protocol": "stdin JSON, stdout JSON with summary, labels, better_play, confidence",
    }


def save_local_ai_config(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    command = str(payload.get("command") or "").strip()
    db.set_setting("local_ai_command", command)
    db.log("info", "local-ai", "Updated local AI command configuration", {"configured": bool(command)})
    return {"ok": True, "local_ai": local_ai_status(db)}


def run_local_ai_review(db: Database, death_id: int) -> Dict[str, Any]:
    status = local_ai_status(db)
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found", "status": status}
    if not status["enabled"]:
        result = {
            "kind": "local_ai_review",
            "summary": "Local AI review is disabled. Configure a local command before running model-based clip review.",
            "labels": [],
            "better_play": "",
            "confidence": 0,
            "status": "disabled",
        }
        db.save_death_analysis(death_id, "local_ai_review", result)
        return {"ok": True, "message": result["summary"], "analysis": result, "status": status}
    request = {
        "death": death,
        "annotations": death.get("annotations") or [],
        "clip_path": death.get("clip_path"),
        "privacy": "local-only",
    }
    try:
        completed = subprocess.run(
            status["command"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            shell=True,
            timeout=90,
        )
    except Exception as exc:
        return {"ok": False, "message": f"Local AI command failed: {exc}", "status": status}
    if completed.returncode != 0:
        return {"ok": False, "message": completed.stderr.strip() or "Local AI command returned an error.", "status": status}
    try:
        model_payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        model_payload = {"summary": completed.stdout.strip()}
    result = {
        "kind": "local_ai_review",
        "summary": str(model_payload.get("summary") or "Local AI review completed."),
        "labels": normalize_text_list(model_payload.get("labels") or model_payload.get("suggested_labels") or []),
        "better_play": str(model_payload.get("better_play") or ""),
        "confidence": float(model_payload.get("confidence") or 0.5),
        "status": "completed",
        "provider": "local-command",
    }
    db.save_death_analysis(death_id, "local_ai_review", result)
    return {"ok": True, "message": result["summary"], "analysis": result, "status": status}


def installer_diagnostics(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    checks = []
    for name, path in paths.items():
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except Exception:
            writable = False
        checks.append({"name": f"{name}_writable", "ok": writable, "path": str(path)})
    tools = tool_status()
    checks.extend({"name": f"tool_{name}", "ok": bool(item.get("available")), "detail": item} for name, item in tools.items())
    checks.append({"name": "python_version", "ok": True, "detail": platform.python_version()})
    checks.append({"name": "schema", "ok": True, "detail": db.schema_info()})
    result = {
        "ok": all(item["ok"] for item in checks if not item["name"].startswith("tool_")),
        "summary": "Core app directories and schema are usable." if checks else "No checks ran.",
        "platform": platform.platform(),
        "checks": checks,
    }
    db.save_structured_analysis(0, "installer_diagnostics", result)
    return result


def smart_review_queue_v2(db: Database, match_id: int) -> Dict[str, Any]:
    from .vision import build_review_queue as base_review_queue

    base = base_review_queue(db, match_id)
    items = ((base.get("analysis") or {}).get("items") or [])
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        reason = str(item.get("reason") or item.get("kind") or "review")
        key = reason.split(",")[0].strip() or item.get("kind") or "review"
        grouped.setdefault(
            key,
            {"theme": key, "priority": 0, "count": 0, "items": [], "coach_read": ""},
        )
        grouped[key]["priority"] = max(grouped[key]["priority"], int(item.get("priority") or 0))
        grouped[key]["count"] += 1
        grouped[key]["items"].append(item)
    groups = sorted(grouped.values(), key=lambda row: (row["priority"], row["count"]), reverse=True)
    for group in groups:
        group["coach_read"] = f"Review this cluster first if it repeats: {group['theme']} ({group['count']} item(s))."
    result = {
        "kind": "review_queue_v2",
        "summary": f"Grouped {len(items)} review item(s) into {len(groups)} coaching cluster(s).",
        "groups": groups[:8],
        "top_items": items[:5],
        "confidence": 0.55 if groups else 0.0,
    }
    db.save_structured_analysis(match_id, "review_queue_v2", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def privacy_inventory(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    stats = storage_stats(paths, db)
    analyses = db.list_structured_analyses(limit=500)
    return {
        "privacy_mode": db.get_setting("privacy_mode", "local-only"),
        "network_uploads": "disabled by core app",
        "storage": stats,
        "database": {
            "path": str(db.path),
            "exists": db.path.exists(),
            "bytes": db.path.stat().st_size if db.path.exists() else 0,
        },
        "structured_records": len(analyses),
        "categories": {
            "clips": str(paths["clips"]),
            "reports": str(paths["reports"]),
            "vision_frames": str(paths["vision"]),
            "deep_frames": str(paths["deep"]),
            "sqlite": str(db.path),
        },
    }


def privacy_export(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    export_dir = paths["data"] / "privacy_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"privacy-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "inventory": privacy_inventory(paths, db),
        "memory": export_memory(db),
        "analytics": analytics_dashboard(db),
        "logs": db.list_logs(500),
        "analyses": db.list_structured_analyses(limit=1000),
    }
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "path": str(target), "message": "Privacy export written locally."}


def privacy_wipe(paths: Dict[str, Path], db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    targets = payload.get("targets") or []
    if isinstance(targets, str):
        targets = [targets]
    allowed = {"clips", "vision", "deep", "reports"}
    selected = [target for target in targets if target in allowed]
    cleanup = cleanup_storage(paths, selected)
    db.log("warning", "privacy", "Privacy wipe completed", {"targets": selected})
    return {"ok": True, "targets": selected, "cleanup": cleanup}


def plugin_registry(db: Database) -> Dict[str, Any]:
    return {
        "plugins": [
            {
                "id": "local-ai-command",
                "name": "Local AI Command",
                "enabled": bool(db.get_setting("local_ai_command", "")),
                "privacy": "local",
                "config_key": "local_ai_command",
            },
            {
                "id": "tesseract-ocr",
                "name": "Tesseract OCR",
                "enabled": bool(tesseract_path()),
                "privacy": "local",
            },
            {
                "id": "ffmpeg-frame-extractor",
                "name": "ffmpeg Frame Extractor",
                "enabled": bool(ffmpeg_path()),
                "privacy": "local",
            },
            {
                "id": "local-playbooks",
                "name": "Editable Local Playbooks",
                "enabled": True,
                "privacy": "local",
            },
        ]
    }


def optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)
