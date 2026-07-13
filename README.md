# TimeTrackr

A personal time tracker that lives in your Windows system tray.

## Requirements

- Python 3.9+
- Windows (uses the system tray via `pystray`)

## Setup

1. **Create a virtual environment** (one-time):

   ```bash
   python -m venv .venv
   ```

2. **Install dependencies**:

   ```bash
   .venv\Scripts\pip install -r requirements.txt
   ```

## Running

**With a console window** (useful for seeing errors):

```bash
.venv\Scripts\python main.py
```

**Without a console window** (normal use):

```bash
.venv\Scripts\pythonw main.py
```

Or just double-click `run.bat` — it runs `pythonw` automatically.

## macOS

Requires Python 3.9+ (`brew install python` or python.org).

```bash
pip3 install -r requirements.txt
python3 main.py          # or double-click run.command in Finder
```

TimeTrackr appears as a clock icon in the macOS menu bar (top-right). Right-click / click it
for Start/Stop tracking and the dashboard. Data lives in `~/.timetrackr/data.db`, identical
to Windows.

### Run it as a Mac app (Launchpad / Spotlight / start at login)

If you have [uv](https://docs.astral.sh/uv/) installed, you can install a thin
`TimeTrackr.app` that launches this repo and manages its own environment:

```bash
./macos/install_mac_app.sh            # build + install to /Applications + add login item
./macos/install_mac_app.sh --no-login # ...without the start-at-login item
```

This builds a small app bundle whose launcher runs `uv run main.py` (uv creates
and syncs the `.venv` from `pyproject.toml` on first launch), installs it to
`/Applications` (or `~/Applications` if that isn't writable), and registers it as
a Login Item so it starts automatically. Launch it from Launchpad or Spotlight.

The bundle is a **thin wrapper** — it points at this repo by absolute path, so it
picks up code changes automatically but stops working if you move or delete the
repo. Re-run the script after moving the repo. Manage or remove the login item
under **System Settings → General → Login Items**. First run may ask to allow
System Events automation (for the login item) and to confirm opening the app.

## Usage

Once started, TimeTrackr runs silently in the system tray (bottom-right corner of the taskbar). You won't see a window or taskbar button — look for the clock icon in the tray. Right-click it to start/stop tracking or open the dashboard. Closing the dashboard window returns the app to the tray; it keeps running in the background.

The dashboard has four tabs:

| Tab | What it shows |
|-----|---------------|
| Recent Entries | Last 30 time entries in a table |
| This Week | Totals grouped by job/project for the current week |
| This Month | Totals grouped by job/project for the current month |
| Calendar | Weekly time-block view — navigate weeks with ◀ ▶, scroll with mouse wheel |

## Data

All data is stored in a local SQLite database at:

```
C:\Users\<you>\.timetrackr\data.db
```

## Autostart (run on login)

To have TimeTrackr launch automatically when you log in to Windows, add it to the registry:

```powershell
$exePath = "C:\path\to\your\venv\Scripts\pythonw.exe"
$scriptPath = "C:\path\to\TimeTrackr\main.py"
Set-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TimeTrackr" -Value "`"$exePath`" `"$scriptPath`""
```

Replace the paths with the actual locations on your machine.

To remove autostart:

```powershell
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TimeTrackr"
```