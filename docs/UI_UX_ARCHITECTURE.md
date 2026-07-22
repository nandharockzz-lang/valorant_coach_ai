# UI/UX Architecture

This file is the living knowledge note for the VALORANT Coach Agent interface. Update it when review workflows, feature placement, or navigation rules change.

## Current Architecture

- The app is a local-first web UI served by `valorant_coach/server.py` and rendered from `static/index.html`.
- Most frontend behavior currently lives in `static/app.js`: API calls, sidebar rendering, match list actions, review rendering, death cards, local AI tools, detector tooling, OCR/calibration tools, and delegated event routing.
- Styling lives in `static/styles.css`, with a dark-theme override block near the end of the file.
- The left sidebar is already tabbed into Recordings, Matches, Status, Memory, and Tools. The main Review area is the primary workspace after a match is selected.

## UX Problems To Avoid

- Do not put every new feature directly into the main Review stack.
- Do not expose diagnostics as primary coaching actions. Users should see one clear coaching path before advanced tools.
- Do not add more similarly named buttons for the same clip workflow. Keep one primary action and place raw/legacy actions under Diagnostics.
- Do not let death cards expand into full dashboards by default. A card should first answer: when, what label, what status, what next action.

## Target Information Architecture

- **Recordings**: recording folder, import one video, scan folder.
- **Matches**: imported matches, match metadata, primary match actions, range scan controls.
- **Review**: video, timeline, death candidates, confirmed death markers, Clip Coach.
- **Coach**: match coach plan, coach memory, priorities, themes, whole-VOD coach moments.
- **Player Status**: aggregate mistake patterns, review coverage, round coverage, perception/coaching trends.
- **Diagnostics/Tools**: OCR health, calibration overlay, detector benchmarking, visual/OCR analysis receipts, legacy clip diagnostics, logs, backups, privacy, local AI setup, detector training.

## Placement Rules

- Primary user-facing review actions belong in the Review workspace action bar.
- Clip-level actions should collapse into **Coach Clip** unless the user opens Diagnostics.
- Match-wide technical analysis belongs in Diagnostics, not in the default review flow.
- Evidence receipts are useful for trust but should stay folded by default.
- Manual editing and corrections should be available, but not more prominent than the review and coaching path.
- Keep backend APIs stable during UI reclustering unless a new UI state requires a small read-only endpoint.
- OCR setup must show the full frame plus the active boxes. Raw crop thumbnails alone are not enough because users need to see whether the box is placed on the correct HUD area.

## Implementation Direction

- Keep the main video and timeline as the first visible elements after opening a match.
- Use tabs inside the Review area: Deaths, Coach, Player Status, Diagnostics.
- Use concise match-level action buttons: Find Deaths, Match Coach Plan, OCR Health, Write Report.
- Keep death cards summary-first: timestamp, round/source, labels, marker lifecycle, Jump, Coach Clip.
- Move context, advice details, evidence receipts, marker editing, detector annotation, and legacy diagnostics into named foldouts.
- Present OCR calibration as **Check OCR Regions**: capture a frame, drag/resize named boxes, save regions, then rerun the OCR check.
- Prefer small frontend render helpers over adding more logic directly inside `renderReport` or `renderDeathCard`.
