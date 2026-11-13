"""Stage 6 checks for the paired LL + raw pilot.

    cache/venv/bin/python scripts/validate_paired_catalog.py --scope pilot

Check 4 beamforms stored IQ. Inspections whose surface reproduces the stored
LL are checked. 0A1OLRV96N3 and 0A49KLT3B7Y were stored as float16, so the
comparison rounds through float16 first. 0ABP0TFUSH1 and 0AGBXB4WEGN are
reported and skipped: their surface still does not reproduce the stored wall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "ILIWorkflows" / "workflows" / "od_corrosion_detection"))
sys.path.insert(0, str(ROOT / "scripts"))
S3_DEST = "s3://ili-ml-datasets/lancedb/research/hamed/od_raw_ll"
PAIR_FIELDS = ("frame_start", "n_frames", "clip_id", "inspection_id")
P5_P999_LSB = 1.0
# Float16 windows in the P4 follow-up bottomed at 99.73% within 1 level.
FLOAT16_WITHIN_1 = 0.997
# How the stored LL was quantized, from the P4 follow-up.
REBEAMFORM = {
    "0AP3OOMEPID": "exact",
    "0A1OLRV96N3": "float16",
    "0A49KLT3B7Y": "float16",
}
SKIP_REBEAMFORM = {
    "0ABP0TFUSH1": "surface does not reproduce the stored wall",
    "0AGBXB4WEGN": "surface does not reproduce the stored wall",
}


def scope_frame(manifest: pd.DataFrame, scope: str) -> pd.DataFrame:
    if scope == "pilot":
        return manifest[manifest["pilot"]].reset_index(drop=True)
    if scope.startswith("tier:"):
        tier = int(scope.split(":", 1)[1])
        return manifest[manifest["tier"] <= tier].reset_index(drop=True)
    raise ValueError(f"scope must be pilot or tier:N, got {scope!r}")


def _eq(left, right) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def pair_problems(manifest: pd.DataFrame, ll: pd.DataFrame, raw: pd.DataFrame) -> list[str]:
    """Volumes in scope must have one LL row and one raw row with the same keys."""
    problems = []
    ll_by = {row.volume_id: row for row in ll.itertuples(index=False)}
    raw_by = {row.volume_id: row for row in raw.itertuples(index=False)}
    ll_counts = ll.groupby("volume_id").size()
    raw_counts = raw.groupby("volume_id").size()
    for row in manifest.itertuples(index=False):
        vid = row.volume_id
        for name, counts, table in (
            ("ll", ll_counts, ll_by),
            ("raw", raw_counts, raw_by),
        ):
            count = int(counts.get(vid, 0))
            if count != 1:
                problems.append(f"{vid} {name} rows {count}")
                continue
            got = table[vid]
            for field in PAIR_FIELDS:
                if not _eq(getattr(row, field), getattr(got, field)):
                    problems.append(f"{vid} {name} {field}")
    return problems


def blob_matches(payload: bytes, shape: list[int], dtype: str, digest: str) -> bool:
    if hashlib.sha256(payload).hexdigest() != digest:
        return False
    values = np.frombuffer(payload, dtype=np.dtype(dtype))
    return int(values.size) == int(np.prod(shape))


def _key_counts(frame: pd.DataFrame, keys: list[str]) -> dict:
    if frame.empty:
        return {}
    counted = frame.groupby(keys, dropna=False).size()
    return {key if isinstance(key, tuple) else (key,): int(value) for key, value in counted.items()}


def count_problems(manifest: pd.DataFrame, dest: pd.DataFrame) -> list[str]:
    """Per-split and per-cell (inspection x anomaly_type) volume counts."""
    problems = []
    if set(manifest["volume_id"]) != set(dest["volume_id"]):
        problems.append("volume id set differs")
    for name, keys in (
        ("split", ["split"]),
        ("cell", ["inspection_id", "anomaly_type"]),
    ):
        if _key_counts(manifest, keys) != _key_counts(dest, keys):
            problems.append(f"{name} counts differ")
    return problems


def _catalog(root: str):
    from ilipyutils.lance.store import DEFAULT_NEW_TABLE_STORAGE_OPTIONS, LanceCatalog
    from select_paired_subset import NAMESPACE

    return LanceCatalog(
        root,
        namespace=tuple(NAMESPACE),
        storage_options={**DEFAULT_NEW_TABLE_STORAGE_OPTIONS, "aws_region": "us-west-2"},
    )


def _table(catalog, name: str, schema: pa.Schema):
    return catalog.open_table(name, schema).to_lance()


def _meta(dataset, columns: list[str]) -> pd.DataFrame:
    return dataset.to_table(columns=columns).to_pandas()


def check_blobs(dataset, rows: pd.DataFrame) -> list[str]:
    from progress import Progress

    problems = []
    addresses = dataset.to_table(
        columns=["volume_id"],
        with_row_address=True,
    ).to_pandas()
    addr = dict(zip(addresses.volume_id, addresses._rowaddr))
    bar = Progress(len(rows), "hash")
    for row in rows.itertuples(index=False):
        blob = dataset.take_blobs("raw_bytes", addresses=[int(addr[row.volume_id])])[0]
        payload = b"" if blob is None else blob.read()
        if not blob_matches(payload, list(row.shape), row.dtype, row.content_sha256):
            problems.append(row.volume_id)
        bar.tick()
    return problems


def check_fresh(rows: pd.DataFrame, read_one) -> list[str]:
    """read_one(row) -> bytes of a fresh chunk-server read."""
    problems = []
    for row in rows.itertuples(index=False):
        fresh = read_one(row)
        if hashlib.sha256(fresh).hexdigest() != row.content_sha256:
            problems.append(row.volume_id)
    return problems


def _float16_volume(fresh: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    span = (vmax - vmin) or 1.0
    norm = np.clip((fresh.astype(np.float64) - vmin) / span, 0.0, 1.0)
    return (norm.astype(np.float16).astype(np.float64) * span + vmin).astype(np.float32)


def within_tolerance(decoded: np.ndarray, fresh: np.ndarray, vmin: float, vmax: float, mode: str) -> dict:
    from check_prerequisites import lsb_error, summarize

    compared = _float16_volume(fresh, vmin, vmax) if mode == "float16" else fresh
    summary = summarize(lsb_error(decoded, compared, vmin, vmax))
    if mode == "float16":
        summary["passed"] = summary["within_1_frac"] >= FLOAT16_WITHIN_1
    else:
        summary["passed"] = summary["p999_lsb"] <= P5_P999_LSB
    summary["mode"] = mode
    return summary


def project_from_log(log_path: Path, full: pd.DataFrame) -> dict:
    """Project tier cost from the pilot's seconds and bytes per frame."""
    if not log_path.exists():
        return {}
    log = pd.read_json(log_path, lines=True)
    if log.empty:
        return {}
    joined = full.merge(log, on="volume_id", how="inner")
    seconds_per_frame = float((joined.seconds / joined.n_frames).median())
    bytes_per_frame = float((joined.bytes / joined.n_frames).median())
    free = shutil.disk_usage(ROOT).free
    tiers = {}
    for tier in (4000, 8000, 12000, 20000):
        rows = full[full.tier <= tier]
        frames = int(rows.n_frames.sum())
        tiers[str(tier)] = {
            "volumes": int(len(rows)),
            "hours_one_reader": round(frames * seconds_per_frame / 3600, 2),
            "gib": round(frames * bytes_per_frame / 1024**3, 1),
        }
    return {
        "pilot_volumes_logged": int(len(log)),
        "median_seconds_per_volume": round(float(joined.seconds.median()), 3),
        "median_seconds_per_frame": round(seconds_per_frame, 4),
        "bytes_per_frame": round(bytes_per_frame, 1),
        "free_gib": round(free / 1024**3, 1),
        "tiers": tiers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="pilot")
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--rebeamform", type=int, default=5, help="Eval volumes per inspection")
    parser.add_argument("--dest", type=Path, default=ROOT / "catalog")
    parser.add_argument("--manifest", type=Path, default=ROOT / "artifacts" / "manifest.parquet")
    parser.add_argument("--log", type=Path, default=ROOT / "work" / "write_raw_pilot.jsonl")
    parser.add_argument("--s3-dest", default=S3_DEST)
    parser.add_argument("--skip-fresh", action="store_true")
    parser.add_argument("--skip-rebeamform", action="store_true")
    parser.add_argument("--skip-s3", action="store_true")
    args = parser.parse_args()

    from copy_ll_subset import subset_schema
    from od_corrosion_detection.catalog_schemas import volumes_schema
    from raw_schema import raw_volumes_schema

    manifest = scope_frame(pd.read_parquet(args.manifest), args.scope)
    catalog = _catalog(str(args.dest))
    ll = _meta(
        _table(catalog, "volumes", volumes_schema()),
        ["volume_id", "frame_start", "n_frames", "clip_id", "inspection_id", "vol_vmin", "vol_vmax"],
    )
    raw_ds = _table(catalog, "raw_volumes", raw_volumes_schema())
    raw = _meta(
        raw_ds,
        [
            "volume_id",
            "frame_start",
            "n_frames",
            "clip_id",
            "inspection_id",
            "raw_kind",
            "upsample",
            "shape",
            "dtype",
            "content_sha256",
        ],
    )
    subset = _meta(_table(catalog, "subset", subset_schema()), ["volume_id", "group"])
    dataset = catalog.connection.open_table("dataset", namespace_path=["kraken", "od_corrosion"])
    dataset_rows = dataset.to_arrow().select(["volume_id", "split", "anomaly_type"]).to_pandas()
    dest_volumes = (
        manifest[["volume_id", "inspection_id"]]
        .merge(dataset_rows, on="volume_id", how="left")
        .drop_duplicates("volume_id")
    )

    report: dict = {"scope": args.scope, "volumes": int(len(manifest))}
    pair = pair_problems(manifest, ll, raw)
    report["pairs"] = {"passed": not pair, "problems": pair[:20], "n_problems": len(pair)}
    print(f"pairs {'PASS' if not pair else 'FAIL'} {len(pair)}", flush=True)

    raw_in_scope = raw[raw.volume_id.isin(manifest.volume_id)]
    blob_bad = check_blobs(raw_ds, raw_in_scope)
    report["blobs"] = {"passed": not blob_bad, "problems": blob_bad[:20], "n_problems": len(blob_bad)}
    print(f"blobs {'PASS' if not blob_bad else 'FAIL'} {len(blob_bad)}", flush=True)

    counts = count_problems(
        manifest[["volume_id", "split", "inspection_id", "anomaly_type"]],
        dest_volumes,
    )
    report["counts"] = {"passed": not counts, "problems": counts}
    report["subset_rows"] = int(subset.volume_id.isin(manifest.volume_id).sum())
    print(f"counts {'PASS' if not counts else 'FAIL'} {counts}", flush=True)

    if not args.skip_fresh:
        pick = manifest.merge(raw_in_scope, on="volume_id", suffixes=("", "_raw"))
        chosen = pick.sample(n=min(args.sample, len(pick)), random_state=20261112)
        report["fresh"] = {"passed": False, "checked": int(len(chosen)), "problems": _fresh_problems(chosen)}
        report["fresh"]["passed"] = not report["fresh"]["problems"]
        print(f"fresh {'PASS' if report['fresh']['passed'] else 'FAIL'}", flush=True)

    if not args.skip_rebeamform:
        report["rebeamform"] = _rebeamform(catalog, manifest, ll, raw_ds, args.rebeamform)
        print(f"rebeamform {'PASS' if report['rebeamform']['passed'] else 'FAIL'}", flush=True)

    report["projection"] = project_from_log(args.log, pd.read_parquet(args.manifest))

    local_ok = report["pairs"]["passed"] and report["blobs"]["passed"] and report["counts"]["passed"]
    if not args.skip_fresh:
        local_ok = local_ok and report["fresh"]["passed"]
    if not args.skip_rebeamform:
        local_ok = local_ok and report["rebeamform"]["passed"]

    if local_ok and not args.skip_s3:
        report["s3"] = _s3_roundtrip(args.dest, args.s3_dest, manifest, args.sample)
        print(f"s3 {'PASS' if report['s3']['passed'] else 'FAIL'}", flush=True)
        local_ok = local_ok and report["s3"]["passed"]

    report["passed"] = bool(local_ok)
    out = ROOT / "work" / "validation_pilot.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"{'PASS' if report['passed'] else 'FAIL'} wrote {out}", flush=True)
    if not report["passed"]:
        raise SystemExit(1)


def _fresh_problems(chosen: pd.DataFrame) -> list[str]:
    from ilipy import Session
    from ilipy.channeldata import ChannelDataReader, ImageProfile

    from raw_reader import read_raw_window

    session = Session(environment="prod")
    session.set_max_concurrent_http_requests(16)
    session.set_cache_sizes(256 * 1024**2, 512 * 1024**2)
    readers: dict[tuple[str, str], object] = {}

    def read_one(row) -> bytes:
        key = (row.inspection_id, row.clip_id)
        if key not in readers:
            session.set_active_inspection(key[0])
            clip = session.get_clip_by_id(key[1])
            readers[key] = ChannelDataReader(session, clip, ImageProfile.ZeroAngle)
        frames = np.ascontiguousarray(
            read_raw_window(
                readers[key],
                int(row.frame_start),
                int(row.n_frames),
                kind=row.raw_kind,
                upsample=bool(row.upsample),
            )
        )
        return frames.tobytes()

    return check_fresh(chosen, read_one)


def _rebeamform(catalog, manifest, ll, raw_ds, per_inspection: int) -> dict:
    from ilipy import Session

    from check_prerequisites import SURFACES, beamform_ll, load_stored_lossless, open_clip, surface_circles

    session = Session(environment="prod")
    checked = {}
    failed = []
    for inspection, mode in REBEAMFORM.items():
        pool = manifest[(manifest.inspection_id == inspection) & (manifest.split == "eval")]
        pool = pool.sort_values("volume_id").head(per_inspection)
        results = []
        for row in pool.itertuples(index=False):
            meta = ll[ll.volume_id == row.volume_id].iloc[0]
            _, decoded = load_stored_lossless(catalog, row.volume_id)
            clip, beamformer, standoff, radius = open_clip(session, inspection, row.clip_id)
            surface = SURFACES[inspection][0]
            circles = surface_circles(
                inspection, row.clip_id, int(row.frame_start), int(row.n_frames), surface, standoff, radius
            )
            iq = _stored_iq(raw_ds, row.volume_id)
            fresh = beamform_ll(beamformer, circles, int(row.frame_start), int(row.n_frames), iq=iq)
            summary = within_tolerance(
                decoded, fresh, float(meta.vol_vmin), float(meta.vol_vmax), mode
            )
            summary["volume_id"] = row.volume_id
            results.append(summary)
            if not summary["passed"]:
                failed.append(row.volume_id)
            print(inspection, row.volume_id, summary["p999_lsb"], summary["passed"], flush=True)
        checked[inspection] = results
    return {
        "passed": not failed,
        "float16_within_1_min": FLOAT16_WITHIN_1,
        "exact_p999_lsb_max": P5_P999_LSB,
        "skipped": SKIP_REBEAMFORM,
        "checked": checked,
        "failed": failed,
    }


def _stored_iq(raw_ds, volume_id: str) -> np.ndarray:
    rows = raw_ds.to_table(
        columns=["volume_id", "shape", "dtype"],
        filter=f"volume_id = '{volume_id}'",
        with_row_address=True,
    ).to_pylist()
    row = rows[0]
    blob = raw_ds.take_blobs("raw_bytes", addresses=[int(row["_rowaddr"])])[0]
    return np.frombuffer(blob.read(), dtype=np.dtype(row["dtype"])).reshape(row["shape"])


def _s3_roundtrip(dest: Path, s3_dest: str, manifest: pd.DataFrame, sample: int) -> dict:
    sync_log = ROOT / "work" / "s3_sync.log"
    sync_log.parent.mkdir(parents=True, exist_ok=True)
    with sync_log.open("a") as handle:
        handle.write(f"\n# sync {dest} -> {s3_dest}\n")
        subprocess.run(
            ["aws", "s3", "sync", str(dest), s3_dest, "--region", "us-west-2"],
            check=True,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    catalog = _catalog(s3_dest)
    from od_corrosion_detection.catalog_schemas import volumes_schema
    from raw_schema import raw_volumes_schema

    ll = _meta(
        _table(catalog, "volumes", volumes_schema()),
        ["volume_id", "frame_start", "n_frames", "clip_id", "inspection_id"],
    )
    raw_ds = _table(catalog, "raw_volumes", raw_volumes_schema())
    raw = _meta(
        raw_ds,
        ["volume_id", "frame_start", "n_frames", "clip_id", "inspection_id", "shape", "dtype", "content_sha256"],
    )
    pick = manifest.sample(n=min(sample, len(manifest)), random_state=20261112)
    pair = pair_problems(pick, ll, raw)
    blobs = check_blobs(raw_ds, raw[raw.volume_id.isin(pick.volume_id)])
    return {"passed": not pair and not blobs, "pair_problems": pair[:10], "blob_problems": blobs[:10]}


if __name__ == "__main__":
    main()
