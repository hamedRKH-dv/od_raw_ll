"""Stage 0 probes: disk, one source row, a one-row S3 table, a 1 GiB local write, one clip per inspection."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import pyarrow as pa

from select_paired_subset import DEFAULT_CATALOG, NAMESPACE

DEST = "s3://ili-ml-datasets/lancedb/research/hamed/od_raw_ll"
INSPECTIONS = ("0A1OLRV96N3", "0A49KLT3B7Y", "0ABP0TFUSH1", "0AGBXB4WEGN", "0AP3OOMEPID")
ROOT = Path(__file__).resolve().parents[1]


def _gib(path: Path) -> dict:
    target = path / "probe.bin"
    payload = b"\0" * (1024 * 1024)
    started = time.perf_counter()
    with target.open("wb") as handle:
        for _ in range(1024):
            handle.write(payload)
    target.unlink()
    seconds = time.perf_counter() - started
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "free_gib": round(usage.free / 1024**3, 1),
        "write_mib_s": round(1024 / seconds, 1),
    }


def _s3(dest: str) -> dict:
    import lancedb

    connection = lancedb.connect(dest)
    name = "_probe"
    table = connection.create_table(
        name,
        pa.table({"probe_id": ["ping"]}),
        namespace_path=NAMESPACE,
        mode="overwrite",
    )
    got = table.to_arrow().column("probe_id")[0].as_py()
    connection.drop_table(name, namespace_path=NAMESPACE)
    if got != "ping":
        raise RuntimeError(f"probe read back {got}")
    return {"dest": dest, "round_trip": True}


def _source(catalog: str) -> dict:
    import lancedb

    connection = lancedb.connect(catalog)
    table = connection.open_table("dataset", namespace_path=NAMESPACE)
    return {"catalog": catalog, "dataset_rows": table.count_rows()}


def _clips(environment: str) -> dict:
    from ilipy import ClipTypes, Session

    session = Session(environment=environment)
    opened = []
    for inspection in INSPECTIONS:
        session.set_active_inspection(inspection)
        clip = session.get_clips_by_type(ClipTypes.ChannelData)[0]
        opened.append({"inspection_id": inspection, "clip_id": clip.clip_id})
    return {"opened": opened}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT / "work")
    parser.add_argument("--catalog-root", default=DEFAULT_CATALOG)
    parser.add_argument("--dest", default=DEST)
    parser.add_argument("--environment", default="prod")
    parser.add_argument("--skip-clips", action="store_true")
    parser.add_argument("--skip-s3", action="store_true")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    report = {"disk": _gib(args.work), "source": _source(args.catalog_root)}
    if not args.skip_s3:
        report["s3"] = _s3(args.dest)
    if not args.skip_clips:
        report["clips"] = _clips(args.environment)
    path = args.work / "probe_access.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["disk"]["free_gib"] < 150:
        raise SystemExit("free space is under 150 GiB")


if __name__ == "__main__":
    main()
