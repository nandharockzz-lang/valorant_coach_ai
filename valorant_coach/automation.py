import json
import csv
import base64
import math
import platform
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import request as urlrequest
from urllib.error import URLError

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from .advice import generate_advice
from .analyzer import analyze_match, import_video, scan_recording_folder
from .clipper import extract_death_clips
from .coach import build_coach_dashboard, build_guided_match_coach
from .db import Database
from .knowledge import build_knowledge_prompt_context, build_vocabulary_pack, vocabulary_key
from .deep_analysis import analyze_hud, analyze_minimap, analyze_ocr, infer_rounds_from_scoreboard, run_tesseract, tesseract_path
from .memory import build_memory_prompt_context, load_coach_memory_state, save_coach_memory_state, update_coach_memory_from_review
from .reports import build_report, write_markdown_report
from .clipper import ffmpeg_path
from .vision import (
    analyze_match_events,
    build_keyframe_gallery,
    build_local_ai_review_sequence,
    build_review_queue,
    compute_metrics,
    crop_region,
    frame_motion,
    load_frame,
    local_ai_sequence_profile,
    reconstruct_rounds,
    score_crosshair_match,
    scan_full_vod_coach_moments,
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
        ("scoreboard_rounds", 72, lambda: infer_rounds_from_scoreboard(db, match_id, dirs["deep"])),
        ("suggest_deaths", 76, lambda: suggest_deaths(db, match_id, dirs["vision"], scaled_job_update(update, 72, 82))),
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
            "scoreboard_rounds": "scoreboard_rounds",
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


def scaled_job_update(update: Callable[[str, int], None], start: int, end: int) -> Callable[[str, int], None]:
    def inner(message: str, progress: int) -> None:
        value = start + int((max(0, min(100, int(progress))) / 100) * (end - start))
        update(message, value)

    return inner


def run_suggest_deaths_job(
    db: Database,
    match_id: int,
    dirs: Dict[str, Path],
    update: Callable[[str, int], None],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    update("Find Deaths: queued.", 1)
    result = suggest_deaths(db, match_id, dirs["vision"], update, options=options)
    update("Find Deaths: refreshing match report.", 96)
    write_markdown_report(db, dirs["reports"], match_id)
    return result


def run_auto_coach_pipeline(
    db: Database,
    match_id: int,
    dirs: Dict[str, Path],
    update: Callable[[str, int], None],
) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")

    result: Dict[str, Any] = {"match_id": match_id, "steps": [], "promoted_deaths": 0, "pending_suggestions": 0}
    update("Auto Coach: reading match metadata.", 3)
    existing_deaths = db.get_deaths(match_id)
    if existing_deaths:
        result["steps"].append(
            {
                "metadata": {
                    "ok": True,
                    "message": f"Kept {len(existing_deaths)} existing death marker(s); metadata seeding was skipped to avoid overwriting review work.",
                }
            }
        )
    else:
        result["steps"].append({"metadata": analyze_match(db, match_id)})

    steps = [
        ("events_v2", "detecting combat and death UI moments", 12, lambda: analyze_match_events(db, match_id, dirs["vision"])),
        ("rounds", "reconstructing round timeline", 20, lambda: reconstruct_rounds(db, match_id, dirs["vision"])),
        ("hud", "extracting HUD regions", 29, lambda: analyze_hud(db, match_id, dirs["deep"])),
        ("minimap", "reading minimap activity", 38, lambda: analyze_minimap(db, match_id, dirs["deep"])),
        ("crosshair", "scoring crosshair placement", 47, lambda: score_crosshair_match(db, match_id, dirs["vision"])),
        ("ocr", "running local OCR on calibrated HUD regions", 55, lambda: analyze_ocr(db, match_id, dirs["deep"])),
        ("scoreboard_rounds", "reading top scoreboard scores for round numbers", 59, lambda: infer_rounds_from_scoreboard(db, match_id, dirs["deep"])),
        ("suggest_deaths", "finding likely deaths from video signals", 64, lambda: suggest_deaths(db, match_id, dirs["vision"], scaled_job_update(update, 60, 69))),
    ]
    for name, message, progress, fn in steps:
        update(f"Auto Coach: {message}.", progress)
        result["steps"].append({name: safe_pipeline_call(fn)})

    update("Auto Coach: preserving confirmed markers and preparing suggestions for review.", 70)
    promotion = promote_confident_death_suggestions(db, match_id)
    result["promotion"] = promotion
    result["promoted_deaths"] = promotion["promoted"]
    result["pending_suggestions"] = promotion["pending"]

    update("Auto Coach: extracting death clips.", 76)
    result["steps"].append({"clips": safe_pipeline_call(lambda: extract_clips_for_match(db, match_id, dirs["clips"]))})

    update("Auto Coach: selecting keyframes and understanding clips.", 83)
    result["steps"].append({"death_batch": safe_pipeline_call(lambda: run_death_batch(db, match_id, dirs))})

    update("Auto Coach: generating personal advice for marked deaths.", 90)
    result["advice"] = generate_missing_death_advice(db, match_id)

    update("Auto Coach: building review order and match plan.", 95)
    result["guided_coach"] = safe_pipeline_call(lambda: build_guided_match_coach(db, match_id))
    result["review_queue"] = safe_pipeline_call(lambda: build_review_queue(db, match_id))
    result["review_queue_v2"] = safe_pipeline_call(lambda: smart_review_queue_v2(db, match_id))
    result["round_story"] = safe_pipeline_call(lambda: reconstruct_round_story(db, match_id))

    update("Auto Coach: writing report and saving summary.", 98)
    report_path = write_markdown_report(db, dirs["reports"], match_id)
    summary = build_auto_coach_summary(db, match_id, result, report_path)
    db.save_structured_analysis(match_id, "auto_coach_summary", summary)
    result["summary"] = summary
    return result


def run_full_vod_coach_pipeline(
    db: Database,
    match_id: int,
    dirs: Dict[str, Path],
    update: Callable[[str, int], None],
) -> Dict[str, Any]:
    match = db.get_match(match_id)
    if not match:
        raise ValueError(f"Unknown match id: {match_id}")
    result: Dict[str, Any] = {"match_id": match_id, "steps": []}

    update("Full VOD Coach: scanning the entire video for coachable moments.", 12)
    scan = scan_full_vod_coach_moments(db, match_id, dirs["vision"])
    result["steps"].append({"full_vod_scan": scan})
    analysis = scan.get("analysis") or {}
    moments = analysis.get("moments") or []

    update("Full VOD Coach: scoring VALORANT-specific focus areas.", 35)
    ranked = rank_full_vod_moments(db, match_id, moments)
    result["ranked"] = ranked

    update("Full VOD Coach: checking local vision model configuration.", 48)
    local_status = local_ai_status(db)
    result["local_ai"] = {
        "enabled": local_status["enabled"],
        "provider": local_status["provider"],
        "model": local_status["model"],
        "base_url": local_status["base_url"],
    }

    reviewed = []
    if local_status["enabled"]:
        top = ranked.get("moments", [])[: int(db.get_setting("full_vod_ai_review_limit", "5") or 5)]
        for index, moment in enumerate(top, start=1):
            progress = 48 + int((index / max(1, len(top))) * 34)
            update(f"Full VOD Coach: asking local vision model about moment {index}/{len(top)}.", progress)
            reviewed.append(run_local_ai_moment_review(db, match_id, moment, local_status))
    else:
        result["local_ai"]["message"] = "Local AI is disabled. Configure LM Studio in Advanced: Automation And Tools for visual explanations."

    update("Full VOD Coach: building personal coach memory.", 88)
    final = build_full_vod_coach_report(db, match_id, ranked, reviewed, local_status)
    final["id"] = db.save_structured_analysis(match_id, "full_vod_coach", final)
    result["full_vod_coach"] = final

    update("Full VOD Coach: refreshing review queue and report.", 96)
    result["review_queue"] = safe_pipeline_call(lambda: build_review_queue(db, match_id))
    write_markdown_report(db, dirs["reports"], match_id)
    return result


def rank_full_vod_moments(db: Database, match_id: int, moments: List[Dict[str, Any]]) -> Dict[str, Any]:
    trends = db.build_trends()
    personal_weights = trends.get("labels") or {}
    feedback = coach_moment_feedback_summary(db)
    weight_map = {
        "crosshair_turn_drift": "crosshair too low/wide",
        "panic_correction_under_pressure": "dry peek",
        "minimap_pressure_missed": "late rotation / bad timing",
        "poor_reset_after_contact": "poor reposition after contact",
    }
    ranked = []
    for moment in moments:
        label = str(moment.get("label") or "")
        personal_label = weight_map.get(label, label)
        personal_boost = min(12, int(personal_weights.get(personal_label, 0)) * 3)
        label_feedback = feedback.get("by_label", {}).get(label, {})
        feedback_boost = int(label_feedback.get("accepted", 0)) * 3 - int(label_feedback.get("rejected", 0)) * 4
        priority = min(100, max(1, int(moment.get("priority") or 0) + personal_boost + feedback_boost))
        item = dict(moment)
        item["moment_id"] = moment_key(moment)
        item["personal_label"] = personal_label
        item["personal_boost"] = personal_boost
        item["feedback_boost"] = feedback_boost
        item["priority"] = priority
        ranked.append(item)
    ranked = sorted(ranked, key=lambda row: row["priority"], reverse=True)
    focus = {}
    for item in ranked:
        label = str(item.get("personal_label") or item.get("label") or "review")
        focus.setdefault(label, {"label": label, "count": 0, "max_priority": 0})
        focus[label]["count"] += 1
        focus[label]["max_priority"] = max(focus[label]["max_priority"], int(item.get("priority") or 0))
    return {
        "moments": ranked,
        "focus": sorted(focus.values(), key=lambda row: (row["count"], row["max_priority"]), reverse=True),
        "personal_memory": personal_weights,
        "feedback": feedback,
    }


def moment_key(moment: Dict[str, Any]) -> str:
    label = str(moment.get("label") or "moment").replace(" ", "_")
    timestamp = float(moment.get("timestamp") or 0)
    return f"{label}-{timestamp:.1f}"


def coach_moment_feedback_summary(db: Database) -> Dict[str, Any]:
    rows = [
        item for item in db.list_structured_analyses("match", limit=1000)
        if item.get("analysis_type") == "coach_moment_feedback"
    ]
    by_label: Dict[str, Dict[str, int]] = {}
    by_moment: Dict[str, Dict[str, Any]] = {}
    counts = {"accepted": 0, "rejected": 0}
    for row in rows:
        payload = row.get("payload") or {}
        verdict = str(payload.get("verdict") or "")
        if verdict not in counts:
            continue
        label = str(payload.get("label") or "unknown")
        moment_id = str(payload.get("moment_id") or "")
        by_label.setdefault(label, {"accepted": 0, "rejected": 0})
        by_label[label][verdict] += 1
        counts[verdict] += 1
        if moment_id:
            by_moment[moment_id] = payload
    return {"counts": counts, "by_label": by_label, "by_moment": by_moment}


def save_coach_moment_feedback(db: Database, match_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    verdict = str(payload.get("verdict") or "").strip()
    if verdict not in {"accepted", "rejected"}:
        return {"ok": False, "message": "verdict must be accepted or rejected"}
    moment = {
        "kind": "coach_moment_feedback",
        "match_id": match_id,
        "moment_id": str(payload.get("moment_id") or "").strip(),
        "timestamp": optional_float(payload.get("timestamp")),
        "label": str(payload.get("label") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "verdict": verdict,
        "note": str(payload.get("note") or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not moment["moment_id"]:
        moment["moment_id"] = moment_key(moment)
    analysis_id = db.save_structured_analysis(match_id, "coach_moment_feedback", moment)
    db.log("info", "coach-moment", f"Saved coach moment feedback #{analysis_id}", {"match_id": match_id, "verdict": verdict})
    return {"ok": True, "id": analysis_id, "feedback": moment, "summary": coach_moment_feedback_summary(db)}


def run_local_ai_moment_review(
    db: Database,
    match_id: int,
    moment: Dict[str, Any],
    status: Dict[str, Any],
) -> Dict[str, Any]:
    images = []
    for path_text in (moment.get("context_frame_paths") or [moment.get("frame_path")])[:3]:
        path = Path(str(path_text or ""))
        if path.exists():
            images.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    prompt = render_moment_prompt(db, match_id, moment)
    audit = redact_model_request({"keyframes": [{"image_base64": image} for image in images], "prompt": prompt}, status)
    audit["kind"] = "local_model_moment_audit"
    audit["match_id"] = match_id
    audit["timestamp"] = moment.get("timestamp")
    db.save_structured_analysis(match_id, "local_model_moment_audit", audit)
    if status["provider"] == "ollama":
        endpoint = status["base_url"].rstrip("/") + "/api/generate"
        body: Dict[str, Any] = {"model": status["model"], "prompt": local_model_system_prompt(status) + "\n\n" + prompt, "stream": False}
        if images:
            body["images"] = images
    else:
        endpoint = status["base_url"].rstrip("/") + "/chat/completions"
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}} for image in images)
        body = {
            "model": status["model"],
            "messages": [
                {"role": "system", "content": local_model_system_prompt(status)},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": 700,
        }
    try:
        response = post_json(endpoint, body, timeout=240)
        text = response.get("response") or (((response.get("choices") or [{}])[0].get("message") or {}).get("content")) or json.dumps(response)
        review = parse_moment_review(text, status["provider"], moment)
    except Exception as exc:
        review = {
            "kind": "full_vod_local_ai_review",
            "status": "failed",
            "provider": status["provider"],
            "timestamp": moment.get("timestamp"),
            "summary": f"Local model review failed: {exc}",
            "labels": [moment.get("personal_label") or moment.get("label")],
            "better_play": moment.get("better_play") or "",
            "confidence": 0.0,
        }
    db.save_structured_analysis(match_id, "full_vod_moment_ai_review", review)
    return review


def render_moment_prompt(db: Database, match_id: int, moment: Dict[str, Any]) -> str:
    match = db.get_match(match_id) or {}
    profile = db.get_profile()
    return (
        "You are a VALORANT VOD coach reviewing local keyframes from one timestamp. "
        "Return strict JSON with summary, visible_evidence, labels, better_play, drill, confidence. "
        f"Map: {match.get('map') or 'unknown'}. Agent: {match.get('agent') or 'unknown'}. "
        f"Player rank: {profile.get('rank') or 'unknown'}. Target style: {profile.get('target_style') or 'unknown'}. "
        f"Timestamp seconds: {moment.get('timestamp')}. Detector label: {moment.get('label')}. "
        f"Detector reason: {moment.get('reason')}. Suggested better play: {moment.get('better_play')}. "
        f"Metrics: {json.dumps(moment.get('metrics') or {})}. "
        "Focus on only what is visible in the provided frames: crosshair placement during turns, angle clearing, movement before contact, minimap timing, and utility/trade discipline. "
        "Do not invent hidden enemy positions, comms, or prior round context. If the frames are insufficient, say so and keep confidence below 0.45."
    )


def parse_moment_review(text: str, provider: str, moment: Dict[str, Any]) -> Dict[str, Any]:
    text = strip_json_fence(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"summary": text}
    return {
        "kind": "full_vod_local_ai_review",
        "status": "completed",
        "provider": provider,
        "timestamp": moment.get("timestamp"),
        "summary": str(parsed.get("summary") or parsed.get("what_happened") or text[:500]),
        "labels": normalize_text_list(parsed.get("labels") or parsed.get("suggested_labels") or [moment.get("personal_label") or moment.get("label")]),
        "better_play": str(parsed.get("better_play") or parsed.get("recommendation") or moment.get("better_play") or ""),
        "drill": str(parsed.get("drill") or ""),
        "confidence": float(parsed.get("confidence") or moment.get("confidence") or 0.55),
    }


def build_full_vod_coach_report(
    db: Database,
    match_id: int,
    ranked: Dict[str, Any],
    reviewed: List[Dict[str, Any]],
    local_status: Dict[str, Any],
) -> Dict[str, Any]:
    moments = ranked.get("moments") or []
    focus = ranked.get("focus") or []
    reviews_by_ts = {round(float(item.get("timestamp") or 0), 2): item for item in reviewed}
    feedback_by_moment = coach_moment_feedback_summary(db).get("by_moment", {})
    enriched = []
    for moment in moments[:18]:
        review = reviews_by_ts.get(round(float(moment.get("timestamp") or 0), 2))
        item = dict(moment)
        item["moment_id"] = item.get("moment_id") or moment_key(item)
        if item["moment_id"] in feedback_by_moment:
            item["feedback"] = feedback_by_moment[item["moment_id"]]
        if review and review.get("status") == "completed":
            item["ai_review"] = review
            item["reason"] = review.get("summary") or item.get("reason")
            item["better_play"] = review.get("better_play") or item.get("better_play")
        enriched.append(item)
    top_focus = focus[0]["label"] if focus else "full VOD review"
    result = {
        "kind": "full_vod_coach",
        "match_id": match_id,
        "summary": f"Full VOD Coach found {len(moments)} ranked moment(s). Main focus: {top_focus}.",
        "moments": enriched,
        "focus": focus,
        "local_ai": {
            "enabled": local_status.get("enabled"),
            "provider": local_status.get("provider"),
            "model": local_status.get("model"),
            "reviewed_moments": len(reviewed),
        },
        "feedback_summary": coach_moment_feedback_summary(db),
        "next_action": "Review the first three moment markers before death review; they are likely mechanics or decision problems that happened before obvious death events.",
        "confidence": round(min(0.85, 0.35 + len(moments) * 0.03 + len(reviewed) * 0.04), 2) if moments else 0.0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    return result


def safe_pipeline_call(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def promote_confident_death_suggestions(
    db: Database,
    match_id: int,
    threshold: float = 0.90,
    duplicate_window_seconds: float = 7.0,
) -> Dict[str, Any]:
    deaths = db.get_deaths(match_id)
    suggestions = db.list_death_suggestions(match_id, "pending")

    skipped = [
        {
            "suggestion_id": suggestion["id"],
            "timestamp": float(suggestion.get("timestamp") or 0),
            "confidence": float(suggestion.get("confidence") or 0),
            "reason": "left for user review",
        }
        for suggestion in suggestions
    ]
    return {
        "ok": True,
        "promoted": 0,
        "pending": len(suggestions),
        "threshold": threshold,
        "promoted_items": [],
        "skipped": skipped,
        "message": (
            f"Kept {len(deaths)} confirmed death marker(s) unchanged; "
            f"{len(suggestions)} candidate(s) remain pending for manual review."
        ),
    }


def round_for_timestamp(rounds: List[Dict[str, Any]], timestamp: float) -> Optional[int]:
    for item in rounds:
        start = item.get("start_ts")
        end = item.get("end_ts")
        if start is None:
            continue
        if float(start) <= timestamp and (end is None or timestamp <= float(end)):
            return int(item.get("round_number") or 0) or None
    return None


def generate_missing_death_advice(db: Database, match_id: int, limit: int = 12) -> Dict[str, Any]:
    generated = []
    skipped = 0
    for death in db.get_deaths(match_id)[:limit]:
        if death.get("advice"):
            skipped += 1
            continue
        try:
            generated.append(generate_advice(db, int(death["id"])))
        except Exception as exc:
            generated.append({"death_id": death["id"], "ok": False, "message": str(exc)})
    return {"ok": True, "generated": len(generated), "skipped": skipped, "items": generated}


def build_auto_coach_summary(db: Database, match_id: int, result: Dict[str, Any], report_path: Path) -> Dict[str, Any]:
    deaths = db.get_deaths(match_id)
    suggestions = db.get_death_suggestions(match_id)
    guided = db.get_latest_structured_analysis("match", match_id, "guided_coach")
    review_order = ((guided or {}).get("payload") or {}).get("review_order") or []
    return {
        "kind": "auto_coach_summary",
        "match_id": match_id,
        "summary": (
            f"Auto Coach marked {len(deaths)} death(s), promoted {result.get('promoted_deaths', 0)} candidate(s), "
            f"and left {len(suggestions)} uncertain candidate(s) for review."
        ),
        "deaths": len(deaths),
        "pending_suggestions": len(suggestions),
        "advice_generated": (result.get("advice") or {}).get("generated", 0),
        "review_items": len(review_order),
        "report_path": str(report_path),
        "next_action": "Open Review, use the timeline markers, and verify pending suggestions so the detector learns your footage.",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


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
        "coach_memory_state": load_coach_memory_state(db),
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
    memory_state = payload.get("coach_memory_state")
    if isinstance(memory_state, dict):
        save_coach_memory_state(db, memory_state)
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
            f"<li>{death_round_label(death)} @ {death.get('timestamp')}: {', '.join(death.get('mistake_labels') or [])}</li>"
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


APP_VERSION = "0.21.1-local"


def app_version(db: Database) -> Dict[str, Any]:
    git = git_version_info()
    return {
        "version": APP_VERSION,
        "build": f"git-{git['commit_count']}" if git.get("commit_count") else "local-dev",
        "git": git,
        "schema": db.schema_info(),
        "changelog": [
            "Keep Clip Coach local-model output as the primary review and show deterministic detector evidence as diagnostics instead of replacing weak reviews with generic fallback text.",
            "Add Find Deaths range testing so a match can scan only a selected start/end time with an optional saved-candidate limit.",
            "Fix Clip Coach regression by preserving representative frames under context budget, shifting combat-report-only anchors earlier, and falling back to deterministic visual coaching when the local model refuses.",
            "Prevent long-lived combat reports from creating repeated death suggestions by detecting combat-report onset only.",
            "Let Find Deaths create lower-confidence suggestions from visible combat report when killfeed/player-name OCR is blocked.",
            "Run Find Deaths as a background job with live progress, capped OCR frames, lower scan FPS, and per-crop Tesseract timeouts.",
            "Cap Clip Coach local-model requests to the configured context window by trimming prompt context and frame count before POST.",
            "Fix Windows cp1252 decode crashes from ffmpeg, Tesseract, git, and custom local-model subprocess output.",
            "Save successful Local AI tests for Clip Coach and log each frame-prep/model-request stage.",
            "Harden Clip Coach against null local-model response fields and log full server tracebacks for failed API calls.",
            "Show whether the player-name killfeed/combat-report death detector ran, including OCR availability and fallback counts.",
            "Make player-name killfeed OCR plus combat-report confirmation the primary death suggestion detector, with configurable in-game name and evidence crops.",
            "Add adaptive detector profile, clip signal timeline UI support, training label dashboard data, semantic minimap/HUD reads, richer coach memory inputs, and multi-pass local vision review.",
            "Add object-proxy frame detection, frame-level contact classes, crosshair-to-contact measurement, richer OCR parsing, per-agent coaching prompt rules, smart death review ranking, and local training-label capture.",
            "Add deterministic per-clip visual detectors for contact cues, death cues, crosshair stability, movement risk, minimap timing, and enemy visibility proxies.",
            "Add per-clip OCR region extraction for HUD, killfeed, lower HUD, combat report, and minimap crops when Tesseract is installed.",
            "Add Clip Coach feedback controls so useful/accurate/wrong verdicts and notes shape future local prompts.",
            "Add match-level themes with repeated mistakes, context patterns, round patterns, evidence examples, and a next practice plan.",
            "Add segmented Clip Coach review with setup, pre-contact, contact, death, and aftermath reads plus an evidence timeline.",
            "Upgrade local review output with claim confidence, review-quality scoring, and explicit VALORANT review pipeline metadata.",
            "Improve personal coach memory with map, agent, weapon, and issue-dimension patterns.",
            "Add chunked KB-constrained context extraction for dense frame modes and infer round number from visible score progression.",
            "Declutter the review UI around the main coach read, evidence timeline, collapsed segment details, and folded context correction.",
            "Run a KB-constrained local context extraction pass before Clip Coach so OCR/vision candidates can ground the final advice.",
            "Add per-death match context extraction and correction so map, agent, round, side, weapon, location, spike state, and alive counts can ground KB retrieval.",
            "Add a local VALORANT knowledge base with structured game data, curated coaching rules, retrieval APIs, UI controls, and Local AI prompt grounding.",
            "Add adaptive Local AI review windows, structured perception/coaching output, HUD context extraction prompts, and improvement trend panels.",
            "Add persistent local coach memory that learns from completed Local AI clip reviews and feeds future prompts.",
            "Make Jump scroll back to the video, fold dashboard panels and Coach Mode, and require consensus before saving scoreboard OCR rounds.",
            "Add a separate aggregate Player Status tab and combine clip review actions into one Coach This Clip workflow.",
            "Add a visual Player Status report and display timeline/spacing-based round estimates when stored round numbers are missing.",
            "Make Coach Memory collapsible, keep the left tab panel scroll-contained, and show immediate Clip Coach loading status.",
            "Redesign the UI with a dark theme, tabbed left dashboard, and compact Coach Memory strip in Review.",
            "Add runtime Local AI FPS override controls with quick FPS presets.",
            "Add batched Local AI clip review and Burst mode for more frames without one oversized model request.",
            "Upgrade the bottom status bar into a compact action pill with busy spinner and job progress percent.",
            "Reduce Local AI frame payload size so Contact mode fits LM Studio context limits more reliably.",
            "Add Local AI review modes: Context, Contact, and Hybrid frame sampling.",
            "Use a higher-density final 5-second local AI frame sequence to catch fast enemy peeks before death.",
            "Move transient app status messages from the Recordings card into a fixed bottom status bar.",
            "Send a dense 10-second pre-death frame sequence to local vision models instead of only a few keyframes.",
            "Ground local vision-model reviews with ordered setup, pre-contact, pressure, correction, death, and aftermath keyframes.",
            "Ask local models to cite visible evidence and avoid confident advice when keyframes are insufficient.",
            "Add local model purpose modes and an olmOCR LM Studio preset for OCR/HUD extraction.",
            "Use stricter local vision-model prompts and JSON parsing for clearer local AI reviews.",
            "Infer unknown death marker rounds from top scoreboard score OCR.",
            "Simplify the match review UI around video, coach priorities, readable suggestions, and compact advice cards.",
            "Shorten generated advice into one diagnosis, one action, and one practice item.",
            "Confirmed death markers are preserved when Auto Coach, Analyze, or Find Deaths runs.",
            "Duplicate death suggestions near accepted, rejected, or already marked deaths are skipped.",
            "Coach moment feedback and LM Studio connection testing for better local model setup and personalization.",
            "Full VOD Coach scans whole matches for crosshair, pressure, minimap, and reset moments with optional local vision-model review.",
            "Auto Coach pipeline with advice generation, review-safe suggestions, and visible job progress.",
            "Persistent jobs, logs, backups, retention, exports, and automation.",
            "Advanced search, playbook editing, correction review, privacy audit, provider registry.",
        ],
    }


def git_version_info() -> Dict[str, Any]:
    root = Path(__file__).resolve().parent.parent

    def run_git(args: List[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()

    commit_count = run_git(["rev-list", "--count", "HEAD"])
    full_hash = run_git(["rev-parse", "HEAD"])
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    commit_date = run_git(["show", "-s", "--format=%cI", "HEAD"])
    dirty = bool(run_git(["status", "--porcelain"]))
    return {
        "commit_count": int(commit_count) if commit_count.isdigit() else None,
        "hash": full_hash,
        "short_hash": full_hash[:7] if full_hash else "",
        "branch": branch,
        "commit_date": commit_date,
        "dirty": dirty,
    }


def provider_registry() -> Dict[str, Any]:
    return {
        "ocr": [{"id": "tesseract", "status": "available-if-installed", "privacy": "local"}],
        "frame_extractors": [{"id": "ffmpeg", "status": "available-if-installed", "privacy": "local"}],
        "playbooks": [{"id": "local-json", "status": "enabled", "privacy": "local"}],
        "knowledge": [{"id": "local-retrieval", "status": "enabled", "privacy": "local files"}],
        "analytics": [{"id": "sqlite-local", "status": "enabled", "privacy": "local"}],
        "ai_review": [
            {"id": "custom-command", "status": "configured-by-user", "privacy": "local process"},
            {"id": "ollama", "status": "configured-by-user", "privacy": "local HTTP"},
            {"id": "lmstudio", "status": "configured-by-user", "privacy": "local HTTP"},
            {"id": "llamacpp", "status": "configured-by-user", "privacy": "local HTTP"},
        ],
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
    if value is None:
        return []
    if isinstance(value, str):
        normalized = value.replace(",", "\n")
        return [item.strip() for item in normalized.splitlines() if item.strip()]
    if isinstance(value, dict):
        value = [value.get("value") or value.get("label") or value.get("text") or value.get("summary") or ""]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
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
            "local VALORANT knowledge snapshots and retrieval index",
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
    manual_counts = benchmark_labels(db)["counts"]
    true_positive = manual_counts.get("true_positive", 0)
    false_positive = manual_counts.get("false_positive", 0)
    missed = manual_counts.get("missed_death", 0) + manual_counts.get("false_negative", 0)
    measured_precision = round(true_positive / (true_positive + false_positive), 3) if true_positive + false_positive else None
    measured_recall = round(true_positive / (true_positive + missed), 3) if true_positive + missed else None
    result = {
        "kind": "evaluation_benchmark",
        "summary": benchmark_summary(measured_precision if measured_precision is not None else precision, coverage, total_deaths, total_suggestions),
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
            "measured_precision": measured_precision,
            "measured_recall": measured_recall,
            "benchmark_labels": manual_counts,
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


def save_clip_review_feedback(db: Database, death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in {"accurate", "useful", "wrong", "not_useful"}:
        return {"ok": False, "message": "verdict must be accurate, useful, wrong, or not_useful"}
    feedback = {
        "kind": "clip_review_feedback",
        "death_id": death_id,
        "verdict": verdict,
        "note": str(payload.get("note") or "").strip(),
        "review_id": optional_int(payload.get("review_id")),
        "labels": normalize_text_list(payload.get("labels") or []),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    analysis_id = db.save_death_analysis(death_id, "clip_review_feedback", feedback)
    db.log("info", "clip-review-feedback", f"Saved Clip Coach feedback #{analysis_id}", {"death_id": death_id, "verdict": verdict})
    return {"ok": True, "id": analysis_id, "feedback": feedback, "summary": clip_review_feedback_summary(db)}


def save_clip_training_label(db: Database, death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    label = {
        "kind": "clip_training_label",
        "death_id": death_id,
        "enemy_visible_frame": optional_int(payload.get("enemy_visible_frame")),
        "first_contact_frame": optional_int(payload.get("first_contact_frame")),
        "death_frame": optional_int(payload.get("death_frame")),
        "crosshair_issue": optional_bool(payload.get("crosshair_issue")),
        "correct_mistake_label": str(payload.get("correct_mistake_label") or "").strip(),
        "notes": str(payload.get("notes") or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    analysis_id = db.save_death_analysis(death_id, "clip_training_label", label)
    db.log("info", "training-label", f"Saved clip training label #{analysis_id}", {"death_id": death_id})
    return {"ok": True, "id": analysis_id, "label": label, "summary": training_label_summary(db)}


def training_label_summary(db: Database) -> Dict[str, Any]:
    rows = [
        item for item in db.list_structured_analyses("death", limit=1000)
        if item.get("analysis_type") == "clip_training_label"
    ]
    labels: Dict[str, int] = {}
    crosshair_yes = 0
    frame_labeled = 0
    for row in rows:
        payload = row.get("payload") or {}
        label = str(payload.get("correct_mistake_label") or "").strip()
        if label:
            labels[label] = labels.get(label, 0) + 1
        if payload.get("crosshair_issue") is True:
            crosshair_yes += 1
        if payload.get("enemy_visible_frame") or payload.get("first_contact_frame") or payload.get("death_frame"):
            frame_labeled += 1
    return {
        "count": len(rows),
        "frame_labeled": frame_labeled,
        "crosshair_issue_yes": crosshair_yes,
        "top_labels": sorted(labels.items(), key=lambda item: (-item[1], item[0]))[:8],
    }


def detector_learning_profile(db: Database) -> Dict[str, Any]:
    feedback = db.detector_feedback_summary()
    labels = training_label_summary(db)
    tuning = detector_tuning(db)
    accepted = int(feedback.get("accepted") or 0)
    rejected = int(feedback.get("rejected") or 0)
    labeled_frames = int(labels.get("frame_labeled") or 0)
    false_positive_pressure = rejected / max(1, accepted + rejected)
    miss_pressure = min(0.18, labeled_frames * 0.012)
    sensitivity = str(tuning.get("recommended") or db.get_setting("detector_sensitivity", "normal"))
    threshold_shift = {"low": 0.08, "normal": 0.0, "high": -0.08}.get(sensitivity, 0.0)
    threshold_shift += min(0.10, false_positive_pressure * 0.10) - miss_pressure
    contact_threshold = round(max(0.28, min(0.60, 0.42 + threshold_shift)), 3)
    confirmed_enemy_threshold = round(max(0.46, min(0.76, 0.62 + threshold_shift)), 3)
    possible_enemy_threshold = round(max(0.28, min(0.56, 0.40 + threshold_shift)), 3)
    return {
        "kind": "adaptive_detector_profile",
        "sensitivity": sensitivity,
        "accepted_suggestions": accepted,
        "rejected_suggestions": rejected,
        "training_labels": labels,
        "contact_threshold": contact_threshold,
        "possible_enemy_threshold": possible_enemy_threshold,
        "confirmed_enemy_threshold": confirmed_enemy_threshold,
        "threshold_shift": round(threshold_shift, 3),
        "external_enemy_detector": bool(str(db.get_setting("enemy_detector_command", "") or "").strip()),
        "external_enemy_detector_command": str(db.get_setting("enemy_detector_command", "") or "").strip(),
        "learning_state": "personalized" if accepted + rejected + labeled_frames >= 5 else "warming_up",
        "summary": (
            f"Detector {sensitivity}; contact threshold {contact_threshold}. "
            f"Learned from {accepted + rejected} suggestion verdict(s) and {labeled_frames} frame-labeled clip(s)."
        ),
    }


def clip_review_feedback_summary(db: Database, death_id: Optional[int] = None) -> Dict[str, Any]:
    rows = [
        item for item in db.list_structured_analyses("death", limit=1000)
        if item.get("analysis_type") == "clip_review_feedback"
    ]
    if death_id is not None:
        rows = [item for item in rows if int(item.get("subject_id") or 0) == int(death_id)]
    counts: Dict[str, int] = {}
    notes = []
    for row in rows:
        payload = row.get("payload") or {}
        verdict = str(payload.get("verdict") or "unknown")
        counts[verdict] = counts.get(verdict, 0) + 1
        if payload.get("note"):
            notes.append(str(payload.get("note"))[:220])
    return {
        "count": len(rows),
        "counts": counts,
        "recent_notes": notes[:8],
        "prompt_guidance": clip_feedback_prompt_guidance(counts, notes),
    }


def clip_feedback_prompt_guidance(counts: Dict[str, int], notes: List[str]) -> str:
    wrong = int(counts.get("wrong") or 0) + int(counts.get("not_useful") or 0)
    good = int(counts.get("accurate") or 0) + int(counts.get("useful") or 0)
    if not good and not wrong:
        return "No Clip Coach feedback has been recorded yet."
    guidance = []
    if wrong:
        guidance.append("Be more conservative: cite visible evidence, lower confidence when uncertain, and avoid unsupported claims.")
    if good:
        guidance.append("Continue using frame-cited, specific better-play advice that the user marked useful.")
    if notes:
        guidance.append("Recent user corrections: " + " | ".join(notes[:3]))
    return " ".join(guidance)


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
    training_summary = training_label_summary(db)
    detector_profile = detector_learning_profile(db)
    base["coach_v2"] = {
        "weighted_profile": weights,
        "skill_scores": skill_scores,
        "weekly_focus": weekly,
        "memory_strength": min(100, len(analyses) * 2 + sum((trends.get("labels") or {}).values()) * 5),
        "training_labels": training_summary,
        "detector_profile": detector_profile,
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
    provider = str(db.get_setting("local_ai_provider", "custom-command") or "custom-command")
    command = str(db.get_setting("local_ai_command", "") or "").strip()
    base_url = str(db.get_setting("local_ai_base_url", default_base_url(provider)) or "").strip()
    model = str(db.get_setting("local_ai_model", default_model(provider)) or "").strip()
    purpose = str(db.get_setting("local_ai_purpose", "coach") or "coach").strip()
    review_mode = str(db.get_setting("local_ai_review_mode", "contact") or "contact").strip()
    review_fps = str(db.get_setting("local_ai_review_fps", "") or "").strip()
    review_window_seconds = normalize_review_window_setting(db.get_setting("local_ai_review_window_seconds", "10"))
    context_limit = normalize_context_limit_setting(db.get_setting("local_ai_context_limit", "8192"))
    image_token_estimate = normalize_image_token_estimate_setting(db.get_setting("local_ai_image_token_estimate", "900"))
    sequence_profile = local_ai_sequence_profile(review_mode, review_fps, review_window_seconds)
    enabled = bool(command) if provider == "custom-command" else bool(base_url and model)
    return {
        "enabled": enabled,
        "privacy": "local process only; no network upload by this app",
        "provider": provider,
        "purpose": purpose,
        "command": command,
        "base_url": base_url,
        "model": model,
        "review_mode": sequence_profile["id"],
        "review_mode_label": sequence_profile["label"],
        "review_fps": review_fps,
        "review_window_seconds": review_window_seconds,
        "context_limit": context_limit,
        "image_token_estimate": image_token_estimate,
        "review_frame_limit": sequence_profile["limit"],
        "status": "configured" if enabled else "disabled",
        "expected_protocol": "custom command uses stdin JSON/stdout JSON; HTTP providers use local-only JSON requests",
        "providers": [
            {"id": "custom-command", "label": "Custom command"},
            {"id": "ollama", "label": "Ollama"},
            {"id": "lmstudio", "label": "LM Studio"},
            {"id": "llamacpp", "label": "llama.cpp server"},
        ],
    }


def save_local_ai_config(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(payload.get("provider") or "custom-command").strip()
    purpose = str(payload.get("purpose") or "coach").strip()
    command = str(payload.get("command") or "").strip()
    base_url = str(payload.get("base_url") or default_base_url(provider)).strip()
    model = str(payload.get("model") or default_model(provider)).strip()
    review_fps = normalize_review_fps_setting(payload.get("review_fps"))
    review_window_seconds = normalize_review_window_setting(payload.get("review_window_seconds"))
    context_limit = normalize_context_limit_setting(payload.get("context_limit"))
    image_token_estimate = normalize_image_token_estimate_setting(payload.get("image_token_estimate"))
    review_mode = local_ai_sequence_profile(str(payload.get("review_mode") or "contact"), review_fps, review_window_seconds)["id"]
    db.set_setting("local_ai_provider", provider)
    db.set_setting("local_ai_purpose", purpose)
    db.set_setting("local_ai_command", command)
    db.set_setting("local_ai_base_url", base_url)
    db.set_setting("local_ai_model", model)
    db.set_setting("local_ai_review_mode", review_mode)
    db.set_setting("local_ai_review_fps", review_fps)
    db.set_setting("local_ai_review_window_seconds", review_window_seconds)
    db.set_setting("local_ai_context_limit", context_limit)
    db.set_setting("local_ai_image_token_estimate", image_token_estimate)
    db.log("info", "local-ai", "Updated local AI configuration", {"provider": provider, "purpose": purpose, "review_mode": review_mode, "review_fps": review_fps, "review_window_seconds": review_window_seconds, "context_limit": context_limit, "image_token_estimate": image_token_estimate, "configured": bool(command or base_url)})
    return {"ok": True, "local_ai": local_ai_status(db)}


def normalize_review_fps_setting(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return str(max(1, min(20, number)))


def normalize_review_window_setting(value: Any) -> str:
    if value is None or value == "":
        return "10"
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return "10"
    return str(max(5, min(20, number)))


def normalize_context_limit_setting(value: Any) -> str:
    if value is None or value == "":
        return "8192"
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return "8192"
    return str(max(4096, min(131072, number)))


def normalize_image_token_estimate_setting(value: Any) -> str:
    if value is None or value == "":
        return "900"
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return "900"
    return str(max(256, min(4096, number)))


def test_local_ai_connection(db: Database, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    if payload:
        provider = str(payload.get("provider") or "lmstudio").strip()
        status = {
            "provider": provider,
            "purpose": str(payload.get("purpose") or "coach").strip(),
            "review_mode": local_ai_sequence_profile(str(payload.get("review_mode") or "contact"), payload.get("review_fps"), payload.get("review_window_seconds"))["id"],
            "review_fps": normalize_review_fps_setting(payload.get("review_fps")),
            "review_window_seconds": normalize_review_window_setting(payload.get("review_window_seconds")),
            "context_limit": normalize_context_limit_setting(payload.get("context_limit")),
            "image_token_estimate": normalize_image_token_estimate_setting(payload.get("image_token_estimate")),
            "command": str(payload.get("command") or "").strip(),
            "base_url": str(payload.get("base_url") or default_base_url(provider)).strip(),
            "model": str(payload.get("model") or "").strip(),
            "enabled": True,
        }
    else:
        status = local_ai_status(db)
    provider = status.get("provider")
    if provider == "custom-command":
        return {
            "ok": bool(status.get("command")),
            "message": "Custom command configured." if status.get("command") else "Custom command is empty.",
            "status": status,
            "models": [],
        }
    base_url = str(status.get("base_url") or "").rstrip("/")
    if not base_url:
        return {"ok": False, "message": "Base URL is empty.", "status": status, "models": []}
    try:
        if provider == "ollama":
            response = get_json(base_url + "/api/tags", timeout=8)
            models = [item.get("name") for item in response.get("models", []) if item.get("name")]
        else:
            response = get_json(base_url + "/models", timeout=8)
            models = [item.get("id") for item in response.get("data", []) if item.get("id")]
    except Exception as exc:
        return {
            "ok": False,
            "message": f"Could not reach {provider}: {exc}",
            "status": status,
            "models": [],
        }
    configured_model = str(status.get("model") or "")
    model_ready = not configured_model or configured_model in models
    message = f"Connected to {provider}. Found {len(models)} model(s)."
    if configured_model and not model_ready:
        message += f" Configured model '{configured_model}' was not in the model list."
    return {
        "ok": True,
        "message": message,
        "status": status,
        "models": models,
        "configured_model_ready": model_ready,
    }


def run_local_ai_review(db: Database, death_id: int) -> Dict[str, Any]:
    status = local_ai_status(db)
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found", "status": status}
    db.log(
        "info",
        "local-ai",
        f"Clip Coach started for death #{death_id}",
        {
            "provider": status.get("provider"),
            "model": status.get("model"),
            "enabled": status.get("enabled"),
            "review_mode": status.get("review_mode"),
        },
    )
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
        db.log("warning", "local-ai", f"Clip Coach stopped before model request for death #{death_id}: local AI disabled.", {"status": status})
        return {"ok": True, "message": result["summary"], "analysis": result, "status": status}
    sequence_profile = local_ai_sequence_profile(str(status.get("review_mode") or "contact"), status.get("review_fps"), status.get("review_window_seconds"))
    db.log("info", "local-ai", f"Clip Coach preparing frame sequence for death #{death_id}.", {"mode": sequence_profile["id"], "limit": sequence_profile["limit"]})
    sequence = build_local_ai_review_sequence(
        db,
        death_id,
        db.path.parent / "vision",
        mode=sequence_profile["id"],
        fps_override=status.get("review_fps"),
        window_seconds=status.get("review_window_seconds"),
    )
    if not sequence.get("ok"):
        db.log("warning", "local-ai", f"Clip Coach stopped before model request for death #{death_id}: {sequence.get('message')}", {"sequence": sequence})
        return {"ok": False, "message": sequence.get("message") or "Could not prepare local AI review sequence.", "status": status}
    keyframes = keyframe_payload(db, death_id, analysis_type="local_ai_sequence", limit=int(sequence_profile["limit"]))
    db.log("info", "local-ai", f"Clip Coach prepared {len(keyframes)} frame(s) for death #{death_id}.", {"sequence_message": sequence.get("message")})
    visual_signals = analyze_clip_visual_signals(db, death_id, keyframes)
    region_ocr = analyze_clip_ocr_regions(db, death_id, keyframes)
    db.log("info", "local-ai", f"Clip Coach running context extraction for death #{death_id}.", {"frame_count": len(keyframes)})
    extraction = run_local_context_extraction(db, death_id, death, keyframes, status)
    if extraction.get("ok"):
        death = db.get_death(death_id) or death
    request = {
        "death": death,
        "annotations": death.get("annotations") or [],
        "clip_path": death.get("clip_path"),
        "keyframes": keyframes,
        "segments": build_clip_segments(keyframes),
        "sequence": sequence.get("analysis") or {},
        "marker_quality": ((sequence.get("analysis") or {}).get("marker_quality") or {}),
        "visual_signals": visual_signals.get("analysis") or {},
        "ocr_regions": region_ocr.get("analysis") or {},
        "context_extraction": extraction.get("analysis") or {},
        "prompt": render_model_prompt(db, death),
        "privacy": "local-only",
    }
    db.save_death_analysis(death_id, "local_model_audit", redact_model_request(request, status))
    if status["provider"] != "custom-command":
        db.log(
            "info",
            "local-ai",
            f"Clip Coach sending review request to {status['provider']} for death #{death_id}.",
            {"frame_count": len(keyframes), "provider": status.get("provider"), "model": status.get("model"), "base_url": status.get("base_url")},
        )
        return run_local_http_review(db, death_id, request, status)
    try:
        completed = subprocess.run(
            status["command"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
    result = build_model_review_result(model_payload, "local-command", completed.stdout or "")
    result = enrich_model_review_result(result, request)
    result = apply_deterministic_review_fallback(result, request)
    db.save_death_analysis(death_id, "local_ai_review", result)
    update_coach_memory_from_review(db, death, result)
    return {"ok": True, "message": result["summary"], "analysis": result, "status": status}


def run_local_context_extraction(
    db: Database,
    death_id: int,
    death: Dict[str, Any],
    keyframes: List[Dict[str, Any]],
    status: Dict[str, Any],
) -> Dict[str, Any]:
    if not keyframes:
        return {"ok": False, "message": "No frames available for context extraction.", "status": status}
    vocabulary = build_vocabulary_pack()
    request = {
        "task": "valorant_context_extraction",
        "death": death,
        "clip_path": death.get("clip_path"),
        "keyframes": keyframes,
        "vocabulary_summary": vocabulary.get("summary"),
        "prompt": render_context_extraction_prompt(db, death, vocabulary),
        "privacy": "local-only",
    }
    try:
        if len(keyframes) > 30:
            db.log(
                "info",
                "local-ai",
                f"Clip Coach chunked context extraction started for death #{death_id}.",
                {"provider": status.get("provider"), "model": status.get("model"), "frame_count": len(keyframes)},
            )
            parsed = run_chunked_context_extraction(request, status)
            db.log("info", "local-ai", f"Clip Coach chunked context extraction completed for death #{death_id}.", {"chunk_count": parsed.get("chunk_count")})
            provider = parsed.get("provider") or status["provider"]
        elif status["provider"] == "custom-command":
            text = run_custom_model_text(status, request, timeout=90)
            provider = "local-command"
            parsed = parse_context_extraction(text, provider)
        else:
            db.log(
                "info",
                "local-ai",
                f"Clip Coach context extraction POST to {status['provider']} for death #{death_id}.",
                {"provider": status.get("provider"), "model": status.get("model"), "frame_count": len(keyframes), "base_url": status.get("base_url")},
            )
            text = run_local_http_text(request, status, local_context_system_prompt(), max_tokens=900, timeout=180)
            db.log("info", "local-ai", f"Clip Coach context extraction response received for death #{death_id}.", {"response_chars": len(text)})
            provider = status["provider"]
            parsed = parse_context_extraction(text, provider)
    except Exception as exc:
        db.log("warning", "local-ai", f"Clip Coach context extraction failed for death #{death_id}: {exc}", {"provider": status.get("provider")})
        result = context_extraction_fallback(f"Context extraction failed: {exc}")
        db.save_death_analysis(death_id, "context_extraction", result)
        return {"ok": False, "message": result["summary"], "analysis": result, "status": status}
    resolved = resolve_context_extraction(db, death_id, death, parsed, vocabulary)
    db.save_death_analysis(death_id, "context_extraction", resolved)
    save_auto_context_correction(db, death_id, death, resolved)
    return {"ok": True, "message": resolved["summary"], "analysis": resolved, "status": status}


def analyze_clip_visual_signals(db: Database, death_id: int, keyframes: List[Dict[str, Any]]) -> Dict[str, Any]:
    frame_rows = keyframes_with_paths(db, keyframes)
    if not frame_rows:
        result = {"kind": "clip_visual_signals", "summary": "No frames available for deterministic visual signals.", "status": "empty", "confidence": 0.0}
        db.save_death_analysis(death_id, "clip_visual_signals", result)
        return {"ok": False, "message": result["summary"], "analysis": result}

    calibration = db.get_calibration()
    detector_profile = detector_learning_profile(db)
    timeline = []
    previous_arr = None
    previous_minimap = None
    previous_crosshair = None
    for row in frame_rows:
        arr = load_frame(row["path"])
        motion = frame_motion(previous_arr, arr) if previous_arr is not None else 0.0
        minimap = crop_region(arr, calibration["minimap"])
        crosshair = crop_region(arr, calibration["crosshair"])
        minimap_motion = frame_motion(previous_minimap, minimap) if previous_minimap is not None else 0.0
        crosshair_drift = frame_motion(previous_crosshair, crosshair) if previous_crosshair is not None else 0.0
        previous_arr = arr
        previous_minimap = minimap
        previous_crosshair = crosshair
        rel = frame_relative_second(row["frame"])
        metrics = compute_metrics(row["path"], rel if rel is not None else float(row["frame"].get("index") or 0), arr, motion, calibration, minimap_motion, crosshair_drift)
        object_proxy = detect_frame_objects(arr, calibration, detector_profile, row.get("path"))
        contact_score = clip_contact_score(metrics)
        death_score = round(metrics.death_score, 3)
        timeline.append(
            {
                "frame": row["frame"].get("sequence_index") or row["frame"].get("index"),
                "timestamp": row["frame"].get("timestamp"),
                "relative_second": rel,
                "class": classify_clip_frame(metrics, object_proxy, contact_score, detector_profile),
                "death_score": death_score,
                "contact_score": round(contact_score, 3),
                "motion": round(metrics.motion, 3),
                "crosshair_activity": round(metrics.crosshair_activity, 3),
                "crosshair_drift": round(metrics.crosshair_drift, 3),
                "minimap_motion": round(metrics.minimap_motion, 3),
                "killfeed_red": round(metrics.killfeed_red, 3),
                "center_red": round(metrics.center_red, 3),
                "combat_report_score": round(metrics.combat_report_score, 3),
                "object_proxy": object_proxy,
                "crosshair_to_contact": crosshair_to_contact_measurement(object_proxy),
                "reason": metrics.reason,
            }
        )

    first_contact = first_signal_frame(timeline, "contact_score", float(detector_profile.get("contact_threshold") or 0.42))
    death_cue = best_signal_frame(timeline, "death_score")
    crosshair_score = score_clip_crosshair(timeline)
    minimap_read = score_clip_minimap(timeline)
    enemy_timeline = build_enemy_visibility_proxy(timeline)
    frame_classes = summarize_frame_classes(timeline)
    contact_measurement = summarize_crosshair_to_contact(timeline)
    result = {
        "kind": "clip_visual_signals",
        "summary": visual_signal_summary(first_contact, death_cue, crosshair_score, minimap_read),
        "timeline": timeline[:120],
        "detector_profile": detector_profile,
        "frame_classifier": frame_classes,
        "enemy_visibility_timeline": enemy_timeline,
        "crosshair_to_contact": contact_measurement,
        "first_contact": first_contact,
        "death_cue": death_cue,
        "crosshair_score": crosshair_score,
        "minimap_read": minimap_read,
        "movement_read": movement_read(timeline),
        "confidence": round(min(0.82, 0.30 + len(timeline) * 0.01 + (0.18 if first_contact else 0) + (0.12 if death_cue else 0)), 2),
        "status": "completed",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.save_death_analysis(death_id, "clip_visual_signals", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def analyze_clip_ocr_regions(db: Database, death_id: int, keyframes: List[Dict[str, Any]]) -> Dict[str, Any]:
    tesseract = tesseract_path()
    if not tesseract:
        result = {
            "kind": "clip_ocr_regions",
            "summary": "Tesseract is not installed; region OCR skipped.",
            "available": False,
            "status": "skipped",
            "confidence": 0.0,
        }
        db.save_death_analysis(death_id, "clip_ocr_regions", result)
        return {"ok": False, "message": result["summary"], "analysis": result}
    frame_rows = keyframes_with_paths(db, keyframes)
    if not frame_rows:
        result = {"kind": "clip_ocr_regions", "summary": "No frames available for region OCR.", "available": True, "status": "empty", "confidence": 0.0}
        db.save_death_analysis(death_id, "clip_ocr_regions", result)
        return {"ok": False, "message": result["summary"], "analysis": result}

    crop_dir = db.path.parent / "vision" / "ocr-regions" / f"death-{death_id}"
    crop_dir.mkdir(parents=True, exist_ok=True)
    calibration = db.get_calibration()
    region_names = ["hud_top", "killfeed", "hud_bottom", "combat_report", "minimap"]
    reads = []
    # OCR only a representative subset to keep the clip review responsive.
    sampled = select_ocr_frames(frame_rows)
    for row in sampled:
        image = Image.open(row["path"]).convert("RGB")
        arr = np.asarray(image).astype(np.float32) / 255.0
        for region_name in region_names:
            region = calibration.get(region_name)
            if not region:
                continue
            crop_arr = crop_region(arr, region)
            crop_img = Image.fromarray(np.clip(crop_arr * 255, 0, 255).astype(np.uint8))
            prepared = preprocess_general_ocr_crop(crop_img)
            crop_path = crop_dir / f"frame-{row['frame'].get('sequence_index') or row['frame'].get('index')}-{region_name}.png"
            prepared.save(crop_path)
            text = run_tesseract(tesseract, crop_path)
            if text:
                reads.append(
                    {
                        "frame": row["frame"].get("sequence_index") or row["frame"].get("index"),
                        "timestamp": row["frame"].get("timestamp"),
                        "relative_second": frame_relative_second(row["frame"]),
                        "region": region_name,
                        "text": text[:240],
                        "kind": classify_ocr_region_text(region_name, text),
                        "crop": str(crop_path),
                    }
                )
    result = {
        "kind": "clip_ocr_regions",
        "summary": f"Region OCR completed with {len(reads)} readable text item(s).",
        "available": True,
        "reads": reads[:40],
        "structured": summarize_clip_ocr_reads(reads),
        "confidence": 0.55 if reads else 0.10,
        "status": "completed",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.save_death_analysis(death_id, "clip_ocr_regions", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def keyframes_with_paths(db: Database, keyframes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for frame in keyframes:
        path = find_frame_path(db.path.parent, str(frame.get("frame_id") or ""))
        if path and path.exists():
            rows.append({"frame": frame, "path": path})
    return rows


def clip_contact_score(metrics: Any) -> float:
    return float(
        min(1.0, metrics.pressure_score * 0.45 + metrics.center_red * 0.30 + metrics.killfeed_red * 0.25 + metrics.crosshair_activity * 2.2)
    )


def classify_clip_frame(metrics: Any, object_proxy: Dict[str, Any], contact_score: float, detector_profile: Optional[Dict[str, Any]] = None) -> str:
    detector_profile = detector_profile or {}
    confirmed_threshold = float(detector_profile.get("confirmed_enemy_threshold") or 0.62)
    possible_threshold = float(detector_profile.get("possible_enemy_threshold") or 0.40)
    contact_threshold = float(detector_profile.get("contact_threshold") or 0.42)
    if metrics.combat_report_score >= 0.50 or metrics.death_score >= 0.62:
        return "post_death_or_death_ui"
    if metrics.death_score >= 0.52 or metrics.center_red >= 0.22:
        return "damage_or_death_cue"
    if object_proxy.get("enemy_like_region", {}).get("confidence", 0) >= confirmed_threshold or contact_score >= contact_threshold + 0.10:
        return "confirmed_contact_proxy"
    if object_proxy.get("enemy_like_region", {}).get("confidence", 0) >= possible_threshold or contact_score >= max(0.28, contact_threshold - 0.08):
        return "possible_contact_proxy"
    return "no_contact_signal"


def detect_frame_objects(arr: np.ndarray, calibration: Dict[str, Dict[str, float]], detector_profile: Optional[Dict[str, Any]] = None, frame_path: Optional[Path] = None) -> Dict[str, Any]:
    detector_profile = detector_profile or {}
    center_region = {"x": 0.18, "y": 0.22, "w": 0.64, "h": 0.56}
    center = crop_region(arr, center_region)
    enemy = detect_enemy_like_region(center, center_region, detector_profile)
    external = run_external_enemy_detector(frame_path, detector_profile)
    if external.get("available") and float(external.get("confidence") or 0) > float(enemy.get("confidence") or 0):
        enemy = {
            "visible": bool(external.get("visible")),
            "confidence": round(float(external.get("confidence") or 0), 2),
            "bbox_norm": external.get("bbox_norm") or enemy.get("bbox_norm"),
            "center_norm": external.get("center_norm") or enemy.get("center_norm"),
            "source": "external_detector",
        }
    crosshair = estimate_crosshair_center(arr, calibration.get("crosshair") or {"x": 0.47, "y": 0.47, "w": 0.06, "h": 0.06})
    spike = detect_bright_icon(crop_region(arr, calibration.get("hud_top") or {"x": 0.2, "y": 0, "w": 0.6, "h": 0.14}), "spike_or_round_icon")
    weapon = detect_bright_icon(crop_region(arr, calibration.get("hud_bottom") or {"x": 0.2, "y": 0.78, "w": 0.6, "h": 0.22}), "weapon_or_ammo_icon")
    return {
        "enemy_like_region": enemy,
        "external_enemy_detector": external,
        "crosshair": crosshair,
        "spike_icon_proxy": spike,
        "weapon_icon_proxy": weapon,
    }


def run_external_enemy_detector(frame_path: Optional[Path], detector_profile: Dict[str, Any]) -> Dict[str, Any]:
    command = str(detector_profile.get("external_enemy_detector_command") or "").strip()
    if not command or not frame_path:
        return {"available": False}
    path = str(frame_path)
    command_line = command.replace("{image}", path) if "{image}" in command else f'{command} "{path}"'
    try:
        completed = subprocess.run(command_line, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=True, timeout=8)
    except Exception as exc:
        return {"available": True, "visible": False, "confidence": 0.0, "error": str(exc)}
    if completed.returncode != 0:
        return {"available": True, "visible": False, "confidence": 0.0, "error": completed.stderr.strip()[:180]}
    try:
        payload = json.loads(strip_json_fence(completed.stdout or "{}"))
    except json.JSONDecodeError:
        return {"available": True, "visible": False, "confidence": 0.0, "error": "external detector returned non-json"}
    detections = payload.get("detections") if isinstance(payload, dict) else []
    if isinstance(payload, dict) and not detections and ("bbox_norm" in payload or "confidence" in payload):
        detections = [payload]
    rows = [row for row in (detections or []) if isinstance(row, dict)]
    if not rows:
        return {"available": True, "visible": False, "confidence": 0.0}
    best = max(rows, key=lambda row: float(row.get("confidence") or row.get("score") or 0))
    confidence = normalize_confidence(best.get("confidence", best.get("score", 0)))
    bbox = best.get("bbox_norm") or best.get("box") or {}
    center = best.get("center_norm") or bbox_center_norm(bbox)
    return {"available": True, "visible": confidence >= 0.35, "confidence": confidence, "bbox_norm": bbox, "center_norm": center, "label": best.get("label") or "enemy"}


def bbox_center_norm(bbox: Any) -> Dict[str, float]:
    if not isinstance(bbox, dict):
        return {}
    try:
        x = float(bbox.get("x") or 0)
        y = float(bbox.get("y") or 0)
        w = float(bbox.get("w") or 0)
        h = float(bbox.get("h") or 0)
    except (TypeError, ValueError):
        return {}
    return {"x": round(x + w / 2.0, 3), "y": round(y + h / 2.0, 3)}


def detect_enemy_like_region(region: np.ndarray, source_region: Dict[str, float], detector_profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    detector_profile = detector_profile or {}
    if region.size == 0:
        return {"visible": False, "confidence": 0.0}
    red = region[:, :, 0]
    green = region[:, :, 1]
    blue = region[:, :, 2]
    sensitivity_shift = -float(detector_profile.get("threshold_shift") or 0.0) * 0.45
    red_floor = max(0.34, min(0.52, 0.42 + sensitivity_shift))
    mask = (red > red_floor) & (red > green * 1.22) & (red > blue * 1.18)
    density = float(mask.mean())
    if density <= 0:
        return {"visible": False, "confidence": 0.0, "density": 0.0}
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return {"visible": False, "confidence": 0.0, "density": round(density, 4)}
    h, w = mask.shape
    x1 = float(xs.min()) / max(1, w)
    x2 = float(xs.max()) / max(1, w)
    y1 = float(ys.min()) / max(1, h)
    y2 = float(ys.max()) / max(1, h)
    area = max(0.0, (x2 - x1) * (y2 - y1))
    compactness = min(1.0, density / max(0.001, area))
    confidence = min(0.92, density * 24.0 + compactness * 0.22 + max(0.0, -float(detector_profile.get("threshold_shift") or 0.0)) * 0.35)
    return {
        "visible": confidence >= 0.35,
        "confidence": round(confidence, 2),
        "density": round(density, 4),
        "bbox_norm": {
            "x": round(float(source_region["x"]) + x1 * float(source_region["w"]), 3),
            "y": round(float(source_region["y"]) + y1 * float(source_region["h"]), 3),
            "w": round((x2 - x1) * float(source_region["w"]), 3),
            "h": round((y2 - y1) * float(source_region["h"]), 3),
        },
        "center_norm": {
            "x": round(float(source_region["x"]) + ((x1 + x2) / 2.0) * float(source_region["w"]), 3),
            "y": round(float(source_region["y"]) + ((y1 + y2) / 2.0) * float(source_region["h"]), 3),
        },
    }


def estimate_crosshair_center(arr: np.ndarray, region: Dict[str, float]) -> Dict[str, Any]:
    crop_img = crop_region(arr, region)
    if crop_img.size == 0:
        return {"x": 0.5, "y": 0.5, "confidence": 0.0}
    gray = crop_img.mean(axis=2)
    contrast = np.abs(gray - float(gray.mean()))
    threshold = max(float(np.percentile(contrast, 92)), 0.05)
    mask = contrast >= threshold
    if not mask.any():
        return {"x": round(float(region.get("x", 0.47)) + float(region.get("w", 0.06)) / 2.0, 3), "y": round(float(region.get("y", 0.47)) + float(region.get("h", 0.06)) / 2.0, 3), "confidence": 0.2}
    ys, xs = np.where(mask)
    h, w = mask.shape
    cx = float(region["x"]) + float(xs.mean()) / max(1, w) * float(region["w"])
    cy = float(region["y"]) + float(ys.mean()) / max(1, h) * float(region["h"])
    return {"x": round(cx, 3), "y": round(cy, 3), "confidence": round(min(0.85, float(mask.mean()) * 9.0 + float(crop_img.std()) * 2.0), 2)}


def detect_bright_icon(region: np.ndarray, label: str) -> Dict[str, Any]:
    if region.size == 0:
        return {"label": label, "visible": False, "confidence": 0.0}
    brightness = region.mean(axis=2)
    density = float((brightness > 0.70).mean())
    contrast = float(region.std())
    confidence = min(0.85, density * 4.0 + contrast * 1.2)
    return {"label": label, "visible": confidence >= 0.25, "confidence": round(confidence, 2), "bright_density": round(density, 3)}


def crosshair_to_contact_measurement(object_proxy: Dict[str, Any]) -> Dict[str, Any]:
    enemy = object_proxy.get("enemy_like_region") or {}
    crosshair = object_proxy.get("crosshair") or {}
    center = enemy.get("center_norm") or {}
    if not enemy.get("visible") or not center:
        return {"available": False}
    dx = float(crosshair.get("x", 0.5)) - float(center.get("x", 0.5))
    dy = float(crosshair.get("y", 0.5)) - float(center.get("y", 0.5))
    distance = (dx * dx + dy * dy) ** 0.5
    ready = distance <= 0.055
    vertical = "high" if dy < -0.035 else "low" if dy > 0.035 else "aligned"
    horizontal = "left" if dx < -0.045 else "right" if dx > 0.045 else "centered"
    return {
        "available": True,
        "distance_norm": round(distance, 3),
        "ready": ready,
        "vertical_error": vertical,
        "horizontal_error": horizontal,
        "confidence": round(min(float(enemy.get("confidence") or 0), float(crosshair.get("confidence") or 0.3)), 2),
    }


def summarize_frame_classes(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    first_by_class: Dict[str, Any] = {}
    for row in timeline:
        cls = str(row.get("class") or "unknown")
        counts[cls] = counts.get(cls, 0) + 1
        first_by_class.setdefault(cls, {"frame": row.get("frame"), "timestamp": row.get("timestamp"), "relative_second": row.get("relative_second")})
    return {
        "counts": counts,
        "first_by_class": first_by_class,
        "summary": ", ".join(f"{key} x{value}" for key, value in sorted(counts.items())) or "no frames classified",
    }


def summarize_crosshair_to_contact(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    measurements = [row.get("crosshair_to_contact") for row in timeline if (row.get("crosshair_to_contact") or {}).get("available")]
    if not measurements:
        return {"available": False, "summary": "No enemy/contact proxy was strong enough for crosshair distance measurement."}
    avg_distance = sum(float(row.get("distance_norm") or 0) for row in measurements) / len(measurements)
    ready_frames = sum(1 for row in measurements if row.get("ready"))
    low_frames = sum(1 for row in measurements if row.get("vertical_error") == "low")
    risk = "ready" if ready_frames >= max(1, len(measurements) // 2) else "too low" if low_frames >= max(1, len(measurements) // 2) else "off target"
    return {
        "available": True,
        "frames_measured": len(measurements),
        "ready_frames": ready_frames,
        "average_distance_norm": round(avg_distance, 3),
        "risk": risk,
        "summary": f"Crosshair-to-contact proxy: {risk}; average normalized distance {avg_distance:.3f}.",
    }


def first_signal_frame(timeline: List[Dict[str, Any]], key: str, threshold: float) -> Dict[str, Any]:
    for row in timeline:
        if float(row.get(key) or 0) >= threshold:
            return signal_frame(row, key)
    return {}


def best_signal_frame(timeline: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    if not timeline:
        return {}
    best = max(timeline, key=lambda row: float(row.get(key) or 0))
    return signal_frame(best, key) if float(best.get(key) or 0) > 0 else {}


def signal_frame(row: Dict[str, Any], key: str) -> Dict[str, Any]:
    return {
        "frame": row.get("frame"),
        "timestamp": row.get("timestamp"),
        "relative_second": row.get("relative_second"),
        "score": row.get(key),
        "reason": row.get("reason") or "",
    }


def score_clip_crosshair(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not timeline:
        return {"score": 0, "risk": "unknown", "summary": "No frames."}
    avg_activity = sum(float(row.get("crosshair_activity") or 0) for row in timeline) / len(timeline)
    avg_drift = sum(float(row.get("crosshair_drift") or 0) for row in timeline) / len(timeline)
    contact_rows = [row for row in timeline if float(row.get("contact_score") or 0) >= 0.38]
    contact_drift = sum(float(row.get("crosshair_drift") or 0) for row in contact_rows) / max(1, len(contact_rows))
    correction_load = avg_activity * 4.0 + avg_drift * 2.0 + contact_drift * 2.6
    score = round(max(0.0, min(100.0, 100.0 - correction_load * 100.0)))
    risk = "late correction" if contact_drift > 0.10 else "unstable" if score < 55 else "mixed" if score < 72 else "stable"
    return {
        "score": score,
        "risk": risk,
        "average_activity": round(avg_activity, 3),
        "average_drift": round(avg_drift, 3),
        "contact_drift": round(contact_drift, 3),
        "summary": f"Crosshair {risk}; score {score}/100.",
    }


def score_clip_minimap(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not timeline:
        return {"risk": "unknown", "summary": "No frames."}
    avg_motion = sum(float(row.get("minimap_motion") or 0) for row in timeline) / len(timeline)
    pressure_overlap = sum(1 for row in timeline if float(row.get("minimap_motion") or 0) > 0.08 and float(row.get("contact_score") or 0) > 0.35)
    contact_frames = [row for row in timeline if str(row.get("class") or "").endswith("contact_proxy")]
    pre_contact_motion = [
        float(row.get("minimap_motion") or 0)
        for row in timeline
        if row.get("relative_second") is not None and float(row.get("relative_second") or 0) <= -1.0
    ]
    avg_pre_contact_motion = sum(pre_contact_motion) / max(1, len(pre_contact_motion))
    risk = "timing cue before contact" if avg_pre_contact_motion > 0.10 and contact_frames else "timing cue" if pressure_overlap >= 2 else "rotation activity" if avg_motion > 0.10 else "low signal"
    interpretation = []
    if avg_pre_contact_motion > 0.10:
        interpretation.append("minimap changed before contact; verify rotations/reveals before judging the duel")
    if pressure_overlap:
        interpretation.append("minimap activity overlaps pressure frames")
    if not interpretation:
        interpretation.append("no strong minimap semantic cue")
    return {
        "risk": risk,
        "average_motion": round(avg_motion, 3),
        "pre_contact_motion": round(avg_pre_contact_motion, 3),
        "pressure_overlap_frames": pressure_overlap,
        "semantic_events": interpretation,
        "summary": f"Minimap {risk}; {pressure_overlap} pressure-overlap frame(s).",
    }


def build_enemy_visibility_proxy(timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for row in timeline:
        score = float(row.get("contact_score") or 0)
        if score < 0.34:
            continue
        rows.append(
            {
                "frame": row.get("frame"),
                "timestamp": row.get("timestamp"),
                "relative_second": row.get("relative_second"),
                "visibility_score": round(score, 3),
                "classification": "likely contact cue" if score >= 0.48 else "possible contact cue",
                "reason": row.get("reason") or "",
            }
        )
    return rows[:24]


def movement_read(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not timeline:
        return {"risk": "unknown", "summary": "No frames."}
    avg_motion = sum(float(row.get("motion") or 0) for row in timeline) / len(timeline)
    contact_motion = [float(row.get("motion") or 0) for row in timeline if float(row.get("contact_score") or 0) >= 0.38]
    avg_contact_motion = sum(contact_motion) / max(1, len(contact_motion))
    risk = "moving during contact" if avg_contact_motion > 0.16 else "high movement" if avg_motion > 0.18 else "stable"
    return {
        "risk": risk,
        "average_motion": round(avg_motion, 3),
        "contact_motion": round(avg_contact_motion, 3),
        "summary": f"Movement {risk}; contact motion {avg_contact_motion:.2f}.",
    }


def visual_signal_summary(first_contact: Dict[str, Any], death_cue: Dict[str, Any], crosshair: Dict[str, Any], minimap: Dict[str, Any]) -> str:
    parts = []
    if first_contact:
        parts.append(f"first contact proxy frame {first_contact.get('frame')}")
    if death_cue:
        parts.append(f"death cue frame {death_cue.get('frame')}")
    parts.append(crosshair.get("summary") or "crosshair unavailable")
    parts.append(minimap.get("summary") or "minimap unavailable")
    return "Deterministic visual signals: " + "; ".join(parts)


def select_ocr_frames(frame_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(frame_rows) <= 8:
        return frame_rows
    scored = sorted(
        frame_rows,
        key=lambda row: (
            abs(float(frame_relative_second(row["frame"]) or 0.0)),
            -(row["frame"].get("sequence_index") or row["frame"].get("index") or 0),
        ),
    )
    selected = scored[:8]
    selected.sort(key=lambda row: row["frame"].get("sequence_index") or row["frame"].get("index") or 0)
    return selected


def preprocess_general_ocr_crop(image: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    gray = gray.resize((max(1, gray.width * 3), max(1, gray.height * 3)))
    return gray.filter(ImageFilter.SHARPEN)


def classify_ocr_region_text(region: str, text: str) -> str:
    lower = str(text or "").lower()
    if region == "killfeed" or any(token in lower for token in ("killed", "headshot", "combat", "damage")):
        return "combat_text"
    if region == "hud_top" or re.search(r"\b\d{1,2}\s*[-:]\s*\d{1,2}\b", lower):
        return "score_or_round"
    if region == "hud_bottom":
        return "weapon_health_hud"
    if region == "minimap":
        return "location_or_minimap_text"
    return "visible_text"


def summarize_clip_ocr_reads(reads: List[Dict[str, Any]]) -> Dict[str, Any]:
    score_text = [row for row in reads if row.get("kind") == "score_or_round"]
    combat_text = [row for row in reads if row.get("kind") == "combat_text"]
    weapon_text = [row for row in reads if row.get("kind") == "weapon_health_hud"]
    all_text = " ".join(str(row.get("text") or "") for row in reads)
    parsed_hud = parse_hud_text_hints(all_text)
    return {
        "score_or_round_reads": [row.get("text") for row in score_text[:5]],
        "combat_reads": [row.get("text") for row in combat_text[:5]],
        "weapon_health_reads": [row.get("text") for row in weapon_text[:5]],
        "parsed_hud": parsed_hud,
        "parsed": parsed_hud,
        "summary": hud_parse_summary(parsed_hud),
        "readable_regions": sorted({str(row.get("region")) for row in reads if row.get("region")}),
    }


def hud_parse_summary(parsed_hud: Dict[str, Any]) -> str:
    parts = []
    score = parsed_hud.get("score") or {}
    if score:
        parts.append(f"score {score.get('left')}-{score.get('right')}")
    if parsed_hud.get("round_number_from_score"):
        parts.append(f"round {parsed_hud.get('round_number_from_score')}")
    if parsed_hud.get("round_timer"):
        parts.append(f"timer {parsed_hud.get('round_timer')}")
    if parsed_hud.get("health") is not None:
        parts.append(f"HP {parsed_hud.get('health')}")
    ammo = parsed_hud.get("ammo") or {}
    if ammo:
        parts.append(f"ammo {ammo.get('magazine')}/{ammo.get('reserve')}")
    if parsed_hud.get("weapon"):
        parts.append(f"weapon {parsed_hud.get('weapon')}")
    if parsed_hud.get("spike_state"):
        parts.append(f"spike {parsed_hud.get('spike_state')}")
    return ", ".join(parts) if parts else "No structured HUD values parsed."


def parse_hud_text_hints(text: str) -> Dict[str, Any]:
    value = str(text or "")
    lower = value.lower()
    score = re.search(r"\b(\d{1,2})\s*[-:]\s*(\d{1,2})\b", value)
    timer = re.search(r"\b([0-1]?\d):([0-5]\d)\b", value)
    hp = re.search(r"\b(?:hp|health)?\s*(100|[1-9]\d?)\b", lower)
    ammo = re.search(r"\b(\d{1,2})\s*/\s*(\d{1,3})\b", value)
    spike_state = ""
    if "spike planted" in lower or "planted" in lower:
        spike_state = "planted"
    elif "spike dropped" in lower or "dropped" in lower:
        spike_state = "dropped"
    elif "spike" in lower:
        spike_state = "spike visible"
    weapon = ""
    for name in ("vandal", "phantom", "operator", "outlaw", "sheriff", "ghost", "spectre", "guardian", "bulldog", "marshal", "odin", "ares", "judge", "bucky", "classic", "frenzy", "shorty", "stinger"):
        if name in lower:
            weapon = name.title()
            break
    return {
        "score": {"left": int(score.group(1)), "right": int(score.group(2))} if score else {},
        "round_number_from_score": int(score.group(1)) + int(score.group(2)) + 1 if score else None,
        "round_timer": f"{timer.group(1)}:{timer.group(2)}" if timer else "",
        "health": int(hp.group(1)) if hp else None,
        "ammo": {"magazine": int(ammo.group(1)), "reserve": int(ammo.group(2))} if ammo else {},
        "weapon": weapon,
        "spike_state": spike_state,
    }


def render_context_extraction_prompt(db: Database, death: Dict[str, Any], vocabulary: Dict[str, Any]) -> str:
    known = build_known_game_context(db, death)
    vocab = compact_vocabulary_for_prompt(vocabulary)
    return (
        "Extract VALORANT match context from this ordered frame sequence before a death. "
        "Use the vocabulary to constrain names and fix OCR confusion. Choose map, agent, weapon, role, and location only from the vocabulary when possible. "
        "Read visible HUD/scoreboard text if present: top score, round number if visible, team alive counts, spike state, weapon, HP, and location label. "
        "Do not invent facts. If a value is not visible or not strongly implied by visible text/HUD, leave it null or empty and lower confidence. "
        "Return one strict JSON object using this schema:\n"
        + json.dumps(context_extraction_schema(), indent=2)
        + "\n\nKnown app context that may be incomplete:\n"
        + known
        + "\n\nAllowed VALORANT vocabulary:\n"
        + json.dumps(vocab, indent=2)
    )


def run_chunked_context_extraction(request: Dict[str, Any], status: Dict[str, Any], chunk_size: int = 30) -> Dict[str, Any]:
    frames = request.get("keyframes") or []
    chunks = [frames[index : index + chunk_size] for index in range(0, len(frames), chunk_size)]
    parsed_chunks = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_request = dict(request)
        chunk_request["keyframes"] = chunk
        chunk_request["prompt"] = (
            request["prompt"]
            + f"\n\nThis is context extraction batch {index}/{len(chunks)} covering {batch_frame_range(chunk)}. "
            "Return only context visible in this batch. Do not infer from other batches."
        )
        if status["provider"] == "custom-command":
            text = run_custom_model_text(status, chunk_request, timeout=90)
            provider = "local-command"
        else:
            text = run_local_http_text(chunk_request, status, local_context_system_prompt(), max_tokens=700, timeout=180)
            provider = status["provider"]
        parsed = parse_context_extraction(text, provider)
        parsed["batch_index"] = index
        parsed["frame_range"] = batch_frame_range(chunk)
        parsed_chunks.append(parsed)
    return merge_context_extraction_chunks(parsed_chunks, status["provider"])


def merge_context_extraction_chunks(chunks: List[Dict[str, Any]], provider: str) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "kind": "context_extraction",
        "provider": provider,
        "map_candidates": [],
        "agent_candidates": [],
        "weapon_candidates": [],
        "location_candidates": [],
        "visible_text": [],
        "unknown_fields": [],
        "chunk_count": len(chunks),
        "chunks": [],
    }
    best_scalars: Dict[str, Dict[str, Any]] = {}
    for chunk in chunks:
        merged["chunks"].append({"batch_index": chunk.get("batch_index"), "frame_range": chunk.get("frame_range"), "visible_text": chunk.get("visible_text") or []})
        for key in ("map_candidates", "agent_candidates", "weapon_candidates", "location_candidates"):
            merged[key].extend(normalize_candidates(chunk.get(key)))
        for key in ("round_number", "side", "spike_state", "team_counts", "round_score"):
            candidate = chunk.get(key)
            confidence = candidate_confidence(candidate)
            if confidence > candidate_confidence(best_scalars.get(key)):
                best_scalars[key] = candidate
        merged["visible_text"].extend(normalize_text_list(chunk.get("visible_text") or []))
        merged["unknown_fields"].extend(normalize_text_list(chunk.get("unknown_fields") or []))
    for key, value in best_scalars.items():
        merged[key] = value
    merged["visible_text"] = merged["visible_text"][:20]
    merged["unknown_fields"] = sorted(set(merged["unknown_fields"]))[:12]
    return merged


def candidate_confidence(value: Any) -> float:
    if isinstance(value, dict):
        return normalize_confidence(value.get("confidence", 0.0))
    return 0.0


def compact_vocabulary_for_prompt(vocabulary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "maps": [item.get("name") for item in vocabulary.get("maps") or [] if item.get("name")],
        "agents": [
            {"name": item.get("name"), "role": item.get("role"), "abilities": item.get("abilities") or []}
            for item in vocabulary.get("agents") or []
            if item.get("name")
        ],
        "weapons": [item.get("name") for item in vocabulary.get("weapons") or [] if item.get("name")],
        "roles": vocabulary.get("roles") or [],
        "callouts": (vocabulary.get("callouts") or [])[:160],
        "hud_terms": vocabulary.get("hud_terms") or [],
    }


def context_extraction_schema() -> Dict[str, Any]:
    candidate = [{"value": "canonical value or null", "confidence": 0.0, "evidence": "visible frame/timing evidence"}]
    return {
        "map_candidates": candidate,
        "agent_candidates": candidate,
        "weapon_candidates": candidate,
        "location_candidates": candidate,
        "round_score": {"ally": None, "enemy": None, "confidence": 0.0, "evidence": ""},
        "round_number": {"value": None, "confidence": 0.0, "evidence": ""},
        "side": {"value": "attack/defense/unknown", "confidence": 0.0, "evidence": ""},
        "spike_state": {"value": "pre-plant/planted/dropped/carried/unknown", "confidence": 0.0, "evidence": ""},
        "team_counts": {"value": "for example 3v4 or empty", "confidence": 0.0, "evidence": ""},
        "visible_text": ["short OCR snippets with frame references"],
        "unknown_fields": ["fields that could not be read"],
    }


def local_context_system_prompt() -> str:
    return (
        "You are a VALORANT HUD/OCR context extractor. "
        "Return strict compact JSON only. Use the provided VALORANT vocabulary as the allowed list for map, agent, weapon, and callout names. "
        "Visible frame evidence is required for every confident field. Do not provide coaching advice."
    )


def run_custom_model_text(status: Dict[str, Any], request: Dict[str, Any], timeout: int = 90) -> str:
    completed = subprocess.run(
        status["command"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Local AI command returned an error.")
    return completed.stdout or "{}"


def budget_local_model_payload(
    payload: Dict[str, Any],
    status: Dict[str, Any],
    system_prompt: str,
    max_tokens: int,
    stage: str,
) -> tuple:
    context_limit = int(normalize_context_limit_setting(status.get("context_limit")))
    image_cost = int(normalize_image_token_estimate_setting(status.get("image_token_estimate")))
    output_reserve = max(256, int(max_tokens or 0))
    safety_reserve = 512
    usable = max(512, context_limit - output_reserve - safety_reserve)
    prompt = str(payload.get("prompt") or "")
    frames = list(payload.get("keyframes") or [])
    image_heavy = str(stage or "").lower() in {"clip_review", "enemy_contact", "crosshair_mechanics", "positioning_context"}
    if image_heavy and frames:
        # Dense clip review fails badly when a long KB/schema prompt leaves room
        # for only the last few images. Keep the prompt compact and preserve a
        # representative visual timeline.
        max_prompt_tokens = min(1800, max(900, int(usable * 0.28)))
    else:
        max_prompt_tokens = min(3200, max(900, int(usable * 0.48)))
    prompt = compact_text_for_token_budget(prompt, max_prompt_tokens)
    fixed_tokens = estimate_text_tokens(system_prompt) + estimate_text_tokens(prompt)
    remaining = usable - fixed_tokens
    if frames:
        max_frames = max(1, remaining // max(1, image_cost))
        budgeted_frames = select_frames_for_context_budget(frames, int(max_frames), stage)
        caption_tokens = estimate_text_tokens("\n".join(str(item.get("caption") or "") for item in budgeted_frames))
        while len(budgeted_frames) > 1 and fixed_tokens + caption_tokens + len(budgeted_frames) * image_cost > usable:
            budgeted_frames = select_frames_for_context_budget(budgeted_frames, len(budgeted_frames) - 1, stage)
            caption_tokens = estimate_text_tokens("\n".join(str(item.get("caption") or "") for item in budgeted_frames))
    else:
        max_frames = 0
        budgeted_frames = []
        caption_tokens = 0
    budgeted = dict(payload)
    budgeted["prompt"] = prompt
    budgeted["keyframes"] = budgeted_frames
    budget = {
        "context_limit": context_limit,
        "max_tokens": max_tokens,
        "safety_reserve": safety_reserve,
        "usable_input_tokens": usable,
        "estimated_text_tokens": estimate_text_tokens(system_prompt) + estimate_text_tokens(prompt),
        "estimated_caption_tokens": caption_tokens,
        "image_token_estimate": image_cost,
        "original_frames": len(frames),
        "sent_frames": len(budgeted_frames),
        "dropped_frames": max(0, len(frames) - len(budgeted_frames)),
        "first_sent_frame": frame_audit_ref(budgeted_frames[0]) if budgeted_frames else None,
        "last_sent_frame": frame_audit_ref(budgeted_frames[-1]) if budgeted_frames else None,
        "sent_frame_range": batch_frame_range(budgeted_frames) if budgeted_frames else "no frames",
        "estimated_total_tokens": estimate_text_tokens(system_prompt) + estimate_text_tokens(prompt) + estimate_text_tokens("\n".join(str(item.get("caption") or "") for item in budgeted_frames)) + len(budgeted_frames) * image_cost + output_reserve + safety_reserve,
        "stage": stage,
        "trimmed": len(budgeted_frames) < len(frames) or prompt != str(payload.get("prompt") or ""),
    }
    return budgeted, budget


def frame_audit_ref(frame: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": frame.get("sequence_index") or frame.get("index"),
        "relative_second": frame.get("relative_second"),
        "seconds_before_death": frame.get("seconds_before_death"),
        "timestamp": frame.get("timestamp"),
        "role": frame.get("role"),
    }


def estimate_text_tokens(text: Any) -> int:
    value = str(text or "")
    if not value:
        return 0
    return max(1, math.ceil(len(value) / 4))


def compact_text_for_token_budget(text: str, max_tokens: int) -> str:
    value = str(text or "")
    max_chars = max(400, int(max_tokens) * 4)
    if len(value) <= max_chars:
        return value
    head_chars = int(max_chars * 0.62)
    tail_chars = max_chars - head_chars - 160
    return (
        value[:head_chars].rstrip()
        + "\n\n[Context trimmed to fit the local model context window. Use visible frame evidence over omitted prompt context.]\n\n"
        + value[-tail_chars:].lstrip()
    )


def select_frames_for_context_budget(frames: List[Dict[str, Any]], limit: int, stage: str) -> List[Dict[str, Any]]:
    if limit >= len(frames):
        return frames
    if limit <= 0:
        return []
    if limit == 1:
        return [frames[-1]]
    stage_text = str(stage or "").lower()
    if any(token in stage_text for token in ("clip_review", "enemy_contact", "crosshair", "positioning", "coach")):
        return select_representative_clip_frames(frames, limit)
    if any(token in stage_text for token in ("contact", "death")):
        return select_representative_clip_frames(frames, limit)
    if limit <= 4:
        picks = [0, len(frames) // 2, len(frames) - 1]
        unique = []
        for index in picks:
            if index not in unique:
                unique.append(index)
        return [frames[index] for index in unique[:limit]]
    step = max(1, len(frames) // limit)
    selected = frames[::step][: limit - 1]
    if frames[-1] not in selected:
        selected.append(frames[-1])
    return selected[:limit]


def select_representative_clip_frames(frames: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if limit >= len(frames):
        return frames
    scored_indices = []
    for index, frame in enumerate(frames):
        rel = frame_relative_second(frame)
        score = 0.0
        if rel is not None:
            distance = abs(float(rel))
            score += max(0.0, 8.0 - distance)
            if -2.5 <= float(rel) <= 0.35:
                score += 5.0
            if -5.0 <= float(rel) < -2.5:
                score += 2.0
        metrics = frame.get("metrics") or {}
        score += float(metrics.get("death_score") or 0) * 4.0
        score += float(metrics.get("pressure_score") or 0) * 3.0
        score += float(metrics.get("crosshair_activity") or 0) * 2.0
        scored_indices.append((score, index))
    keep = {0, len(frames) - 1}
    for _, index in sorted(scored_indices, reverse=True):
        keep.add(index)
        if len(keep) >= limit:
            break
    if len(keep) < limit:
        step = max(1, len(frames) / float(limit))
        for item in range(limit):
            keep.add(min(len(frames) - 1, int(round(item * step))))
            if len(keep) >= limit:
                break
    return [frames[index] for index in sorted(keep)[:limit]]


def run_local_http_text(
    payload: Dict[str, Any],
    status: Dict[str, Any],
    system_prompt: str,
    max_tokens: int = 900,
    timeout: int = 180,
) -> str:
    provider = status["provider"]
    payload, budget = budget_local_model_payload(payload, status, system_prompt, max_tokens, "context_extraction")
    images = [item["image_base64"] for item in payload.get("keyframes") or [] if item.get("image_base64")]
    prompt = payload["prompt"]
    if provider == "ollama":
        endpoint = status["base_url"].rstrip("/") + "/api/generate"
        manifest = "\n".join(item.get("caption") or "" for item in payload.get("keyframes") or [])
        body = {"model": status["model"], "prompt": system_prompt + "\n\n" + prompt + "\n\nOrdered frames:\n" + manifest, "stream": False}
        if images:
            body["images"] = images
    else:
        endpoint = status["base_url"].rstrip("/") + "/chat/completions"
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in payload.get("keyframes") or []:
            caption = item.get("caption") or f"Frame {item.get('index') or ''}".strip()
            content.append({"type": "text", "text": caption})
            if item.get("image_base64"):
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{item['image_base64']}"}})
        body = {
            "model": status["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "temperature": 0.05,
            "max_tokens": max_tokens,
        }
    response = post_json(endpoint, body, timeout=timeout)
    return extract_model_response_text(response)


def parse_context_extraction(text: str, provider: str) -> Dict[str, Any]:
    raw = text or ""
    try:
        parsed = json.loads(strip_json_fence(raw))
    except json.JSONDecodeError:
        parsed = {"visible_text": [raw[:500]], "unknown_fields": ["json_parse"]}
    if not isinstance(parsed, dict):
        parsed = {"visible_text": [str(parsed)[:500]], "unknown_fields": ["non_object_response"]}
    parsed["kind"] = "context_extraction"
    parsed["provider"] = provider
    parsed["raw_preview"] = raw[:800]
    return parsed


def resolve_context_extraction(
    db: Database,
    death_id: int,
    death: Dict[str, Any],
    extraction: Dict[str, Any],
    vocabulary: Dict[str, Any],
) -> Dict[str, Any]:
    existing = (db.get_latest_structured_analysis("death", death_id, "context_correction") or {}).get("payload") or {}
    manual_locked = bool(existing) and existing.get("source") != "context_extraction"
    resolved = {
        "map": resolve_candidate(extraction.get("map_candidates"), vocabulary, "maps"),
        "agent": resolve_candidate(extraction.get("agent_candidates"), vocabulary, "agents"),
        "weapon": resolve_candidate(extraction.get("weapon_candidates"), vocabulary, "weapons"),
        "location": resolve_location_candidate(extraction.get("location_candidates"), vocabulary),
        "round_number": resolve_round_number(extraction.get("round_number")),
        "side": resolve_scalar(extraction.get("side"), {"attack", "attacking", "defense", "defending"}, aliases={"attacking": "attack", "defending": "defense"}),
        "spike_state": resolve_scalar(extraction.get("spike_state"), {"pre-plant", "planted", "dropped", "carried", "unknown"}),
        "team_counts": resolve_team_counts(extraction.get("team_counts")),
        "round_score": resolve_round_score(extraction.get("round_score")),
    }
    if not resolved["round_number"].get("value"):
        resolved["round_number"] = infer_round_from_score(resolved.get("round_score") or {})
    auto_corrections = {}
    for key, value in resolved.items():
        if key == "round_score" or not value.get("value"):
            continue
        if manual_locked and existing.get(key):
            value["applied"] = False
            value["blocked_by_manual"] = True
            continue
        threshold = 0.72 if key in {"map", "agent", "weapon", "round_number"} else 0.65
        value["applied"] = float(value.get("confidence") or 0) >= threshold
        value["status"] = context_candidate_status(float(value.get("confidence") or 0), value["applied"])
        if value["applied"]:
            auto_corrections[key] = value["value"]
    for key, value in resolved.items():
        if isinstance(value, dict) and "status" not in value:
            value["status"] = context_candidate_status(float(value.get("confidence") or 0), bool(value.get("applied")))
    summary_bits = []
    for key in ("map", "agent", "round_number", "weapon", "location"):
        value = resolved.get(key) or {}
        if value.get("value"):
            summary_bits.append(f"{key}={value['value']} ({int(float(value.get('confidence') or 0) * 100)}%)")
    return {
        "kind": "context_extraction",
        "summary": "Context extraction: " + (", ".join(summary_bits) if summary_bits else "no high-confidence VALORANT context found"),
        "resolved": resolved,
        "auto_corrections": auto_corrections,
        "manual_locked": manual_locked,
        "visible_text": normalize_text_list(extraction.get("visible_text") or []),
        "unknown_fields": normalize_text_list(extraction.get("unknown_fields") or []),
        "raw_candidates": {
            "map_candidates": normalize_candidates(extraction.get("map_candidates")),
            "agent_candidates": normalize_candidates(extraction.get("agent_candidates")),
            "weapon_candidates": normalize_candidates(extraction.get("weapon_candidates")),
            "location_candidates": normalize_candidates(extraction.get("location_candidates")),
        },
        "round_score": resolved.get("round_score") or {},
        "status": "completed",
        "provider": extraction.get("provider") or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_auto_context_correction(db: Database, death_id: int, death: Dict[str, Any], extraction: Dict[str, Any]) -> None:
    corrections = extraction.get("auto_corrections") or {}
    if not corrections:
        return
    existing = (db.get_latest_structured_analysis("death", death_id, "context_correction") or {}).get("payload") or {}
    trusted_existing = {
        key: existing.get(key)
        for key in ("map", "agent", "side", "round_number", "weapon", "location", "spike_state", "team_counts")
        if existing.get(key)
    }
    source = "mixed" if trusted_existing and existing.get("source") != "context_extraction" else "context_extraction"
    context = {
        "kind": "context_correction",
        "source": source,
        "confidence": max(float((extraction.get("resolved") or {}).get(key, {}).get("confidence") or 0) for key in corrections),
        "notes": extraction.get("summary") or "Auto-filled from KB-constrained local context extraction.",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    context.update(trusted_existing)
    context.update(corrections)
    match = db.get_match(int(death.get("match_id") or 0)) if death.get("match_id") else None
    if match and (context.get("map") or context.get("agent")):
        updates = {}
        if context.get("map"):
            updates["map"] = context["map"]
        if context.get("agent"):
            updates["agent"] = context["agent"]
        db.update_match(int(match["id"]), **updates)
    if context.get("round_number"):
        db.update_death_round_number(death_id, int(context["round_number"]))
    db.save_death_analysis(death_id, "context_correction", context)


def context_extraction_fallback(message: str) -> Dict[str, Any]:
    return {
        "kind": "context_extraction",
        "summary": message,
        "resolved": {},
        "auto_corrections": {},
        "visible_text": [],
        "unknown_fields": ["context_extraction_failed"],
        "status": "failed",
        "confidence": 0.0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def default_base_url(provider: str) -> str:
    return {
        "ollama": "http://127.0.0.1:11434",
        "lmstudio": "http://127.0.0.1:1234/v1",
        "llamacpp": "http://127.0.0.1:8080",
    }.get(provider, "")


def default_model(provider: str) -> str:
    return {
        "ollama": "llava",
        "lmstudio": "local-model",
        "llamacpp": "local-model",
    }.get(provider, "")


def keyframe_payload(db: Database, death_id: int, analysis_type: str = "keyframes", limit: int = 6) -> List[Dict[str, Any]]:
    latest = db.get_latest_structured_analysis("death", death_id, analysis_type)
    frames = ((latest or {}).get("payload") or {}).get("frames") or []
    encoded = []
    for index, item in enumerate(frames[:limit], start=1):
        frame_id = str(item.get("frame_id") or "")
        path = find_frame_path(db.path.parent, frame_id)
        payload = {key: item.get(key) for key in ("role", "timestamp", "reason")}
        payload["index"] = index
        payload["sequence_index"] = item.get("sequence_index")
        payload["relative_second"] = item.get("relative_second")
        payload["seconds_before_death"] = item.get("seconds_before_death")
        payload["caption"] = keyframe_caption(payload)
        payload["frame_id"] = frame_id
        if path and path.exists():
            payload["image_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        encoded.append(payload)
    return encoded


def keyframe_caption(item: Dict[str, Any]) -> str:
    role = str(item.get("role") or "frame")
    ts = item.get("timestamp")
    rel = item.get("relative_second")
    pieces = [f"Frame {item.get('sequence_index') or item.get('index')}: {role}"]
    if rel is not None:
        pieces.append(f"relative {float(rel):+.2f}s in the review sequence")
    if item.get("seconds_before_death") is not None:
        pieces.append(f"{item.get('seconds_before_death')}s before death")
    if ts is not None:
        pieces.append(f"VOD timestamp {ts}s")
    if item.get("reason"):
        pieces.append(f"detector note: {item.get('reason')}")
    return ". ".join(pieces) + "."


def build_clip_segments(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    segments = [
        {"id": "setup", "label": "Setup", "range": (-999.0, -4.0), "purpose": "positioning, route, minimap/HUD, utility before contact"},
        {"id": "pre_contact", "label": "Pre-contact", "range": (-4.0, -1.5), "purpose": "angle clearing, crosshair readiness, movement before enemy appears"},
        {"id": "contact", "label": "First contact", "range": (-1.5, -0.5), "purpose": "enemy visibility, reaction, crosshair correction, first bullets"},
        {"id": "death", "label": "Death moment", "range": (-0.5, 0.2), "purpose": "final duel, damage/death cue, movement/shooting error"},
        {"id": "aftermath", "label": "Aftermath", "range": (0.2, 999.0), "purpose": "killfeed, scoreboard, death banner, context confirmation"},
    ]
    result = []
    for segment in segments:
        start, end = segment["range"]
        segment_frames = []
        for frame in frames:
            rel = frame_relative_second(frame)
            if rel is None:
                continue
            if start <= rel < end:
                segment_frames.append(frame_summary(frame))
        if segment_frames:
            result.append(
                {
                    "id": segment["id"],
                    "label": segment["label"],
                    "purpose": segment["purpose"],
                    "frame_count": len(segment_frames),
                    "frames": segment_frames,
                    "start_relative_second": segment_frames[0].get("relative_second"),
                    "end_relative_second": segment_frames[-1].get("relative_second"),
                }
            )
    if not result and frames:
        result.append(
            {
                "id": "clip",
                "label": "Clip",
                "purpose": "full ordered frame sequence",
                "frame_count": len(frames),
                "frames": [frame_summary(frame) for frame in frames],
            }
        )
    return result


def frame_relative_second(frame: Dict[str, Any]) -> Optional[float]:
    for key in ("relative_second",):
        if frame.get(key) is not None:
            try:
                return float(frame.get(key))
            except (TypeError, ValueError):
                return None
    if frame.get("seconds_before_death") is not None:
        try:
            return -float(frame.get("seconds_before_death"))
        except (TypeError, ValueError):
            return None
    return None


def frame_summary(frame: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "index": frame.get("sequence_index") or frame.get("index"),
        "relative_second": frame_relative_second(frame),
        "seconds_before_death": frame.get("seconds_before_death"),
        "timestamp": frame.get("timestamp"),
        "role": frame.get("role") or "frame",
        "caption": frame.get("caption") or "",
    }


def find_frame_path(data_dir: Path, frame_id: str) -> Optional[Path]:
    for root in (data_dir / "vision", data_dir / "deep"):
        matches = list(root.glob(f"**/{frame_id}.jpg"))
        if matches:
            return matches[0]
    return None


def render_model_prompt(db: Database, death: Dict[str, Any]) -> str:
    purpose = str(db.get_setting("local_ai_purpose", "coach") or "coach")
    if purpose == "ocr":
        return render_ocr_model_prompt(death)
    templates = prompt_templates(db)["templates"]
    key = str(db.get_setting("active_prompt_template", "default") or "default")
    template = templates.get(key) or templates["default"]
    sequence_profile = local_ai_sequence_profile(
        str(db.get_setting("local_ai_review_mode", "contact") or "contact"),
        db.get_setting("local_ai_review_fps", ""),
        db.get_setting("local_ai_review_window_seconds", "10"),
    )
    labels = ", ".join(death.get("mistake_labels") or [])
    base = template["prompt"].format(
        round=death_round_label(death),
        timestamp=death.get("timestamp") or "?",
        labels=labels or "unlabeled",
        notes=death.get("notes") or "",
    )
    return (
        base
        + "\n\n"
        + build_known_game_context(db, death)
        + "\n\n"
        + build_extracted_context_prompt(db, death)
        + "\n\n"
        + build_clip_segment_prompt(db, death)
        + "\n\n"
        + build_deterministic_signal_prompt(db, death)
        + "\n\n"
        + build_agent_coaching_prompt(db, death)
        + "\n\n"
        + build_knowledge_prompt_context(db, death)
        + "\n\n"
        + build_memory_prompt_context(db)
        + "\n\nReturn one strict JSON object using this schema:\n"
        + json.dumps(local_ai_review_schema(), indent=2)
        + f"\n\nYou will receive an ordered frame sequence using this sampling mode: {sequence_profile['label']}. "
        "Treat the images as a short local video clip in chronological order. First analyze each named segment separately, then synthesize one coach read. Track crosshair movement, clearing path, movement while aiming, minimap/HUD changes, and fight setup over time. "
        "Enemies can appear for only one or two frames, so scan every frame for a visible opponent, damage cue, tracer, muzzle flash, or sudden contact. "
        "Every coaching claim must cite a segment and frame/timing evidence. Use only visible evidence from those frames. Do not assume enemy positions, player intent, comms, utility usage, or the outcome unless visible. "
        "If enemy/contact is not visible, say that specifically but still coach visible crosshair, movement, angle exposure, HUD/minimap, and timing evidence. "
        "Use 'insufficient visual evidence' only for a specific field or segment that cannot be read; do not make it the whole review unless every frame is blank, unrelated, or post-death only. "
        "Separate perception from coaching: perception must describe only what is visible, and coaching must explain the decision error and action."
    )


def build_known_game_context(db: Database, death: Dict[str, Any]) -> str:
    match = db.get_match(int(death.get("match_id") or 0)) if death.get("match_id") else None
    correction_row = db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_correction") if death.get("id") else None
    correction = (correction_row or {}).get("payload") or {}
    annotations = death.get("annotations") or []
    annotation_bits = []
    for row in annotations[:3]:
        payload = row.get("payload") or {}
        if payload.get("better_decision"):
            annotation_bits.append(str(payload.get("better_decision")))
    context = {
        "map": correction.get("map") or (match or {}).get("map") or "unknown",
        "agent": correction.get("agent") or (match or {}).get("agent") or "unknown",
        "round": correction.get("round_number") or death.get("round_number") or "unknown",
        "side": correction.get("side") or "unknown",
        "weapon": correction.get("weapon") or "unknown",
        "location": correction.get("location") or "unknown",
        "spike_state": correction.get("spike_state") or "unknown",
        "timestamp_seconds": death.get("timestamp") or "unknown",
        "existing_labels": death.get("mistake_labels") or [],
        "marker_notes": death.get("notes") or "",
        "saved_user_annotations": annotation_bits,
        "hud_fields_to_extract_if_visible": ["hp", "weapon", "score", "teammates_alive", "spike_state", "round_timer"],
    }
    return "Known match context:\n" + json.dumps(context, indent=2)


def build_extracted_context_prompt(db: Database, death: Dict[str, Any]) -> str:
    if not death.get("id"):
        return "Auto-extracted match context: none."
    row = db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_extraction")
    payload = (row or {}).get("payload") or {}
    if not payload:
        return "Auto-extracted match context: none."
    compact = {
        "summary": payload.get("summary") or "",
        "resolved": payload.get("resolved") or {},
        "visible_text": (payload.get("visible_text") or [])[:8],
        "unknown_fields": payload.get("unknown_fields") or [],
        "note": "Use auto-extracted values as context only. Manual/known context wins when there is a conflict.",
    }
    return "Auto-extracted match context from local OCR/vision pass:\n" + json.dumps(compact, indent=2)[:1400]


def build_clip_segment_prompt(db: Database, death: Dict[str, Any]) -> str:
    if not death.get("id"):
        return "Clip segments: unavailable."
    frames = keyframe_payload(db, int(death["id"]), analysis_type="local_ai_sequence", limit=120)
    segments = build_clip_segments(frames)
    compact = [
        {
            "id": segment.get("id"),
            "label": segment.get("label"),
            "purpose": segment.get("purpose"),
            "frames": [
                {
                    "index": frame.get("index"),
                    "relative_second": frame.get("relative_second"),
                    "seconds_before_death": frame.get("seconds_before_death"),
                    "role": frame.get("role"),
                }
                for frame in (segment.get("frames") or [])[:18]
            ],
        }
        for segment in segments
    ]
    return "Clip review segments. Use these segment ids in segment_reviews and evidence_timeline:\n" + json.dumps(compact, indent=2)[:1800]


def build_deterministic_signal_prompt(db: Database, death: Dict[str, Any]) -> str:
    if not death.get("id"):
        return "Deterministic local visual signals: unavailable."
    death_id = int(death["id"])
    visual = (db.get_latest_structured_analysis("death", death_id, "clip_visual_signals") or {}).get("payload") or {}
    ocr = (db.get_latest_structured_analysis("death", death_id, "clip_ocr_regions") or {}).get("payload") or {}
    feedback = clip_review_feedback_summary(db, death_id)
    compact = {
        "visual_signals": {
            "summary": visual.get("summary") or "",
            "first_contact": visual.get("first_contact") or {},
            "death_cue": visual.get("death_cue") or {},
            "crosshair_score": visual.get("crosshair_score") or {},
            "movement_read": visual.get("movement_read") or {},
            "minimap_read": visual.get("minimap_read") or {},
            "enemy_visibility_timeline": (visual.get("enemy_visibility_timeline") or [])[:8],
        },
        "ocr_regions": {
            "summary": ocr.get("summary") or "",
            "structured": ocr.get("structured") or {},
            "reads": (ocr.get("reads") or [])[:8],
        },
        "past_review_feedback_for_this_marker": feedback,
        "instruction": "Treat deterministic signals as local measurements, not final coaching. Use them to verify or question what the frame images appear to show.",
    }
    return "Deterministic local visual/OCR signals:\n" + json.dumps(compact, indent=2)[:2400]


def build_agent_coaching_prompt(db: Database, death: Dict[str, Any]) -> str:
    match = db.get_match(int(death.get("match_id") or 0)) if death.get("match_id") else {}
    correction = (db.get_latest_structured_analysis("death", int(death.get("id") or 0), "context_correction") or {}).get("payload") or {}
    agent = str(correction.get("agent") or (match or {}).get("agent") or "").strip()
    rules = agent_coaching_rules(agent)
    return "Agent/role-specific coaching constraints:\n" + json.dumps({"agent": agent or "unknown", **rules}, indent=2)


def agent_coaching_rules(agent: str) -> Dict[str, Any]:
    lower = str(agent or "").lower()
    if lower in {"jett", "raze", "neon", "reyna", "phoenix", "yoru", "iso"}:
        return {
            "role": "Duelist",
            "checklist": [
                "Was the first contact planned with trade timing or escape?",
                "Did the crosshair arrive before the body committed?",
                "Was the entry path clearing one angle at a time?",
            ],
            "coach_bias": "Prioritize entry timing, escape route, pre-aim, and whether the duel was tradeable.",
        }
    if lower in {"omen", "brimstone", "viper", "astra", "harbor", "clove"}:
        return {
            "role": "Controller",
            "checklist": [
                "Were smokes placed before crossing or taking space?",
                "Was the rotate safe relative to minimap pressure?",
                "Did the player take a dry fight while utility could change the angle?",
            ],
            "coach_bias": "Prioritize smoke timing, map control, rotate safety, and utility before contact.",
        }
    if lower in {"sova", "fade", "skye", "breach", "kayo", "kay/o", "gekko", "tejo"}:
        return {
            "role": "Initiator",
            "checklist": [
                "Was info/flash/stun utility available before first contact?",
                "Was teammate spacing close enough to convert utility into a trade?",
                "Did the player fight before information was gathered?",
            ],
            "coach_bias": "Prioritize info utility, flash/stun timing, and teammate conversion.",
        }
    if lower in {"cypher", "killjoy", "sage", "chamber", "deadlock", "vyse"}:
        return {
            "role": "Sentinel",
            "checklist": [
                "Was the death caused by an unnecessary re-peek after info?",
                "Was the player anchored around utility or exposed away from it?",
                "Could the player have delayed instead of taking contact?",
            ],
            "coach_bias": "Prioritize anchor discipline, utility contact, delay value, and safe re-peeks.",
        }
    return {
        "role": "Unknown",
        "checklist": [
            "Was first contact supported by info, utility, or trade timing?",
            "Was crosshair placement ready before contact?",
            "Was the player exposed to multiple angles?",
        ],
        "coach_bias": "Use general VALORANT fundamentals: angle isolation, crosshair readiness, trade spacing, utility timing.",
    }


def render_ocr_model_prompt(death: Dict[str, Any]) -> str:
    labels = ", ".join(death.get("mistake_labels") or []) or "unlabeled"
    return (
        "You are reading an ordered VALORANT VOD frame sequence as a local OCR/HUD extraction model. "
        "Focus on visible text and HUD elements only. Do not invent gameplay coaching. "
        "Return strict JSON with keys: summary, extracted_text, scoreboard, labels, better_play, confidence. "
        "scoreboard should include left_score, right_score, inferred_round if visible. "
        f"Marker: {death_round_label(death)} at {death.get('timestamp') or '?'} seconds. "
        f"Current labels: {labels}. Notes: {death.get('notes') or ''}. "
        "If text is unreadable, say unreadable and set confidence below 0.4."
    )


def death_round_label(death: Dict[str, Any]) -> str:
    return f"Round {death.get('round_number')}" if death.get("round_number") else "Round unknown"


def run_local_http_review(db: Database, death_id: int, payload: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    keyframes = payload.get("keyframes") or []
    review_mode = str(status.get("review_mode") or "")
    if status.get("purpose") != "ocr" and review_mode in {"adaptive", "hybrid"}:
        return run_local_http_review_multipass(db, death_id, payload, status)
    if len(keyframes) > 30:
        return run_local_http_review_batched(db, death_id, payload, status, chunk_size=30)
    return run_local_http_review_single(db, death_id, payload, status)


def run_local_http_review_multipass(db: Database, death_id: int, payload: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    frames = payload.get("keyframes") or []
    if not frames:
        return run_local_http_review_single(db, death_id, payload, status)
    passes = [
        {
            "id": "enemy_contact",
            "label": "Enemy/contact visibility",
            "frames": select_frames_for_pass(frames, "contact"),
            "instruction": (
                "Pass 1: scan every provided frame for enemies, enemy outline, weapon, tracer, muzzle flash, damage cue, killfeed, and first contact. "
                "Do not coach yet. Return strict JSON with perception.enemy_seen, perception.enemy_frames, first_contact_time, visible_evidence, evidence_timeline, claim_confidence.enemy_contact."
            ),
        },
        {
            "id": "crosshair_mechanics",
            "label": "Crosshair/mechanics",
            "frames": select_frames_for_pass(frames, "crosshair"),
            "instruction": (
                "Pass 2: judge crosshair readiness over time. Track whether the crosshair is already on likely head height before contact or corrects late. "
                "Return strict JSON with crosshair_level, crosshair_alignment, movement_state, visible_evidence, evidence_timeline, and any crosshair_issue."
            ),
        },
        {
            "id": "positioning_context",
            "label": "Positioning/minimap/HUD",
            "frames": select_frames_for_pass(frames, "context"),
            "instruction": (
                "Pass 3: read positioning, angle exposure, minimap/HUD/timer/spike cues, tradeability, and utility evidence. "
                "Return strict JSON with positioning_issue, utility_issue, score/hp/weapon/spike if visible, visible_evidence, and confidence."
            ),
        },
    ]
    pass_reviews = []
    for review_pass in passes:
        pass_payload = dict(payload)
        pass_payload["keyframes"] = review_pass["frames"]
        pass_payload["prompt"] = (
            payload["prompt"]
            + "\n\n"
            + review_pass["instruction"]
            + "\nThis is a specialized local vision pass. Cite frame numbers and visible evidence. If enemy/contact is not visible, say that specifically and still report any readable crosshair, movement, HUD, minimap, or post-death evidence."
        )
        response = run_local_http_review_single(db, death_id, pass_payload, status, save=False, stage=review_pass["id"])
        if not response.get("ok"):
            return response
        review = response.get("analysis") or {}
        review["pass_id"] = review_pass["id"]
        review["pass_label"] = review_pass["label"]
        review["frame_range"] = batch_frame_range(review_pass["frames"])
        pass_reviews.append(review)
    combined = synthesize_multipass_reviews(db, death_id, payload, status, pass_reviews)
    combined["multi_pass_reviews"] = pass_reviews
    combined["multi_pass"] = {"enabled": True, "pass_count": len(pass_reviews), "passes": [{"id": item["pass_id"], "label": item["pass_label"], "frame_range": item.get("frame_range")} for item in pass_reviews]}
    combined = enrich_model_review_result(combined, payload)
    combined = apply_deterministic_review_fallback(combined, payload)
    db.save_death_analysis(death_id, "local_ai_review", combined)
    death = payload.get("death") or db.get_death(death_id) or {}
    update_coach_memory_from_review(db, death, combined)
    return {"ok": True, "message": combined["summary"], "analysis": combined, "status": status}


def select_frames_for_pass(frames: List[Dict[str, Any]], pass_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    if len(frames) <= limit:
        return frames
    if pass_id == "contact":
        return frames[-limit:]
    if pass_id == "crosshair":
        middle = max(0, len(frames) - limit - 8)
        return frames[middle : middle + limit]
    step = max(1, len(frames) // limit)
    selected = frames[::step][:limit]
    return selected if selected else frames[-limit:]


def synthesize_multipass_reviews(
    db: Database,
    death_id: int,
    payload: Dict[str, Any],
    status: Dict[str, Any],
    pass_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    compact = [
        {
            "pass_id": review.get("pass_id"),
            "frame_range": review.get("frame_range"),
            "summary": review.get("summary"),
            "perception": review.get("perception") or {},
            "coaching": review.get("coaching") or {},
            "visible_evidence": review.get("visible_evidence") or [],
            "labels": review.get("labels") or [],
            "confidence": review.get("confidence"),
        }
        for review in pass_reviews
    ]
    prompt = (
        payload["prompt"]
        + "\n\nSynthesize these specialized local VLM passes into one final personal VALORANT coach review. "
        "Resolve conflicts by trusting visible evidence and lowering confidence. Enemy/contact pass controls enemy visibility claims; crosshair pass controls aim claims; positioning pass controls utility/minimap/HUD claims. "
        "Do not collapse the whole answer to insufficient evidence if any pass found visible crosshair, movement, HUD, minimap, contact, damage, or death-cue evidence. "
        "Return strict JSON with one diagnosis, visible evidence timeline, coaching labels, better_play, drill, and claim_confidence.\n\n"
        + json.dumps(compact, indent=2)
    )
    provider = status["provider"]
    synthesis_budget = max(900, int(normalize_context_limit_setting(status.get("context_limit"))) - 900 - 512)
    prompt = compact_text_for_token_budget(prompt, synthesis_budget)
    try:
        if provider == "ollama":
            endpoint = status["base_url"].rstrip("/") + "/api/generate"
            body = {"model": status["model"], "prompt": local_model_system_prompt(status) + "\n\n" + prompt, "stream": False}
        else:
            endpoint = status["base_url"].rstrip("/") + "/chat/completions"
            body = {
                "model": status["model"],
                "messages": [
                    {"role": "system", "content": local_model_system_prompt(status)},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.05,
                "max_tokens": 900,
            }
        db.log(
            "info",
            "local-ai",
            f"Clip Coach synthesis POST to {provider}.",
            {"endpoint": endpoint, "model": status.get("model"), "pass_count": len(pass_reviews), "image_count": 0},
        )
        response = post_json(endpoint, body, timeout=180)
        text = extract_model_response_text(response)
        db.log("info", "local-ai", f"Clip Coach synthesis response received from {provider}.", {"response_chars": len(text)})
        result = parse_model_review(text, provider)
    except Exception as exc:
        db.log("warning", "local-ai", f"Clip Coach synthesis failed before/while contacting {provider}: {exc}", {"provider": provider})
        result = fallback_batched_review(provider, pass_reviews)
    result["kind"] = "local_ai_review"
    result["status"] = "completed"
    result["provider"] = provider
    return result


def run_local_http_review_single(db: Database, death_id: int, payload: Dict[str, Any], status: Dict[str, Any], save: bool = True, stage: str = "clip_review") -> Dict[str, Any]:
    provider = status["provider"]
    payload, budget = budget_local_model_payload(payload, status, local_model_system_prompt(status), 900, stage)
    payload["request_budget"] = budget
    images = [item["image_base64"] for item in payload.get("keyframes") or [] if item.get("image_base64")]
    prompt = payload["prompt"]
    if provider == "ollama":
        endpoint = status["base_url"].rstrip("/") + "/api/generate"
        manifest = "\n".join(item.get("caption") or "" for item in payload.get("keyframes") or [])
        body = {"model": status["model"], "prompt": local_model_system_prompt(status) + "\n\n" + prompt + "\n\nOrdered frames:\n" + manifest, "stream": False}
        if images:
            body["images"] = images
    else:
        endpoint = status["base_url"].rstrip("/") + "/chat/completions"
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in payload.get("keyframes") or []:
            caption = item.get("caption") or f"Frame {item.get('index') or ''}".strip()
            content.append({"type": "text", "text": caption})
            if item.get("image_base64"):
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{item['image_base64']}"}})
        body = {
            "model": status["model"],
            "messages": [
                {"role": "system", "content": local_model_system_prompt(status)},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }
    try:
        db.save_death_analysis(death_id, "local_model_request_budget", {"kind": "local_model_request_budget", "provider": provider, "model": status.get("model"), "save_final_review": save, **budget})
        db.log(
            "info",
            "local-ai",
            f"Clip Coach POST to {provider}.",
            {"endpoint": endpoint, "model": status.get("model"), "frame_count": len(payload.get("keyframes") or []), "image_count": len(images), "budget": budget},
        )
        response = post_json(endpoint, body, timeout=240)
    except Exception as exc:
        db.log("error", "local-ai", f"Clip Coach {provider} request failed: {exc}", {"endpoint": endpoint, "model": status.get("model")})
        return {"ok": False, "message": f"{provider} request failed: {exc}", "status": status}
    text = extract_model_response_text(response)
    db.log("info", "local-ai", f"Clip Coach response received from {provider}.", {"response_chars": len(text)})
    result = parse_model_review(text, provider)
    result = enrich_model_review_result(result, payload)
    if save:
        result = apply_deterministic_review_fallback(result, payload)
        db.save_death_analysis(death_id, "local_ai_review", result)
        death = payload.get("death") or db.get_death(death_id) or {}
        update_coach_memory_from_review(db, death, result)
    return {"ok": True, "message": result["summary"], "analysis": result, "status": status}


def run_local_http_review_batched(
    db: Database,
    death_id: int,
    payload: Dict[str, Any],
    status: Dict[str, Any],
    chunk_size: int = 30,
) -> Dict[str, Any]:
    frames = payload.get("keyframes") or []
    chunks = [frames[index : index + chunk_size] for index in range(0, len(frames), chunk_size)]
    chunk_reviews = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_payload = dict(payload)
        chunk_payload["keyframes"] = chunk
        chunk_payload["prompt"] = render_batch_model_prompt(payload["prompt"], index, len(chunks), chunk)
        response = run_local_http_review_single(db, death_id, chunk_payload, status, save=False)
        if not response.get("ok"):
            return response
        review = response.get("analysis") or {}
        review["batch_index"] = index
        review["frame_range"] = batch_frame_range(chunk)
        chunk_reviews.append(review)
    combined = synthesize_batched_reviews(db, death_id, payload, status, chunk_reviews)
    combined = promote_best_batch_if_synthesis_is_weak(combined, chunk_reviews)
    combined["batch_reviews"] = chunk_reviews
    combined["batches"] = len(chunk_reviews)
    combined = enrich_model_review_result(combined, payload)
    combined = apply_deterministic_review_fallback(combined, payload)
    db.save_death_analysis(death_id, "local_ai_review", combined)
    death = payload.get("death") or db.get_death(death_id) or {}
    update_coach_memory_from_review(db, death, combined)
    return {"ok": True, "message": combined["summary"], "analysis": combined, "status": status}


def render_batch_model_prompt(base_prompt: str, index: int, total: int, frames: List[Dict[str, Any]]) -> str:
    frame_range = batch_frame_range(frames)
    return (
        base_prompt
        + f"\n\nThis is batch {index}/{total}, covering {frame_range}. "
        "Your job for this batch is detection first: inspect every frame for any visible enemy body, head, weapon, outline, muzzle flash, tracer, damage cue, or sudden contact. "
        "Return strict JSON using the same perception/coaching schema. "
        "In visible_evidence, cite exact frame numbers/timing for anything you see. If no enemy is visible, say that explicitly and still describe crosshair/movement evidence."
    )


def batch_frame_range(frames: List[Dict[str, Any]]) -> str:
    if not frames:
        return "no frames"
    start = frames[0].get("seconds_before_death")
    end = frames[-1].get("seconds_before_death")
    first_index = frames[0].get("sequence_index") or frames[0].get("index")
    last_index = frames[-1].get("sequence_index") or frames[-1].get("index")
    return f"frames {first_index}-{last_index}, {start}s to {end}s before death"


def synthesize_batched_reviews(
    db: Database,
    death_id: int,
    payload: Dict[str, Any],
    status: Dict[str, Any],
    chunk_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    provider = status["provider"]
    prompt = render_batch_synthesis_prompt(payload["prompt"], chunk_reviews)
    synthesis_budget = max(900, int(normalize_context_limit_setting(status.get("context_limit"))) - 900 - 512)
    prompt = compact_text_for_token_budget(prompt, synthesis_budget)
    if provider == "ollama":
        endpoint = status["base_url"].rstrip("/") + "/api/generate"
        body = {"model": status["model"], "prompt": local_model_system_prompt(status) + "\n\n" + prompt, "stream": False}
    else:
        endpoint = status["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": status["model"],
            "messages": [
                {"role": "system", "content": local_model_system_prompt(status)},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }
    try:
        db.log(
            "info",
            "local-ai",
            f"Clip Coach batch synthesis POST to {provider}.",
            {"endpoint": endpoint, "model": status.get("model"), "batch_count": len(chunk_reviews), "image_count": 0},
        )
        response = post_json(endpoint, body, timeout=180)
        text = extract_model_response_text(response)
        db.log("info", "local-ai", f"Clip Coach batch synthesis response received from {provider}.", {"response_chars": len(text)})
        result = parse_model_review(text, provider)
    except Exception as exc:
        db.log("warning", "local-ai", f"Clip Coach batch synthesis failed before/while contacting {provider}: {exc}", {"provider": provider})
        result = fallback_batched_review(provider, chunk_reviews)
    result["kind"] = "local_ai_review"
    result["status"] = "completed"
    result["provider"] = provider
    return result


def render_batch_synthesis_prompt(base_prompt: str, chunk_reviews: List[Dict[str, Any]]) -> str:
    compact_reviews = [
        {
            "batch_index": review.get("batch_index"),
            "frame_range": review.get("frame_range"),
            "summary": review.get("summary"),
            "visible_evidence": review.get("visible_evidence") or [],
            "labels": review.get("labels") or [],
            "better_play": review.get("better_play"),
            "confidence": review.get("confidence"),
        }
        for review in chunk_reviews
    ]
    return (
        base_prompt
        + "\n\nCombine these per-batch visual reviews into one final VALORANT coaching analysis. "
        "Prioritize any batch that saw a visible enemy/contact cue. Do not invent details beyond the batch evidence. "
        "Return strict JSON using the same perception/coaching schema with one final diagnosis.\n\n"
        + json.dumps(compact_reviews)
    )


def fallback_batched_review(provider: str, chunk_reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    evidence = []
    labels = []
    summaries = []
    confidence = 0.0
    for review in chunk_reviews:
        summaries.append(str(review.get("summary") or ""))
        evidence.extend(review.get("visible_evidence") or [])
        labels.extend(review.get("labels") or [])
        confidence = max(confidence, float(review.get("confidence") or 0))
    summary = "Batched Local AI review completed. " + " ".join(summaries[:2])[:450]
    better_play = next((str(review.get("better_play")) for review in chunk_reviews if review.get("better_play")), "")
    drill = next((str(review.get("drill")) for review in chunk_reviews if review.get("drill")), "")
    return {
        "kind": "local_ai_review",
        "summary": summary,
        "visible_evidence": evidence[:8],
        "labels": sorted(set(labels))[:6],
        "better_play": better_play,
        "drill": drill,
        "perception": {
            "enemy_seen": "uncertain",
            "enemy_frames": [],
            "first_contact_time": "unknown",
            "time_to_death": "unknown",
            "crosshair_level": "unknown",
            "crosshair_alignment": "unknown",
            "peek_type": "unknown",
            "movement_state": "unknown",
            "utility_seen": "unknown",
            "weapon_seen": "unknown",
            "hp_seen": "unknown",
            "score_seen": "unknown",
            "teammates_alive_seen": "unknown",
            "spike_state_seen": "unknown",
            "evidence": evidence[:8],
            "confidence": confidence or 0.5,
        },
        "coaching": {
            "summary": summary,
            "why_death_happened": summary,
            "first_mistake": "",
            "better_decision": better_play,
            "utility_issue": "",
            "crosshair_issue": "",
            "positioning_issue": "",
            "mechanical_issue": "",
            "drill": drill,
            "labels": sorted(set(labels))[:6],
            "confidence": confidence or 0.5,
        },
        "confidence": confidence or 0.5,
        "status": "completed",
        "provider": provider,
    }


def promote_best_batch_if_synthesis_is_weak(combined: Dict[str, Any], chunk_reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not review_needs_deterministic_fallback(combined):
        return combined
    candidates = [review for review in chunk_reviews if not review_needs_deterministic_fallback(review)]
    if not candidates:
        return combined
    best = max(candidates, key=review_usefulness_score)
    promoted = dict(best)
    promoted["summary"] = str(best.get("summary") or "Best visual batch review selected because synthesis was weak.")
    promoted["kind"] = "local_ai_review"
    promoted["status"] = "completed"
    promoted["provider"] = combined.get("provider") or best.get("provider")
    promoted["synthesis_original_summary"] = combined.get("summary") or ""
    promoted["batch_promotion_reason"] = "Batch synthesis was weak; selected the strongest visual batch review instead."
    return promoted


def review_usefulness_score(review: Dict[str, Any]) -> float:
    score = normalize_confidence(review.get("confidence", 0.0)) * 3.0
    score += min(4, len(review.get("visible_evidence") or [])) * 0.35
    score += min(4, len(review.get("evidence_timeline") or [])) * 0.35
    if str(review.get("better_play") or "").strip():
        score += 1.0
    if review.get("labels"):
        score += 0.5
    return score


def extract_model_response_text(response: Any) -> str:
    if not isinstance(response, dict):
        return str(response or "{}")
    direct = response.get("response")
    if direct is not None:
        return model_content_to_text(direct)
    choices = response.get("choices") or []
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else {}
        if isinstance(message, dict) and message.get("content") is not None:
            return model_content_to_text(message.get("content"))
        if isinstance(first, dict) and first.get("text") is not None:
            return model_content_to_text(first.get("text"))
    return json.dumps(response)


def model_content_to_text(content: Any) -> str:
    if content is None:
        return "{}"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("text") is not None:
                    parts.append(str(item.get("text") or ""))
                elif item.get("content") is not None:
                    parts.append(str(item.get("content") or ""))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip() or "{}"
    if isinstance(content, dict):
        for key in ("text", "content", "summary", "response"):
            if content.get(key) is not None:
                return model_content_to_text(content.get(key))
        return json.dumps(content)
    return str(content)


def post_json(url: str, body: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def get_json(url: str, timeout: int = 60) -> Dict[str, Any]:
    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def resolve_candidate(candidates: Any, vocabulary: Dict[str, Any], group_key: str) -> Dict[str, Any]:
    rows = normalize_candidates(candidates)
    canonical = {vocabulary_key(item.get("name")): item.get("name") for item in vocabulary.get(group_key) or [] if item.get("name")}
    aliases = vocabulary.get("aliases") or {}
    best = {"value": "", "confidence": 0.0, "evidence": "", "source": "not_found"}
    for row in rows:
        key = vocabulary_key(row.get("value"))
        value = canonical.get(key) or aliases.get(key)
        if not value:
            continue
        confidence = normalize_confidence(row.get("confidence", 0.0))
        if confidence > float(best["confidence"]):
            best = {"value": value, "confidence": confidence, "evidence": row.get("evidence") or "", "source": "kb_vocabulary"}
    return best


def resolve_location_candidate(candidates: Any, vocabulary: Dict[str, Any]) -> Dict[str, Any]:
    rows = normalize_candidates(candidates)
    lookup = {vocabulary_key(item): item for item in vocabulary.get("callouts") or [] if item}
    best = {"value": "", "confidence": 0.0, "evidence": "", "source": "not_found"}
    for row in rows:
        raw = str(row.get("value") or "").strip()
        key = vocabulary_key(raw)
        value = lookup.get(key) or raw
        if not value:
            continue
        confidence = normalize_confidence(row.get("confidence", 0.0))
        if confidence > float(best["confidence"]):
            source = "kb_callout" if key in lookup else "model_text"
            best = {"value": value, "confidence": confidence, "evidence": row.get("evidence") or "", "source": source}
    return best


def resolve_scalar(payload: Any, allowed: set, aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    aliases = aliases or {}
    if isinstance(payload, dict):
        raw = str(payload.get("value") or "").strip().lower()
        confidence = normalize_confidence(payload.get("confidence", 0.0))
        evidence = str(payload.get("evidence") or "")
    else:
        raw = str(payload or "").strip().lower()
        confidence = 0.4 if raw else 0.0
        evidence = ""
    value = aliases.get(raw, raw)
    if value not in allowed or value == "unknown":
        return {"value": "", "confidence": 0.0, "evidence": evidence, "source": "not_found"}
    return {"value": value, "confidence": confidence, "evidence": evidence, "source": "model_json"}


def resolve_round_number(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        raw = payload.get("value")
        confidence = normalize_confidence(payload.get("confidence", 0.0))
        evidence = str(payload.get("evidence") or "")
    else:
        raw = payload
        confidence = 0.4
        evidence = ""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return {"value": "", "confidence": 0.0, "evidence": evidence, "source": "not_found"}
    if value < 1 or value > 30:
        return {"value": "", "confidence": 0.0, "evidence": evidence, "source": "out_of_range"}
    return {"value": value, "confidence": confidence, "evidence": evidence, "source": "hud_ocr"}


def resolve_team_counts(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        raw = str(payload.get("value") or "")
        confidence = normalize_confidence(payload.get("confidence", 0.0))
        evidence = str(payload.get("evidence") or "")
    else:
        raw = str(payload or "")
        confidence = 0.4 if raw else 0.0
        evidence = ""
    match = re.search(r"\b([1-5])\s*v(?:s)?\s*([1-5])\b", raw, re.IGNORECASE)
    if not match:
        return {"value": "", "confidence": 0.0, "evidence": evidence, "source": "not_found"}
    return {"value": f"{match.group(1)}v{match.group(2)}", "confidence": confidence, "evidence": evidence, "source": "hud_ocr"}


def resolve_round_score(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ally": None, "enemy": None, "confidence": 0.0, "evidence": "", "source": "not_found"}
    try:
        ally = int(float(payload.get("ally")))
        enemy = int(float(payload.get("enemy")))
    except (TypeError, ValueError):
        return {"ally": None, "enemy": None, "confidence": 0.0, "evidence": str(payload.get("evidence") or ""), "source": "not_found"}
    if ally < 0 or enemy < 0 or ally > 13 or enemy > 13:
        return {"ally": None, "enemy": None, "confidence": 0.0, "evidence": str(payload.get("evidence") or ""), "source": "out_of_range"}
    return {
        "ally": ally,
        "enemy": enemy,
        "confidence": normalize_confidence(payload.get("confidence", 0.0)),
        "evidence": str(payload.get("evidence") or ""),
        "source": "hud_ocr",
    }


def infer_round_from_score(score: Dict[str, Any]) -> Dict[str, Any]:
    ally = score.get("ally")
    enemy = score.get("enemy")
    if ally is None or enemy is None:
        return {"value": "", "confidence": 0.0, "evidence": "", "source": "not_found", "status": "unknown"}
    try:
        round_number = int(ally) + int(enemy) + 1
    except (TypeError, ValueError):
        return {"value": "", "confidence": 0.0, "evidence": "", "source": "not_found", "status": "unknown"}
    if round_number < 1 or round_number > 25:
        return {"value": "", "confidence": 0.0, "evidence": "", "source": "out_of_range", "status": "unknown"}
    confidence = round(max(0.0, min(0.82, float(score.get("confidence") or 0) * 0.9)), 2)
    return {
        "value": round_number,
        "confidence": confidence,
        "evidence": f"Inferred from visible score {ally}-{enemy}: next round is {round_number}.",
        "source": "score_progression",
        "status": context_candidate_status(confidence, confidence >= 0.72),
    }


def context_candidate_status(confidence: float, applied: bool = False) -> str:
    if applied or confidence >= 0.82:
        return "confirmed"
    if confidence >= 0.55:
        return "candidate"
    return "uncertain"


def normalize_candidates(value: Any) -> List[Dict[str, Any]]:
    if not value:
        return []
    rows = value if isinstance(value, list) else [value]
    normalized = []
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("value") or row.get("name") or "").strip()
            if not text:
                continue
            normalized.append(
                {
                    "value": text,
                    "confidence": normalize_confidence(row.get("confidence", 0.0)),
                    "evidence": str(row.get("evidence") or row.get("reason") or ""),
                }
            )
        else:
            text = str(row or "").strip()
            if text:
                normalized.append({"value": text, "confidence": 0.4, "evidence": ""})
    normalized.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)
    return normalized[:6]


def parse_model_review(text: str, provider: str) -> Dict[str, Any]:
    text = strip_json_fence(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"summary": text}
    return build_model_review_result(parsed, provider, text)


def build_model_review_result(parsed: Dict[str, Any], provider: str, raw_text: str = "") -> Dict[str, Any]:
    if not isinstance(parsed, dict):
        parsed = {"summary": raw_text or str(parsed)}
    perception = normalize_perception(parsed.get("perception") or parsed)
    coaching = normalize_coaching(parsed.get("coaching") or parsed)
    confidence = normalize_confidence(parsed.get("confidence", coaching.get("confidence", perception.get("confidence", 0.55))))
    labels = normalize_text_list(parsed.get("labels") or parsed.get("suggested_labels") or coaching.get("labels") or [])
    summary = str(
        parsed.get("summary")
        or coaching.get("summary")
        or parsed.get("what_happened")
        or raw_text[:500]
        or "Local AI review completed."
    )
    result = {
        "kind": "local_ai_review",
        "summary": summary,
        "visible_evidence": normalize_text_list(parsed.get("visible_evidence") or parsed.get("evidence") or perception.get("evidence") or []),
        "labels": labels,
        "better_play": str(parsed.get("better_play") or parsed.get("recommendation") or coaching.get("better_decision") or ""),
        "drill": str(parsed.get("drill") or coaching.get("drill") or ""),
        "confidence": confidence,
        "perception": perception,
        "coaching": coaching,
        "first_mistake": str(parsed.get("first_mistake") or coaching.get("first_mistake") or ""),
        "utility_issue": str(parsed.get("utility_issue") or coaching.get("utility_issue") or ""),
        "crosshair_issue": str(parsed.get("crosshair_issue") or coaching.get("crosshair_issue") or ""),
        "positioning_issue": str(parsed.get("positioning_issue") or coaching.get("positioning_issue") or ""),
        "mechanical_issue": str(parsed.get("mechanical_issue") or coaching.get("mechanical_issue") or ""),
        "status": "completed",
        "provider": provider,
    }
    if parsed.get("extracted_text") is not None:
        result["extracted_text"] = str(parsed.get("extracted_text") or "")
    if parsed.get("scoreboard") is not None:
        result["scoreboard"] = parsed.get("scoreboard")
    if parsed.get("hud_context") is not None:
        result["hud_context"] = parsed.get("hud_context")
    result["segment_reviews"] = normalize_segment_reviews(parsed.get("segment_reviews") or parsed.get("segments") or [])
    result["evidence_timeline"] = normalize_evidence_timeline(parsed.get("evidence_timeline") or parsed.get("timeline") or parsed.get("visible_evidence") or parsed.get("evidence") or [])
    result["claim_confidence"] = normalize_claim_confidence(parsed.get("claim_confidence") or parsed.get("claim_confidences") or {})
    return result


def enrich_model_review_result(result: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    segments = payload.get("segments") or build_clip_segments(payload.get("keyframes") or [])
    result["clip_segments"] = segments
    result["segment_reviews"] = merge_segment_reviews(segments, result.get("segment_reviews") or [], result)
    result["evidence_timeline"] = build_review_evidence_timeline(result, payload.get("keyframes") or [])
    result["claim_confidence"] = build_claim_confidence(result)
    result["review_quality"] = score_review_quality(result)
    result["review_pipeline"] = build_review_pipeline_audit(payload, result)
    result["deterministic_signals"] = {
        "visual": payload.get("visual_signals") or {},
        "ocr": payload.get("ocr_regions") or {},
    }
    return result


def apply_deterministic_review_fallback(result: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(result)
    support = deterministic_review_fallback(payload)
    diagnostics = review_diagnostics(result, payload, support)
    merged["review_diagnostics"] = diagnostics
    if support:
        merged["fallback_support"] = support
    if review_needs_deterministic_fallback(result):
        merged["fallback_reason"] = "Local VLM review was weak; deterministic visual signals were attached as supporting diagnostics without replacing the model output."
    merged["review_quality"] = score_review_quality(merged)
    merged["review_pipeline"] = build_review_pipeline_audit(payload, merged)
    return merged


def review_diagnostics(result: Dict[str, Any], payload: Dict[str, Any], support: Dict[str, Any]) -> Dict[str, Any]:
    budget = payload.get("request_budget") or {}
    sent_frames = int(budget.get("sent_frames") or len(payload.get("keyframes") or []))
    prepared_frames = int(budget.get("original_frames") or len(payload.get("keyframes") or []))
    warnings = []
    if sent_frames <= 3:
        warnings.append("Very few images reached the local model; increase context limit, lower image token estimate, or use a smaller review mode.")
    if review_needs_deterministic_fallback(result):
        warnings.append("The local model response was weak or insufficient; deterministic detector evidence is shown separately.")
    if not payload.get("keyframes"):
        warnings.append("No clip frames were available for the local model.")
    return {
        "prepared_frames": prepared_frames,
        "sent_frames": sent_frames,
        "sent_frame_range": budget.get("sent_frame_range") or batch_frame_range(payload.get("keyframes") or []),
        "trimmed": bool(budget.get("trimmed")),
        "model_summary": result.get("summary") or "",
        "model_confidence": result.get("confidence"),
        "model_weak": review_needs_deterministic_fallback(result),
        "support_available": bool(support),
        "warnings": warnings,
    }


def review_needs_deterministic_fallback(result: Dict[str, Any]) -> bool:
    summary = str(result.get("summary") or "").lower()
    better_play = str(result.get("better_play") or "").strip()
    evidence = result.get("visible_evidence") or []
    timeline = result.get("evidence_timeline") or []
    confidence = normalize_confidence(result.get("confidence", 0.0))
    insufficient = "insufficient visual evidence" in summary or "not enough visual evidence" in summary
    empty_action = not better_play or "insufficient visual evidence" in better_play.lower()
    weak_evidence = len(evidence) + len(timeline) <= 1
    return insufficient and empty_action and (weak_evidence or confidence < 0.45)


def deterministic_review_fallback(payload: Dict[str, Any]) -> Dict[str, Any]:
    visual = payload.get("visual_signals") or {}
    if not visual or visual.get("status") == "empty":
        return {}
    crosshair = visual.get("crosshair_score") or {}
    movement = visual.get("movement_read") or {}
    minimap = visual.get("minimap_read") or {}
    first_contact = visual.get("first_contact") or {}
    death_cue = visual.get("death_cue") or {}
    enemy_timeline = visual.get("enemy_visibility_timeline") or []
    evidence = []
    if first_contact:
        evidence.append(f"Frame {first_contact.get('frame')} shows the strongest contact proxy near {format_relative_second(first_contact.get('relative_second'))}.")
    if death_cue:
        evidence.append(f"Frame {death_cue.get('frame')} has the strongest death/combat-report cue.")
    if crosshair.get("summary"):
        evidence.append(crosshair["summary"])
    if movement.get("summary"):
        evidence.append(movement["summary"])
    if minimap.get("summary"):
        evidence.append(minimap["summary"])
    if not evidence:
        return {}
    labels = deterministic_fallback_labels(crosshair, movement, first_contact, enemy_timeline)
    first_mistake = labels[0] if labels else "review fight setup and crosshair readiness"
    better_play = deterministic_better_play(labels, first_contact)
    drill = deterministic_drill(labels)
    summary = f"Local detector support: {first_mistake}."
    confidence = min(0.52, max(0.34, float(visual.get("confidence") or 0.0) * 0.65))
    return {
        "summary": summary,
        "visible_evidence": evidence[:8],
        "labels": labels[:4] or ["needs manual review"],
        "better_play": better_play,
        "drill": drill,
        "confidence": round(confidence, 2),
        "perception": {
            "enemy_seen": "uncertain" if not enemy_timeline else "possible",
            "enemy_frames": [str(row.get("frame")) for row in enemy_timeline[:6] if row.get("frame")],
            "first_contact_time": format_relative_second(first_contact.get("relative_second")) if first_contact else "unknown",
            "time_to_death": "unknown",
            "crosshair_level": str(crosshair.get("level") or "unknown"),
            "crosshair_alignment": str(crosshair.get("risk") or "unknown"),
            "peek_type": "unknown",
            "movement_state": str(movement.get("risk") or "unknown"),
            "utility_seen": "unknown",
            "weapon_seen": "unknown",
            "hp_seen": "unknown",
            "score_seen": "unknown",
            "teammates_alive_seen": "unknown",
            "spike_state_seen": "unknown",
            "evidence": evidence[:8],
            "confidence": round(confidence, 2),
        },
        "coaching": {
            "summary": summary,
            "why_death_happened": summary,
            "first_mistake": first_mistake,
            "better_decision": better_play,
            "utility_issue": "uncertain; local fallback did not verify utility usage",
            "crosshair_issue": str(crosshair.get("summary") or "uncertain"),
            "positioning_issue": "uncertain; verify angle exposure manually" if first_contact else "uncertain",
            "mechanical_issue": str(movement.get("summary") or "uncertain"),
            "drill": drill,
            "labels": labels[:4] or ["needs manual review"],
            "confidence": round(confidence, 2),
        },
    }


def deterministic_fallback_labels(crosshair: Dict[str, Any], movement: Dict[str, Any], first_contact: Dict[str, Any], enemy_timeline: List[Dict[str, Any]]) -> List[str]:
    labels = []
    crosshair_text = (str(crosshair.get("risk") or "") + " " + str(crosshair.get("summary") or "")).lower()
    movement_text = str(movement.get("risk") or "").lower()
    if any(token in crosshair_text for token in ("wide", "low", "late", "unstable", "poor")):
        labels.append("crosshair readiness")
    if "moving during contact" in movement_text or "high movement" in movement_text:
        labels.append("movement during fight")
    if first_contact or enemy_timeline:
        labels.append("first contact review")
    if not labels:
        labels.append("needs manual review")
    return labels


def deterministic_better_play(labels: List[str], first_contact: Dict[str, Any]) -> str:
    if "crosshair readiness" in labels:
        return "Before committing to the angle, place the crosshair at likely head height and clear one slice at a time before exposing your body."
    if "movement during fight" in labels:
        return "Stabilize before the first accurate burst; avoid carrying movement into the contact frame unless you are intentionally jiggling for info."
    if first_contact:
        return "Pause the clip at the first contact cue and check whether the swing exposed more than one angle or lacked a trade/escape plan."
    return "Use the clip timeline to manually verify the first visible mistake, then label the marker so future reviews can learn from it."


def deterministic_drill(labels: List[str]) -> str:
    if "crosshair readiness" in labels:
        return "Deathmatch block: clear every angle with the crosshair already at head height before you move past the corner."
    if "movement during fight" in labels:
        return "Range/deathmatch block: strafe, stop, fire a 2-3 bullet burst, then reset before the next peek."
    return "Review 5 deaths and pause 2 seconds before death to call the first fix before watching the outcome."


def build_review_pipeline_audit(payload: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    context = payload.get("context_extraction") or {}
    segments = payload.get("segments") or []
    request_budget = payload.get("request_budget") or {}
    return {
        "kind": "valorant_specific_review_pipeline",
        "privacy": payload.get("privacy") or "local-only",
        "steps": [
            {"id": "frames", "label": "Extract ordered frames", "status": "complete", "count": len(payload.get("keyframes") or [])},
            {"id": "visual", "label": "Run deterministic visual detectors", "status": "complete" if payload.get("visual_signals") else "skipped", "summary": (payload.get("visual_signals") or {}).get("summary") or ""},
            {"id": "ocr", "label": "Run OCR over HUD regions", "status": (payload.get("ocr_regions") or {}).get("status") or "skipped", "summary": (payload.get("ocr_regions") or {}).get("summary") or ""},
            {"id": "context", "label": "Read HUD/context with KB vocabulary", "status": "complete" if context else "skipped", "summary": context.get("summary") or ""},
            {"id": "segments", "label": "Split clip into review segments", "status": "complete", "count": len(segments)},
            {"id": "kb", "label": "Retrieve VALORANT-specific coaching constraints", "status": "complete"},
            {"id": "memory", "label": "Apply personal coach memory", "status": "complete"},
            {"id": "synthesis", "label": "Generate final coach read", "status": result.get("status") or "complete"},
        ],
        "request_budget": request_budget,
        "fallback": result.get("fallback_reason") or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_segment_reviews(value: Any) -> List[Dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        result.append(
            {
                "segment_id": str(row.get("segment_id") or row.get("id") or "").strip(),
                "summary": str(row.get("summary") or row.get("read") or "").strip()[:260],
                "evidence": normalize_text_list(row.get("evidence") or []),
                "mistake": str(row.get("mistake") or row.get("issue") or "").strip()[:180],
                "confidence": normalize_confidence(row.get("confidence", 0.0)),
            }
        )
    return result[:8]


def merge_segment_reviews(segments: List[Dict[str, Any]], reviews: List[Dict[str, Any]], result: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_id = {str(row.get("segment_id") or ""): row for row in reviews if row.get("segment_id")}
    merged = []
    for segment in segments:
        segment_id = str(segment.get("id") or "")
        review = by_id.get(segment_id) or {}
        merged.append(
            {
                "segment_id": segment_id,
                "label": segment.get("label") or segment_id,
                "purpose": segment.get("purpose") or "",
                "frame_count": segment.get("frame_count") or len(segment.get("frames") or []),
                "frame_range": segment_frame_range(segment),
                "summary": review.get("summary") or default_segment_summary(segment_id, result),
                "evidence": review.get("evidence") or [],
                "mistake": review.get("mistake") or "",
                "confidence": review.get("confidence", result.get("confidence") or 0.0),
            }
        )
    return merged


def segment_frame_range(segment: Dict[str, Any]) -> str:
    frames = segment.get("frames") or []
    if not frames:
        return ""
    first = frames[0].get("index")
    last = frames[-1].get("index")
    start = frames[0].get("relative_second")
    end = frames[-1].get("relative_second")
    return f"frames {first}-{last}, {format_relative_second(start)} to {format_relative_second(end)}"


def default_segment_summary(segment_id: str, result: Dict[str, Any]) -> str:
    perception = result.get("perception") or {}
    if segment_id == "contact":
        return f"Contact read: enemy={perception.get('enemy_seen', 'uncertain')}, first_contact={perception.get('first_contact_time', 'unknown')}."
    if segment_id == "death":
        return str(result.get("summary") or "Final duel/death moment reviewed.")[:260]
    if segment_id == "pre_contact":
        return f"Pre-contact read: crosshair={perception.get('crosshair_alignment', 'unknown')}, movement={perception.get('movement_state', 'unknown')}."
    return "Segment available for review; model did not provide a separate read."


def build_review_evidence_timeline(result: Dict[str, Any], frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    timeline = normalize_evidence_timeline(result.get("evidence_timeline") or [])
    if timeline:
        return enrich_timeline_with_timestamps(timeline, frames)
    items = []
    perception = result.get("perception") or {}
    if perception.get("enemy_seen") and str(perception.get("enemy_seen")).lower() not in {"false", "unknown"}:
        items.append(timeline_item("contact", "enemy/contact cue", perception.get("first_contact_time"), perception.get("enemy_frames"), perception.get("evidence"), result.get("confidence")))
    if perception.get("crosshair_alignment") and perception.get("crosshair_alignment") != "unknown":
        items.append(timeline_item("pre_contact", "crosshair", "pre-contact", perception.get("crosshair_alignment"), perception.get("evidence"), result.get("confidence")))
    if perception.get("movement_state") and perception.get("movement_state") != "unknown":
        items.append(timeline_item("death", "movement", "final duel", perception.get("movement_state"), perception.get("evidence"), result.get("confidence")))
    for text in normalize_text_list(result.get("visible_evidence") or result.get("evidence") or [])[:6]:
        items.append(timeline_item(infer_segment_from_text(text), "visible evidence", extract_time_hint(text), text, text, result.get("confidence")))
    return enrich_timeline_with_timestamps(deduplicate_timeline(items), frames)[:10]


def normalize_evidence_timeline(value: Any) -> List[Dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows:
        if isinstance(row, dict):
            text = str(row.get("evidence") or row.get("summary") or row.get("text") or row.get("claim") or "").strip()
            if not text:
                continue
            result.append(
                {
                    "segment_id": str(row.get("segment_id") or row.get("segment") or infer_segment_from_text(text)),
                    "time": str(row.get("time") or row.get("timestamp") or row.get("relative_second") or extract_time_hint(text) or ""),
                    "frame": str(row.get("frame") or row.get("frame_index") or ""),
                    "event": str(row.get("event") or row.get("type") or "evidence"),
                    "evidence": text[:260],
                    "claim_confidence": normalize_confidence(row.get("claim_confidence", row.get("confidence", 0.0))),
                    "video_timestamp": row.get("video_timestamp"),
                }
            )
        else:
            text = str(row or "").strip()
            if text:
                result.append(timeline_item(infer_segment_from_text(text), "evidence", extract_time_hint(text), text, text, 0.45))
    return result[:12]


def timeline_item(segment_id: str, event: str, time_value: Any, frame: Any, evidence: Any, confidence: Any) -> Dict[str, Any]:
    return {
        "segment_id": segment_id or "clip",
        "time": str(time_value or ""),
        "frame": ", ".join(normalize_text_list(frame)) if isinstance(frame, list) else str(frame or ""),
        "event": event,
        "evidence": "; ".join(normalize_text_list(evidence))[:260] if isinstance(evidence, list) else str(evidence or "")[:260],
        "claim_confidence": normalize_confidence(confidence),
    }


def enrich_timeline_with_timestamps(items: List[Dict[str, Any]], frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    frame_lookup = {}
    for frame in frames:
        index = str(frame.get("sequence_index") or frame.get("index") or "")
        if index:
            frame_lookup[index] = frame
    enriched = []
    for item in items:
        row = dict(item)
        frame_key = first_number_string(row.get("frame") or row.get("evidence") or "")
        frame = frame_lookup.get(frame_key or "")
        if frame:
            row["video_timestamp"] = frame.get("timestamp")
            row["relative_second"] = frame_relative_second(frame)
            row["time"] = row.get("time") or format_relative_second(row.get("relative_second"))
            row["frame"] = row.get("frame") or frame_key
        enriched.append(row)
    enriched.sort(key=lambda row: timeline_sort_value(row))
    return enriched[:12]


def deduplicate_timeline(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = (item.get("segment_id"), item.get("event"), item.get("evidence"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def timeline_sort_value(row: Dict[str, Any]) -> float:
    if row.get("relative_second") is not None:
        try:
            return float(row.get("relative_second"))
        except (TypeError, ValueError):
            pass
    text = str(row.get("time") or "")
    number = extract_time_number(text)
    if number is not None:
        return number
    order = {"setup": -8, "pre_contact": -3, "contact": -1, "death": 0, "aftermath": 1}
    return float(order.get(str(row.get("segment_id") or ""), 9))


def infer_segment_from_text(text: Any) -> str:
    lower = str(text or "").lower()
    if "after" in lower or "killfeed" in lower or "death banner" in lower:
        return "aftermath"
    if "death" in lower or "final" in lower or "damage" in lower:
        return "death"
    if "enemy" in lower or "contact" in lower or "muzzle" in lower or "tracer" in lower:
        return "contact"
    if "crosshair" in lower or "clear" in lower or "peek" in lower:
        return "pre_contact"
    return "setup"


def extract_time_hint(text: Any) -> str:
    value = str(text or "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?\s*s", value, re.IGNORECASE)
    return match.group(0) if match else ""


def extract_time_number(text: Any) -> Optional[float]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def first_number_string(text: Any) -> str:
    match = re.search(r"\d+", str(text or ""))
    return match.group(0) if match else ""


def format_relative_second(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:+.1f}s"


def normalize_claim_confidence(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {str(key): normalize_confidence(score) for key, score in value.items()}


def build_claim_confidence(result: Dict[str, Any]) -> Dict[str, float]:
    existing = normalize_claim_confidence(result.get("claim_confidence") or {})
    perception = result.get("perception") or {}
    coaching = result.get("coaching") or {}
    defaults = {
        "enemy_contact": perception.get("confidence", result.get("confidence", 0.0)),
        "crosshair": result.get("confidence", 0.0) if perception.get("crosshair_alignment") != "unknown" else 0.25,
        "movement": result.get("confidence", 0.0) if perception.get("movement_state") != "unknown" else 0.25,
        "utility": coaching.get("confidence", result.get("confidence", 0.0)) if coaching.get("utility_issue") else 0.25,
        "first_mistake": coaching.get("confidence", result.get("confidence", 0.0)) if coaching.get("first_mistake") else 0.25,
    }
    defaults.update(existing)
    return {key: normalize_confidence(value) for key, value in defaults.items()}


def score_review_quality(result: Dict[str, Any]) -> Dict[str, Any]:
    evidence_count = len(result.get("evidence_timeline") or []) + len(result.get("visible_evidence") or [])
    segment_count = sum(1 for item in result.get("segment_reviews") or [] if item.get("summary"))
    confidence = normalize_confidence(result.get("confidence", 0.0))
    score = min(1.0, evidence_count * 0.08 + segment_count * 0.08 + confidence * 0.45)
    return {
        "score": round(score, 2),
        "evidence_count": evidence_count,
        "segment_count": segment_count,
        "needs_manual_review": score < 0.45,
        "summary": "strong evidence" if score >= 0.7 else "usable but verify" if score >= 0.45 else "weak visual evidence",
    }


def local_ai_review_schema() -> Dict[str, Any]:
    return {
        "summary": "one concise sentence",
        "segment_reviews": [
            {
                "segment_id": "setup/pre_contact/contact/death/aftermath",
                "summary": "visible-only read for this segment",
                "evidence": ["frame/timing citations"],
                "mistake": "segment-specific issue or insufficient visual evidence",
                "confidence": 0.0,
            }
        ],
        "evidence_timeline": [
            {
                "segment_id": "setup/pre_contact/contact/death/aftermath",
                "time": "relative time or frame number",
                "frame": "frame index if visible",
                "event": "enemy/contact/crosshair/movement/utility/hud/death",
                "evidence": "visible fact with frame reference",
                "claim_confidence": 0.0,
            }
        ],
        "perception": {
            "enemy_seen": "true/false/uncertain",
            "enemy_frames": ["frame numbers or timestamps where enemy/contact cue is visible"],
            "first_contact_time": "seconds before death or unknown",
            "time_to_death": "seconds from first contact to death or unknown",
            "crosshair_level": "head/chest/low/unknown",
            "crosshair_alignment": "on_angle/wide/late_correction/unknown",
            "peek_type": "jiggle/wide/swing/held_angle/rotation/unknown",
            "movement_state": "standing/strafing/running/jumping/crouching/unknown",
            "utility_seen": "none/own/team/enemy/unknown",
            "weapon_seen": "weapon name or unknown",
            "hp_seen": "number or unknown",
            "score_seen": "score or unknown",
            "teammates_alive_seen": "number or unknown",
            "spike_state_seen": "carried/planted/dropped/unknown",
            "evidence": ["visible evidence with frame references"],
            "confidence": 0.0,
        },
        "coaching": {
            "why_death_happened": "visible-evidence-based read",
            "first_mistake": "earliest visible mistake or insufficient visual evidence",
            "better_decision": "specific action the player should take",
            "utility_issue": "yes/no/uncertain plus reason",
            "crosshair_issue": "yes/no/uncertain plus reason",
            "positioning_issue": "yes/no/uncertain plus reason",
            "mechanical_issue": "yes/no/uncertain plus reason",
            "drill": "one practice item",
            "labels": ["1-4 normalized mistake labels"],
            "confidence": 0.0,
        },
        "labels": ["same as coaching.labels"],
        "better_play": "same as coaching.better_decision",
        "drill": "same as coaching.drill",
        "claim_confidence": {
            "enemy_contact": 0.0,
            "crosshair": 0.0,
            "movement": 0.0,
            "utility": 0.0,
            "first_mistake": 0.0,
        },
        "confidence": 0.0,
    }


def normalize_perception(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    fields = {
        "enemy_seen": payload.get("enemy_seen", "uncertain"),
        "enemy_frames": normalize_text_list(payload.get("enemy_frames") or payload.get("contact_frames") or []),
        "first_contact_time": payload.get("first_contact_time") or payload.get("first_contact") or "unknown",
        "time_to_death": payload.get("time_to_death") or "unknown",
        "crosshair_level": payload.get("crosshair_level") or payload.get("crosshair") or "unknown",
        "crosshair_alignment": payload.get("crosshair_alignment") or "unknown",
        "peek_type": payload.get("peek_type") or "unknown",
        "movement_state": payload.get("movement_state") or "unknown",
        "utility_seen": payload.get("utility_seen") or payload.get("utility_used") or "unknown",
        "weapon_seen": payload.get("weapon_seen") or payload.get("weapon") or "unknown",
        "hp_seen": payload.get("hp_seen") or payload.get("hp") or "unknown",
        "score_seen": payload.get("score_seen") or payload.get("score") or payload.get("scoreboard") or "unknown",
        "teammates_alive_seen": payload.get("teammates_alive_seen") or payload.get("teammates_alive") or "unknown",
        "spike_state_seen": payload.get("spike_state_seen") or payload.get("spike_state") or "unknown",
        "evidence": normalize_text_list(payload.get("evidence") or payload.get("visible_evidence") or []),
        "confidence": normalize_confidence(payload.get("confidence", 0.0)),
    }
    return fields


def normalize_coaching(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    return {
        "summary": str(payload.get("summary") or payload.get("why_death_happened") or ""),
        "why_death_happened": str(payload.get("why_death_happened") or payload.get("summary") or payload.get("what_happened") or ""),
        "first_mistake": str(payload.get("first_mistake") or ""),
        "better_decision": str(payload.get("better_decision") or payload.get("better_play") or payload.get("recommendation") or ""),
        "utility_issue": str(payload.get("utility_issue") or ""),
        "crosshair_issue": str(payload.get("crosshair_issue") or ""),
        "positioning_issue": str(payload.get("positioning_issue") or ""),
        "mechanical_issue": str(payload.get("mechanical_issue") or ""),
        "drill": str(payload.get("drill") or ""),
        "labels": normalize_text_list(payload.get("labels") or payload.get("suggested_labels") or []),
        "confidence": normalize_confidence(payload.get("confidence", 0.0)),
    }


def normalize_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.55
    if number > 1:
        number = number / 100.0
    return round(max(0.0, min(1.0, number)), 2)


def local_model_system_prompt(status: Dict[str, Any]) -> str:
    purpose = str(status.get("purpose") or "coach")
    if purpose == "ocr":
        return (
            "You are an OCR and HUD extraction model for VALORANT screenshots. "
            "Only report visible text, scoreboard numbers, round/timer/HUD details, and uncertainty. "
            "Return strict compact JSON. Do not provide tactical advice unless directly supported by visible HUD text."
        )
    return (
        "You are a VALORANT VOD coach reviewing an ordered local frame sequence before a death. "
        "Return strict compact JSON with separate perception and coaching objects. "
        "Analyze how the player clears, moves, aims, checks HUD/minimap, and enters the fight across time. Enemies may be visible for only one or two frames, so inspect the whole sequence frame-by-frame. "
        "Use only visible frame evidence. Do not invent hidden enemies, comms, unseen utility, prior context, or player intent. "
        "If enemy/contact is not visible, say enemy_contact is uncertain and still provide low-confidence coaching from visible crosshair, movement, HUD/minimap, and exposure evidence. "
        "Only make the entire review 'insufficient visual evidence' when the sequence itself is blank, unrelated, unreadable, or only post-death UI."
    )


def strip_json_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        return value[start : end + 1]
    return value


def redact_model_request(payload: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    keyframes = payload.get("keyframes") or []
    return {
        "kind": "local_model_audit",
        "provider": status.get("provider"),
        "model": status.get("model"),
        "base_url": status.get("base_url"),
        "sent_clip_path": bool(payload.get("clip_path")),
        "prepared_keyframes": len(keyframes),
        "prepared_frame_range": batch_frame_range(keyframes) if keyframes else "no frames",
        "marker_quality": payload.get("marker_quality") or {},
        "sent_image_bytes": sum(len(item.get("image_base64") or "") for item in keyframes),
        "prompt_preview": str(payload.get("prompt") or "")[:600],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


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
    deaths = db.get_deaths(match_id)
    smart_items = build_smart_death_review_items(db, deaths)
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items + smart_items:
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
        "summary": f"Ranked {len(smart_items)} death review item(s) and grouped {len(items) + len(smart_items)} total item(s) into {len(groups)} coaching cluster(s).",
        "groups": groups[:8],
        "top_items": sorted(items + smart_items, key=lambda row: int(row.get("priority") or 0), reverse=True)[:8],
        "death_items": smart_items[:12],
        "confidence": 0.55 if groups else 0.0,
    }
    db.save_structured_analysis(match_id, "review_queue_v2", result)
    return {"ok": True, "message": result["summary"], "analysis": result}


def build_smart_death_review_items(db: Database, deaths: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    label_counts: Dict[str, int] = {}
    for death in deaths:
        for label in death.get("mistake_labels") or []:
            if label and label != "needs manual review":
                label_counts[label] = label_counts.get(label, 0) + 1
        review = ((death.get("local_ai_review") or {}).get("payload") or {})
        for label in review.get("labels") or []:
            if label:
                label_counts[label] = label_counts.get(label, 0) + 1
    rows = []
    for death in deaths:
        review = ((death.get("local_ai_review") or {}).get("payload") or {})
        visual = ((death.get("clip_visual_signals") or {}).get("payload") or {})
        training = ((death.get("clip_training_label") or {}).get("payload") or {})
        labels = [label for label in (death.get("mistake_labels") or []) + (review.get("labels") or []) if label and label != "needs manual review"]
        repeated = max([label_counts.get(label, 0) for label in labels] or [0])
        quality = review.get("review_quality") or {}
        visual_conf = float(visual.get("confidence") or 0)
        contact_count = len(visual.get("enemy_visibility_timeline") or [])
        missing_review = not bool(review)
        missing_training = not bool(training)
        low_quality = bool(review) and bool((quality or {}).get("needs_manual_review"))
        priority = 35
        priority += min(25, repeated * 6)
        priority += 18 if missing_review else 0
        priority += 14 if low_quality else 0
        priority += 10 if missing_training else 0
        priority += min(16, int(visual_conf * 16))
        priority += min(12, contact_count * 3)
        priority = max(1, min(100, priority))
        reason_bits = []
        if repeated:
            reason_bits.append(f"repeated pattern x{repeated}")
        if missing_review:
            reason_bits.append("needs Clip Coach")
        if low_quality:
            reason_bits.append("review needs verification")
        if missing_training:
            reason_bits.append("needs training label")
        if contact_count:
            reason_bits.append(f"{contact_count} contact cue(s)")
        rows.append(
            {
                "kind": "smart_death_review",
                "death_id": death.get("id"),
                "timestamp": death.get("timestamp"),
                "priority": priority,
                "reason": ", ".join(reason_bits) or "review for pattern coverage",
                "labels": labels[:5],
                "learning_value": priority,
                "next_action": "Run Clip Coach" if missing_review else "Add training label" if missing_training else "Verify low-confidence evidence" if low_quality else "Review repeated pattern",
            }
        )
    return sorted(rows, key=lambda row: int(row.get("priority") or 0), reverse=True)


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
            "knowledge": str(paths.get("knowledge") or ""),
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
            {
                "id": "valorant-knowledge-base",
                "name": "VALORANT Knowledge Base",
                "enabled": True,
                "privacy": "local files",
            },
        ]
    }


def setup_wizard_status(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    diagnostics = installer_diagnostics(paths, db)
    settings = {
        "recording_dir": db.get_setting("recording_dir", ""),
        "auto_import": db.get_setting("auto_import", "false"),
        "auto_analysis": db.get_setting("auto_analysis", "false"),
        "frame_sample_rate": db.get_setting("frame_sample_rate", "standard"),
        "detector_sensitivity": db.get_setting("detector_sensitivity", "normal"),
    }
    ready = bool(settings["recording_dir"]) and diagnostics.get("ok", False)
    steps = [
        {"id": "recording_dir", "label": "Recording folder", "ok": bool(settings["recording_dir"])},
        {"id": "ffmpeg", "label": "ffmpeg for clips/frame analysis", "ok": bool(ffmpeg_path()), "optional": True},
        {"id": "tesseract", "label": "Tesseract for OCR", "ok": bool(tesseract_path()), "optional": True},
        {"id": "local_ai", "label": "Local model provider", "ok": local_ai_status(db)["enabled"], "optional": True},
        {"id": "directories", "label": "Writable app folders", "ok": diagnostics.get("ok", False)},
    ]
    return {"ready": ready, "settings": settings, "steps": steps, "diagnostics": diagnostics, "local_ai": local_ai_status(db)}


def save_setup_wizard(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("recording_dir", "player_name", "auto_import", "auto_analysis", "frame_sample_rate", "detector_sensitivity", "enemy_detector_command"):
        if key in payload:
            db.set_setting(key, str(payload.get(key) or ""))
    if any(key in payload for key in ("local_ai_provider", "local_ai_command", "local_ai_base_url", "local_ai_model", "local_ai_review_mode", "local_ai_review_fps", "local_ai_review_window_seconds", "local_ai_context_limit", "local_ai_image_token_estimate")):
        save_local_ai_config(
            db,
            {
                "provider": payload.get("local_ai_provider"),
                "command": payload.get("local_ai_command"),
                "base_url": payload.get("local_ai_base_url"),
                "model": payload.get("local_ai_model"),
                "review_mode": payload.get("local_ai_review_mode"),
                "review_fps": payload.get("local_ai_review_fps"),
                "review_window_seconds": payload.get("local_ai_review_window_seconds"),
                "context_limit": payload.get("local_ai_context_limit"),
                "image_token_estimate": payload.get("local_ai_image_token_estimate"),
            },
        )
    db.set_setting("setup_completed", "true")
    return {"ok": True}


def prompt_templates(db: Database) -> Dict[str, Any]:
    raw = db.get_setting("prompt_templates", "")
    if raw:
        try:
            templates = json.loads(raw)
        except json.JSONDecodeError:
            templates = {}
    else:
        templates = {}
    defaults = {
        "default": {
            "name": "Default death review",
            "role": "all",
            "prompt": (
                "Review this VALORANT death locally. Return JSON with summary, labels, better_play, confidence. "
                "Round: {round}. Timestamp: {timestamp}. Labels: {labels}. Notes: {notes}. "
                "Focus on positioning, utility, crosshair placement, minimap/timing, and the better next decision."
            ),
        },
        "duelist": {
            "name": "Duelist entry review",
            "role": "duelist",
            "prompt": (
                "Review this VALORANT duelist death. Return JSON with summary, labels, better_play, confidence. "
                "Round {round}, timestamp {timestamp}, labels {labels}, notes {notes}. "
                "Judge entry timing, trade path, escape plan, and whether utility or teammate contact enabled the fight."
            ),
        },
        "controller": {
            "name": "Controller timing review",
            "role": "controller",
            "prompt": (
                "Review this VALORANT controller death. Return JSON with summary, labels, better_play, confidence. "
                "Round {round}, timestamp {timestamp}, labels {labels}, notes {notes}. "
                "Judge smoke timing, map control, rotate timing, and whether the death happened through unsupported space."
            ),
        },
    }
    defaults.update(templates)
    return {"templates": defaults, "active": db.get_setting("active_prompt_template", "default")}


def save_prompt_template(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    key = str(payload.get("key") or "").strip()
    if not key:
        return {"ok": False, "message": "template key is required"}
    templates = prompt_templates(db)["templates"]
    templates[key] = {
        "name": str(payload.get("name") or key),
        "role": str(payload.get("role") or "all"),
        "prompt": str(payload.get("prompt") or ""),
    }
    custom = {item_key: value for item_key, value in templates.items() if item_key not in {"default", "duelist", "controller"}}
    db.set_setting("prompt_templates", json.dumps(custom))
    if payload.get("active"):
        db.set_setting("active_prompt_template", key)
    return {"ok": True, "templates": prompt_templates(db)}


def update_match_metadata(db: Database, match_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {}
    for key in ("map", "agent", "status", "duration", "started_at"):
        if key in payload:
            allowed[key] = payload.get(key)
    if not allowed:
        return {"ok": False, "message": "no metadata fields supplied"}
    db.update_match(match_id, **allowed)
    db.log("info", "metadata", f"Updated match #{match_id} metadata", allowed)
    return {"ok": True, "match": db.get_match(match_id)}


def save_benchmark_label(db: Database, payload: Dict[str, Any]) -> Dict[str, Any]:
    label = {
        "kind": "benchmark_label",
        "label_type": str(payload.get("label_type") or "").strip(),
        "match_id": int(payload.get("match_id") or 0),
        "death_id": int(payload.get("death_id") or 0) if payload.get("death_id") else None,
        "suggestion_id": int(payload.get("suggestion_id") or 0) if payload.get("suggestion_id") else None,
        "timestamp": optional_float(payload.get("timestamp")),
        "note": str(payload.get("note") or "").strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not label["match_id"] or label["label_type"] not in {"true_positive", "false_positive", "missed_death", "false_negative"}:
        return {"ok": False, "message": "match_id and valid label_type are required"}
    analysis_id = db.save_structured_analysis(label["match_id"], "benchmark_label", label)
    return {"ok": True, "id": analysis_id, "label": label, "evaluation": evaluation_benchmark(db)}


def benchmark_labels(db: Database) -> Dict[str, Any]:
    labels = [item for item in db.list_structured_analyses("match", limit=500) if item.get("analysis_type") == "benchmark_label"]
    counts: Dict[str, int] = {}
    for item in labels:
        kind = str((item.get("payload") or {}).get("label_type") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {"labels": labels, "count": len(labels), "counts": counts}


def detector_tuning(db: Database) -> Dict[str, Any]:
    feedback = db.detector_feedback_summary()
    labels = benchmark_labels(db)["counts"]
    current = db.get_setting("detector_sensitivity", "normal")
    score = 0
    score += int(feedback.get("rejected") or 0) * -1
    score += int(feedback.get("accepted") or 0)
    score += labels.get("false_positive", 0) * -2
    score += labels.get("missed_death", 0) * 2
    if score <= -2:
        recommended = "low"
    elif score >= 2:
        recommended = "high"
    else:
        recommended = "normal"
    result = {
        "current": current,
        "recommended": recommended,
        "score": score,
        "feedback": feedback,
        "benchmark_counts": labels,
        "summary": f"Detector tuning recommends {recommended} sensitivity from feedback and benchmark labels.",
    }
    db.save_structured_analysis(0, "detector_tuning", result)
    return result


def apply_detector_tuning(db: Database) -> Dict[str, Any]:
    tuning = detector_tuning(db)
    db.set_setting("detector_sensitivity", tuning["recommended"])
    return {"ok": True, "tuning": tuning}


def session_report(db: Database, session_id: Optional[int] = None) -> Dict[str, Any]:
    sessions = db.get_session_summary()
    target_session = None
    if session_id:
        target_session = next((item for item in sessions.get("recent", []) if int(item["id"]) == session_id), None)
    target_session = target_session or sessions.get("active") or (sessions.get("recent") or [{}])[0]
    trends = db.build_trends()
    coach = coach_dashboard_v2(db)
    top_labels = list((trends.get("labels") or {}).items())[:3]
    result = {
        "kind": "session_report",
        "session": target_session,
        "summary": f"Session focus: {(target_session or {}).get('focus_label') or 'none'}. Top issue: {top_labels[0][0] if top_labels else 'not enough data'}.",
        "top_mistakes": [{"label": label, "count": count} for label, count in top_labels],
        "best_improvement": infer_best_improvement(trends),
        "next_drills": ((coach.get("coach_v2") or {}).get("weekly_focus") or {}).get("drills") or [],
        "coach_v2": coach.get("coach_v2") or {},
    }
    db.save_structured_analysis(0, "session_report", result)
    return {"ok": True, "report": result}


def infer_best_improvement(trends: Dict[str, Any]) -> str:
    recent = trends.get("matches") or []
    if len(recent) < 2:
        return "Need at least two reviewed matches to infer improvement."
    latest = recent[0].get("death_count") or 0
    previous = recent[1].get("death_count") or 0
    if latest < previous:
        return f"Death count dropped from {previous} to {latest} in the latest reviewed match."
    return "No clear numeric improvement yet; keep annotating deaths and accepting/rejecting advice."


def model_audit(db: Database) -> Dict[str, Any]:
    audits = [
        item for item in db.list_structured_analyses("death", limit=500)
        if item.get("analysis_type") in {"local_model_audit", "local_model_request_budget", "local_ai_review"}
    ]
    return {
        "local_ai": local_ai_status(db),
        "records": audits,
        "count": len(audits),
        "summary": f"{len(audits)} local model audit/review record(s). Core app does not upload to cloud providers.",
    }


def redacted_debug_bundle(paths: Dict[str, Path], db: Database) -> Dict[str, Any]:
    export_dir = paths["data"] / "debug_bundles"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"debug-bundle-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "version": app_version(db),
        "diagnostics": installer_diagnostics(paths, db),
        "privacy_inventory": privacy_inventory(paths, db),
        "model_audit": model_audit(db),
        "logs": db.list_logs(200),
        "settings": {
            "auto_import": db.get_setting("auto_import", "false"),
            "auto_analysis": db.get_setting("auto_analysis", "false"),
            "detector_sensitivity": db.get_setting("detector_sensitivity", "normal"),
            "frame_sample_rate": db.get_setting("frame_sample_rate", "standard"),
        },
    }
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "path": str(target), "message": "Redacted debug bundle written locally."}


def optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def optional_bool(value: Any) -> Optional[bool]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None
