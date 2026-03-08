#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drone_import.py — Copy and organize DJI Neo 2 footage.

Naming convention:  YYYYMMDD.XX.subject/
    YYYYMMDD  = shoot date from file metadata (GPS/exiftool/SRT)
    XX        = 2–3 letter location code (auto-suggested or custom)
    subject   = what you filmed (harbor, bridge, park, etc.)

Examples:
    python drone_import.py                          # auto-detect DJI card
    python drone_import.py /Volumes/DJI_001         # explicit source
    python drone_import.py /Volumes/DJI_001 --move  # move instead of copy
    python drone_import.py /path/to/files --dest ~/Videos/Drone --dry-run
    python drone_import.py /path/to/files --location SF --subject bay-bridge
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Paths ──────────────────────────────────────────────────────────────────────

CONFIG_DIR  = Path.home() / ".drone_library"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE     = CONFIG_DIR / "library.db"

DEFAULT_CONFIG: dict = {
    "library_root": str(Path.home() / "Videos" / "Drone"),
    "move_by_default": False,
    # Add your own: "Old Saybrook": "OS", "Downtown Marina": "DM", etc.
    "location_abbreviations": {
        "New York City": "NYC",
        "Los Angeles":   "LA",
        "San Francisco": "SF",
        "Chicago":       "CHI",
        "Seattle":       "SEA",
        "Portland":      "PDX",
        "Austin":        "ATX",
        "Denver":        "DEN",
        "Miami":         "MIA",
        "Boston":        "BOS",
        "San Diego":     "SD",
        "Las Vegas":     "LV",
    },
}

VIDEO_EXTS = {".mp4", ".mov"}

VERBOSE = False  # set from --verbose flag in main()


def vprint(*a, **kw) -> None:
    if VERBOSE:
        print(*a, **kw)

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        merged = {**DEFAULT_CONFIG, **saved}
        merged["location_abbreviations"] = {
            **DEFAULT_CONFIG["location_abbreviations"],
            **saved.get("location_abbreviations", {}),
        }
        return merged
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"  Config saved: {CONFIG_FILE}")


# ── Database ───────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY,
            folder_name   TEXT NOT NULL,
            folder_path   TEXT NOT NULL,
            shoot_date    TEXT,
            location      TEXT,
            location_code TEXT,
            subject       TEXT,
            latitude      REAL,
            longitude     REAL,
            imported_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            id            INTEGER PRIMARY KEY,
            session_id    INTEGER REFERENCES sessions(id),
            original_name TEXT,
            stored_path   TEXT NOT NULL UNIQUE,
            file_size     INTEGER,
            quick_hash    TEXT,
            duration_s    REAL,
            resolution    TEXT,
            imported_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_date     ON sessions(shoot_date);
        CREATE INDEX IF NOT EXISTS idx_sessions_loc      ON sessions(location_code);
        CREATE INDEX IF NOT EXISTS idx_sessions_subject  ON sessions(subject);
        CREATE INDEX IF NOT EXISTS idx_files_hash        ON files(quick_hash);
    """)
    conn.commit()
    return conn


# ── Hashing ────────────────────────────────────────────────────────────────────

def quick_hash(path: Path) -> str:
    """
    Fast fingerprint: first 1MB + last 1MB + file size.
    Reliable for duplicate detection without reading entire 4K clips.
    """
    h    = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    chunk = 1 << 20  # 1 MB
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, 2)
            h.update(f.read(chunk))
    return "q:" + h.hexdigest()


def is_duplicate(conn: sqlite3.Connection, h: str) -> Optional[str]:
    """Return stored path if this hash is already in the library."""
    row = conn.execute(
        "SELECT stored_path FROM files WHERE quick_hash = ?", (h,)
    ).fetchone()
    return row[0] if row else None


# ── DJI Card Detection ─────────────────────────────────────────────────────────

def find_dji_volumes() -> list:
    candidates = []

    # macOS
    vol_root = Path("/Volumes")
    if vol_root.exists():
        for vol in vol_root.iterdir():
            if vol.name.upper().startswith("DJI") or (vol / "DCIM").exists():
                candidates.append(vol)

    # Linux (udisks2 / udev mount points)
    for mount_root in [Path("/media"), Path("/mnt"), Path("/run/media")]:
        if not mount_root.exists():
            continue
        for entry in mount_root.iterdir():
            sub = [entry] if entry.is_dir() and (entry / "DCIM").exists() else []
            if entry.is_dir():
                for child in entry.iterdir():
                    if child.is_dir() and (
                        child.name.upper().startswith("DJI") or (child / "DCIM").exists()
                    ):
                        sub.append(child)
            candidates.extend(sub)

    # Windows: lettered drives containing DCIM
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            p = Path(f"{letter}:\\")
            if p.exists() and (p / "DCIM").exists():
                candidates.append(p)

    return candidates


# ── Metadata Extraction ────────────────────────────────────────────────────────

def _run(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def get_metadata_exiftool(path: Path) -> dict:
    """Extract GPS, date, duration, resolution using exiftool (if installed)."""
    if not shutil.which("exiftool"):
        return {}
    result = _run([
        "exiftool", "-json", "-n",
        "-CreateDate", "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude",
        "-Duration", "-ImageSize", str(path),
    ])
    if result.returncode != 0:
        vprint(f"    [exiftool] error on {path.name}: {result.stderr.strip()}")
        return {}
    try:
        data = json.loads(result.stdout)[0]
    except (json.JSONDecodeError, IndexError):
        return {}
    vprint(f"    [exiftool] {path.name}: " +
           ", ".join(f"{k}={v}" for k, v in data.items() if k != "SourceFile" and v))

    meta = {}

    for field in ("CreateDate", "DateTimeOriginal"):
        val = data.get(field, "")
        if val and val != "0000:00:00 00:00:00":
            try:
                meta["date"] = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                break
            except ValueError:
                pass

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    if lat is not None and lon is not None:
        meta["lat"] = float(lat)
        meta["lon"] = float(lon)

    dur = data.get("Duration")
    if dur is not None:
        meta["duration_s"] = float(dur)

    size = data.get("ImageSize")
    if size:
        meta["resolution"] = str(size)

    return meta


def parse_srt_metadata(srt_path: Path) -> dict:
    """
    DJI .SRT sidecar files embed per-frame telemetry.
    Parse the first frame for shoot date and GPS.

    DJI SRT GPS format (longitude first):
        GPS(120.123456, 30.654321, 50)
    """
    meta = {}
    try:
        content = srt_path.read_text(errors="replace")
    except OSError:
        return meta

    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", content)
    if m:
        try:
            meta["date"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    # DJI SRT: GPS(longitude, latitude, altitude)
    gps = re.search(r"GPS\(([-\d.]+),\s*([-\d.]+),\s*([-\d.]+)\)", content)
    if gps:
        meta["lon"] = float(gps.group(1))
        meta["lat"] = float(gps.group(2))

    return meta


def get_metadata(path: Path) -> dict:
    """Try exiftool → .SRT sidecar → file mtime."""
    meta = get_metadata_exiftool(path)
    source = "exiftool" if meta else ""

    if "lat" not in meta:
        for ext in (".SRT", ".srt"):
            srt = path.with_suffix(ext)
            if srt.exists():
                srt_meta = parse_srt_metadata(srt)
                for k, v in srt_meta.items():
                    if k not in meta:
                        meta[k] = v
                if not source:
                    source = f"SRT ({srt.name})"
                vprint(f"    [SRT]      {srt.name}: date={srt_meta.get('date')}, "
                       f"lat={srt_meta.get('lat')}, lon={srt_meta.get('lon')}")
                break

    if "date" not in meta:
        meta["date"] = datetime.fromtimestamp(path.stat().st_mtime)
        meta["date_source"] = "mtime"
        source = "mtime"

    meta.setdefault("date_source", source or "exiftool")
    vprint(f"    [meta]     {path.name}: date={meta.get('date')}, "
           f"source={meta['date_source']}, GPS={'yes' if 'lat' in meta else 'no'}")
    return meta


# ── Geocoding ──────────────────────────────────────────────────────────────────

def reverse_geocode(lat: float, lon: float) -> dict:
    """Return address dict or {} if geopy isn't installed / request fails."""
    try:
        from geopy.geocoders import Nominatim
        from geopy.exc import GeocoderTimedOut
    except ImportError:
        return {}
    try:
        geo = Nominatim(user_agent="drone_importer/1.0")
        loc = geo.reverse((lat, lon), exactly_one=True, timeout=8)
        if not loc:
            return {}
        addr = loc.raw.get("address", {})
        return {
            "city":    addr.get("city") or addr.get("town") or addr.get("village") or "",
            "state":   addr.get("state", ""),
            "country": addr.get("country", ""),
            "display": loc.address,
        }
    except Exception:
        return {}


def suggest_location_code(city: str, state: str, cfg: dict) -> str:
    abbrevs = cfg.get("location_abbreviations", {})
    for name, code in abbrevs.items():
        if name.lower() in city.lower() or city.lower() in name.lower():
            return code
    words = city.split()
    if len(words) >= 2:
        return "".join(w[0] for w in words if w).upper()[:3]
    if city:
        return city[:3].upper()
    if state:
        return state[:2].upper()
    return "XX"


# ── File Discovery ─────────────────────────────────────────────────────────────

def find_video_files(source: Path) -> list:
    videos = []
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            p = Path(root) / f
            if p.suffix.lower() in VIDEO_EXTS:
                videos.append(p)
    return videos


def group_into_sessions(videos: list, meta_cache: dict) -> list:
    """
    Group clips into sessions by shoot time.
    Clips more than 2 hours apart are treated as separate sessions.
    """
    if not videos:
        return []
    dated = sorted(
        ((meta_cache.get(str(v), {}).get("date", datetime(1970, 1, 1)), v) for v in videos),
        key=lambda x: x[0],
    )
    sessions, current, prev_dt = [], [dated[0][1]], dated[0][0]
    for dt, v in dated[1:]:
        gap_min = abs((dt - prev_dt).total_seconds()) / 60
        if gap_min <= 120:
            current.append(v)
            vprint(f"    [group]    {v.name} → same session (gap {gap_min:.0f} min)")
        else:
            vprint(f"    [group]    {v.name} → new session (gap {gap_min:.0f} min > 120)")
            sessions.append(current)
            current = [v]
        prev_dt = dt
    sessions.append(current)
    return sessions


# ── Interactive Prompts ────────────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{question}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)
    return ans if ans else default


def confirm(question: str, default: bool = True) -> bool:
    tag = "[Y/n]" if default else "[y/N]"
    ans = prompt(f"{question} {tag}").lower()
    return default if not ans else ans.startswith("y")


# ── Core Import ────────────────────────────────────────────────────────────────

def import_session(
    files: list,
    meta_cache: dict,
    cfg: dict,
    conn: sqlite3.Connection,
    move: bool = False,
    dry_run: bool = False,
    forced_location: str = "",
    forced_subject: str = "",
) -> None:
    library_root = Path(cfg["library_root"]).expanduser()
    first        = files[0]
    meta         = meta_cache.get(str(first), {})
    shoot_date   = meta.get("date", datetime.now())
    date_str     = shoot_date.strftime("%Y%m%d")
    date_source  = meta.get("date_source", "metadata")

    lat = meta.get("lat")
    lon = meta.get("lon")
    geo: dict = {}
    city  = ""
    state = ""

    print(f"\n{'─'*60}")
    print(f"  Files:  {len(files)}  |  Date: {shoot_date.strftime('%B %d, %Y')} (from {date_source})")
    for f in files[:6]:
        print(f"    {f.name}")
    if len(files) > 6:
        print(f"    … and {len(files) - 6} more")

    if lat is not None and lon is not None:
        print(f"\n  GPS: {lat:.5f}, {lon:.5f}  — looking up …")
        geo  = reverse_geocode(lat, lon)
        city = geo.get("city", "")
        state = geo.get("state", "")
        if geo:
            print(f"  Location: {geo.get('display', 'unknown')}")
        else:
            print("  (reverse geocoding failed — check network or install geopy)")
    else:
        print("\n  No GPS found in metadata.")

    print()

    if forced_location:
        location_code = forced_location.upper()
        print(f"  Location code: {location_code} (from --location flag)")
    else:
        suggestion = suggest_location_code(city, state, cfg) if (city or state) else "XX"
        location_code = prompt("  Location code (2–3 letters)", suggestion).upper()
        # Save new custom abbreviation if city is known and code is non-default
        if city and location_code and location_code != suggestion:
            cfg.setdefault("location_abbreviations", {})[city] = location_code
            save_config(cfg)

    if forced_subject:
        subject = forced_subject.lower()
        print(f"  Subject: {subject} (from --subject flag)")
    else:
        subject = prompt("  Subject (e.g. harbor, bridge, downtown)").lower()
        if not subject:
            subject = "misc"

    subject = re.sub(r"[^\w-]", "-", subject).strip("-")

    folder_name = f"{date_str}.{location_code}.{subject}"
    dest_folder = library_root / folder_name

    # Avoid collision with existing same-day/same-name folder
    if dest_folder.exists():
        n = 2
        while (library_root / f"{folder_name}.{n}").exists():
            n += 1
        folder_name = f"{folder_name}.{n}"
        dest_folder = library_root / folder_name
        print(f"\n  Folder already exists — using '{folder_name}'")

    print(f"\n  Destination: {dest_folder}")
    if dry_run:
        print("  [DRY RUN — no files will be touched]")

    if not dry_run:
        dest_folder.mkdir(parents=True, exist_ok=True)

    now_str = datetime.now().isoformat()
    session_id = None

    if not dry_run:
        cur = conn.execute(
            """INSERT INTO sessions
               (folder_name, folder_path, shoot_date, location, location_code,
                subject, latitude, longitude, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                folder_name, str(dest_folder),
                shoot_date.strftime("%Y-%m-%d"),
                geo.get("display", f"{city}, {state}".strip(", ")),
                location_code, subject, lat, lon, now_str,
            ),
        )
        session_id = cur.lastrowid
        conn.commit()

    transferred = skipped = 0

    for src in files:
        size     = src.stat().st_size
        dest     = dest_folder / src.name
        h        = quick_hash(src)
        tag      = "MOVE" if move else "COPY"
        print(f"\n  [{tag}] {src.name}  ({size / 1e6:.1f} MB)", end="", flush=True)
        vprint(f"\n    hash={h}")
        file_meta = meta_cache.get(str(src), {})
        if file_meta.get("resolution"):
            vprint(f"    resolution={file_meta['resolution']}")
        if file_meta.get("duration_s"):
            vprint(f"    duration={file_meta['duration_s']:.1f}s")

        if not dry_run:
            dup = is_duplicate(conn, h)
            if dup:
                print(f"\n    Already in library: {dup}  — skipping.")
                skipped += 1
                continue

        if not dry_run:
            if move:
                shutil.move(str(src), dest)
            else:
                shutil.copy2(str(src), dest)

            conn.execute(
                """INSERT OR IGNORE INTO files
                   (session_id, original_name, stored_path, file_size, quick_hash,
                    duration_s, resolution, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, src.name, str(dest), size, h,
                    file_meta.get("duration_s"), file_meta.get("resolution"), now_str,
                ),
            )
            conn.commit()

        print("  ✓", end="")
        transferred += 1

        # Sidecar files (.SRT telemetry, .JPG thumbnail)
        # Deduplicate by inode so .SRT and .srt don't double-copy on
        # case-insensitive filesystems (macOS HFS+/APFS).
        seen_inodes: set = set()
        for ext in (".SRT", ".srt", ".JPG", ".jpg"):
            sidecar = src.with_suffix(ext)
            if not sidecar.exists():
                continue
            inode = sidecar.stat().st_ino
            if inode in seen_inodes:
                continue
            seen_inodes.add(inode)
            s_dest = dest_folder / sidecar.name
            if not dry_run:
                if move:
                    shutil.move(str(sidecar), s_dest)
                else:
                    shutil.copy2(str(sidecar), s_dest)
            print(f"  +{sidecar.name}", end="")

    print(f"\n\n  {transferred} transferred, {skipped} skipped (already in library).")


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Import and organize DJI Neo 2 footage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("source",       nargs="?",       help="Source directory (SD card, DCIM folder, or any folder)")
    p.add_argument("--move",       action="store_true", help="Move files instead of copying")
    p.add_argument("--dest",       metavar="DIR",   help="Override library destination root")
    p.add_argument("--dry-run",    action="store_true", help="Show what would happen without touching files")
    p.add_argument("--location",   metavar="CODE",  help="Skip location prompt (e.g. --location SF)")
    p.add_argument("--subject",    metavar="WORD",  help="Skip subject prompt (e.g. --subject harbor)")
    p.add_argument("--one-session",action="store_true", help="Treat all found files as one session (skip auto-grouping)")
    p.add_argument("--set-dest",   metavar="DIR",   help="Set default library root and exit")
    p.add_argument("--verbose", "-v", action="store_true", help="Show detailed metadata and transfer info")
    args = p.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    cfg  = load_config()
    conn = init_db()

    if args.set_dest:
        cfg["library_root"] = str(Path(args.set_dest).expanduser())
        save_config(cfg)
        print(f"Library root set to: {cfg['library_root']}")
        return

    if args.dest:
        cfg["library_root"] = str(Path(args.dest).expanduser())

    # Resolve source
    if args.source:
        source = Path(args.source).expanduser()
        if not source.exists():
            print(f"Error: '{source}' not found.")
            sys.exit(1)
    else:
        volumes = find_dji_volumes()
        if not volumes:
            print("No DJI volume detected.")
            print("Connect your drone or card, or pass a path:  drone_import.py /path/to/DCIM")
            sys.exit(1)
        if len(volumes) == 1:
            source = volumes[0]
            print(f"Detected: {source}")
        else:
            print("Multiple DJI volumes found:")
            for i, v in enumerate(volumes, 1):
                print(f"  [{i}] {v}")
            choice = prompt("Choose volume", "1")
            try:
                source = volumes[int(choice) - 1]
            except (ValueError, IndexError):
                print("Invalid choice.")
                sys.exit(1)

    print(f"\nScanning {source} …")
    videos = find_video_files(source)

    if not videos:
        print("No video files found.")
        sys.exit(0)

    print(f"Found {len(videos)} video file(s). Reading metadata …")
    if not shutil.which("exiftool"):
        print("  (tip: install exiftool for GPS and accurate dates — `brew install exiftool`)")

    meta_cache = {str(v): get_metadata(v) for v in videos}

    sessions = [videos] if args.one_session else group_into_sessions(videos, meta_cache)
    print(f"Grouped into {len(sessions)} session(s).")
    print(f"Library root: {cfg['library_root']}\n")

    for i, session_files in enumerate(sessions, 1):
        print(f"\nSession {i} / {len(sessions)}")
        import_session(
            session_files, meta_cache, cfg, conn,
            move=args.move or cfg.get("move_by_default", False),
            dry_run=args.dry_run,
            forced_location=args.location or "",
            forced_subject=args.subject or "",
        )

    if args.dry_run:
        print("\n[DRY RUN complete — no files were transferred.]")
    else:
        print(f"\nDone. Library: {cfg['library_root']}")
        print(f"Search with:  python drone_search.py list")


if __name__ == "__main__":
    main()
