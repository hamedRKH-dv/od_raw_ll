"""Time raw channel-data reads for windows already in the OD LL catalog.

Read-only. For each window in ``sample_windows.json`` it opens the clip,
reads the same frame range the LL volume covers as raw RF frames
(``ChannelDataReader.get_rf_frames``, ZeroAngle profile: the profile LL
beamforming reads), and records shape, bytes, seconds, and a zlib-1 size as
a rough compressibility probe.

NOT YET RUN. It reads inspection data through your own ilipy login, so run
it yourself:

    ili-login -e prod
    dv_env/bin/python scripts/od_raw_ll/time_raw_window.py

The numbers it prints replace the assumed rows in ``estimate.py``
(``--raw-kib-per-frame`` and ``--raw-s-per-volume``).
"""

from __future__ import annotations

import argparse
import json
import time
import zlib

import numpy as np
from ilipy import Session
from ilipy.channeldata import ChannelDataReader, ImageProfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-json", default="scripts/od_raw_ll/sample_windows.json")
    parser.add_argument("--output", default="scripts/od_raw_ll/raw_window_timings.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--environment", default="prod")
    args = parser.parse_args()

    with open(args.windows_json) as handle:
        windows = json.load(handle)
    if args.limit:
        windows = windows[: args.limit]

    session = Session(environment=args.environment)
    results = []
    active = None
    for window in windows:
        record = dict(window)
        try:
            if active != window["inspection_id"]:
                session.set_active_inspection(window["inspection_id"])
                active = window["inspection_id"]
            t0 = time.perf_counter()
            clip = session.get_clip_by_id(window["clip_id"])
            reader = ChannelDataReader(session, clip, ImageProfile.ZeroAngle)
            n = int(window["n_frames"])
            shape = list(reader.get_rf_buffer_shape(n))
            t1 = time.perf_counter()
            frames = reader.get_rf_frames(int(window["frame_start"]), n)
            t2 = time.perf_counter()
            # Second read of the same range: chunks are now in the session cache,
            # so this is decode/copy only and the difference is download time.
            reader.get_rf_frames(int(window["frame_start"]), n)
            t3 = time.perf_counter()
            raw = np.ascontiguousarray(frames)
            zlib_bytes = len(zlib.compress(raw.tobytes(), 1))
            record.update(
                shape=shape,
                dtype=str(raw.dtype),
                raw_bytes=int(raw.nbytes),
                kib_per_frame=round(raw.nbytes / n / 1024, 1),
                open_s=round(t1 - t0, 3),
                read_cold_s=round(t2 - t1, 3),
                read_warm_s=round(t3 - t2, 3),
                zlib1_ratio=round(raw.nbytes / zlib_bytes, 2),
                rf_decimation=int(reader.get_rf_decimation()),
            )
        except Exception as error:  # noqa: BLE001 - report and continue
            record["error"] = f"{type(error).__name__}: {error}"
        print(json.dumps(record))
        results.append(record)

    with open(args.output, "w") as handle:
        json.dump(results, handle, indent=2)
    good = [r for r in results if "error" not in r]
    if good:
        print(
            f"median kib_per_frame={np.median([r['kib_per_frame'] for r in good]):.1f} "
            f"median cold s/volume={np.median([r['open_s'] + r['read_cold_s'] for r in good]):.2f}"
        )
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
