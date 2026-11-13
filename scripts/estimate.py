"""Data-prep time and size estimate for the paired raw + LL subset.

Measured inputs come from ``catalog_census.json`` (the live mahsa catalog).
Everything else is an ASSUMPTION with a low and a high value, and is printed
as such. Replace the assumed values with the output of
``time_raw_window.py`` once it has been run.

Run:
    python3 scripts/od_raw_ll/estimate.py
"""

from __future__ import annotations

import argparse
import json

KIB = 1024
GIB = 1024**3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", default="scripts/od_raw_ll/catalog_census.json")
    parser.add_argument("--workers", type=int, default=8)
    # ASSUMED. od_chunk_prefetch_report.md: "One 1024-frame span is about
    # 107 MiB decoded" -> ~107 KiB per frame, and "about two channel-data
    # chunks" -> ~512 frames per chunk.
    parser.add_argument("--raw-kib-per-frame", type=float, default=107.0)
    parser.add_argument("--chunk-frames", type=int, default=512)
    # ASSUMED download rate for whole chunks, frames per second. Low is the
    # pod pipeline rate the prefetcher kept ahead of (225 fps, same report),
    # so it is a floor, not a measurement of the link. High is a guess.
    parser.add_argument("--download-fps", type=float, nargs=2, default=[225.0, 1000.0])
    # ASSUMED fixed cost per volume: open reader, write row, bookkeeping.
    parser.add_argument("--overhead-s", type=float, nargs=2, default=[1.0, 0.3])
    # Override the whole per-volume read cost with a measured value.
    parser.add_argument("--raw-s-per-volume", type=float, default=None)
    # ASSUMED S3 throughput for copying LL blobs and writing raw, MB/s.
    parser.add_argument("--s3-mb-per-s", type=float, nargs=2, default=[50.0, 150.0])
    args = parser.parse_args()

    with open(args.census) as handle:
        census = json.load(handle)
    volumes = census["volumes"]
    frames = census["total_frames"]
    mean_frames = frames / volumes
    ll_bytes_per_frame = census["lossy_bytes"] / frames

    print("MEASURED (live catalog)")
    print(f"  volumes={volumes}  frames={frames}  mean frames/volume={mean_frames:.1f}")
    print(f"  LL lossy JP2: {census['lossy_bytes'] / 1e9:.1f} GB, {ll_bytes_per_frame / 1e3:.1f} kB/frame")
    print("ASSUMED (not measured)")
    print(f"  raw {args.raw_kib_per_frame} KiB/frame uncompressed, {args.chunk_frames} frames/chunk")
    print(f"  download {args.download_fps} fps, overhead {args.overhead_s} s/volume, S3 {args.s3_mb_per_s} MB/s")

    # A window of n frames touches 1 + (n - 1) / chunk chunks on average, and
    # every touched chunk is downloaded whole.
    chunks_per_volume = 1 + (mean_frames - 1) / args.chunk_frames
    frames_downloaded = chunks_per_volume * args.chunk_frames
    print(f"  => {chunks_per_volume:.2f} chunks per volume, {frames_downloaded:.0f} frames downloaded to keep {mean_frames:.0f}")

    subsets = {
        "30% subset": round(0.30 * volumes),
        "10k volumes": 10_000,
        "full dataset": volumes,
    }
    print(f"\n{'subset':<14}{'volumes':>8}{'frames':>10}{'raw GiB':>9}{'LL GB':>7}"
          f"{'1 worker h':>16}{f'{args.workers} workers h':>16}{'S3 io min':>14}")
    for name, n_volumes in subsets.items():
        n_frames = n_volumes * mean_frames
        raw_bytes = n_frames * args.raw_kib_per_frame * KIB
        ll_bytes = n_frames * ll_bytes_per_frame
        hours = []
        for fps, overhead in zip(args.download_fps, args.overhead_s):
            per_volume = (
                args.raw_s_per_volume
                if args.raw_s_per_volume is not None
                else frames_downloaded / fps + overhead
            )
            hours.append(n_volumes * per_volume / 3600)
        io_min = [(raw_bytes + ll_bytes) / (rate * 1e6) / 60 for rate in args.s3_mb_per_s]
        slow, fast = max(hours), min(hours)
        print(
            f"{name:<14}{n_volumes:>8}{n_frames:>10.0f}{raw_bytes / GIB:>9.0f}{ll_bytes / 1e9:>7.1f}"
            f"{f'{fast:.1f} - {slow:.1f}':>16}"
            f"{f'{fast / args.workers:.1f} - {slow / args.workers:.1f}':>16}"
            f"{f'{min(io_min):.0f} - {max(io_min):.0f}':>14}"
        )
    print("\nraw GiB is uncompressed; S3 io is the LL copy plus the raw upload, uncompressed.")


if __name__ == "__main__":
    main()
