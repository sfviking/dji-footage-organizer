# dji-footage-organizer

Import, organize, and search DJI drone footage. Designed for the DJI Neo 2 but works with any DJI drone that produces MP4 + SRT files.

## How it works

Footage is organized into dated folders named `YYYYMMDD.LocationCode.Subject`:

```
~/Videos/Drone/
├── 20240627.SF.harbor/
│   ├── DJI_0001.MP4
│   ├── DJI_0001.SRT
│   ├── DJI_0002.MP4
│   └── DJI_0002.SRT
└── 20240628.GG.bridge/
    ├── DJI_0003.MP4
    └── DJI_0003.SRT
```

- **Date** — pulled from file metadata (exiftool → DJI SRT telemetry → file mtime)
- **Location code** — 2–3 letters, auto-suggested from GPS reverse geocoding, or type your own (e.g. `OS` for Old Saybrook). Custom codes are saved and reused automatically.
- **Subject** — what you filmed: `harbor`, `bridge`, `downtown`, etc.

Multiple clips shot within 2 hours are grouped as one session. Duplicate files are detected by content hash and skipped.

## Requirements

```bash
# Required: exiftool (accurate GPS and dates from video files)
brew install exiftool          # macOS
sudo apt install libimage-exiftool-perl  # Ubuntu/Debian

# Recommended: geopy (GPS → place name lookup)
pip install geopy
```

Python 3.9+ required. No other dependencies.

## Usage

### Import footage

```bash
python drone_import.py                        # auto-detect connected DJI card
python drone_import.py /Volumes/DJI_001       # explicit source path
python drone_import.py /Volumes/DJI_001 --move          # move instead of copy
python drone_import.py /path/to/files --dry-run         # preview without touching files
python drone_import.py /path/to/files --location SF --subject harbor  # skip prompts
```

On first run, set your library destination:

```bash
python drone_import.py --set-dest ~/Videos/Drone
```

### Search your library

```bash
python drone_search.py list                         # all sessions, newest first
python drone_search.py search harbor                # keyword search
python drone_search.py search --date 2024-06        # by month
python drone_search.py search --location SF         # by location code
python drone_search.py search harbor --date 2024    # combine filters
python drone_search.py stats                        # totals and top locations
python drone_search.py rebuild                      # re-index after manual folder changes
python drone_search.py setup ~/Videos/Drone         # set library root
```

## Configuration

Settings are stored in `~/.drone_library/config.json`. The most useful thing to customize is `location_abbreviations` — any code you type during import is saved here automatically, but you can also add them manually:

```json
{
  "library_root": "/Users/you/Videos/Drone",
  "move_by_default": false,
  "location_abbreviations": {
    "Old Saybrook": "OS",
    "Downtown Marina": "DM",
    "San Francisco": "SF"
  }
}
```

The session index lives at `~/.drone_library/library.db` (SQLite). If you ever move folders around manually, run `drone_search.py rebuild` to sync the index.

## Testing

`make_test_card.py` generates a fake DJI SD card with realistic SRT telemetry for two sessions (Fisherman's Wharf and Golden Gate Bridge):

```bash
python make_test_card.py
python drone_import.py /tmp/dji_test_card --dest /tmp/dji_library
python drone_search.py list
```
