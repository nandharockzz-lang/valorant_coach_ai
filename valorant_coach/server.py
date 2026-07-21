import json
import mimetypes
import os
import shutil
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .advice import generate_advice
from .analyzer import analyze_match, import_video, scan_recording_folder
from .automation import (
    JobManager,
    RecordingWatcher,
    advanced_search,
    analytics_dashboard,
    app_version,
    apply_correction,
    apply_detector_tuning,
    apply_retention,
    backup_database,
    benchmark_labels,
    cleanup_storage,
    delete_playbook,
    detector_tuning,
    evaluation_benchmark,
    export_memory,
    export_report,
    import_stats,
    import_memory,
    installer_diagnostics,
    list_backups,
    list_annotations,
    list_corrections,
    local_ai_status,
    model_audit,
    playbooks,
    plugin_registry,
    prompt_templates,
    privacy_audit,
    privacy_export,
    privacy_inventory,
    privacy_wipe,
    provider_registry,
    redacted_debug_bundle,
    restore_database,
    run_auto_coach_pipeline,
    run_death_batch,
    run_full_vod_coach_pipeline,
    run_local_ai_review,
    run_match_pipeline,
    run_suggest_deaths_job,
    save_benchmark_label,
    save_clip_review_feedback,
    save_clip_training_label,
    save_coach_moment_feedback,
    save_clip_annotation,
    save_local_ai_config,
    save_manual_correction,
    save_playbook,
    save_prompt_template,
    save_setup_wizard,
    scan_and_maybe_analyze,
    search_deaths,
    session_report,
    setup_wizard_status,
    smart_review_queue_v2,
    storage_stats,
    test_local_ai_connection,
    tool_status,
    coach_dashboard_v2,
    reconstruct_round_story,
    update_match_metadata,
)
from .clipper import extract_death_clips, ffmpeg_path
from .coach import build_coach_dashboard, build_guided_match_coach, build_match_review
from .db import Database
from .detector import (
    build_detector_candidates,
    detector_status,
    detector_training_dashboard,
    evaluate_detector_dataset,
    export_detector_dataset,
    list_detector_candidates,
    prelabel_detector_candidates,
    save_detector_annotation,
    train_detector,
)
from .deep_analysis import (
    ai_review_status,
    analyze_gameplay,
    analyze_hud,
    analyze_minimap,
    analyze_ocr,
    infer_rounds_from_scoreboard,
    tesseract_path,
)
from .knowledge import knowledge_status, prompt_preview, rebuild_knowledge_base, search_knowledge
from .reports import build_report, save_death_context_correction, write_markdown_report
from .signals import signal_registry
from .vision import (
    analyze_match_events,
    build_keyframe_gallery,
    build_review_queue,
    describe_clip,
    reconstruct_rounds,
    score_crosshair_match,
    understand_clip,
)


if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    ASSET_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    ASSET_ROOT = ROOT

DATA_DIR = ROOT / "data"
STATIC_DIR = ASSET_ROOT / "static"
REPORTS_DIR = ROOT / "reports"
CLIPS_DIR = ROOT / "clips"
VISION_DIR = ROOT / "data" / "vision"
DEEP_DIR = ROOT / "data" / "deep"
KNOWLEDGE_DIR = ROOT / "knowledge"
DB = Database(DATA_DIR / "coach.sqlite3")
PATHS = {
    "data": DATA_DIR,
    "reports": REPORTS_DIR,
    "clips": CLIPS_DIR,
    "vision": VISION_DIR,
    "deep": DEEP_DIR,
    "knowledge": KNOWLEDGE_DIR,
}
JOBS = JobManager(DB)
WATCHER = RecordingWatcher(JOBS)


class CoachHandler(BaseHTTPRequestHandler):
    server_version = "ValorantCoach/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif parsed.path.startswith("/static/"):
            rel = parsed.path.removeprefix("/static/")
            self.serve_file(STATIC_DIR / rel)
        elif parsed.path == "/api/health":
            self.json_response({"ok": True})
        elif parsed.path == "/api/version":
            self.json_response(app_version(DB))
        elif parsed.path == "/api/providers":
            self.json_response(provider_registry())
        elif parsed.path == "/api/plugins":
            self.json_response(plugin_registry(DB))
        elif parsed.path == "/api/local-ai":
            self.json_response(local_ai_status(DB))
        elif parsed.path == "/api/knowledge/status":
            self.json_response(knowledge_status(KNOWLEDGE_DIR))
        elif parsed.path == "/api/knowledge/search":
            query = parse_qs(parsed.query)
            self.json_response(
                search_knowledge(
                    KNOWLEDGE_DIR,
                    query=str((query.get("q") or [""])[0]),
                    context={
                        "map": (query.get("map") or [""])[0],
                        "agent": (query.get("agent") or [""])[0],
                        "labels": (query.get("topic") or [""])[0],
                    },
                )
            )
        elif parsed.path == "/api/knowledge/prompt-preview":
            query = parse_qs(parsed.query)
            death_id = int((query.get("death_id") or ["0"])[0] or 0)
            self.json_response(prompt_preview(DB, death_id, KNOWLEDGE_DIR))
        elif parsed.path == "/api/setup":
            self.json_response(setup_wizard_status(PATHS, DB))
        elif parsed.path == "/api/prompts":
            self.json_response(prompt_templates(DB))
        elif parsed.path == "/api/diagnostics":
            self.json_response(installer_diagnostics(PATHS, DB))
        elif parsed.path == "/api/evaluation":
            self.json_response(evaluation_benchmark(DB))
        elif parsed.path == "/api/evaluation/labels":
            self.json_response(benchmark_labels(DB))
        elif parsed.path == "/api/detector/tuning":
            self.json_response(detector_tuning(DB))
        elif parsed.path == "/api/detector/status":
            self.json_response(detector_status(DB, DATA_DIR))
        elif parsed.path == "/api/detector/dashboard":
            self.json_response(detector_training_dashboard(DB, DATA_DIR))
        elif parsed.path == "/api/detector/candidates":
            query = parse_qs(parsed.query)
            match_id = parse_optional_int((query.get("match_id") or [""])[0])
            limit = parse_optional_int((query.get("limit") or ["120"])[0]) or 120
            self.json_response(list_detector_candidates(DB, match_id, limit))
        elif parsed.path == "/api/capabilities":
            ffmpeg = ffmpeg_path()
            tesseract = tesseract_path()
            pyinstaller = shutil.which("pyinstaller")
            self.json_response(
                {
                    "ffmpeg": bool(ffmpeg),
                    "ffmpeg_path": ffmpeg,
                    "tesseract": bool(tesseract),
                    "tesseract_path": tesseract,
                    "pyinstaller": bool(pyinstaller),
                    "pyinstaller_path": pyinstaller or "",
                }
            )
        elif parsed.path == "/api/settings":
            self.json_response(self.settings_payload())
        elif parsed.path == "/api/jobs":
            self.json_response({"jobs": JOBS.list()})
        elif parsed.path == "/api/logs":
            self.json_response({"logs": DB.list_logs()})
        elif parsed.path == "/api/schema":
            self.json_response(DB.schema_info())
        elif parsed.path == "/api/signals":
            self.json_response(signal_registry())
        elif parsed.path == "/api/watcher":
            self.json_response({"watcher": WATCHER.status(), "settings": self.settings_payload()})
        elif parsed.path == "/api/storage":
            self.json_response({"storage": storage_stats(PATHS, DB)})
        elif parsed.path == "/api/backups":
            self.json_response(list_backups(PATHS))
        elif parsed.path == "/api/tools":
            self.json_response(tool_status())
        elif parsed.path == "/api/privacy":
            self.json_response(privacy_audit(PATHS, DB))
        elif parsed.path == "/api/privacy/inventory":
            self.json_response(privacy_inventory(PATHS, DB))
        elif parsed.path == "/api/privacy/model-audit":
            self.json_response(model_audit(DB))
        elif parsed.path == "/api/corrections":
            self.json_response(list_corrections(DB))
        elif parsed.path == "/api/annotations":
            self.json_response(list_annotations(DB))
        elif parsed.path == "/api/memory/export":
            self.json_response(export_memory(DB))
        elif parsed.path == "/api/analytics":
            self.json_response(analytics_dashboard(DB))
        elif parsed.path == "/api/playbooks":
            self.json_response(playbooks(DB))
        elif parsed.path == "/api/calibration":
            self.json_response({"regions": DB.get_calibration()})
        elif parsed.path == "/api/matches":
            self.json_response({"matches": DB.list_matches()})
        elif parsed.path == "/api/trends":
            self.json_response(DB.build_trends())
        elif parsed.path == "/api/coach":
            self.json_response(build_coach_dashboard(DB))
        elif parsed.path == "/api/coach/v2":
            self.json_response(coach_dashboard_v2(DB))
        elif parsed.path == "/api/sessions/report":
            self.json_response(session_report(DB))
        elif parsed.path.startswith("/api/deaths/") and parsed.path.endswith("/clip"):
            self.handle_death_clip_get(parsed.path)
        elif parsed.path.startswith("/api/vision/frame/"):
            self.handle_vision_frame_get(parsed.path)
        elif parsed.path.startswith("/api/matches/"):
            self.handle_match_get(parsed.path)
        else:
            self.not_found()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/settings":
                payload = self.read_json()
                for key in (
                    "recording_dir",
                    "player_name",
                    "auto_import",
                    "auto_analysis",
                    "privacy_mode",
                    "detector_sensitivity",
                    "enemy_detector_command",
                    "enemy_detector_model_path",
                    "ocr_engine",
                    "frame_sample_rate",
                    "death_scan_max_ocr_frames",
                    "storage_cleanup_days",
                    "max_concurrent_jobs",
                    "skip_completed_analysis",
                ):
                    if key in payload:
                        DB.set_setting(key, str(payload.get(key) or ""))
                self.json_response({"ok": True, "settings": self.settings_payload()})
            elif parsed.path == "/api/calibration":
                payload = self.read_json()
                regions = payload.get("regions") or {}
                if not isinstance(regions, dict):
                    self.bad_request("regions must be an object")
                    return
                DB.save_calibration(regions)
                self.json_response({"ok": True, "regions": DB.get_calibration()})
            elif parsed.path == "/api/scan":
                self.json_response({"ok": True, **scan_and_maybe_analyze(DB, PATHS, JOBS)})
            elif parsed.path == "/api/watcher/start":
                WATCHER.start(DB, PATHS)
                DB.set_setting("auto_import", "true")
                self.json_response({"ok": True, "watcher": WATCHER.status()})
            elif parsed.path == "/api/watcher/stop":
                WATCHER.stop()
                DB.set_setting("auto_import", "false")
                self.json_response({"ok": True, "watcher": WATCHER.status()})
            elif parsed.path == "/api/memory/import":
                self.json_response(import_memory(DB, self.read_json()))
            elif parsed.path == "/api/setup":
                self.json_response(save_setup_wizard(DB, self.read_json()))
            elif parsed.path == "/api/local-ai/config":
                self.json_response(save_local_ai_config(DB, self.read_json()))
            elif parsed.path == "/api/local-ai/test":
                self.json_response(test_local_ai_connection(DB, self.read_json()))
            elif parsed.path == "/api/knowledge/rebuild":
                payload = self.read_json()
                fetch_remote = str(payload.get("fetch_remote", "true")).lower() not in {"0", "false", "no", "off"}
                self.json_response(rebuild_knowledge_base(KNOWLEDGE_DIR, fetch_remote=fetch_remote))
            elif parsed.path == "/api/prompts":
                self.json_response(save_prompt_template(DB, self.read_json()))
            elif parsed.path == "/api/evaluation/labels":
                self.json_response(save_benchmark_label(DB, self.read_json()))
            elif parsed.path == "/api/detector/tuning/apply":
                self.json_response(apply_detector_tuning(DB))
            elif parsed.path == "/api/detector/export":
                self.json_response(export_detector_dataset(DB, DATA_DIR, self.read_json()))
            elif parsed.path == "/api/detector/train":
                payload = self.read_json()
                job_id = JOBS.start(
                    "Train enemy detector",
                    lambda update, options=payload: detector_training_job(options, update),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path == "/api/detector/candidates":
                self.json_response(build_detector_candidates(DB, DATA_DIR, self.read_json()))
            elif parsed.path == "/api/detector/prelabel":
                self.json_response(prelabel_detector_candidates(DB, DATA_DIR, self.read_json()))
            elif parsed.path == "/api/detector/evaluate":
                self.json_response(evaluate_detector_dataset(DB, DATA_DIR, self.read_json()))
            elif parsed.path == "/api/privacy/export":
                self.json_response(privacy_export(PATHS, DB))
            elif parsed.path == "/api/privacy/wipe":
                self.json_response(privacy_wipe(PATHS, DB, self.read_json()))
            elif parsed.path == "/api/privacy/debug-bundle":
                self.json_response(redacted_debug_bundle(PATHS, DB))
            elif parsed.path == "/api/storage/cleanup":
                payload = self.read_json()
                targets = payload.get("targets") or []
                if isinstance(targets, str):
                    targets = [targets]
                self.json_response(cleanup_storage(PATHS, [str(item) for item in targets]))
            elif parsed.path == "/api/storage/retention":
                self.json_response(apply_retention(PATHS, DB))
            elif parsed.path == "/api/backups/create":
                self.json_response(backup_database(PATHS))
            elif parsed.path == "/api/backups/restore":
                payload = self.read_json()
                self.json_response(restore_database(PATHS, str(payload.get("path") or "")))
            elif parsed.path == "/api/corrections":
                self.json_response(save_manual_correction(DB, self.read_json()))
            elif parsed.path.startswith("/api/corrections/") and parsed.path.endswith("/apply"):
                correction_id = int(parsed.path.split("/")[3])
                self.json_response(apply_correction(DB, correction_id))
            elif parsed.path == "/api/playbooks":
                self.json_response(save_playbook(DB, self.read_json()))
            elif parsed.path == "/api/search/deaths":
                self.json_response(search_deaths(DB, self.read_json()))
            elif parsed.path == "/api/search/advanced":
                self.json_response(advanced_search(DB, self.read_json()))
            elif parsed.path == "/api/stats/import":
                payload = self.read_json()
                self.json_response(import_stats(DB, Path(str(payload.get("path") or ""))))
            elif parsed.path == "/api/sessions/report":
                payload = self.read_json()
                session_id = int(payload.get("session_id") or 0) or None
                self.json_response(session_report(DB, session_id))
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = int(parsed.path.split("/")[3])
                JOBS.cancel(job_id)
                self.json_response({"ok": True})
            elif parsed.path == "/api/videos/import":
                payload = self.read_json()
                path = Path(str(payload.get("path") or ""))
                if not path.exists():
                    self.bad_request("video path does not exist")
                    return
                match_id = import_video(DB, path)
                self.json_response({"ok": True, "match_id": match_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/analyze"):
                match_id = int(parsed.path.split("/")[3])
                result = analyze_match(DB, match_id)
                clip_result = self.extract_clips_for_match(match_id)
                write_markdown_report(DB, REPORTS_DIR, match_id)
                self.json_response({"ok": True, **result, "clips": clip_result})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/pipeline"):
                match_id = int(parsed.path.split("/")[3])
                job_id = JOBS.start(
                    f"Analyze match #{match_id}",
                    lambda update, mid=match_id: run_match_pipeline(DB, mid, PATHS, update),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path.startswith("/api/matches/") and (
                parsed.path.endswith("/auto-coach")
                or parsed.path.endswith("/auto_coach")
                or parsed.path.endswith("/autocoach")
                or parsed.path.endswith("/autoCoach")
            ):
                match_id = int(parsed.path.split("/")[3])
                job_id = JOBS.start(
                    f"Auto coach match #{match_id}",
                    lambda update, mid=match_id: run_auto_coach_pipeline(DB, mid, PATHS, update),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/full-vod-coach"):
                match_id = int(parsed.path.split("/")[3])
                job_id = JOBS.start(
                    f"Full VOD coach match #{match_id}",
                    lambda update, mid=match_id: run_full_vod_coach_pipeline(DB, mid, PATHS, update),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/coach-moment-feedback"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(save_coach_moment_feedback(DB, match_id, self.read_json()))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/batch-deaths"):
                match_id = int(parsed.path.split("/")[3])
                job_id = JOBS.start(
                    f"Batch deaths match #{match_id}",
                    lambda update, mid=match_id: batch_death_job(mid, update),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/clips"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response({"ok": True, "clips": self.extract_clips_for_match(match_id)})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/coach-review"):
                match_id = int(parsed.path.split("/")[3])
                review = build_match_review(DB, match_id)
                self.json_response({"ok": True, "review": review, "coach": build_coach_dashboard(DB)})
            elif parsed.path.startswith("/api/matches/") and (
                parsed.path.endswith("/guided-coach")
                or parsed.path.endswith("/guided_coach")
                or parsed.path.endswith("/coach")
                or parsed.path.endswith("/coach-me")
            ):
                match_id = int(parsed.path.split("/")[3])
                guided = build_guided_match_coach(DB, match_id)
                self.json_response({"ok": True, "guided_coach": guided, "coach": build_coach_dashboard(DB)})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/suggest-deaths"):
                match_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                job_id = JOBS.start(
                    f"Find Deaths match #{match_id}",
                    lambda update, mid=match_id, options=payload: run_suggest_deaths_job(DB, mid, PATHS, update, options=options),
                )
                self.json_response({"ok": True, "job_id": job_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/events-v2"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(analyze_match_events(DB, match_id, VISION_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/rounds/reconstruct"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(reconstruct_rounds(DB, match_id, VISION_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/crosshair"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(score_crosshair_match(DB, match_id, VISION_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/review-queue"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(build_review_queue(DB, match_id))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/review-queue-v2"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(smart_review_queue_v2(DB, match_id))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/story"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(reconstruct_round_story(DB, match_id))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/playbook"):
                match_id = int(parsed.path.split("/")[3])
                match = DB.get_match(match_id)
                if not match:
                    self.not_found()
                    return
                self.json_response(playbooks(DB, match))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/metadata"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(update_match_metadata(DB, match_id, self.read_json()))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/hud"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(analyze_hud(DB, match_id, DEEP_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/minimap"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(analyze_minimap(DB, match_id, DEEP_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/ocr"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(analyze_ocr(DB, match_id, DEEP_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/scoreboard-rounds"):
                match_id = int(parsed.path.split("/")[3])
                self.json_response(infer_rounds_from_scoreboard(DB, match_id, DEEP_DIR))
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/deaths"):
                match_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                death_id = DB.create_death(
                    match_id=match_id,
                    round_number=parse_optional_int(payload.get("round_number")),
                    timestamp=parse_optional_float(payload.get("timestamp")),
                    labels=normalize_labels(payload.get("mistake_labels") or payload.get("labels") or []),
                    notes=str(payload.get("notes") or ""),
                    confidence=float(payload.get("confidence") or 0),
                )
                self.json_response({"ok": True, "death_id": death_id})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/report/write"):
                match_id = int(parsed.path.split("/")[3])
                path = write_markdown_report(DB, REPORTS_DIR, match_id)
                self.json_response({"ok": True, "path": str(path)})
            elif parsed.path.startswith("/api/matches/") and parsed.path.endswith("/report/export"):
                match_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                self.json_response(export_report(DB, match_id, str(payload.get("format") or "json")))
            elif parsed.path == "/api/coach/profile":
                payload = self.read_json()
                DB.save_profile(
                    rank=str(payload.get("rank") or "").strip(),
                    main_agents=normalize_labels(payload.get("main_agents") or []),
                    target_style=str(payload.get("target_style") or "").strip(),
                    notes=str(payload.get("notes") or "").strip(),
                )
                self.json_response({"ok": True, "coach": build_coach_dashboard(DB)})
            elif parsed.path == "/api/coach/goals":
                payload = self.read_json()
                goal_id = DB.create_goal(
                    focus_label=str(payload.get("focus_label") or "").strip(),
                    description=str(payload.get("description") or "").strip(),
                    target_matches=int(payload.get("target_matches") or 2),
                )
                self.json_response({"ok": True, "goal_id": goal_id, "coach": build_coach_dashboard(DB)})
            elif parsed.path == "/api/sessions/start":
                payload = self.read_json()
                session_id = DB.start_play_session(
                    name=str(payload.get("name") or "VALORANT Session").strip(),
                    focus_label=str(payload.get("focus_label") or "").strip(),
                    notes=str(payload.get("notes") or "").strip(),
                )
                self.json_response({"ok": True, "session_id": session_id, "coach": build_coach_dashboard(DB)})
            elif parsed.path == "/api/sessions/end":
                payload = self.read_json()
                session_id = int(payload.get("session_id") or 0)
                if not session_id:
                    active = DB.get_active_play_session()
                    session_id = int((active or {}).get("id") or 0)
                if not session_id:
                    self.bad_request("No active session to end.")
                    return
                DB.end_play_session(session_id, str(payload.get("notes") or "").strip())
                self.json_response({"ok": True, "coach": build_coach_dashboard(DB)})
            elif parsed.path.startswith("/api/coach/goals/") and parsed.path.endswith("/complete"):
                goal_id = int(parsed.path.split("/")[4])
                DB.complete_goal(goal_id)
                self.json_response({"ok": True, "coach": build_coach_dashboard(DB)})
            elif parsed.path.startswith("/api/advice/") and parsed.path.endswith("/feedback"):
                advice_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                feedback_id = DB.save_advice_feedback(
                    advice_id=advice_id,
                    verdict=str(payload.get("verdict") or "").strip(),
                    note=str(payload.get("note") or "").strip(),
                )
                self.json_response({"ok": True, "feedback_id": feedback_id, "coach": build_coach_dashboard(DB)})
            elif parsed.path.startswith("/api/suggestions/"):
                suggestion_id = int(parsed.path.split("/")[3])
                payload = self.read_json()
                action = str(payload.get("action") or "").strip()
                suggestion = DB.get_death_suggestion(suggestion_id)
                if not suggestion:
                    self.not_found()
                    return
                if action == "accept":
                    death_id = DB.create_death(
                        match_id=int(suggestion["match_id"]),
                        round_number=parse_optional_int(payload.get("round_number")),
                        timestamp=parse_optional_float(payload.get("timestamp")) or float(suggestion["timestamp"]),
                        labels=normalize_labels(payload.get("mistake_labels") or ["needs manual review"]),
                        notes=str(payload.get("notes") or suggestion["reason"]),
                        confidence=float(payload.get("confidence") or suggestion["confidence"] or 0),
                    )
                    DB.update_death_suggestion_status(suggestion_id, "accepted")
                    DB.save_detector_feedback(suggestion, "accepted", {"source": "suggestion_action"})
                    cleaned = DB.cleanup_pending_death_suggestions(int(suggestion["match_id"]))
                    self.json_response({"ok": True, "death_id": death_id, "cleaned_duplicates": cleaned})
                elif action == "reject":
                    DB.update_death_suggestion_status(suggestion_id, "rejected")
                    DB.save_detector_feedback(suggestion, "rejected", {"source": "suggestion_action"})
                    cleaned = DB.cleanup_pending_death_suggestions(int(suggestion["match_id"]))
                    self.json_response({"ok": True, "cleaned_duplicates": cleaned})
                else:
                    self.bad_request("action must be accept or reject")
            elif parsed.path.startswith("/api/deaths/"):
                death_id = int(parsed.path.split("/")[3])
                if parsed.path.endswith("/vision"):
                    self.json_response(describe_clip(DB, death_id))
                elif parsed.path.endswith("/keyframes"):
                    self.json_response(build_keyframe_gallery(DB, death_id, VISION_DIR))
                elif parsed.path.endswith("/understand"):
                    self.json_response(understand_clip(DB, death_id))
                elif parsed.path.endswith("/gameplay"):
                    self.json_response(analyze_gameplay(DB, death_id))
                elif parsed.path.endswith("/ai-review"):
                    self.json_response(ai_review_status(DB, death_id))
                elif parsed.path.endswith("/local-ai-review"):
                    self.json_response(run_local_ai_review(DB, death_id))
                elif parsed.path.endswith("/review-feedback"):
                    self.json_response(save_clip_review_feedback(DB, death_id, self.read_json()))
                elif parsed.path.endswith("/training-label"):
                    self.json_response(save_clip_training_label(DB, death_id, self.read_json()))
                elif parsed.path.endswith("/detector-annotations"):
                    self.json_response(save_detector_annotation(DB, death_id, self.read_json()))
                elif parsed.path.endswith("/annotations"):
                    self.json_response(save_clip_annotation(DB, death_id, self.read_json()))
                elif parsed.path.endswith("/advice"):
                    self.json_response({"ok": True, "advice": generate_advice(DB, death_id)})
                elif parsed.path.endswith("/context"):
                    self.json_response(save_death_context_correction(DB, death_id, self.read_json()))
                else:
                    payload = self.read_json()
                    DB.update_death_full(
                        death_id,
                        round_number=parse_optional_int(payload.get("round_number")),
                        timestamp=parse_optional_float(payload.get("timestamp")),
                        labels=normalize_labels(payload.get("mistake_labels") or payload.get("labels") or []),
                        notes=str(payload.get("notes") or ""),
                        confidence=float(payload.get("confidence") or 0),
                    )
                    self.json_response({"ok": True})
            else:
                self.not_found()
        except Exception as exc:
            DB.add_log("error", "http", str(exc), {"path": parsed.path, "traceback": traceback.format_exc()})
            self.error_response(500, str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/playbooks/"):
                key = unquote(parsed.path.removeprefix("/api/playbooks/"))
                if not key:
                    self.bad_request("playbook key is required")
                    return
                self.json_response(delete_playbook(DB, key))
            elif parsed.path.startswith("/api/deaths/"):
                death_id = int(parsed.path.split("/")[3])
                DB.delete_death(death_id)
                self.json_response({"ok": True})
            else:
                self.not_found()
        except Exception as exc:
            DB.add_log("error", "http", str(exc), {"path": parsed.path, "traceback": traceback.format_exc()})
            self.error_response(500, str(exc))

    def handle_match_get(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            self.not_found()
            return
        match_id = int(parts[2])
        if len(parts) == 4 and parts[3] == "video":
            match = DB.get_match(match_id)
            if not match:
                self.not_found()
                return
            self.stream_file(Path(match["video_path"]))
        elif len(parts) == 3:
            report = build_report(DB, match_id)
            self.json_response(report)
        elif len(parts) == 4 and parts[3] == "report":
            report = build_report(DB, match_id)
            self.json_response(report)
        else:
            self.not_found()

    def handle_death_clip_get(self, path: str) -> None:
        death_id = int(path.strip("/").split("/")[2])
        death = DB.get_death(death_id)
        if not death or not death.get("clip_path"):
            self.not_found()
            return
        clip_path = Path(death["clip_path"]).resolve()
        if not str(clip_path).startswith(str(CLIPS_DIR.resolve())):
            self.not_found()
            return
        self.stream_file(clip_path)

    def handle_vision_frame_get(self, path: str) -> None:
        frame_id = path.strip("/").split("/")[-1]
        matches = list(VISION_DIR.glob(f"**/{frame_id}.jpg")) + list(DEEP_DIR.glob(f"**/{frame_id}.jpg"))
        if not matches:
            self.not_found()
            return
        frame_path = matches[0].resolve()
        if not (str(frame_path).startswith(str(VISION_DIR.resolve())) or str(frame_path).startswith(str(DEEP_DIR.resolve()))):
            self.not_found()
            return
        self.stream_file(frame_path)

    def extract_clips_for_match(self, match_id: int) -> Dict[str, Any]:
        match = DB.get_match(match_id)
        if not match:
            raise ValueError(f"Unknown match id: {match_id}")
        return extract_death_clips(DB, match_id, Path(match["video_path"]), CLIPS_DIR)

    def settings_payload(self) -> Dict[str, Any]:
        return {
            "recording_dir": DB.get_setting("recording_dir", ""),
            "player_name": DB.get_setting("player_name", "SicaJR"),
            "auto_import": DB.get_setting("auto_import", "false"),
            "auto_analysis": DB.get_setting("auto_analysis", "false"),
            "privacy_mode": DB.get_setting("privacy_mode", "local-only"),
            "detector_sensitivity": DB.get_setting("detector_sensitivity", "normal"),
            "enemy_detector_command": DB.get_setting("enemy_detector_command", ""),
            "enemy_detector_model_path": DB.get_setting("enemy_detector_model_path", ""),
            "ocr_engine": DB.get_setting("ocr_engine", "tesseract"),
            "frame_sample_rate": DB.get_setting("frame_sample_rate", "standard"),
            "death_scan_max_ocr_frames": DB.get_setting("death_scan_max_ocr_frames", "180"),
            "storage_cleanup_days": DB.get_setting("storage_cleanup_days", "30"),
            "max_concurrent_jobs": DB.get_setting("max_concurrent_jobs", "1"),
            "skip_completed_analysis": DB.get_setting("skip_completed_analysis", "true"),
        }

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def serve_file(self, path: Path, content_type: str = "") -> None:
        path = path.resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())):
            self.not_found()
            return
        if not path.exists() or not path.is_file():
            self.not_found()
            return
        if not content_type:
            guessed, _ = mimetypes.guess_type(str(path))
            content_type = guessed or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_file(self, path: Path) -> None:
        path = path.resolve()
        if not path.exists() or not path.is_file():
            self.not_found()
            return

        size = path.stat().st_size
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        start = 0
        end = size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            status = 206
            value = range_header.removeprefix("bytes=").split(",", 1)[0]
            left, _, right = value.partition("-")
            if left:
                start = int(left)
            if right:
                end = int(right)
            end = min(end, size - 1)

        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def bad_request(self, message: str) -> None:
        self.error_response(400, message)

    def not_found(self) -> None:
        self.error_response(404, "not found")

    def error_response(self, status: int, message: str) -> None:
        self.json_response({"ok": False, "error": message}, status)

    def log_message(self, fmt: str, *args: Tuple[Any, ...]) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    host = "127.0.0.1"
    port = int(os.environ.get("VALORANT_COACH_PORT", "8766"))
    if str(DB.get_setting("auto_import", "false")).lower() in {"1", "true", "yes", "on"}:
        WATCHER.start(DB, PATHS)
    server = ThreadingHTTPServer((host, port), CoachHandler)
    print(f"VALORANT Coach Agent running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def normalize_labels(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",")]
    elif not isinstance(value, (list, tuple, set)):
        value = [value]
    return [str(item).strip().lower() for item in value if str(item).strip()]


def parse_optional_int(value: Any) -> Any:
    if value in (None, ""):
        return None
    return int(value)


def parse_optional_float(value: Any) -> Any:
    if value in (None, ""):
        return None
    return float(value)


def batch_death_job(match_id: int, update: Any) -> Dict[str, Any]:
    update("Running keyframes and clip understanding for all deaths.", 30)
    result = run_death_batch(DB, match_id, PATHS)
    update("Refreshing review queue.", 85)
    queue = build_review_queue(DB, match_id)
    write_markdown_report(DB, REPORTS_DIR, match_id)
    return {"death_batch": result, "review_queue": queue}


def detector_training_job(options: Dict[str, Any], update: Any) -> Dict[str, Any]:
    update("Preparing detector training run.", 3)
    result = train_detector(DB, DATA_DIR, options, update)
    update(result.get("message") or "Detector training finished.", 100 if result.get("ok") else 95)
    return result
