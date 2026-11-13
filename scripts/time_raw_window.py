"""Time raw channel-data reads for windows in the subset manifest.

Read-only. One form per process, so a warm cache does not leak across forms.
Prints a progress line after every window, including the projected hours and
GiB for the 500 pilot, the first 8,000 volumes, and the full 24,000. Ctrl+C
is safe: nothing is written except the JSON of windows finished so far.

    cache/venv/bin/python scripts/time_raw_window.py --form rf --limit 1
    cache/venv/bin/python scripts/time_raw_window.py --form rf
    cache/venv/bin/python scripts/time_raw_window.py --form iq_up0
    cache/venv/bin/python scripts/time_raw_window.py --form iq_up1
"""

from __future__ import annotations

import argparse
import json
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
from ilipy import Session
from ilipy.channeldata import ChannelDataReader, ImageProfile

from progress import Progress

ROOT = Path(__file__).resolve().parents[1]
FORMS = ("rf", "iq_up0", "iq_up1")


def read_form(reader: ChannelDataReader, start: int, count: int, form: str):
    if form == "rf":
        return reader.get_rf_frames(start, count)
    return reader.get_iq_frames(start, count, upsample=(form == "iq_up1"))


def project(manifest: pd.DataFrame, seconds_per_volume: float, kib_per_frame: float) -> str:
    scopes = {
        "pilot": manifest[manifest.pilot],
        "8k": manifest[manifest.tier <= 4000],
        "24k": manifest,
    }
    parts = []
    for name, rows in scopes.items():
        hours = len(rows) * seconds_per_volume / 3600
        gib = float(rows.n_frames.sum()) * kib_per_frame / 1024**2
        parts.append(f"{name} {hours:.1f}h {gib:.0f}GiB")
    return "  ".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--form", choices=FORMS, required=True)
    parser.add_argument("--windows-json", type=Path, default=ROOT / "artifacts" / "sample_windows.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "artifacts" / "manifest.parquet")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Read only the first N windows, then stop.")
    parser.add_argument("--environment", default="prod")
    args = parser.parse_args()
    if args.output is None:
        args.output = ROOT / "work" / f"raw_timings_{args.form}.json"

    windows = json.loads(args.windows_json.read_text())
    if args.limit:
        windows = windows[: args.limit]
    manifest = pd.read_parquet(args.manifest)
    session = Session(environment=args.environment)
    results = []
    active = None
    bar = Progress(len(windows), args.form)
    for window in windows:
        record = dict(window)
        record["form"] = args.form
        try:
            if active != window["inspection_id"]:
                session.set_active_inspection(window["inspection_id"])
                active = window["inspection_id"]
            t0 = time.perf_counter()
            clip = session.get_clip_by_id(window["clip_id"])
            reader = ChannelDataReader(session, clip, ImageProfile.ZeroAngle)
            n = int(window["n_frames"])
            t1 = time.perf_counter()
            frames = read_form(reader, int(window["frame_start"]), n, args.form)
            t2 = time.perf_counter()
            read_form(reader, int(window["frame_start"]), n, args.form)
            t3 = time.perf_counter()
            raw = np.ascontiguousarray(frames)
            zlib_bytes = len(zlib.compress(raw.tobytes(), 1))
            record.update(
                shape=list(raw.shape),
                dtype=str(raw.dtype),
                raw_bytes=int(raw.nbytes),
                kib_per_frame=round(raw.nbytes / n / 1024, 1),
                open_s=round(t1 - t0, 3),
                read_cold_s=round(t2 - t1, 3),
                read_warm_s=round(t3 - t2, 3),
                zlib1_ratio=round(raw.nbytes / zlib_bytes, 2),
            )
        except Exception as error:  # noqa: BLE001 - record and keep going
            record["error"] = f"{type(error).__name__}: {error}"
        results.append(record)
        good = [row for row in results if "error" not in row]
        detail = record.get("error", "")
        if good:
            seconds = float(np.median([row["open_s"] + row["read_cold_s"] for row in good]))
            kib = float(np.median([row["kib_per_frame"] for row in good]))
            detail = f"{kib:.0f} KiB/fr  cold {seconds:.1f}s  {project(manifest, seconds, kib)}"
        bar.tick(detail)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2) + "\n")

    good = [row for row in results if "error" not in row]
    print(f"wrote {args.output}  ok {len(good)}  failed {len(results) - len(good)}")


if __name__ == "__main__":
    main()
