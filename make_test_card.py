#!/usr/bin/env python3
"""
Creates a fake DJI Neo 2 SD card at /tmp/dji_test_card/ for testing drone_import.py.

Two sessions:
  Session A — harbor shoot, June 27 2024, San Francisco waterfront GPS
  Session B — bridge shoot, June 28 2024, same area (different day → separate session)

Each session has two clips with matching .SRT telemetry sidecars.
"""

import struct
import os
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path("/tmp/dji_test_card")
DCIM = ROOT / "DCIM" / "DJI_001"
DCIM.mkdir(parents=True, exist_ok=True)

# ── Minimal valid MP4 ──────────────────────────────────────────────────────────
# Just enough for the OS to recognize it as MP4; exiftool will read basic info.
# DJI GPS won't be present — script will fall back to .SRT sidecar.

def make_mp4(path: Path, size_kb: int = 800) -> None:
    """Write a minimal ftyp+mdat MP4 so exiftool doesn't error out."""
    ftyp = (
        struct.pack(">I", 20) +     # box size
        b"ftyp" +
        b"mp42" +                   # major brand
        struct.pack(">I", 0) +      # minor version
        b"mp42" + b"isom"           # compatible brands
    )
    payload = b"\x00" * (size_kb * 1024)
    mdat = struct.pack(">I", 8 + len(payload)) + b"mdat" + payload
    path.write_bytes(ftyp + mdat)


# ── DJI SRT template ───────────────────────────────────────────────────────────
# Real format from DJI Neo / Mini series.
# GPS(longitude, latitude, altitude)

def make_srt(path: Path, start_dt: datetime, lon: float, lat: float, clip_secs: int = 45) -> None:
    lines = []
    t = start_dt
    frame_ms = 33  # ~30fps
    for i in range(1, (clip_secs * 1000 // frame_ms) + 1):
        t_start = timedelta(milliseconds=(i - 1) * frame_ms)
        t_end   = timedelta(milliseconds=i * frame_ms)

        def fmt(td):
            total_ms = int(td.total_seconds() * 1000)
            h, rem = divmod(total_ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, ms  = divmod(rem, 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        ts = (t + t_start).strftime("%Y-%m-%d %H:%M:%S") + f".{(i * frame_ms) % 1000:03d}"

        # DJI subtly drifts GPS — add tiny offset per frame so it looks real
        lon_f = lon + i * 0.000002
        lat_f = lat + i * 0.000001

        lines.append(str(i))
        lines.append(f"{fmt(t_start)} --> {fmt(t_end)}")
        lines.append('<font size="28">SrtCnt : ' + str(i) + f", DiffTime : {frame_ms}ms")
        lines.append(ts)
        lines.append(
            f"[iso : 100] [shutter : 1/2000.0] [fnum : 1.7] [ev : 0] "
            f"[GPS({lon_f:.6f}, {lat_f:.6f}, 12)] "
            f"[distance : {i * 2}] [speed : 8.3] [uavBat : 87] [signal : 4]"
        )
        lines.append("</font>")
        lines.append("")

    path.write_text("\n".join(lines))


# ── Session A: harbor, 2024-06-27, Fisherman's Wharf SF ───────────────────────
# lat=37.8085, lon=-122.4155

session_a_start = datetime(2024, 6, 27, 14, 32, 11)

for i, (offset_min, clip_s, kb) in enumerate([(0, 52, 1200), (7, 38, 900)], start=1):
    clip_dt = session_a_start + timedelta(minutes=offset_min)
    mp4 = DCIM / f"DJI_000{i}.MP4"
    srt = DCIM / f"DJI_000{i}.SRT"
    make_mp4(mp4, kb)
    make_srt(srt, clip_dt, lon=-122.4155, lat=37.8085, clip_secs=clip_s)
    print(f"Created {mp4.name}  ({kb} KB)  + {srt.name}")

# ── Session B: bridge, 2024-06-28 (next day → separate session) ───────────────
# Golden Gate Bridge area: lat=37.8199, lon=-122.4783

session_b_start = datetime(2024, 6, 28, 10, 15, 0)

for i, (offset_min, clip_s, kb) in enumerate([(0, 61, 1500), (12, 44, 1050)], start=3):
    clip_dt = session_b_start + timedelta(minutes=offset_min)
    mp4 = DCIM / f"DJI_000{i}.MP4"
    srt = DCIM / f"DJI_000{i}.SRT"
    make_mp4(mp4, kb)
    make_srt(srt, clip_dt, lon=-122.4783, lat=37.8199, clip_secs=clip_s)
    print(f"Created {mp4.name}  ({kb} KB)  + {srt.name}")

print(f"\nTest card ready at: {ROOT}")
print("Directory structure:")
for p in sorted(DCIM.iterdir()):
    print(f"  {p.name}  ({p.stat().st_size // 1024} KB)")
