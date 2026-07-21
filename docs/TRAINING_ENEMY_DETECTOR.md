# Training A Local VALORANT Enemy Detector

This detector is local and post-game only. It is for imported recordings and Clip Coach review, not live gameplay assistance.

## 1. Install Optional Training Dependency

```powershell
pip install -r requirements-detector.txt
```

## 2. Add Detector Annotations

Open a death card, expand the advanced tools, and save detector boxes:

- `enemy_body`
- `enemy_head`
- `teammate`
- `weapon`
- `ability_effect`
- `no_enemy` for negative frames

Boxes use normalized coordinates: `x`, `y`, `w`, `h` from `0.0` to `1.0`.

## 3. Export Dataset

Use the Detector panel in Automation, or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/detector/export -ContentType application/json -Body "{}"
```

The app writes a YOLO dataset under `data/detector_dataset`.

## 4. Train

Use the Detector panel, or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/detector/train -ContentType application/json -Body '{"epochs":40,"imgsz":640}'
```

After training succeeds, the app sets `enemy_detector_command` to:

```powershell
python -m valorant_coach.detector --infer --model "path\to\best.pt" --image "{image}"
```

## 5. Validate

Run Clip Coach on unseen deaths and check:

- enemy boxes appear only when an enemy is visible
- teammates and ability effects are not falsely marked as enemies
- Clip Coach cites detector evidence separately from VLM evidence
