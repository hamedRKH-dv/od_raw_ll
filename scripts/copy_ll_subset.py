"""Copy LL volumes (+ dataset/annotations/subset) for a manifest slice.

Stage 4. Source is read-only. Default dest is a removable work catalog for
smoke tests; pass --dest catalog for the real build.

`all_binary` fails on this lance/catalog combo (ArrowInvalid on blob-v2), so
volumes are read via take_blobs + materialize_blob_batch — same path as
anomaly_sampling/lance_writer.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pandas as pd
import pyarrow as pa
from ilipyutils.lance.store import (
    DEFAULT_NEW_TABLE_STORAGE_OPTIONS,
    LanceCatalog,
    schemas_equal,
)
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parents[1] / "ILIWorkflows" / "workflows" / "od_corrosion_detection"
sys.path.insert(0, str(WORKFLOW))
sys.path.insert(0, str(ROOT / "scripts"))

from od_corrosion_detection.catalog_schemas import (  # noqa: E402
    annotations_schema,
    volumes_schema,
)
from od_corrosion_detection.lance_ingest.promote import materialize_blob_batch  # noqa: E402
from progress import Progress  # noqa: E402
from select_paired_subset import DEFAULT_CATALOG, NAMESPACE, slice_manifest  # noqa: E402

STORAGE = {**DEFAULT_NEW_TABLE_STORAGE_OPTIONS, "aws_region": "us-west-2"}
BLOB_COLS = frozenset({"frame_bytes", "lossless_frame_bytes"})
TABLES = ("volumes", "dataset", "annotations")


def subset_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("volume_id", pa.string()),
            pa.field("tier", pa.int32()),
            pa.field("pilot", pa.bool_()),
            pa.field("group", pa.string()),
            pa.field("depth_bin", pa.string()),
            pa.field("sample_weight", pa.float64()),
        ]
    )


def _id_filter(ids: Sequence[str], column: str = "volume_id") -> str:
    if not ids:
        raise ValueError("empty id list")
    return " OR ".join(f"{column} = '{i}'" for i in ids)


def _chunks(ids: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(ids), size):
        yield list(ids[i : i + size])


def _existing_ids(table, key: str) -> set[str]:
    if table.count_rows() == 0:
        return set()
    return set(table.to_lance().to_table(columns=[key]).column(key).to_pylist())


def _open_source(root: str) -> LanceCatalog:
    # open_table validates schema; we open raw then wrap with live schemas.
    return LanceCatalog(root, namespace=tuple(NAMESPACE), storage_options=STORAGE)


def _source_table(catalog: LanceCatalog, name: str):
    import lancedb

    conn = lancedb.connect(catalog.root, storage_options=STORAGE)
    return conn.open_table(name, namespace_path=list(NAMESPACE))


def copy_ll_rows(
    source_root: str,
    dest_root: str,
    manifest: pd.DataFrame,
    *,
    batch_rows: int = 2,
    show_progress: bool = False,
) -> dict[str, int]:
    """Copy manifest volumes into dest. Skips ids already present. Returns counts inserted."""
    if manifest.empty:
        return {t: 0 for t in (*TABLES, "subset")}

    volume_ids = list(dict.fromkeys(manifest["volume_id"].astype(str)))
    source = _open_source(source_root)
    dest = LanceCatalog(
        dest_root,
        namespace=tuple(NAMESPACE),
        storage_options=STORAGE,
        create_missing=True,
    )

    # volumes/annotations match catalog_schemas; dataset has extra columns on source.
    src_tables = {name: _source_table(source, name) for name in TABLES}
    vol_schema = volumes_schema()
    ann_schema = annotations_schema()
    dataset_schema_live = src_tables["dataset"].schema
    schemas = {
        "volumes": vol_schema,
        "annotations": ann_schema,
        "dataset": dataset_schema_live,
    }
    dest_tables = {
        "volumes": dest.open_table("volumes", vol_schema),
        "annotations": dest.open_table("annotations", ann_schema),
        "dataset": dest.open_table("dataset", dataset_schema_live),
    }
    dest_subset = dest.open_table("subset", subset_schema())

    inserted = {name: 0 for name in (*TABLES, "subset")}

    # --- volumes via take_blobs ---
    have = _existing_ids(dest_tables["volumes"], "volume_id")
    need = [v for v in volume_ids if v not in have]
    if need:
        meta_cols = [f.name for f in vol_schema if f.name not in BLOB_COLS]
        ds = src_tables["volumes"].to_lance()
        rows_by_id: dict[str, dict] = {}
        addr_by_id: dict[str, int] = {}
        for chunk in _chunks(need, 64):
            filt = _id_filter(chunk)
            for batch in ds.scanner(
                columns=meta_cols,
                filter=filt,
                with_row_address=True,
            ).to_batches():
                metas = batch.select(meta_cols).to_pylist()
                addrs = batch.column("_rowaddr").to_pylist()
                for row, addr in zip(metas, addrs, strict=True):
                    rows_by_id[str(row["volume_id"])] = row
                    addr_by_id[str(row["volume_id"])] = int(addr)
        missing = [v for v in need if v not in rows_by_id]
        if missing:
            raise RuntimeError(f"source missing {len(missing)} volumes, e.g. {missing[0]}")

        bar = Progress(len(need), "ll") if show_progress else None
        for chunk in _chunks(need, batch_rows):
            addresses = [addr_by_id[v] for v in chunk]
            arrays: list[pa.Array] = []
            fields: list[pa.Field] = []
            for field in vol_schema:
                if field.name in BLOB_COLS:
                    payloads = [
                        blob.read() if blob is not None else None
                        for blob in ds.take_blobs(field.name, addresses=addresses)
                    ]
                    arrays.append(pa.array(payloads, type=pa.binary()))
                    fields.append(pa.field(field.name, pa.binary(), nullable=field.nullable))
                else:
                    arrays.append(
                        pa.array(
                            [rows_by_id[v][field.name] for v in chunk],
                            type=field.type,
                        )
                    )
                    fields.append(field)
            binary = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))
            owned = materialize_blob_batch(binary, vol_schema)
            dest.insert_only(
                dest_tables["volumes"],
                pa.Table.from_batches([owned]),
                on=("volume_id",),
                schema=vol_schema,
            )
            inserted["volumes"] += len(chunk)
            if bar is not None:
                bar.advance(len(chunk))

    # --- dataset + annotations (no blobs) ---
    for name, key in (("dataset", "sample_id"), ("annotations", "sample_id")):
        have = _existing_ids(dest_tables[name], key) if dest_tables[name].count_rows() else set()
        for chunk in _chunks(volume_ids, 64):
            filt = _id_filter(chunk)
            table = src_tables[name].to_lance().to_table(filter=filt)
            if table.num_rows == 0:
                continue
            if name == "annotations":
                # Drop columns so insert matches catalog_schemas (source already matches).
                table = table.select(ann_schema.names)
            if have:
                keep = [i for i, sid in enumerate(table.column(key).to_pylist()) if sid not in have]
                if not keep:
                    continue
                table = table.take(keep)
            dest.insert_only(
                dest_tables[name],
                table,
                on=(key,),
                schema=schemas[name],
            )
            inserted[name] += table.num_rows
            have.update(table.column(key).to_pylist())

    # --- subset metadata ---
    have = _existing_ids(dest_subset, "volume_id")
    sub = manifest.loc[
        ~manifest["volume_id"].astype(str).isin(have),
        ["volume_id", "tier", "pilot", "group", "depth_bin", "sample_weight"],
    ].drop_duplicates("volume_id")
    if len(sub):
        rows = sub.astype({"tier": "int32", "pilot": "bool", "sample_weight": "float64"}).to_dict(
            "records"
        )
        dest.insert_only(dest_subset, rows, on=("volume_id",), schema=subset_schema())
        inserted["subset"] = len(rows)

    return inserted


def verify_pilot(
    source_root: str,
    dest_root: str,
    manifest: pd.DataFrame,
    *,
    sample: int = 2,
) -> dict:
    """Byte-hash frame_bytes (and lossless when present) for a few pilot volumes."""
    source = _open_source(source_root)
    dest_cat = LanceCatalog(dest_root, namespace=tuple(NAMESPACE), storage_options=STORAGE)
    schema = volumes_schema()
    src_v = _source_table(source, "volumes")
    dst_table = dest_cat.open_table("volumes", schema)
    if not schemas_equal(dst_table.schema, schema):
        raise RuntimeError("dest volumes schema != volumes_schema()")

    ids = list(manifest["volume_id"].astype(str)[:sample])
    report = {"checked": [], "volumes_dest": dst_table.count_rows(), "schemas_equal": True}

    def payload_hash(ds, vid: str) -> dict[str, str | None]:
        meta = ds.to_table(
            columns=["volume_id"],
            filter=f"volume_id = '{vid}'",
            with_row_address=True,
            blob_handling="blobs_descriptions",
        ).to_pylist()
        if len(meta) != 1:
            raise RuntimeError(f"{vid}: {len(meta)} rows")
        addr = int(meta[0]["_rowaddr"])
        out: dict[str, str | None] = {}
        for col in ("frame_bytes", "lossless_frame_bytes"):
            blob = ds.take_blobs(col, addresses=[addr])[0]
            data = blob.read() if blob is not None else None
            out[col] = None if data is None else hashlib.sha256(data).hexdigest()
        return out

    src_ds, dst_ds = src_v.to_lance(), dst_table.to_lance()
    for vid in ids:
        s, d = payload_hash(src_ds, vid), payload_hash(dst_ds, vid)
        if s != d:
            raise RuntimeError(f"hash mismatch {vid}: {s} vs {d}")
        report["checked"].append({"volume_id": vid, **s})
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=DEFAULT_CATALOG)
    p.add_argument(
        "--dest",
        type=Path,
        default=ROOT / "work" / "catalog_ll_test",
        help="Removable default under work/; use catalog/ for the real Stage 4 dest",
    )
    p.add_argument("--manifest", type=Path, default=ROOT / "artifacts" / "manifest.parquet")
    p.add_argument("--pilot", action="store_true", help="Restrict to pilot=True rows")
    p.add_argument("--tier", type=int, default=None, help="Volumes with tier <= N, including eval")
    p.add_argument("--limit", type=int, default=0, help="Cap volumes (0 = all selected)")
    p.add_argument("--batch-rows", type=int, default=2)
    p.add_argument("--skip-verify", action="store_true")
    p.add_argument("--once", action="store_true", help="Do not repeat the copy to prove resume")
    args = p.parse_args()

    if args.pilot and args.tier is not None:
        p.error("pass only one of --pilot and --tier")
    manifest = slice_manifest(pd.read_parquet(args.manifest), pilot=args.pilot, tier=args.tier)
    if args.limit:
        manifest = manifest.head(args.limit).copy()
    print(
        f"copying {len(manifest)} volumes -> {args.dest} (pilot={args.pilot})",
        flush=True,
    )
    counts = copy_ll_rows(
        args.source,
        str(args.dest),
        manifest,
        batch_rows=args.batch_rows,
        show_progress=True,
    )
    print(json.dumps({"inserted": counts}, indent=2), flush=True)

    if not args.once:
        again = copy_ll_rows(args.source, str(args.dest), manifest, batch_rows=args.batch_rows)
        print(json.dumps({"second_run": again}, indent=2), flush=True)
        if any(again.values()):
            raise SystemExit(f"second run inserted rows: {again}")

    if not args.skip_verify:
        report = verify_pilot(args.source, str(args.dest), manifest, sample=min(2, len(manifest)))
        print(json.dumps({"verify": report}, indent=2), flush=True)
        out = ROOT / "work" / "ll_copy_test.json"
        out.write_text(json.dumps({"inserted": counts, "verify": report}, indent=2) + "\n")
        print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
