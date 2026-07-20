# Windows Packaging

The app can be shipped either as a portable Python folder or as a PyInstaller executable app folder.

## Portable Package

```powershell
cd C:\Users\hjr\valorant-coach-agent
powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1
```

The output folder is `dist`.

Run:

```text
dist\launch.bat
```

Desktop tray shell:

```text
dist\launch_desktop.bat
```

The tray shell starts the local server and provides menu actions for the dashboard, data folder, clips folder, reports folder, restart, stop, and quit. It also runs a local watchdog that restarts the server after an unexpected process exit or repeated failed health checks, unless the user chose Stop or Quit.

The packaged app includes the automation panel, persistent background job queue, auto-import watcher with file-stability checks, storage cleanup, retention, analytics, logs, backups, advanced search/filter, report export, local stat import, memory export/import, app version/schema metadata, provider registry, plugin registry, installer diagnostics, detector benchmark metrics, privacy audit/export/wipe controls, playbook editor, correction review queue, Smart Queue, Story reconstruction, clip annotations, Personal Coach v2, and optional local-AI command review. Optional tools still need to be installed or placed in `tools`.

Create a desktop shortcut:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

Enable start at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\set_startup.ps1
```

Disable start at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\set_startup.ps1 -Disable
```

## ffmpeg

For visual scanning, clip extraction, HUD sampling, and minimap sampling, put ffmpeg here:

```text
dist\tools\ffmpeg\bin\ffmpeg.exe
```

The app also checks PATH, `C:\Program Files\ffmpeg\bin\ffmpeg.exe`, and `C:\ffmpeg\bin\ffmpeg.exe`.

## Tesseract OCR

OCR uses the Tesseract CLI. Install it on the target machine or put it on PATH.

The app checks PATH plus:

```text
<app>\tools\tesseract\tesseract.exe
C:\Program Files\Tesseract-OCR\tesseract.exe
C:\Program Files (x86)\Tesseract-OCR\tesseract.exe
```

## EXE App Folder

Install dependencies:

```powershell
cd C:\Users\hjr\valorant-coach-agent
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

Build:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

Run:

```text
dist\ValorantCoachAgent\ValorantCoachAgent.exe
```

The executable starts the local server and opens `http://127.0.0.1:8766` automatically.

Writable runtime folders are created beside the executable:

- `data`
- `clips`
- `reports`
- `tools`

## Single-File EXE

Single-file mode is supported, but app-folder mode is easier to debug and easier to bundle local tools with.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -OneFile
```

For single-file mode, place optional tools next to the generated `.exe` or put them on PATH.

## Installer ZIP

Create a portable Windows release zip with a desktop shortcut installer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

Output:

```text
dist\valorant-coach-agent-windows.zip
```
