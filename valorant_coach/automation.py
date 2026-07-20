import json
import csv
import base64
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import request as urlrequest
from urllib.error import URLError

from .advice import generate_advice
from .analyzer import analyze_match, import_video, scan_recording_folder
from .clipper import extract_death_clips
from .coach import build_coach_dashboard, build_guided_match_coach
from .db import Database
from .deep_analysis import analyze_hud, analyze_minimap, analyze_ocr, infer_rounds_from_scoreboard
from .memory import build_memory_prompt_context, load_coach_memory_state, save_coach_memory_state, update_coach_memory_from_review
from .reports import build_report, write_markdown_report
from .clipper import ffmpeg_path
from .deep_analysis import tesseract_path
from .vision import (
    analyze_match_events,
    build_keyframe_gallery,
    build_local_ai_review_sequence,
    build_review_queue,
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
        ("suggest_deaths", "finding likely deaths from video signals", 64, lambda: suggest_deaths(db, match_id, dirs["vision"])),
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


APP_VERSION = "0.12.5-local"


def app_version(db: Database) -> Dict[str, Any]:
    git = git_version_info()
    return {
        "version": APP_VERSION,
        "build": f"git-{git['commit_count']}" if git.get("commit_count") else "local-dev",
        "git": git,
        "schema": db.schema_info(),
        "changelog": [
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
    provider = str(db.get_setting("local_ai_provider", "custom-command") or "custom-command")
    command = str(db.get_setting("local_ai_command", "") or "").strip()
    base_url = str(db.get_setting("local_ai_base_url", default_base_url(provider)) or "").strip()
    model = str(db.get_setting("local_ai_model", default_model(provider)) or "").strip()
    purpose = str(db.get_setting("local_ai_purpose", "coach") or "coach").strip()
    review_mode = str(db.get_setting("local_ai_review_mode", "contact") or "contact").strip()
    review_fps = str(db.get_setting("local_ai_review_fps", "") or "").strip()
    sequence_profile = local_ai_sequence_profile(review_mode, review_fps)
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
    review_mode = local_ai_sequence_profile(str(payload.get("review_mode") or "contact"), review_fps)["id"]
    db.set_setting("local_ai_provider", provider)
    db.set_setting("local_ai_purpose", purpose)
    db.set_setting("local_ai_command", command)
    db.set_setting("local_ai_base_url", base_url)
    db.set_setting("local_ai_model", model)
    db.set_setting("local_ai_review_mode", review_mode)
    db.set_setting("local_ai_review_fps", review_fps)
    db.log("info", "local-ai", "Updated local AI configuration", {"provider": provider, "purpose": purpose, "review_mode": review_mode, "review_fps": review_fps, "configured": bool(command or base_url)})
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


def test_local_ai_connection(db: Database, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    if payload:
        provider = str(payload.get("provider") or "lmstudio").strip()
        status = {
            "provider": provider,
            "purpose": str(payload.get("purpose") or "coach").strip(),
            "review_mode": local_ai_sequence_profile(str(payload.get("review_mode") or "contact"), payload.get("review_fps"))["id"],
            "review_fps": normalize_review_fps_setting(payload.get("review_fps")),
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
    sequence_profile = local_ai_sequence_profile(str(status.get("review_mode") or "contact"), status.get("review_fps"))
    sequence = build_local_ai_review_sequence(db, death_id, db.path.parent / "vision", mode=sequence_profile["id"], fps_override=status.get("review_fps"))
    if not sequence.get("ok"):
        return {"ok": False, "message": sequence.get("message") or "Could not prepare local AI review sequence.", "status": status}
    request = {
        "death": death,
        "annotations": death.get("annotations") or [],
        "clip_path": death.get("clip_path"),
        "keyframes": keyframe_payload(db, death_id, analysis_type="local_ai_sequence", limit=int(sequence_profile["limit"])),
        "prompt": render_model_prompt(db, death),
        "privacy": "local-only",
    }
    db.save_death_analysis(death_id, "local_model_audit", redact_model_request(request, status))
    if status["provider"] != "custom-command":
        return run_local_http_review(db, death_id, request, status)
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
    update_coach_memory_from_review(db, death, result)
    return {"ok": True, "message": result["summary"], "analysis": result, "status": status}


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
        + build_memory_prompt_context(db)
        + f"\n\nYou will receive an ordered frame sequence using this sampling mode: {sequence_profile['label']}. "
        "Treat the images as a short local video clip in chronological order. Track crosshair movement, clearing path, movement while aiming, minimap/HUD changes, and fight setup over time. "
        "Enemies can appear for only one or two frames, so scan every frame for a visible opponent, damage cue, tracer, muzzle flash, or sudden contact. "
        "Use only visible evidence from those frames. Do not assume enemy positions, player intent, comms, utility usage, or the outcome unless visible. "
        "If the frames do not prove a claim, write 'insufficient visual evidence' and reduce confidence. "
        "Return strict JSON with keys: summary, visible_evidence, labels, better_play, drill, confidence."
    )


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
    if len(keyframes) > 30:
        return run_local_http_review_batched(db, death_id, payload, status, chunk_size=30)
    return run_local_http_review_single(db, death_id, payload, status)


def run_local_http_review_single(db: Database, death_id: int, payload: Dict[str, Any], status: Dict[str, Any], save: bool = True) -> Dict[str, Any]:
    provider = status["provider"]
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
        response = post_json(endpoint, body, timeout=240)
    except Exception as exc:
        return {"ok": False, "message": f"{provider} request failed: {exc}", "status": status}
    text = response.get("response") or (((response.get("choices") or [{}])[0].get("message") or {}).get("content")) or json.dumps(response)
    result = parse_model_review(text, provider)
    if save:
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
    combined["batch_reviews"] = chunk_reviews
    combined["batches"] = len(chunk_reviews)
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
        "Return strict JSON with summary, visible_evidence, labels, better_play, drill, confidence. "
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
        response = post_json(endpoint, body, timeout=180)
        text = response.get("response") or (((response.get("choices") or [{}])[0].get("message") or {}).get("content")) or json.dumps(response)
        result = parse_model_review(text, provider)
    except Exception:
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
        "Return strict JSON with summary, visible_evidence, labels, better_play, drill, confidence.\n\n"
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
    return {
        "kind": "local_ai_review",
        "summary": "Batched Local AI review completed. " + " ".join(summaries[:2])[:450],
        "visible_evidence": evidence[:8],
        "labels": sorted(set(labels))[:6],
        "better_play": next((str(review.get("better_play")) for review in chunk_reviews if review.get("better_play")), ""),
        "drill": next((str(review.get("drill")) for review in chunk_reviews if review.get("drill")), ""),
        "confidence": confidence or 0.5,
        "status": "completed",
        "provider": provider,
    }


def post_json(url: str, body: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def get_json(url: str, timeout: int = 60) -> Dict[str, Any]:
    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def parse_model_review(text: str, provider: str) -> Dict[str, Any]:
    text = strip_json_fence(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"summary": text}
    result = {
        "kind": "local_ai_review",
        "summary": str(parsed.get("summary") or parsed.get("what_happened") or text[:500]),
        "visible_evidence": normalize_text_list(parsed.get("visible_evidence") or parsed.get("evidence") or []),
        "labels": normalize_text_list(parsed.get("labels") or parsed.get("suggested_labels") or []),
        "better_play": str(parsed.get("better_play") or parsed.get("recommendation") or ""),
        "drill": str(parsed.get("drill") or ""),
        "confidence": float(parsed.get("confidence") or 0.55),
        "status": "completed",
        "provider": provider,
    }
    if parsed.get("extracted_text") is not None:
        result["extracted_text"] = str(parsed.get("extracted_text") or "")
    if parsed.get("scoreboard") is not None:
        result["scoreboard"] = parsed.get("scoreboard")
    return result


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
        "Return strict compact JSON with summary, visible_evidence, labels, better_play, drill, confidence. "
        "Analyze how the player clears, moves, aims, checks HUD/minimap, and enters the fight across time. Enemies may be visible for only one or two frames, so inspect the whole sequence frame-by-frame. "
        "Use only visible frame evidence. Do not invent hidden enemies, comms, unseen utility, prior context, or player intent. "
        "If the sequence is visually insufficient, say 'insufficient visual evidence' and set confidence below 0.45."
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
    return {
        "kind": "local_model_audit",
        "provider": status.get("provider"),
        "model": status.get("model"),
        "base_url": status.get("base_url"),
        "sent_clip_path": bool(payload.get("clip_path")),
        "sent_keyframes": len(payload.get("keyframes") or []),
        "sent_image_bytes": sum(len(item.get("image_base64") or "") for item in payload.get("keyframes") or []),
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
    for key in ("recording_dir", "auto_import", "auto_analysis", "frame_sample_rate", "detector_sensitivity"):
        if key in payload:
            db.set_setting(key, str(payload.get(key) or ""))
    if any(key in payload for key in ("local_ai_provider", "local_ai_command", "local_ai_base_url", "local_ai_model", "local_ai_review_mode", "local_ai_review_fps")):
        save_local_ai_config(
            db,
            {
                "provider": payload.get("local_ai_provider"),
                "command": payload.get("local_ai_command"),
                "base_url": payload.get("local_ai_base_url"),
                "model": payload.get("local_ai_model"),
                "review_mode": payload.get("local_ai_review_mode"),
                "review_fps": payload.get("local_ai_review_fps"),
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
        if item.get("analysis_type") in {"local_model_audit", "local_ai_review"}
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
