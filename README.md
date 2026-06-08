# CoC2 Scene Reader

A local scene browser for **Corruption of Champions II**. Parses the game's JavaScript scene files into a clean, readable format — with full-text search, favorites, character filtering, and an image gallery. I tested it on Steam version, so can't promise anything about other versions.

\---

## Features

* 📖 **Scene viewer** — renders scene text with branching dialogue, conditions highlighted, and character info
* 🔍 **Search \& filter** — full-text search across all scene content; filter character, and tags
* ⭐ **Favorites** — bookmark scenes with a star; toggle the favorites-only filter to review them later
* 🖼️ **Image gallery** — browse character crop images and CG artwork; images are grouped by character
* 🌙 **Dark / light theme** — persisted per-browser
* ⌨️ **Keyboard navigation** — `Arrow` keys move between scenes, `Esc` closes panels
* 📋 **Copy** — copy a scene's plain text or full JSON to clipboard

\---

## Requirements

|Option|What you need|
|-|-|
|**EXE (Windows)**|Nothing — just run `CoC2Reader.exe`|
|**Python**|Python 3.9+|

\---

## Quick Start

### Using the EXE

1. Place `CoC2Reader.exe` anywhere on your PC.
2. Double-click it — a browser tab opens automatically at `http://localhost:8765`.
3. On first run, paste the path to your CoC2 game folder (e.g. `C:\\Program Files (x86)\\Steam\\steamapps\\common\\Corruption of Champions II`) and click **Build database**. This takes about a minute.
4. Done — the scene list will populate.

### Using Python
Just double-click **`run.bat`**.

The server auto-opens `http://localhost:8765` in your browser.

\---

## First-Time Setup

The app needs two things from your game installation:

|What|Where|
|-|-|
|**Scene files** (`.js`)|Inside the game folder — detected automatically|
|**Bitmaps folder**|`.../resources/app/resources/bitmaps` — also auto-detected for standard Steam paths|

If detection fails, you'll be prompted to enter the paths manually. The bitmaps folder can also be served from a separate HTTP address (e.g. `http://localhost:8081`).

The extracted database (`coc2.db`) is saved next to the app and reused on subsequent launches. To rebuild it, click **Rebuild** in the setup page.

\---

## Project Structure

```
├── server.py          # HTTP server — serves the viewer and handles API calls
├── extractor.py       # Scene parser — reads .js files, builds coc2.db (SQLite)
├── viewer.html        # Single-page frontend (sql.js, vanilla JS)
├── diagnose.py        # Debug tool — analyses scene file structure
├── run.bat            # Shortcut to launch server.py on Windows
├── CoC2Reader.exe     # Standalone executable (no Python required)
└── coc2.db            # Generated database (created on first run)
```

\---

## How It Works

1. **`extractor.py`** scans the game's `.js` scene files, detects scene functions via regex, parses branching `if/else` blocks, strips JS tags, extracts character names from `showBust` / `getNPC` calls, auto-detects content, and writes everything into a SQLite database with FTS5 full-text search.
2. **`server.py`** starts a local HTTP server on port `8765`. On first launch it serves a setup wizard; afterwards it serves `viewer.html` along with the database and bitmap images.
3. **`viewer.html`** loads the database entirely in-browser via [sql.js](https://github.com/sql-js/sql-js), so all querying happens client-side with no external dependencies.
4. **Favorites** are stored in both `localStorage` and `favorites.json` next to the server, so they persist across browser sessions and machines.

\---

## Database Schema (overview)

|Table|Contents|
|-|-|
|`scenes`|Scene ID, source file, flag, tags, character count|
|`segments`|Parsed text blocks (type: `always` or `branch`)|
|`variants`|`if/else` branches within a segment|
|`characters`|Unique character names|
|`scenecharacters`|Scene ↔ character mapping|
|`characterimages`|Character name ↔ bitmap path|
|`cgimages`|CG artwork paths|
|`fts`|FTS5 virtual table for full-text search|

\---

## CLI: Extractor

You can also run the extractor standalone:

```bash
python extractor.py "C:/Path/To/CoC2" --db coc2.db --json output.json --bitmaps "C:/Path/To/CoC2/resources/app/resources/bitmaps" --min-chars 1000
```

|Argument|Default|Description|
|-|-|-|
|`folder`|CoC2 Steam path|Game folder containing `.js` scene files|
|`--db`|`coc2.db`|Output SQLite database path|
|`--json`|`output.json`|Optional JSON export|
|`--bitmaps`|CoC2 Steam path|Bitmaps folder for image indexing|
|`--min-chars`|`1000`|Minimum scene text length to include|
|`--fresh`|—|Delete existing DB and rebuild from scratch|

\---

## Notes

* The app runs **entirely locally** — no data leaves your machine.
* Images are served directly from the game's `bitmaps` folder; no copying is required if using the Python server.
* When opening `viewer.html` directly as a `file://` URL (without the server), sql.js cannot auto-load the database — use `python -m http.server 8080` or the provided `run.bat` instead.

