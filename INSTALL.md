# Install VALORANT Coach Agent On Another System

This guide installs the local-first VALORANT Coach Agent on a Windows PC. The app runs locally at `localhost` and can open in either a browser or a Windows app-window shell. It does not hook VALORANT, read game memory, inspect packets, automate input, or provide live tactical advice.

## Requirements

- Windows 10 or Windows 11
- Python 3.9 or newer
- A folder containing completed VALORANT recordings from OBS, Medal, ShadowPlay, or similar
- Python packages from `requirements.txt`
- Optional: `ffmpeg` for death clips and frame extraction
- Optional: Tesseract OCR for HUD, killfeed, and combat-report text extraction

No Node.js, npm, FastAPI, or OpenCV is required for the current MVP.

## Copy The Project

Copy the full `valorant-coach-agent` folder to the new machine.

Recommended location:

```powershell
C:\Users\<your-user>\valorant-coach-agent
```

Do not copy these runtime folders unless you intentionally want the old machine's local data:

- `data`
- `reports`
- `clips`
- `samples`

Those folders are recreated automatically.

## Verify Python

Open PowerShell and run:

```powershell
python --version
```

If Python is not installed, install Python 3.9 or newer from the official Python installer and enable **Add python.exe to PATH** during installation.

## Start The App

From PowerShell:

```powershell
cd C:\Users\<your-user>\valorant-coach-agent
python -m pip install -r requirements.txt
python run.py
```

Open this URL in a browser:

```text
http://127.0.0.1:8766
```

Keep the PowerShell window open while using the app. Press `Ctrl+C` in that window to stop the server.

For the app-window launcher with a tray menu:

```powershell
launch_app.bat
```

This opens the dashboard in a standalone Microsoft Edge app window instead of a normal browser tab. The tray menu can open the app window, open the browser dashboard, open data/clips/reports folders, restart the server, stop the server, or quit. This is an app-window shell around the local web UI, not a compiled native WebView2 binary yet.

For plain browser mode:

```powershell
launch.bat
```

For tray-only browser mode:

```powershell
launch_desktop.bat
```

## Automation

Use the **Automation** panel to configure:

- auto-import from the recording folder
- auto-analysis after import
- detector sensitivity
- frame sample rate
- local-only privacy mode
- storage cleanup
- data retention
- database backup/restore
- tool checks for ffmpeg, Tesseract, and PyInstaller
- job cancellation and persistent job history
- death search/filter
- JSON/HTML report export
- local tracker/stat file import
- memory export/import
- app version and schema status
- local provider registry
- local plugin registry
- installer diagnostics
- detector benchmark metrics
- privacy audit
- privacy export and wipe controls
- advanced death search with text, label, phase, confidence, and clip filters
- editable map/agent playbooks
- correction review and apply queue
- optional local-AI command configuration
- Ollama, LM Studio, and llama.cpp local model provider settings
- editable local model prompt templates
- detector tuning from benchmark labels
- session reports
- match metadata quick edit

Use **Pipeline** on a match to queue the full background analysis flow. Use **Batch Deaths** to run keyframes and clip understanding for all marked deaths.

Use **Smart Queue** to group high-value review items into themes. Use **Story** to reconstruct a local round-by-round narrative from rounds, deaths, labels, and stored analyses.

The watcher waits until a recording file size is stable before importing, so it should not grab an actively written VOD.

The app-window and desktop tray launchers include a watchdog. If the server process exits unexpectedly or fails three local health checks, it restarts the server and writes a note to:

```text
logs\desktop-shell.log
logs\windows-app-shell.log
```

## If Port 8766 Is Busy

Start the app on another port:

```powershell
$env:VALORANT_COACH_PORT="8770"
python run.py
```

Then open:

```text
http://127.0.0.1:8770
```

## Import Recordings

In the dashboard:

1. Enter your VALORANT recordings folder.
2. Click **Save**.
3. Click **Scan Folder**.
4. Select a match and click **Analyze**.
5. Review the VOD in the embedded player.
6. Add or edit death markers with timestamps, labels, and notes.
7. Click **Get Advice** on any death marker to generate a coaching suggestion.
8. Click **Suggest Deaths**, **Events v2**, **Rounds**, **HUD**, **Minimap**, **Crosshair**, **OCR**, or **Queue** if `ffmpeg` is installed.
9. Click **Extract Clips** to create short death clips.
10. Click **Keyframes** on a death to extract the most important frames.
11. Click **Understand** on a death after clips are extracted to generate a structured local clip read.
12. Click **Write Report** to generate a Markdown report under `reports`.

Supported video formats:

- `.mp4`
- `.mkv`
- `.mov`
- `.avi`
- `.webm`

## Optional Event Sidecars

The current MVP uses optional `.events.json` sidecar files for structured death/round analysis.

For this video:

```text
Ascent_Jett_ranked.mp4
```

create this file in the same folder:

```text
Ascent_Jett_ranked.events.json
```

Example:

```json
{
  "map": "Ascent",
  "agent": "Jett",
  "rounds": [
    {"round_number": 1, "start_ts": 0, "end_ts": 94, "outcome": "lost", "side": "attack"}
  ],
  "deaths": [
    {
      "round_number": 1,
      "timestamp": 82.4,
      "labels": ["dry peek", "exposed to multiple angles"],
      "confidence": 0.82,
      "notes": "Died taking mid without utility or a teammate ready to trade."
    }
  ]
}
```

Without a sidecar, the app still imports the VOD and creates a report marked as needing manual review.

## Optional Clip Extraction

Install `ffmpeg` and make sure it is available on PATH:

```powershell
ffmpeg -version
```

When available, the app creates short clips around each death marker under `clips`.

If `ffmpeg` is not installed, the app still works; clip extraction is skipped and the dashboard reports that status.

## Optional OCR

Install Tesseract OCR and make sure `tesseract.exe` is available on PATH:

```powershell
tesseract --version
```

The app also checks the common install locations:

```text
<app>\tools\tesseract\tesseract.exe
C:\Program Files\Tesseract-OCR\tesseract.exe
C:\Program Files (x86)\Tesseract-OCR\tesseract.exe
```

When Tesseract and ffmpeg are available, the **OCR** match action samples frames, crops calibrated HUD regions, and stores non-empty text reads in the local database.

## Feedback Learning

When the app suggests a death, click **Accept** only if the event is truly useful and **Reject** when it is not. The app logs the detector confidence and reason locally, then adjusts the future event threshold for your recording style.

## Manual Corrections

Death cards include correction fields for round phase and notes. Use them to correct OCR, round phase, detector event type, or keyframe interpretation. Corrections are stored locally as structured analysis records.

Death cards also include clip annotation fields:

- mistake start
- first contact
- death moment
- better decision
- annotation labels
- annotation notes

These annotations feed Personal Coach v2 and the evaluation benchmark.

## Optional Local AI Review

The app can call a local command for model-based review. This is disabled by default.

Configure the command in the Automation panel. The app sends JSON on stdin and expects JSON on stdout:

```json
{
  "summary": "What happened",
  "labels": ["dry peek"],
  "better_play": "What to do next time",
  "confidence": 0.7
}
```

The core app does not upload clips or frames. Any network behavior would come from the command you configure, so use a truly local model runner if you want local-only review.

For Ollama, use provider `ollama`, base URL `http://127.0.0.1:11434`, and a vision-capable model name. For LM Studio, use provider `lmstudio`, base URL `http://127.0.0.1:1234/v1`, and the loaded model name.

## Benchmark Labels

Use benchmark labels to measure detector quality:

- **True Positive** on a death card when the detected/marked death is correct.
- **False Positive** on a suggestion when the detector proposed a bad event.
- **Missed Death** in the match report when a real death was not detected.

The Automation panel shows measured precision/recall when enough labels exist and can apply detector sensitivity tuning.

## Backup And Restore

Use **Backup DB** in the Automation panel before major cleanup or migration. Backups are stored under:

```text
data\backups
```

Restoring a backup replaces `data\coach.sqlite3`; restart the app afterward.

## Calibration

Use the **Calibration** panel to tune normalized screen regions for your capture layout:

- `x` and `y` are the top-left corner from `0` to `1`
- `w` and `h` are width and height from `0` to `1`

The default regions assume standard 16:9 VALORANT HUD placement. Save calibration before running HUD, Minimap, or OCR analysis if your recording is cropped, ultrawide, or scaled.

You can also open any match report and drag the overlay boxes directly on top of the VOD player. Choose a region, move or resize its box, then click **Save Overlay**.

## Build An EXE

Install PyInstaller:

```powershell
python -m pip install pyinstaller
```

Build a Windows app folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

Run:

```text
dist\ValorantCoachAgent\ValorantCoachAgent.exe
```

The packaged app opens the browser automatically and stores runtime data beside the executable.

## Troubleshooting

- **Browser cannot connect**: confirm `python run.py` is still running and check the exact port printed in PowerShell.
- **`python` is not recognized**: reinstall Python and enable PATH, or run with `py run.py`.
- **No videos found**: confirm the recording folder path is correct and contains supported video extensions.
- **OCR says Tesseract is missing**: install Tesseract or add its install folder to PATH.
- **Analysis says manual review needed**: add a matching `.events.json` sidecar, then run Analyze again.
- **Windows Firewall prompt appears**: allow access for private networks only if you want to reach the app from another device on your LAN. Local browser usage normally works on `127.0.0.1`.

## Data Location

Local runtime data is stored inside the project folder:

- SQLite database: `data/coach.sqlite3`
- Generated reports: `reports`
- Future clips: `clips`

Delete `data/coach.sqlite3` if you want to reset the app's imported match list.
