"""Write raw IQ/RF windows for a manifest slice into raw_volumes.

Stage 5. One process, one Session, clips in order. Resume skips volume_ids
already stored. Stops before reading when free space is under 20 GiB.
Uncompressed bytes (codec "raw"); hash is SHA-256 of those bytes.

ponytail: one reader process. Upgrade path is one process per clip group
if the pilot's wall time matters; the resume key stays volume_id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
from ilipyutils.lance.store import DEFAULT_NEW_TABLE_STORAGE_OPTIONS, LanceCatalog
from lance.blob import blob_array

from raw_reader import read_raw_window
from raw_schema import raw_volumes_schema
from progress import Progress
from select_paired_subset import NAMESPACE, slice_manifest

ROOT = Path(__file__).resolve().parents[1]
STORAGE = {**DEFAULT_NEW_TABLE_STORAGE_OPTIONS, "aws_region": "us-west-2"}
MIN_FREE = 20 * 1024**3
HTTP_REQUESTS = 16
CENTRAL_CACHE = 256 * 1024**2
HTTP_CACHE = 512 * 1024**2


class _NopPrefetch:
    def launch(self) -> None:
        return None

    def wait(self) -> None:
        return None

    def clear(self) -> None:
        return None


def _free_bytes(path: Path) -> int:
    probe = path if path.exists() else path.parent
    return shutil.disk_usage(probe).free


def _existing_ids(catalog: LanceCatalog) -> set[str]:
    table = catalog.open_table("raw_volumes", raw_volumes_schema())
    if table.count_rows() == 0:
        return set()
    return set(table.to_lance().to_table(columns=["volume_id"]).column(0).to_pylist())


def _rows_table(rows: list[dict]) -> pa.Table:
    schema = raw_volumes_schema()
    arrays = []
    for field in schema:
        values = [row[field.name] for row in rows]
        if field.name == "raw_bytes":
            arrays.append(blob_array(values))
        else:
            arrays.append(pa.array(values, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def write_volumes(
    dest,
    manifest: pd.DataFrame,
    *,
    open_reader,
    kind: str = "iq",
    upsample: bool = False,
    codec: str = "raw",
    batch_rows: int = 8,
    free_bytes=_free_bytes,
    log_path: Path | None = None,
    make_prefetcher=_NopPrefetch,
    show_progress: bool = False,
) -> dict:
    """Insert missing volumes. Returns inserted count and stopped flag."""
    if codec != "raw":
        raise ValueError("pilot stores uncompressed codec 'raw'")
    if kind not in ("iq", "rf"):
        raise ValueError(f"kind must be 'rf' or 'iq', got {kind!r}")

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    catalog = LanceCatalog(
        str(dest),
        namespace=tuple(NAMESPACE),
        storage_options=STORAGE,
        create_missing=True,
    )
    table = catalog.open_table("raw_volumes", raw_volumes_schema())
    have = _existing_ids(catalog)
    todo = manifest.loc[~manifest["volume_id"].astype(str).isin(have)].copy()
    pending: list[dict] = []
    inserted = 0
    bar = Progress(len(todo), "raw") if show_progress and len(todo) else None

    def flush() -> None:
        nonlocal inserted, pending
        if not pending:
            return
        catalog.insert_only(
            table,
            _rows_table(pending),
            on=("volume_id",),
            schema=raw_volumes_schema(),
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as handle:
                for row in pending:
                    handle.write(
                        json.dumps(
                            {
                                "volume_id": row["volume_id"],
                                "seconds": row["seconds"],
                                "bytes": row["bytes"],
                            }
                        )
                        + "\n"
                    )
        inserted += len(pending)
        pending = []

    stopped = False
    for (_, _), group in todo.groupby(["inspection_id", "clip_id"], sort=False):
        windows = group.sort_values("frame_start")
        kept = []
        for _, row in windows.iterrows():
            if free_bytes(dest) < MIN_FREE:
                stopped = True
                break
            kept.append(row)
        if not kept:
            break
        reader = open_reader(str(kept[0]["inspection_id"]), str(kept[0]["clip_id"]))
        decimation = int(reader.get_iq_decimation() if kind == "iq" else reader.get_rf_decimation())
        prefetcher = make_prefetcher()
        for row in kept:
            reader.queue_frames(int(row["frame_start"]), int(row["n_frames"]), prefetcher)
        prefetcher.launch()
        prefetcher.wait()
        for row in kept:
            started = time.perf_counter()
            frames = np.ascontiguousarray(
                read_raw_window(
                    reader,
                    int(row["frame_start"]),
                    int(row["n_frames"]),
                    kind=kind,
                    upsample=upsample,
                )
            )
            payload = frames.tobytes()
            pending.append(
                {
                    "volume_id": str(row["volume_id"]),
                    "inspection_id": str(row["inspection_id"]),
                    "clip_id": str(row["clip_id"]),
                    "frame_start": int(row["frame_start"]),
                    "n_frames": int(row["n_frames"]),
                    "raw_kind": kind,
                    "upsample": bool(upsample),
                    "shape": [int(x) for x in frames.shape],
                    "dtype": np.dtype(frames.dtype).name,
                    "codec": codec,
                    "decimation": decimation,
                    "content_sha256": hashlib.sha256(payload).hexdigest(),
                    "created_at": datetime.now(timezone.utc),
                    "raw_bytes": payload,
                    "seconds": round(time.perf_counter() - started, 3),
                    "bytes": len(payload),
                }
            )
            if len(pending) >= batch_rows:
                flush()
            if bar is not None:
                bar.advance(1, f"{int(row['n_frames'])} fr")
        prefetcher.clear()
        if stopped:
            break
    flush()
    return {"inserted": inserted, "stopped": "low_disk" if stopped else False}


def verify_stored(dest, volume_ids: list[str]) -> list[str]:
    """Return volume ids whose stored blob hash differs from content_sha256."""
    catalog = LanceCatalog(str(dest), namespace=tuple(NAMESPACE), storage_options=STORAGE)
    table = catalog.open_table("raw_volumes", raw_volumes_schema())
    ds = table.to_lance()
    bad = []
    for volume_id in volume_ids:
        rows = ds.to_table(
            columns=["volume_id", "content_sha256"],
            filter=f"volume_id = '{volume_id}'",
            with_row_address=True,
        ).to_pylist()
        if len(rows) != 1:
            bad.append(volume_id)
            continue
        blob = ds.take_blobs("raw_bytes", addresses=[int(rows[0]["_rowaddr"])])[0]
        digest = hashlib.sha256(blob.read()).hexdigest()
        if digest != rows[0]["content_sha256"]:
            bad.append(volume_id)
    return bad


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--tier", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0, help="Shortest N volumes only")
    parser.add_argument("--dest", type=Path, default=ROOT / "work" / "catalog_raw_test")
    parser.add_argument("--manifest", type=Path, default=ROOT / "artifacts" / "manifest.parquet")
    parser.add_argument("--kind", choices=("iq", "rf"), default="iq")
    parser.add_argument("--upsample", action="store_true")
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--log", type=Path, default=ROOT / "work" / "write_raw_log.jsonl")
    parser.add_argument("--skip-hash", action="store_true", help="Leave the full hash check to the validator")
    args = parser.parse_args()
    if args.pilot and args.tier is not None:
        parser.error("pass only one of --pilot and --tier")
    if not args.pilot and args.tier is None:
        parser.error("pass --pilot or --tier N")

    manifest = slice_manifest(pd.read_parquet(args.manifest), pilot=args.pilot, tier=args.tier)
    if args.limit:
        manifest = manifest.sort_values(["n_frames", "volume_id"]).head(args.limit)
    print(f"selected {len(manifest)} volumes -> {args.dest}", flush=True)

    from ilipy import ChunkPrefetcher, Session
    from ilipy.channeldata import ChannelDataReader, ImageProfile

    session = Session(environment=args.environment)
    session.set_max_concurrent_http_requests(HTTP_REQUESTS)
    session.set_cache_sizes(CENTRAL_CACHE, HTTP_CACHE)
    active: dict[str, str | None] = {"id": None}

    def open_reader(inspection_id: str, clip_id: str):
        if active["id"] != inspection_id:
            session.set_active_inspection(inspection_id)
            active["id"] = inspection_id
        clip = session.get_clip_by_id(clip_id)
        return ChannelDataReader(session, clip, ImageProfile.ZeroAngle)

    report = write_volumes(
        args.dest,
        manifest,
        open_reader=open_reader,
        kind=args.kind,
        upsample=args.upsample,
        log_path=args.log,
        make_prefetcher=ChunkPrefetcher,
        show_progress=True,
    )
    print(json.dumps(report), flush=True)
    if args.skip_hash:
        return
    ids = list(manifest["volume_id"].astype(str))
    bad = verify_stored(args.dest, ids)
    if bad:
        raise SystemExit(f"hash mismatch: {bad[:5]}")
    print(f"hash ok for {len(ids)} volumes", flush=True)


if __name__ == "__main__":
    main()
