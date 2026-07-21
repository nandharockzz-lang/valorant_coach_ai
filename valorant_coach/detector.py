import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from .automation import find_frame_path, normalize_text_list, optional_float
from .db import Database


DETECTOR_CLASSES = ["enemy_body", "enemy_head", "teammate", "weapon", "ability_effect"]
NEGATIVE_CLASS = "no_enemy"
DATASET_DIR_NAME = "detector_dataset"
MODEL_DIR_NAME = "detector_models"


def detector_status(db: Database, data_dir: Path) -> Dict[str, Any]:
    model_path = str(db.get_setting("enemy_detector_model_path", "") or "").strip()
    command = str(db.get_setting("enemy_detector_command", "") or "").strip()
    dataset_dir = data_dir / DATASET_DIR_NAME
    annotations = detector_annotation_summary(db)
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
        "classes": DETECTOR_CLASSES,
        "negative_class": NEGATIVE_CLASS,
        "summary": detector_status_summary(configured, model_path, ultralytics, annotations),
    }


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


def train_detector(db: Database, data_dir: Path, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    export = export_detector_dataset(db, data_dir, payload)
    if not export.get("ok"):
        return {"ok": False, "message": "No detector boxes were exported; add enemy/head annotations first.", "export": export}
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
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=None)
    weights = project / name / "weights" / "best.pt"
    result = {
        "ok": completed.returncode == 0 and weights.exists(),
        "message": "Detector training completed." if completed.returncode == 0 else "Detector training failed.",
        "command": " ".join(cmd),
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "model_path": str(weights) if weights.exists() else "",
        "export": export,
    }
    if result["ok"]:
        db.set_setting("enemy_detector_model_path", str(weights))
        db.set_setting("enemy_detector_command", f'{sys.executable} -m valorant_coach.detector --infer --model "{weights}" --image "{{image}}"')
    db.log("info" if result["ok"] else "error", "detector", result["message"], {"model_path": result.get("model_path"), "returncode": completed.returncode})
    return result


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
