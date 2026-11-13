"""Read-only census of the mahsa OD corrosion Lance catalog.

Reads metadata columns only (never the packed frame blobs) and prints the
numbers the raw + LL data-prep estimate needs: sample counts, frame counts,
window lengths, per-inspection and per-split breakdowns, and stored bytes.

Run:
    uv run --python 3.12 --with lancedb==0.40.0 --with pylance==10.0.0 \
        --with "pyarrow>=17,<18" --with pandas \
        python scripts/catalog_census.py
"""

from __future__ import annotations

import argparse
import json

import lancedb
import pandas as pd

OD_NAMESPACE = ["kraken", "od_corrosion"]
DEFAULT_CATALOG = (
    "s3://ili-ml-datasets/lancedb/research/mahsa/kraken/od_corrosion/ll/merged_7neg_8_10_11_13"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", default=DEFAULT_CATALOG)
    parser.add_argument("--json", default=None, help="Optional path for a JSON dump")
    parser.add_argument(
        "--windows-json",
        default=None,
        help="Optional path: dump 4 random windows per inspection for time_raw_window.py",
    )
    args = parser.parse_args()

    connection = lancedb.connect(args.catalog_root)

    def table(name: str):
        return connection.open_table(name, namespace_path=OD_NAMESPACE).to_lance()

    volumes = (
        table("volumes")
        .to_table(
            columns=[
                "volume_id",
                "inspection_id",
                "clip_id",
                "mode",
                "frame_start",
                "n_frames",
                "frame_h",
                "frame_w",
                "codec",
                "frame_lengths",
            ]
        )
        .to_pandas()
    )
    volumes["lossy_bytes"] = volumes["frame_lengths"].apply(
        lambda lengths: 0 if lengths is None else int(sum(lengths))
    )
    volumes = volumes.drop(columns=["frame_lengths"])
    dataset = table("dataset").to_table().to_pandas()
    annotations = (
        table("annotations").to_table(columns=["sample_id", "volume_id"]).to_pandas()
    )

    samples = dataset.merge(volumes, on="volume_id", how="left")
    samples["positive"] = samples["sample_id"].isin(annotations["sample_id"])

    pd.set_option("display.width", 220)
    print(f"catalog: {args.catalog_root}")
    print(
        f"volumes={len(volumes)} dataset={len(dataset)} annotations={len(annotations)}"
    )
    print(f"unique volumes referenced by dataset: {samples['volume_id'].nunique()}")
    print(f"total frames (volumes): {int(volumes['n_frames'].sum())}")
    print(f"lossy stored bytes: {int(volumes['lossy_bytes'].sum())}")
    print("\nn_frames per volume:")
    print(volumes["n_frames"].describe(percentiles=[0.1, 0.5, 0.9, 0.99]).round(1))
    print("\nmode / codec / frame size:")
    print(
        volumes.groupby(["mode", "codec", "frame_h", "frame_w"])
        .agg(volumes=("volume_id", "count"), frames=("n_frames", "sum"))
        .to_string()
    )
    per_inspection = volumes.groupby("inspection_id").agg(
        volumes=("volume_id", "count"),
        clips=("clip_id", "nunique"),
        frames=("n_frames", "sum"),
        mean_frames=("n_frames", "mean"),
        lossy_gb=("lossy_bytes", lambda s: s.sum() / 1e9),
    )
    print("\nper inspection:")
    print(per_inspection.round(2).to_string())
    print("\nsplit x positive:")
    print(
        samples.pivot_table(
            index="split",
            columns="positive",
            values="sample_id",
            aggfunc="count",
            fill_value=0,
            margins=True,
        ).to_string()
    )
    print("\nanomaly_type x split:")
    print(
        samples.pivot_table(
            index="anomaly_type",
            columns="split",
            values="sample_id",
            aggfunc="count",
            fill_value=0,
            margins=True,
        ).to_string()
    )

    if args.windows_json:
        sample = pd.concat(
            group.sample(n=4, random_state=7)
            for _, group in volumes.groupby("inspection_id")
        )[["inspection_id", "clip_id", "frame_start", "n_frames", "volume_id"]]
        with open(args.windows_json, "w") as handle:
            json.dump(sample.to_dict(orient="records"), handle, indent=2)
        print(f"\nwrote {len(sample)} windows to {args.windows_json}")

    if args.json:
        payload = {
            "catalog": args.catalog_root,
            "volumes": int(len(volumes)),
            "dataset": int(len(dataset)),
            "annotations": int(len(annotations)),
            "total_frames": int(volumes["n_frames"].sum()),
            "lossy_bytes": int(volumes["lossy_bytes"].sum()),
            "clips": int(volumes["clip_id"].nunique()),
            "n_frames_median": float(volumes["n_frames"].median()),
            "n_frames_mean": float(volumes["n_frames"].mean()),
            "n_frames_max": int(volumes["n_frames"].max()),
            "mode": sorted(volumes["mode"].unique().tolist()),
            "codec": sorted(volumes["codec"].unique().tolist()),
            "frame_hw": [
                [int(h), int(w)]
                for h, w in sorted(
                    {(int(h), int(w)) for h, w in zip(volumes["frame_h"], volumes["frame_w"])}
                )
            ],
            "positives": int(samples["positive"].sum()),
            "by_split": {
                split: {
                    "rows": int(len(group)),
                    "positives": int(group["positive"].sum()),
                }
                for split, group in samples.groupby("split")
            },
            "per_inspection": json.loads(per_inspection.to_json(orient="index")),
        }
        with open(args.json, "w") as handle:
            json.dump(payload, handle, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
