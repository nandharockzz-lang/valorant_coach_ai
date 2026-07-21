# Training A Local VALORANT Enemy Detector

This detector is local and post-game only. It is for imported recordings and Clip Coach review, not live gameplay assistance.

## 1. Install Optional Training Dependency

```powershell
pip install -r requirements-detector.txt
```

## 2. Use The Model Dashboard

Open Automation -> Detector Model Dashboard.

The dashboard shows:

- dataset readiness percentage
- current training stage
- recommended next action
- labeled boxes, unique frames, negative frames, and queue size
- milestone progress for 300 / 1,000 / 1,500 boxes
- class coverage targets for enemy body, enemy head, teammates, weapons, ability effects, and no_enemy frames
- latest training job progress
- latest evaluation precision and recall

The readiness percentage is a dataset coverage estimate, not proof that the model is accurate. Use Evaluate Detector after training to measure quality against your saved labels.

## 3. Add Detector Annotations

Open a death card, expand the advanced tools, choose a keyframe, and drag a detector box on the image:

- `enemy_body`
- `enemy_head`
- `teammate`
- `weapon`
- `ability_effect`
- `no_enemy` for negative frames

The UI fills normalized coordinates: `x`, `y`, `w`, `h` from `0.0` to `1.0`.

## 4. Build The Active-Learning Queue

Use Automation -> Detector Model Dashboard -> Build Label Queue.

The queue prioritizes already-extracted frames from:

- confirmed death clips
- local AI frame sequences
- keyframes near contact/death
- visual contact-proxy frames

This does not rescan full videos. Run keyframes or Clip Coach first for better candidates.

## 5. Export Dataset

Use the Detector panel in Automation, or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/detector/export -ContentType application/json -Body "{}"
```

The app writes a YOLO dataset under `data/detector_dataset`.

## 6. Train

Use the dashboard Train Detector button. The Jobs panel and dashboard show training progress. If Ultralytics prints epoch progress, the job progress updates during the run.

You can adjust epochs, image size, and base model in the dashboard before pressing Train Detector, or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8765/api/detector/train -ContentType application/json -Body '{"epochs":40,"imgsz":640}'
```

After training succeeds, the app sets `enemy_detector_command` to:

```powershell
python -m valorant_coach.detector --infer --model "path\to\best.pt" --image "{image}"
```

## 7. Pre-label New Frames

After the first model exists, use Automation -> Detector Model Dashboard -> Pre-label Queue.

The app runs the model on unlabeled candidate frames. Correct the predicted boxes instead of starting from empty frames.

## 8. Evaluate

Use Automation -> Detector Model Dashboard -> Evaluate Detector, then check:

- enemy boxes appear only when an enemy is visible
- teammates and ability effects are not falsely marked as enemies
- Clip Coach cites detector evidence separately from VLM evidence

## Practical Milestones

- 200-300 boxes: rough prototype
- 500-1,000 boxes: useful for your recording style
- 1,500+ boxes: stronger personal detector
