#!/usr/bin/env python3
"""
drone_search.py — Index and search your organized drone footage library.

Commands:
    list                          List all sessions (newest first)
    search [keyword]              Full-text search across subject, location, folder
    search --date 2024-06         Filter by date prefix (year, month, or full date)
    search --location SF          Filter by location code
    stats                         Library statistics
    rebuild                       Rebuild index by scanning library folder on disk
    setup                         Configure library root path

Examples:
    python drone_search.py list
    python drone_search.py search harbor
    python drone_search.py search --date 2024-06 --location SF
    python drone_search.py search downtown --date 2024
    python drone_search.py stats
    python drone_search.py rebuild
    python drone_search.py setup ~/Videos/Drone
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CONFIG_DIR  = Path.home() / ".drone_library"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE     = CONFIG_DIR / "library.db"
VIDEO_EXTS  = {".mp4", ".mov"}

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"library_root": str(Path.home() / "Videos" / "Drone")}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def open_db() -> sqlite3.Connection:
    if not DB_FILE.exists():
        print("No library database found.")
        print("Run 'drone_import.py' first, or use 'rebuild' to index an existing folder.")
        sys.exit(1)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_size(n: int) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dur(s) -> str:
    if s is None:
        return "?"
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def col(text: str, width: int) -> str:
    return str(text)[:width].ljust(width)


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list(args, cfg, conn) -> None:
    rows = conn.execute("""
        SELECT s.id, s.folder_name, s.shoot_date, s.location_code, s.subject,
               COUNT(f.id)      AS file_count,
               SUM(f.file_size) AS total_size,
               SUM(f.duration_s) AS total_dur
        FROM sessions s
        LEFT JOIN files f ON f.session_id = s.id
        GROUP BY s.id
        ORDER BY s.shoot_date DESC, s.id DESC
    """).fetchall()

    if not rows:
        print("Library is empty. Import footage with drone_import.py")
        return

    hdr = f"{'Folder':<38}  {'Date':<12}  {'Loc':>4}  {'Files':>5}  {'Size':>8}  {'Duration':>9}"
    print(f"\n{hdr}")
    print("─" * len(hdr))
    for r in rows:
        print(
            f"{col(r['folder_name'], 38)}  "
            f"{col(r['shoot_date'] or '—', 12)}  "
            f"{col(r['location_code'] or '—', 4)}  "
            f"{r['file_count']:>5}  "
            f"{fmt_size(r['total_size']):>8}  "
            f"{fmt_dur(r['total_dur']):>9}"
        )
    print(f"\n{len(rows)} session(s) total.")


def cmd_search(args, cfg, conn) -> None:
    clauses = ["1=1"]
    params  = []

    if args.keyword:
        kw = f"%{args.keyword}%"
        clauses.append("(s.subject LIKE ? OR s.location LIKE ? OR s.folder_name LIKE ? OR s.location_code LIKE ?)")
        params.extend([kw, kw, kw, kw])

    if args.date:
        clauses.append("s.shoot_date LIKE ?")
        params.append(f"{args.date}%")

    if args.location:
        clauses.append("UPPER(s.location_code) = ?")
        params.append(args.location.upper())

    if args.subject:
        clauses.append("s.subject LIKE ?")
        params.append(f"%{args.subject}%")

    where = " AND ".join(clauses)
    rows  = conn.execute(f"""
        SELECT s.id, s.folder_name, s.folder_path, s.shoot_date,
               s.location_code, s.subject, s.location,
               COUNT(f.id)       AS file_count,
               SUM(f.file_size)  AS total_size,
               SUM(f.duration_s) AS total_dur
        FROM sessions s
        LEFT JOIN files f ON f.session_id = s.id
        WHERE {where}
        GROUP BY s.id
        ORDER BY s.shoot_date DESC
    """, params).fetchall()

    if not rows:
        print("No matching sessions.")
        return

    for r in rows:
        print(f"\n{'─'*60}")
        print(f"  Folder:    {r['folder_name']}")
        print(f"  Path:      {r['folder_path']}")
        print(f"  Date:      {r['shoot_date'] or '—'}")
        print(f"  Location:  {r['location'] or r['location_code'] or '—'}")
        print(f"  Subject:   {r['subject'] or '—'}")
        print(f"  Files:     {r['file_count']}  ({fmt_size(r['total_size'])})")
        print(f"  Duration:  {fmt_dur(r['total_dur'])}")

        files = conn.execute(
            "SELECT stored_path, file_size, duration_s FROM files WHERE session_id = ? ORDER BY stored_path",
            (r["id"],),
        ).fetchall()
        if files:
            print(f"  Contents:")
            for f in files:
                name = Path(f["stored_path"]).name
                print(f"    {name:<30}  {fmt_size(f['file_size']):>8}  {fmt_dur(f['duration_s']):>8}")

    print(f"\n{len(rows)} session(s) found.")


def cmd_stats(args, cfg, conn) -> None:
    stats = conn.execute("""
        SELECT
            COUNT(DISTINCT s.id)   AS sessions,
            COUNT(f.id)            AS files,
            SUM(f.file_size)       AS total_bytes,
            SUM(f.duration_s)      AS total_seconds,
            MIN(s.shoot_date)      AS first_date,
            MAX(s.shoot_date)      AS last_date
        FROM sessions s
        LEFT JOIN files f ON f.session_id = s.id
    """).fetchone()

    top_locs = conn.execute("""
        SELECT location_code, subject, COUNT(*) AS cnt
        FROM sessions
        GROUP BY location_code, subject
        ORDER BY cnt DESC
        LIMIT 8
    """).fetchall()

    top_subjects = conn.execute("""
        SELECT subject, COUNT(*) AS cnt
        FROM sessions
        GROUP BY subject
        ORDER BY cnt DESC
        LIMIT 5
    """).fetchall()

    print(f"\nLibrary Statistics")
    print(f"{'─'*36}")
    print(f"  Sessions:       {stats['sessions']}")
    print(f"  Video files:    {stats['files']}")
    print(f"  Total storage:  {fmt_size(stats['total_bytes'])}")
    print(f"  Total footage:  {fmt_dur(stats['total_seconds'])}")
    print(f"  Date range:     {stats['first_date']} → {stats['last_date']}")
    print(f"  Library root:   {cfg.get('library_root', '—')}")

    if top_subjects:
        print(f"\n  Top subjects:")
        for r in top_subjects:
            print(f"    {r['subject']:<20}  {r['cnt']} session(s)")

    if top_locs:
        print(f"\n  Top location+subject combos:")
        for r in top_locs:
            tag = f"{r['location_code']}.{r['subject']}"
            print(f"    {tag:<24}  {r['cnt']}×")


def cmd_rebuild(args, cfg, conn) -> None:
    """
    Scan the library folder and add any sessions/files not yet in the database.
    Useful after manually moving folders or on first run after migrating existing footage.
    """
    library_root = Path(cfg["library_root"]).expanduser()
    if not library_root.exists():
        print(f"Library root not found: {library_root}")
        print(f"Set it with:  python drone_search.py setup /path/to/folder")
        sys.exit(1)

    print(f"Rebuilding index from {library_root} …")

    # Matches YYYYMMDD.XX.subject  and  YYYYMMDD.XX.subject.2  etc.
    folder_re = re.compile(r"^(\d{8})\.([A-Z0-9]+)\.(.+?)(?:\.\d+)?$", re.IGNORECASE)

    added_sessions = 0
    added_files    = 0
    now_str        = datetime.now().isoformat()

    for folder in sorted(library_root.iterdir()):
        if not folder.is_dir():
            continue
        m = folder_re.match(folder.name)
        if not m:
            continue

        date_str, loc_code, subject = m.groups()
        try:
            shoot_date = datetime.strptime(date_str, "%Y%m%d").date().isoformat()
        except ValueError:
            continue

        existing = conn.execute(
            "SELECT id FROM sessions WHERE folder_path = ?", (str(folder),)
        ).fetchone()

        if existing:
            session_id = existing[0]
        else:
            cur = conn.execute(
                """INSERT INTO sessions
                   (folder_name, folder_path, shoot_date, location_code, subject, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (folder.name, str(folder), shoot_date, loc_code.upper(), subject.lower(), now_str),
            )
            session_id = cur.lastrowid
            added_sessions += 1

        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in VIDEO_EXTS:
                continue
            exists = conn.execute(
                "SELECT id FROM files WHERE stored_path = ?", (str(f),)
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT OR IGNORE INTO files
                       (session_id, original_name, stored_path, file_size, imported_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, f.name, str(f), f.stat().st_size, now_str),
                )
                added_files += 1

    conn.commit()
    print(f"Done. Added {added_sessions} session(s) and {added_files} file(s) to index.")


def cmd_setup(args, cfg) -> None:
    if args.path:
        p = Path(args.path).expanduser()
        cfg["library_root"] = str(p)
        save_config(cfg)
        print(f"Library root set to: {p}")
    else:
        print(f"Current library root: {cfg.get('library_root', 'not set')}")
        print(f"Config file:         {CONFIG_FILE}")
        print(f"Database:            {DB_FILE}")
        print()
        print("To change:  python drone_search.py setup /new/path")
        print("Or edit:    " + str(CONFIG_FILE))


# ── Entry Point ────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Search and manage your drone footage library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command")

    # list
    sub.add_parser("list", help="List all sessions (newest first)")

    # search
    ps = sub.add_parser("search", help="Search sessions by keyword, date, or location")
    ps.add_argument("keyword",     nargs="?",        help="Keyword (subject, location, folder name)")
    ps.add_argument("--date",      metavar="PREFIX", help="Date prefix: 2024, 2024-06, 2024-06-27")
    ps.add_argument("--location",  metavar="CODE",   help="Location code, e.g. SF")
    ps.add_argument("--subject",   metavar="WORD",   help="Subject keyword, e.g. harbor")

    # stats
    sub.add_parser("stats", help="Show library statistics")

    # rebuild
    sub.add_parser("rebuild", help="Rebuild index by scanning library folder on disk")

    # setup
    pset = sub.add_parser("setup", help="Configure library root path")
    pset.add_argument("path", nargs="?", help="Path to set as library root")

    args = p.parse_args()

    if not args.command:
        p.print_help()
        sys.exit(0)

    cfg = load_config()

    if args.command == "setup":
        cmd_setup(args, cfg)
        return

    # rebuild needs to create tables if they don't exist yet
    if args.command == "rebuild":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY, folder_name TEXT, folder_path TEXT,
                shoot_date TEXT, location TEXT, location_code TEXT, subject TEXT,
                latitude REAL, longitude REAL, imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, session_id INTEGER, original_name TEXT,
                stored_path TEXT UNIQUE, file_size INTEGER, quick_hash TEXT,
                duration_s REAL, resolution TEXT, imported_at TEXT
            );
        """)
        cmd_rebuild(args, cfg, conn)
        return

    conn = open_db()
    dispatch = {"list": cmd_list, "search": cmd_search, "stats": cmd_stats}
    dispatch[args.command](args, cfg, conn)


if __name__ == "__main__":
    main()
