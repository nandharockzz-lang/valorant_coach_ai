import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from .automation import find_frame_path, normalize_text_list, optional_float
from .db import Database


DETECTOR_CLASSES = ["enemy_body", "enemy_head", "teammate", "weapon", "ability_effect"]
NEGATIVE_CLASS = "no_enemy"
DATASET_DIR_NAME = "detector_dataset"
MODEL_DIR_NAME = "detector_models"
DETECTOR_CLASS_TARGETS = {
    "enemy_body": 800,
    "enemy_head": 300,
    "teammate": 150,
    "weapon": 100,
    "ability_effect": 100,
    NEGATIVE_CLASS: 200,
}
DETECTOR_MILESTONES = [
    {"id": "prototype", "label": "Prototype detector", "target_boxes": 300, "description": "Enough labels to train a rough first model."},
    {"id": "useful", "label": "Useful personal detector", "target_boxes": 1000, "description": "Usually enough to help pre-label your own recordings."},
    {"id": "strong", "label": "Strong personal detector", "target_boxes": 1500, "description": "Better coverage across maps, agents, lighting, and HUD states."},
]


def detector_status(db: Database, data_dir: Path) -> Dict[str, Any]:
    model_path = str(db.get_setting("enemy_detector_model_path", "") or "").strip()
    command = str(db.get_setting("enemy_detector_command", "") or "").strip()
    dataset_dir = data_dir / DATASET_DIR_NAME
    annotations = detector_annotation_summary(db)
    candidates = detector_candidate_summary(db)
    ultralytics = module_available("ultralytics")
    configured = bool(command or model_path)
    suggested_command = ""
    if model_path:
        suggested_command = f'{sys.executable} -m valorant_coach.detector --infer --model "{model_path}" --image "{{image}}"'
    return {
        "ok": configured,
        "configured": configured,
        "model_path": model_path,
        "model_exists": bool(model_path and Path(model_path).exists()),
        "command": command,
        "suggested_command": suggested_command,
        "ultralytics_available": ultralytics,
        "dataset_dir": str(dataset_dir),
        "dataset_exists": dataset_dir.exists(),
        "annotations": annotations,
        "candidates": candidates,
        "classes": DETECTOR_CLASSES,
        "negative_class": NEGATIVE_CLASS,
        "summary": detector_status_summary(configured, model_path, ultralytics, annotations),
    }


def detector_training_dashboard(db: Database, data_dir: Path) -> Dict[str, Any]:
    status = detector_status(db, data_dir)
    annotations = status.get("annotations") or {}
    candidates = detector_candidate_summary(db)
    class_counts = annotations.get("class_counts") or {}
    box_count = int(annotations.get("box_count") or 0)
    frame_count = int(annotations.get("frame_count") or 0)
    negative_count = int(annotations.get("negative_count") or 0)
    latest_eval = latest_detector_evaluation(db)
    latest_job = latest_detector_training_job(db)
    latest_model = latest_detector_model(data_dir)

    label_score = min(box_count / 1000.0, 1.0) * 45.0
    frame_score = min(frame_count / 500.0, 1.0) * 15.0
    negative_score = min(negative_count / DETECTOR_CLASS_TARGETS[NEGATIVE_CLASS], 1.0) * 10.0
    balance_items = [
        min(float(class_counts.get(label) or 0) / float(target), 1.0)
        for label, target in DETECTOR_CLASS_TARGETS.items()
        if label != NEGATIVE_CLASS
    ]
    balance_score = (sum(balance_items) / max(1, len(balance_items))) * 20.0
    model_score = 10.0 if status.get("model_exists") else 0.0
    readiness = int(round(min(100.0, label_score + frame_score + negative_score + balance_score + model_score)))
    if int(annotations.get("annotation_count") or 0) > 0:
        readiness = max(1, readiness)

    class_progress = []
    for label, target in DETECTOR_CLASS_TARGETS.items():
        current = negative_count if label == NEGATIVE_CLASS else int(class_counts.get(label) or 0)
        class_progress.append(
            {
                "label": label,
                "current": current,
                "target": target,
                "remaining": max(0, target - current),
                "percent": int(round(min(100.0, (current / max(1, target)) * 100.0))),
            }
        )

    milestones = []
    for milestone in DETECTOR_MILESTONES:
        target = int(milestone["target_boxes"])
        milestones.append(
            {
                **milestone,
                "current_boxes": box_count,
                "remaining_boxes": max(0, target - box_count),
                "percent": int(round(min(100.0, (box_count / max(1, target)) * 100.0))),
                "complete": box_count >= target,
            }
        )

    gaps = detector_training_gaps(status, annotations, candidates, latest_eval)
    next_action = detector_next_training_action(status, annotations, candidates, latest_eval)
    stage = detector_training_stage(status, annotations, latest_eval)

    return {
        "ok": True,
        "readiness_percent": readiness,
        "stage": stage,
        "stage_label": detector_stage_label(stage),
        "next_action": next_action,
        "summary": detector_dashboard_summary(readiness, stage, box_count, frame_count),
        "annotations": annotations,
        "candidates": candidates,
        "class_targets": DETECTOR_CLASS_TARGETS,
        "class_progress": class_progress,
        "milestones": milestones,
        "model": {
            "configured": bool(status.get("configured")),
            "model_exists": bool(status.get("model_exists")),
            "model_path": status.get("model_path") or "",
            "latest_trained_model": latest_model,
            "ultralytics_available": bool(status.get("ultralytics_available")),
            "dataset_dir": status.get("dataset_dir") or "",
            "dataset_exists": bool(status.get("dataset_exists")),
        },
        "latest_evaluation": latest_eval,
        "latest_training_job": latest_job,
        "gaps": gaps,
        "note": "Readiness is a local dataset coverage estimate. Use evaluation precision and recall to judge model quality.",
    }


def detector_training_stage(status: Dict[str, Any], annotations: Dict[str, Any], latest_eval: Optional[Dict[str, Any]]) -> str:
    boxes = int(annotations.get("box_count") or 0)
    if latest_eval:
        return "evaluated"
    if status.get("model_exists"):
        return "trained"
    if boxes >= 300:
        return "trainable"
    if boxes > 0:
        return "labeling"
    return "empty"


def detector_stage_label(stage: str) -> str:
    return {
        "empty": "Needs labels",
        "labeling": "Labeling in progress",
        "trainable": "Ready for first training",
        "trained": "Model trained",
        "evaluated": "Model evaluated",
    }.get(stage, "Unknown")


def detector_dashboard_summary(readiness: int, stage: str, boxes: int, frames: int) -> str:
    if stage == "empty":
        return "No detector training labels yet. Build the queue and label visible enemies first."
    return f"{readiness}% dataset readiness from {boxes} labeled box(es) across {frames} frame(s)."


def detector_training_gaps(
    status: Dict[str, Any],
    annotations: Dict[str, Any],
    candidates: Dict[str, Any],
    latest_eval: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    counts = annotations.get("class_counts") or {}
    gaps = []
    if not status.get("ultralytics_available"):
        gaps.append({"severity": "blocked", "label": "Training dependency missing", "detail": "Install requirements-detector.txt before local YOLO training."})
    if int(annotations.get("box_count") or 0) < 300:
        gaps.append({"severity": "high", "label": "Not enough boxes", "detail": "Aim for at least 300 enemy/head boxes for a rough prototype."})
    if int(counts.get("enemy_body") or 0) < 50:
        gaps.append({"severity": "high", "label": "Enemy body coverage low", "detail": "Label full body boxes from different ranges and maps."})
    if int(counts.get("enemy_head") or 0) < 30:
        gaps.append({"severity": "medium", "label": "Enemy head coverage low", "detail": "Add head boxes so crosshair/contact reviews can become more precise."})
    if int(annotations.get("negative_count") or 0) < 30:
        gaps.append({"severity": "medium", "label": "Negative frames low", "detail": "Mark no_enemy frames to reduce false positives."})
    if int(candidates.get("needs_label") or 0) > 0:
        gaps.append({"severity": "medium", "label": "Queue needs review", "detail": f"{int(candidates.get('needs_label') or 0)} candidate frame(s) still need labels."})
    if int(annotations.get("box_count") or 0) >= 300 and not status.get("model_exists"):
        gaps.append({"severity": "high", "label": "No trained model yet", "detail": "Train the detector after exporting the current dataset."})
    if status.get("model_exists") and not latest_eval:
        gaps.append({"severity": "medium", "label": "No evaluation yet", "detail": "Run evaluation to measure precision and recall against your labels."})
    return gaps


def detector_next_training_action(
    status: Dict[str, Any],
    annotations: Dict[str, Any],
    candidates: Dict[str, Any],
    latest_eval: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    boxes = int(annotations.get("box_count") or 0)
    if int(candidates.get("count") or 0) == 0:
        return {"action": "build_queue", "label": "Build label queue", "detail": "Create candidate frames from recent keyframes and Clip Coach output."}
    if boxes < 300:
        return {"action": "label", "label": "Label more enemy frames", "detail": f"Add {300 - boxes} more box label(s) for the first prototype milestone."}
    if not status.get("ultralytics_available"):
        return {"action": "install_dependency", "label": "Install training dependency", "detail": "Run pip install -r requirements-detector.txt, then train."}
    if not status.get("model_exists"):
        return {"action": "train", "label": "Train detector", "detail": "Dataset is ready for the first YOLO training run."}
    if not latest_eval:
        return {"action": "evaluate", "label": "Evaluate detector", "detail": "Measure precision and recall before trusting the detector."}
    if int(candidates.get("needs_label") or 0) > 0:
        return {"action": "prelabel", "label": "Pre-label queue", "detail": "Use the trained model to speed up the remaining labels."}
    return {"action": "improve", "label": "Keep labeling hard examples", "detail": "Add missed enemies, false positives, and new map/agent situations before retraining."}


def latest_detector_evaluation(db: Database) -> Optional[Dict[str, Any]]:
    row = db.get_latest_structured_analysis("match", 0, "detector_evaluation")
    payload = (row or {}).get("payload") or {}
    if not payload:
        return None
    return {
        "created_at": row.get("created_at") or payload.get("created_at") or "",
        "summary": payload.get("summary") or "",
        "precision": payload.get("precision"),
        "recall": payload.get("recall"),
        "frames": payload.get("frames"),
        "true_positive": payload.get("true_positive"),
        "false_positive": payload.get("false_positive"),
        "false_negative": payload.get("false_negative"),
    }


def latest_detector_training_job(db: Database) -> Optional[Dict[str, Any]]:
    for job in db.list_jobs(100):
        if "train enemy detector" in str(job.get("name") or "").lower():
            return {
                "id": job.get("id"),
                "name": job.get("name"),
                "status": job.get("status"),
                "progress": job.get("progress"),
                "message": job.get("message") or "",
                "updated_at": job.get("updated_at") or "",
                "result": job.get("result") or {},
            }
    return None


def latest_detector_model(data_dir: Path) -> str:
    root = data_dir / MODEL_DIR_NAME
    if not root.exists():
        return ""
    models = sorted(root.glob("*/weights/best.pt"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return str(models[0]) if models else ""


def detector_status_summary(configured: bool, model_path: str, ultralytics: bool, annotations: Dict[str, Any]) -> str:
    if configured and model_path and Path(model_path).exists():
        return f"Trained detector is configured with {annotations.get('box_count', 0)} local box annotation(s)."
    if configured:
        return "Detector command is configured, but the model path is missing or external."
    if annotations.get("box_count"):
        return "Detector annotations exist; export a YOLO dataset and train a model."
    if ultralytics:
        return "Ultralytics is installed. Add enemy/head boxes to build a detector dataset."
    return "Detector is not trained yet. Install optional training dependencies and add local annotations."


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def save_detector_annotation(db: Database, death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    death = db.get_death(death_id)
    if not death:
        return {"ok": False, "message": "death not found"}
    annotation = normalize_detector_annotation(death_id, payload)
    if annotation.get("label") != NEGATIVE_CLASS and not annotation["bbox_norm"]:
        return {"ok": False, "message": "bbox_norm is required unless label is no_enemy"}
    if annotation.get("label") not in DETECTOR_CLASSES + [NEGATIVE_CLASS]:
        return {"ok": False, "message": f"label must be one of {', '.join(DETECTOR_CLASSES + [NEGATIVE_CLASS])}"}
    analysis_id = db.save_death_analysis(death_id, "detector_annotation", annotation)
    db.log("info", "detector", f"Saved detector annotation #{analysis_id}", {"death_id": death_id, "label": annotation["label"]})
    return {"ok": True, "id": analysis_id, "annotation": annotation, "summary": detector_annotation_summary(db)}


def build_detector_candidates(db: Database, data_dir: Path, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    match_id = optional_int(payload.get("match_id"))
    limit = max(10, min(500, optional_int(payload.get("limit")) or 120))
    deaths = []
    if match_id:
        deaths = db.get_deaths(match_id)
    else:
        for match in db.list_matches()[:25]:
            deaths.extend(db.get_deaths(int(match["id"])))
    existing_annotations = annotated_frame_ids(db)
    seen_frame_ids = set()
    seen_hashes: List[str] = []
    candidates = []
    for death in deaths:
        for frame in candidate_frames_for_death(db, data_dir, death):
            frame_id = str(frame.get("frame_id") or "").strip()
            if not frame_id or frame_id in seen_frame_ids:
                continue
            path = find_frame_path(data_dir, frame_id)
            if not path or not path.exists():
                continue
            image_hash = image_fingerprint(path)
            if image_hash and any(hamming_distance(image_hash, other) <= 3 for other in seen_hashes):
                continue
            seen_frame_ids.add(frame_id)
            if image_hash:
                seen_hashes.append(image_hash)
            candidate = normalize_detector_candidate(death, frame, path, existing_annotations)
            candidates.append(candidate)
    candidates = sorted(candidates, key=lambda item: (-float(item.get("priority") or 0), int(item.get("death_id") or 0), int(item.get("frame_number") or 0)))[:limit]
    result = {
        "kind": "detector_candidate_queue",
        "match_id": match_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(candidates),
        "candidates": candidates,
        "summary": f"Built {len(candidates)} detector labeling candidate(s).",
    }
    db.save_structured_analysis(match_id or 0, "detector_candidate_queue", result)
    db.log("info", "detector", result["summary"], {"match_id": match_id, "limit": limit})
    return {"ok": True, **result}


def list_detector_candidates(db: Database, match_id: Optional[int] = None, limit: int = 120) -> Dict[str, Any]:
    subject_id = int(match_id or 0)
    latest = db.get_latest_structured_analysis("match", subject_id, "detector_candidate_queue")
    payload = (latest or {}).get("payload") or {}
    rows = list(payload.get("candidates") or [])[: max(1, min(500, int(limit or 120)))]
    annotations = annotated_frame_ids(db)
    prelabels = latest_prelabels_by_frame(db)
    for row in rows:
        frame_id = str(row.get("frame_id") or "")
        row["labeled"] = frame_id in annotations
        row["prelabel"] = prelabels.get(frame_id)
        row["status"] = "labeled" if row["labeled"] else "prelabeled" if row.get("prelabel") else "needs_label"
    return {
        "ok": True,
        "kind": "detector_candidate_queue",
        "match_id": match_id,
        "count": len(rows),
        "candidates": rows,
        "summary": payload.get("summary") or "No detector candidate queue yet.",
    }


def prelabel_detector_candidates(db: Database, data_dir: Path, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    model_path = str(payload.get("model_path") or db.get_setting("enemy_detector_model_path", "") or "").strip()
    if not model_path:
        return {"ok": False, "message": "enemy detector model path is not configured"}
    if not Path(model_path).exists():
        return {"ok": False, "message": "enemy detector model file does not exist"}
    queue = list_detector_candidates(db, optional_int(payload.get("match_id")), optional_int(payload.get("limit")) or 80)
    rows = [row for row in queue.get("candidates") or [] if not row.get("labeled")]
    saved = []
    for row in rows:
        frame_id = str(row.get("frame_id") or "")
        path = find_frame_path(data_dir, frame_id)
        if not path:
            continue
        inference = infer_image(model_path, str(path), confidence=float(payload.get("confidence") or 0.25))
        prelabel = {
            "kind": "detector_prelabel",
            "frame_id": frame_id,
            "death_id": row.get("death_id"),
            "candidate_id": row.get("candidate_id"),
            "detections": inference.get("detections") or [],
            "ok": bool(inference.get("ok")),
            "error": inference.get("error") or "",
            "model_path": model_path,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.save_death_analysis(int(row.get("death_id") or 0), "detector_prelabel", prelabel)
        saved.append(prelabel)
    return {"ok": True, "count": len(saved), "prelabels": saved, "message": f"Pre-labeled {len(saved)} detector candidate frame(s)."}


def evaluate_detector_dataset(db: Database, data_dir: Path, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    model_path = str(payload.get("model_path") or db.get_setting("enemy_detector_model_path", "") or "").strip()
    annotations_by_frame = annotations_grouped_by_frame(db)
    if not annotations_by_frame:
        return {"ok": False, "message": "No detector annotations available for evaluation."}
    if not model_path:
        return {"ok": False, "message": "enemy detector model path is not configured"}
    if not Path(model_path).exists():
        return {"ok": False, "message": "enemy detector model file does not exist"}
    confidence = float(payload.get("confidence") or 0.25)
    frames = sorted(annotations_by_frame.keys())[: max(1, min(500, optional_int(payload.get("limit")) or 120))]
    true_positive = false_positive = false_negative = 0
    examples = []
    for frame_id in frames:
        path = find_frame_path(data_dir, frame_id)
        if not path:
            continue
        expected = [row for row in annotations_by_frame[frame_id] if row.get("label") != NEGATIVE_CLASS]
        actual = infer_image(model_path, str(path), confidence=confidence)
        detections = actual.get("detections") or []
        matched = set()
        for detection in detections:
            best_index, best_iou = best_iou_match(detection, expected, matched)
            if best_iou >= 0.30:
                true_positive += 1
                matched.add(best_index)
            else:
                false_positive += 1
        false_negative += max(0, len(expected) - len(matched))
        if len(examples) < 20 and (false_positive or false_negative):
            examples.append({"frame_id": frame_id, "expected": expected, "detections": detections})
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    result = {
        "kind": "detector_evaluation",
        "ok": True,
        "model_path": model_path,
        "confidence": confidence,
        "frames": len(frames),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "examples": examples,
        "summary": f"Detector evaluation: precision {precision:.2f}, recall {recall:.2f} on {len(frames)} labeled frame(s).",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.save_structured_analysis(0, "detector_evaluation", result)
    return result


def normalize_detector_annotation(death_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    label = str(payload.get("label") or "enemy_body").strip()
    frame_id = str(payload.get("frame_id") or "").strip()
    bbox = normalize_bbox(payload.get("bbox_norm") or payload.get("bbox") or {})
    return {
        "kind": "detector_annotation",
        "death_id": death_id,
        "frame_id": frame_id,
        "frame_number": optional_int(payload.get("frame_number")),
        "relative_second": optional_float(payload.get("relative_second")),
        "label": label,
        "bbox_norm": bbox,
        "notes": str(payload.get("notes") or "").strip(),
        "source": "local-user",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def candidate_frames_for_death(db: Database, data_dir: Path, death: Dict[str, Any]) -> List[Dict[str, Any]]:
    death_id = int(death.get("id") or 0)
    frames: Dict[str, Dict[str, Any]] = {}
    for analysis_type in ("local_ai_sequence", "keyframes"):
        row = db.get_latest_structured_analysis("death", death_id, analysis_type)
        for item in (((row or {}).get("payload") or {}).get("frames") or []):
            frame_id = str(item.get("frame_id") or "").strip()
            if not frame_id:
                continue
            frames[frame_id] = {**frames.get(frame_id, {}), **item, "source_analysis": analysis_type}
    visual = (db.get_latest_structured_analysis("death", death_id, "clip_visual_signals") or {}).get("payload") or {}
    visual_by_frame = {str(row.get("frame")): row for row in visual.get("timeline") or []}
    for item in frames.values():
        key = str(item.get("sequence_index") or item.get("index") or "")
        if key in visual_by_frame:
            item["visual_signal"] = visual_by_frame[key]
    return list(frames.values())


def normalize_detector_candidate(death: Dict[str, Any], frame: Dict[str, Any], path: Path, annotations: set[str]) -> Dict[str, Any]:
    frame_id = str(frame.get("frame_id") or "")
    rel = optional_float(frame.get("relative_second"))
    seconds_before = optional_float(frame.get("seconds_before_death"))
    if rel is None and seconds_before is not None:
        rel = -seconds_before
    visual = frame.get("visual_signal") or {}
    priority = candidate_priority(frame, visual, rel)
    reason_bits = []
    if frame.get("role"):
        reason_bits.append(str(frame.get("role")))
    if visual.get("class"):
        reason_bits.append(str(visual.get("class")).replace("_", " "))
    if visual.get("contact_score") is not None:
        reason_bits.append(f"contact {visual.get('contact_score')}")
    return {
        "candidate_id": f"death-{death.get('id')}-{frame_id}",
        "death_id": int(death.get("id") or 0),
        "match_id": int(death.get("match_id") or 0),
        "frame_id": frame_id,
        "frame_number": frame.get("sequence_index") or frame.get("index"),
        "timestamp": frame.get("timestamp"),
        "relative_second": rel,
        "seconds_before_death": seconds_before,
        "role": frame.get("role") or "frame",
        "priority": round(priority, 3),
        "reason": ", ".join(reason_bits) or "candidate frame",
        "path": str(path),
        "labeled": frame_id in annotations,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def candidate_priority(frame: Dict[str, Any], visual: Dict[str, Any], rel: Optional[float]) -> float:
    score = 0.2
    role = str(frame.get("role") or "").lower()
    if "contact" in role:
        score += 0.35
    if "death" in role or "pressure" in role:
        score += 0.20
    if rel is not None and -5.0 <= rel <= 0.5:
        score += 0.20
    cls = str(visual.get("class") or "")
    if "enemy_seen_by_detector" in cls:
        score += 0.35
    elif "contact" in cls:
        score += 0.25
    elif "damage" in cls or "death" in cls:
        score += 0.10
    score += min(0.20, float(visual.get("contact_score") or 0) * 0.20)
    return min(1.0, score)


def image_fingerprint(path: Path) -> str:
    try:
        image = Image.open(path).convert("L").resize((8, 8))
    except Exception:
        return ""
    pixels = list(image.getdata())
    avg = sum(pixels) / max(1, len(pixels))
    return "".join("1" if value >= avg else "0" for value in pixels)


def hamming_distance(left: str, right: str) -> int:
    if not left or not right or len(left) != len(right):
        return 999
    return sum(1 for a, b in zip(left, right) if a != b)


def annotated_frame_ids(db: Database) -> set[str]:
    return {str((row.get("payload") or {}).get("frame_id") or "") for row in detector_annotation_rows(db, limit=10000) if (row.get("payload") or {}).get("frame_id")}


def annotations_grouped_by_frame(db: Database) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in detector_annotation_rows(db, limit=10000):
        payload = row.get("payload") or {}
        frame_id = str(payload.get("frame_id") or "")
        if frame_id:
            grouped.setdefault(frame_id, []).append(payload)
    return grouped


def latest_prelabels_by_frame(db: Database) -> Dict[str, Dict[str, Any]]:
    rows = [
        item for item in db.list_structured_analyses("death", limit=5000)
        if item.get("analysis_type") == "detector_prelabel"
    ]
    result: Dict[str, Dict[str, Any]] = {}
    for row in reversed(rows):
        payload = row.get("payload") or {}
        frame_id = str(payload.get("frame_id") or "")
        if frame_id:
            result[frame_id] = payload
    return result


def detector_candidate_summary(db: Database) -> Dict[str, Any]:
    latest = db.get_latest_structured_analysis("match", 0, "detector_candidate_queue")
    payload = (latest or {}).get("payload") or {}
    rows = payload.get("candidates") or []
    labeled = len([row for row in rows if row.get("labeled") or str(row.get("frame_id") or "") in annotated_frame_ids(db)])
    return {"count": len(rows), "labeled": labeled, "needs_label": max(0, len(rows) - labeled)}


def best_iou_match(detection: Dict[str, Any], expected: List[Dict[str, Any]], matched: set[int]) -> Tuple[int, float]:
    best_index = -1
    best_score = 0.0
    label = str(detection.get("label") or "")
    for index, row in enumerate(expected):
        if index in matched:
            continue
        if label and row.get("label") and label != row.get("label"):
            continue
        score = bbox_iou(detection.get("bbox_norm") or {}, row.get("bbox_norm") or {})
        if score > best_score:
            best_index = index
            best_score = score
    return best_index, best_score


def bbox_iou(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    if not left or not right:
        return 0.0
    lx1, ly1 = float(left.get("x") or 0), float(left.get("y") or 0)
    lx2, ly2 = lx1 + float(left.get("w") or 0), ly1 + float(left.get("h") or 0)
    rx1, ry1 = float(right.get("x") or 0), float(right.get("y") or 0)
    rx2, ry2 = rx1 + float(right.get("w") or 0), ry1 + float(right.get("h") or 0)
    ix1, iy1 = max(lx1, rx1), max(ly1, ry1)
    ix2, iy2 = min(lx2, rx2), min(ly2, ry2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    union = max(0.000001, (lx2 - lx1) * (ly2 - ly1) + (rx2 - rx1) * (ry2 - ry1) - intersection)
    return intersection / union


def optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_bbox(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    try:
        x = clamp01(float(value.get("x", 0)))
        y = clamp01(float(value.get("y", 0)))
        w = clamp01(float(value.get("w", 0)))
        h = clamp01(float(value.get("h", 0)))
    except (TypeError, ValueError):
        return {}
    if w <= 0 or h <= 0:
        return {}
    if x + w > 1:
        w = max(0.001, 1.0 - x)
    if y + h > 1:
        h = max(0.001, 1.0 - y)
    return {"x": round(x, 5), "y": round(y, 5), "w": round(w, 5), "h": round(h, 5)}


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def detector_annotation_summary(db: Database) -> Dict[str, Any]:
    rows = detector_annotation_rows(db, limit=5000)
    counts: Dict[str, int] = {}
    negative_count = 0
    frame_ids = set()
    death_ids = set()
    box_count = 0
    for row in rows:
        payload = row.get("payload") or {}
        label = str(payload.get("label") or "")
        counts[label] = counts.get(label, 0) + 1
        if label == NEGATIVE_CLASS:
            negative_count += 1
        elif payload.get("bbox_norm"):
            box_count += 1
        if payload.get("frame_id"):
            frame_ids.add(str(payload.get("frame_id")))
        death_ids.add(int(payload.get("death_id") or row.get("subject_id") or 0))
    return {
        "annotation_count": len(rows),
        "box_count": box_count,
        "negative_count": negative_count,
        "frame_count": len(frame_ids),
        "death_count": len([item for item in death_ids if item]),
        "class_counts": counts,
        "ready_for_export": box_count > 0,
    }


def detector_annotation_rows(db: Database, limit: int = 1000) -> List[Dict[str, Any]]:
    return [
        item for item in db.list_structured_analyses("death", limit=limit)
        if item.get("analysis_type") == "detector_annotation"
    ]


def export_detector_dataset(db: Database, data_dir: Path, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    dataset_dir = Path(str(payload.get("dataset_dir") or data_dir / DATASET_DIR_NAME))
    dataset_dir.mkdir(parents=True, exist_ok=True)
    images_dir = dataset_dir / "images" / "train"
    labels_dir = dataset_dir / "labels" / "train"
    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    negatives: set[str] = set()
    skipped = []
    for row in detector_annotation_rows(db, limit=10000):
        item = row.get("payload") or {}
        frame_id = str(item.get("frame_id") or "").strip()
        if not frame_id:
            skipped.append({"reason": "missing frame_id", "id": row.get("id")})
            continue
        if item.get("label") == NEGATIVE_CLASS:
            negatives.add(frame_id)
            grouped.setdefault(frame_id, [])
            continue
        if item.get("label") not in DETECTOR_CLASSES or not item.get("bbox_norm"):
            skipped.append({"reason": "invalid label or bbox", "id": row.get("id")})
            continue
        grouped.setdefault(frame_id, []).append(item)

    exported_images = 0
    exported_boxes = 0
    for frame_id, annotations in grouped.items():
        frame_path = find_frame_path(data_dir, frame_id)
        if not frame_path or not frame_path.exists():
            skipped.append({"reason": "frame not found", "frame_id": frame_id})
            continue
        image_name = safe_frame_image_name(frame_id, frame_path)
        shutil.copy2(frame_path, images_dir / image_name)
        label_lines = []
        for annotation in annotations:
            class_id = DETECTOR_CLASSES.index(str(annotation["label"]))
            label_lines.append(yolo_label_line(class_id, annotation["bbox_norm"]))
            exported_boxes += 1
        (labels_dir / f"{Path(image_name).stem}.txt").write_text("\n".join(label_lines) + ("\n" if label_lines else ""), encoding="utf-8")
        exported_images += 1

    data_yaml = dataset_dir / "data.yaml"
    names = "\n".join(f"  {index}: {name}" for index, name in enumerate(DETECTOR_CLASSES))
    data_yaml.write_text(
        f"path: {dataset_dir.as_posix()}\ntrain: images/train\nval: images/train\nnames:\n{names}\n",
        encoding="utf-8",
    )
    result = {
        "ok": exported_boxes > 0,
        "dataset_dir": str(dataset_dir),
        "data_yaml": str(data_yaml),
        "images": exported_images,
        "boxes": exported_boxes,
        "negative_frames": len(negatives),
        "skipped": skipped[:50],
        "classes": DETECTOR_CLASSES,
        "message": f"Exported {exported_images} image(s) and {exported_boxes} box label(s) to YOLO format.",
    }
    db.log("info", "detector", "Exported detector dataset", result)
    return result


def safe_frame_image_name(frame_id: str, frame_path: Path) -> str:
    suffix = frame_path.suffix if frame_path.suffix.lower() in {".jpg", ".jpeg", ".png"} else ".jpg"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in frame_id)
    return f"{safe}{suffix}"


def yolo_label_line(class_id: int, bbox: Dict[str, float]) -> str:
    x = float(bbox["x"])
    y = float(bbox["y"])
    w = float(bbox["w"])
    h = float(bbox["h"])
    cx = x + w / 2.0
    cy = y + h / 2.0
    return f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


def train_detector(
    db: Database,
    data_dir: Path,
    payload: Optional[Dict[str, Any]] = None,
    update: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    payload = payload or {}
    if update:
        update("Exporting YOLO detector dataset.", 5)
    export = export_detector_dataset(db, data_dir, payload)
    if not export.get("ok"):
        return {"ok": False, "message": "No detector boxes were exported; add enemy/head annotations first.", "export": export}
    if update:
        update(f"Exported {export.get('images', 0)} image(s) and {export.get('boxes', 0)} box label(s).", 15)
    if not module_available("ultralytics"):
        return {
            "ok": False,
            "message": "Ultralytics is not installed. Run: pip install ultralytics",
            "export": export,
        }
    model = str(payload.get("base_model") or "yolo11n.pt")
    epochs = max(1, min(300, int(float(payload.get("epochs") or 40))))
    imgsz = max(320, min(1280, int(float(payload.get("imgsz") or 640))))
    project = data_dir / MODEL_DIR_NAME
    name = str(payload.get("run_name") or f"valorant-enemy-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    train_code = (
        "from ultralytics import YOLO; "
        "import sys; "
        "YOLO(sys.argv[1]).train(data=sys.argv[2], epochs=int(sys.argv[3]), "
        "imgsz=int(sys.argv[4]), project=sys.argv[5], name=sys.argv[6])"
    )
    cmd = [sys.executable, "-c", train_code, model, str(export["data_yaml"]), str(epochs), str(imgsz), str(project), name]
    if update:
        update(f"Starting YOLO training for {epochs} epoch(s) at {imgsz}px.", 25)
    stdout_tail, returncode = run_training_process(cmd, epochs, update)
    weights = project / name / "weights" / "best.pt"
    result = {
        "ok": returncode == 0 and weights.exists(),
        "message": "Detector training completed." if returncode == 0 else "Detector training failed.",
        "command": " ".join(cmd),
        "returncode": returncode,
        "stdout_tail": stdout_tail[-4000:],
        "stderr_tail": "",
        "model_path": str(weights) if weights.exists() else "",
        "export": export,
        "epochs": epochs,
        "imgsz": imgsz,
    }
    if result["ok"]:
        db.set_setting("enemy_detector_model_path", str(weights))
        db.set_setting("enemy_detector_command", f'{sys.executable} -m valorant_coach.detector --infer --model "{weights}" --image "{{image}}"')
    db.log("info" if result["ok"] else "error", "detector", result["message"], {"model_path": result.get("model_path"), "returncode": returncode})
    return result


def run_training_process(
    cmd: List[str],
    epochs: int,
    update: Optional[Callable[[str, int], None]] = None,
) -> Tuple[str, int]:
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: List[str] = []
    epoch_pattern = re.compile(rf"\b(\d+)\s*/\s*{int(epochs)}\b")
    assert process.stdout is not None
    for line in process.stdout:
        clean = line.rstrip()
        if clean:
            lines.append(clean)
            lines = lines[-250:]
            match = epoch_pattern.search(clean)
            if match and update:
                epoch = max(1, min(epochs, int(match.group(1))))
                progress = min(92, 25 + int((epoch / max(1, epochs)) * 67))
                update(f"Training detector epoch {epoch}/{epochs}.", progress)
    returncode = process.wait()
    return "\n".join(lines), int(returncode or 0)


def infer_image(model_path: str, image_path: str, confidence: float = 0.25) -> Dict[str, Any]:
    if not model_path or not Path(model_path).exists():
        return {"available": True, "ok": False, "error": "model not found", "detections": []}
    if not image_path or not Path(image_path).exists():
        return {"available": True, "ok": False, "error": "image not found", "detections": []}
    try:
        from ultralytics import YOLO
    except Exception as exc:
        return {"available": True, "ok": False, "error": f"ultralytics unavailable: {exc}", "detections": []}
    model = YOLO(model_path)
    image = Image.open(image_path)
    width, height = image.size
    results = model.predict(source=image_path, conf=confidence, verbose=False)
    detections = []
    for result in results:
        names = getattr(result, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            continue
        for box in boxes:
            cls_id = int(box.cls[0].item()) if getattr(box, "cls", None) is not None else 0
            conf = float(box.conf[0].item()) if getattr(box, "conf", None) is not None else 0.0
            xyxy = box.xyxy[0].tolist()
            detections.append(
                {
                    "label": str(names.get(cls_id, DETECTOR_CLASSES[cls_id] if cls_id < len(DETECTOR_CLASSES) else f"class_{cls_id}")),
                    "confidence": round(conf, 4),
                    "bbox_norm": xyxy_to_norm(xyxy, width, height),
                    "source": "trained_yolo",
                }
            )
    detections.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)
    return {"available": True, "ok": True, "detections": detections, "model": model_path, "image": image_path}


def xyxy_to_norm(xyxy: Iterable[float], width: int, height: int) -> Dict[str, float]:
    x1, y1, x2, y2 = [float(value) for value in xyxy]
    return normalize_bbox({"x": x1 / width, "y": y1 / height, "w": (x2 - x1) / width, "h": (y2 - y1) / height})


def main() -> int:
    parser = argparse.ArgumentParser(description="Local VALORANT trained enemy detector utilities.")
    parser.add_argument("--infer", action="store_true", help="Run inference on one image and print JSON.")
    parser.add_argument("--model", default="", help="YOLO model path.")
    parser.add_argument("--image", default="", help="Image path.")
    parser.add_argument("--confidence", type=float, default=0.25, help="Inference confidence threshold.")
    args = parser.parse_args()
    if args.infer:
        print(json.dumps(infer_image(args.model, args.image, args.confidence)))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
