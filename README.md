# VALORANT Coach Agent

Local-first post-match coaching agent for VALORANT VODs.

This MVP avoids game hooks, memory reads, packet inspection, input automation, and live tactical advice. It imports completed recordings, stores match metadata in SQLite, streams VODs in the browser, tracks death markers, generates reports, and supports sidecar event files for repeatable death/positioning analysis.

## Run

```powershell
cd C:\Users\hjr\valorant-coach-agent
python -m pip install -r requirements.txt
python run.py
```

Open:

```text
http://127.0.0.1:8766
```

For setup on another machine, see [INSTALL.md](INSTALL.md).

## Importing VODs

Use the dashboard to set a recording folder and scan it, or import a single recording by absolute path.

Supported video extensions:

- `.mp4`
- `.mkv`
- `.mov`
- `.avi`
- `.webm`

## Review Workflow

1. Select a match and click **Analyze**.
2. Use the embedded VOD player to review deaths.
3. Add or edit death markers with round, timestamp, labels, confidence, and notes.
4. Use **Jump** beside any death marker to replay the moment.
5. Click **Get Advice** to generate coaching for a marked death.
6. Click **Extract Clips** if `ffmpeg` is installed.
7. Click **Write Report** to save a Markdown report under `reports`.

The Trends panel summarizes recurring mistake labels by match, map, and agent.

The Coach panel stores your rank, main agents, coaching notes, active session focus, advice feedback, and a personalized next-session plan.

Use the preset label buttons while adding or editing deaths so the coach can compare matches cleanly. After a match is labeled, click **Coach Review** to generate a match-level summary against your active focus.

If `ffmpeg` is installed, **Suggest Deaths** scans sampled VOD frames locally and proposes death markers for review. **Analyze Clip** samples an extracted death clip locally and reports whether the frames look like a death/combat-report transition.

The app checks for `ffmpeg` on PATH, `tools/ffmpeg/bin/ffmpeg.exe` inside the project, `C:\Program Files\ffmpeg\bin\ffmpeg.exe`, and `C:\ffmpeg\bin\ffmpeg.exe`.

The local visual model computes frame-level signals for motion, contrast, red UI activity, killfeed-region activity, center-screen overlay changes, lower-HUD darkness, and crosshair-region activity. It persists a Visual Read for each analyzed death clip and feeds those observations into later advice generation.

Sessions, visual drag calibration, HUD sampling, minimap interpretation, Tesseract OCR, death-event detector v2, round reconstruction, crosshair scoring, clip understanding, personal coach memory, gameplay hypotheses, and disabled-by-default AI review gates are also available from the dashboard. For portable Windows packaging and executable builds, see [PACKAGING.md](PACKAGING.md).

Advanced local analysis buttons:

- **Events v2**: scans calibrated HUD/combat-report/killfeed regions for likely death or combat transitions.
- **Rounds**: reconstructs likely round boundaries from sampled HUD and scene transitions.
- **Crosshair**: scores crosshair-region stability and drift from sampled frames.
- **Minimap**: samples the calibrated minimap region and adds rotation/readability interpretation.
- **Queue**: builds a prioritized review queue from deaths, suggestions, detector events, and match-wide signals.
- **Keyframes** on a death: extracts pre-contact, first-pressure, peak-motion, death-UI, and post-death frames.
- **Understand** on a death: combines clip visual reads into a local structured death explanation.

Accepted/rejected death suggestions are stored as detector feedback. The detector uses that history to adjust its local threshold for your recordings.

Use `launch_desktop.bat` for a Windows tray shell with quick actions for opening the dashboard, data, clips, reports, restarting the server, and quitting.

Automation features:

- **Pipeline** queues full match analysis in the background.
- **Batch Deaths** runs keyframes and clip understanding for every marked death.
- **Automation** panel controls auto-import, auto-analysis, detector sensitivity, frame sample rate, storage cleanup, analytics, persistent job status, logs, tools, backups, search, exports, and memory export/import.
- The Automation panel also includes app version/schema status, provider registry, privacy audit, advanced death search, editable local playbooks, and a correction review queue.
- It now also includes local plugin status, installer diagnostics, detector benchmark metrics, local-AI command configuration, privacy export, and privacy wipe controls.
- Setup Wizard checks recording folder, writable app folders, ffmpeg, Tesseract, and local model provider readiness.
- Local AI supports custom command, Ollama, LM Studio, and llama.cpp-style local HTTP providers.
- Prompt templates are editable for role/map/agent-specific model review criteria.
- Detector Tuning recommends sensitivity from accepted/rejected suggestions plus benchmark labels.
- Session Report summarizes top mistakes, improvement signal, and next drills for a play block.
- **Queue** creates a prioritized review list for the match.
- **Smart Queue** groups review items into coaching clusters and identifies the highest-value review themes.
- **Story** reconstructs a local per-round narrative from rounds, deaths, labels, and saved analyses.
- Manual correction fields on death cards store corrected phase/OCR/event/keyframe notes locally.
- Death cards include clip annotation fields for mistake start, first contact, death moment, better decision, labels, and notes.
- Optional **Local AI** review can run a user-configured local command. The core app sends JSON to that local process over stdin and does not upload clips or frames.
- Benchmark labels can mark true positives, false positives, and missed deaths to measure precision/recall and tune the detector.
- Match cards include metadata quick edit for map, agent, and status.
- Persistent jobs survive app restart; interrupted queued/running jobs are marked failed on the next launch.
- The watcher waits for recording file size to stabilize before import.
- Reports can be exported as Markdown, JSON, or HTML.
- `launch_desktop.bat` runs a tray shell with crash recovery. If the local server exits or fails repeated health checks, it restarts automatically and writes recovery notes to `logs/desktop-shell.log`.

Personal Coach v2 adds weighted pattern memory, skill ratings, a weekly focus plan, and memory-strength tracking. It learns from death labels, clip annotations, accepted/rejected advice, detector feedback, and saved local visual analyses.

Build a portable Windows installer zip:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

## Optional Sidecar Analysis

For a VOD named:

```text
Ascent ranked 2026-07-20.mp4
```

create a sidecar next to it:

```text
Ascent ranked 2026-07-20.events.json
```

Example:

```json
{
  "map": "Ascent",
  "agent": "Jett",
  "rounds": [
    {"round_number": 1, "start_ts": 0, "end_ts": 95, "outcome": "lost", "side": "attack"}
  ],
  "deaths": [
    {
      "round_number": 1,
      "timestamp": 82.4,
      "labels": ["dry peek", "exposed to multiple angles"],
      "confidence": 0.82,
      "notes": "Died fighting mid from top mid with no smoke or teammate trade spacing."
    }
  ]
}
```

If no sidecar exists, the MVP still creates a report and marks the match as needing manual review.

## Next CV Upgrade

Install `ffmpeg` to enable clip extraction and frame sampling. Install Tesseract OCR to enable real HUD/killfeed/combat-report text extraction. Future CV upgrades can add:

- round boundary detection
- death overlay / combat report detection
- clip extraction
- crosshair and exposure heuristics
