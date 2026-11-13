"""Stratified manifest for the raw + LL paired OD subset.

Rules: plans/subset_sampler.plan.md. Metadata only; frame blobs are never read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

NAMESPACE = ["kraken", "od_corrosion"]
DEFAULT_CATALOG = (
    "s3://ili-ml-datasets/lancedb/research/mahsa/kraken/od_corrosion/ll/merged_7neg_8_10_11_13"
)
GROUPS = ("positive", "confuser", "background")
BACKGROUND = {"nominal", "random"}
COLUMNS = [
    "volume_id",
    "sample_ids",
    "inspection_id",
    "clip_id",
    "frame_start",
    "n_frames",
    "split",
    "anomaly_type",
    "group",
    "depth_bin",
    "cell_size",
    "cell_take",
    "sample_weight",
    "tier",
    "pilot",
]
ROOT = Path(__file__).resolve().parents[1]


def group_of(anomaly_type: str) -> str:
    if anomaly_type == "od_corrosion":
        return "positive"
    if anomaly_type in BACKGROUND:
        return "background"
    return "confuser"


def depth_bin(value: float) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if value <= 0.8:
        return "(0.0, 0.8]"
    if value <= 1.0:
        return "(0.8, 1.0]"
    if value <= 1.3:
        return "(1.0, 1.3]"
    if value <= 2.0:
        return "(1.3, 2.0]"
    return "(2.0, 10.0]"


def rank_of(volume_id: str, seed: int) -> int:
    digest = hashlib.sha1(f"{seed}:{volume_id}".encode()).hexdigest()
    return int(digest[:12], 16)


def collapse_volumes(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per volume. Mixed class or split on one volume is an error."""
    counts = rows.groupby("volume_id")[["split", "anomaly_type"]].nunique()
    mixed = counts[(counts > 1).any(axis=1)]
    if not mixed.empty:
        raise RuntimeError(f"volume has mixed split or class: {list(mixed.index[:5])}")
    frame = rows.copy()
    frame["depth_mm"] = pd.to_numeric(frame["depth_mm"], errors="coerce")
    base = (
        frame.groupby("volume_id", sort=False)
        .agg(
            inspection_id=("inspection_id", "first"),
            clip_id=("clip_id", "first"),
            frame_start=("frame_start", "first"),
            n_frames=("n_frames", "first"),
            split=("split", "first"),
            anomaly_type=("anomaly_type", "first"),
            depth_mm=("depth_mm", "max"),
            has_lossless=("has_lossless", "max"),
        )
        .reset_index()
    )
    sample_ids = frame.groupby("volume_id", sort=False)["sample_id"].agg(
        lambda values: sorted(set(values))
    )
    return base.merge(sample_ids.rename("sample_ids"), on="volume_id")


def allocate(cell_sizes: dict, budget: int, floor: int = 50) -> dict:
    """Give each cell min(size, floor), then share the rest by sqrt(size)."""
    sizes = dict(cell_sizes)
    if budget >= sum(sizes.values()):
        return sizes
    floors = {key: min(size, floor) for key, size in sizes.items()}
    if sum(floors.values()) > budget:
        raise RuntimeError(f"floor {floor} exceeds budget {budget} across {len(sizes)} cells")
    alloc = dict(floors)
    left = budget - sum(alloc.values())
    while left > 0:
        open_keys = sorted((key for key in sizes if alloc[key] < sizes[key]), key=str)
        if not open_keys:
            break
        weights = {key: math.sqrt(sizes[key]) for key in open_keys}
        total = sum(weights.values())
        used = 0
        for key in open_keys:
            add = min(sizes[key] - alloc[key], int(left * weights[key] / total))
            alloc[key] += add
            used += add
        left -= used
        if used == 0:
            for key in sorted(open_keys, key=lambda key: (-sizes[key], str(key))):
                if left == 0:
                    break
                if alloc[key] < sizes[key]:
                    alloc[key] += 1
                    left -= 1
            break
    placed = sum(alloc.values())
    if placed != budget:
        raise RuntimeError(f"allocated {placed} of budget {budget}")
    return alloc


def group_budgets(budget: int, mix: tuple[float, float, float]) -> dict[str, int]:
    raw = [int(round(budget * part)) for part in mix]
    raw[-1] += budget - sum(raw)
    if min(raw) < 0:
        raise RuntimeError(f"mix {mix} does not sum to 1")
    return dict(zip(GROUPS, raw))


def order_cell(cell: pd.DataFrame, seed: int) -> pd.DataFrame:
    ordered = cell.copy()
    if "_rank" not in ordered:
        ordered["_rank"] = ordered.volume_id.map(lambda volume_id: rank_of(volume_id, seed))
    keys = ["clip_id", "depth_bin"] if ordered.group.iloc[0] == "positive" else ["clip_id"]
    ordered = ordered.sort_values("_rank", kind="mergesort")
    ordered["_rr"] = ordered.groupby(keys, sort=False).cumcount()
    return ordered.sort_values(["_rr", "_rank"], kind="mergesort")


def _cells(pool: pd.DataFrame, group: str, seed: int, *, lossless_only: bool) -> tuple[dict, dict, dict]:
    chosen: dict = {}
    eligible_sizes: dict = {}
    full_sizes: dict = {}
    sub = pool[pool.group == group]
    for key, cell in sub.groupby(["inspection_id", "anomaly_type"], sort=True):
        ordered = order_cell(cell, seed)
        eligible = ordered[ordered.has_lossless] if lossless_only else ordered
        if eligible.empty:
            continue
        chosen[key] = eligible
        eligible_sizes[key] = len(eligible)
        full_sizes[key] = len(cell)
    return chosen, eligible_sizes, full_sizes


def _materialize(cells: dict, takes: dict, full_sizes: dict, tier: int | None) -> pd.DataFrame:
    parts = []
    for key, eligible in cells.items():
        taken = takes.get(key, 0)
        if taken <= 0:
            continue
        part = eligible.head(taken).copy()
        part["cell_size"] = full_sizes[key]
        part["cell_take"] = taken
        part["sample_weight"] = full_sizes[key] / taken
        if tier is not None:
            part["tier"] = tier
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _select_train(pool: pd.DataFrame, tiers: tuple[int, ...], mix, seed: int, floor: int) -> pd.DataFrame:
    parts = []
    for group in GROUPS:
        cells, sizes, full_sizes = _cells(pool, group, seed, lossless_only=False)
        previous = {key: 0 for key in sizes}
        takes_at = {}
        for tier in tiers:
            budget = group_budgets(tier, mix)[group]
            takes = allocate(sizes, budget, floor) if sizes else {}
            for key, taken in takes.items():
                if taken < previous[key]:
                    raise RuntimeError(f"tier take shrank for {key}")
            previous = {key: takes.get(key, 0) for key in sizes}
            takes_at[tier] = takes
        largest = takes_at[tiers[-1]]
        chosen_rows = []
        for key, eligible in cells.items():
            taken = largest.get(key, 0)
            if taken <= 0:
                continue
            part = eligible.head(taken).copy()
            part["cell_size"] = full_sizes[key]
            part["cell_take"] = taken
            part["sample_weight"] = full_sizes[key] / taken
            entered = []
            for position in range(taken):
                entered.append(next(tier for tier in tiers if takes_at[tier][key] > position))
            part["tier"] = entered
            chosen_rows.append(part)
        if chosen_rows:
            parts.append(pd.concat(chosen_rows, ignore_index=True))
    if not parts:
        raise RuntimeError("train selection is empty")
    return pd.concat(parts, ignore_index=True)


def _select_eval(pool: pd.DataFrame, eval_size: int, tier: int, seed: int, floor: int) -> pd.DataFrame:
    parts = []
    held = 0
    for group in ("positive", "confuser"):
        cells, sizes, full_sizes = _cells(pool, group, seed, lossless_only=True)
        part = _materialize(cells, sizes, full_sizes, tier)
        if not part.empty:
            parts.append(part)
            held += len(part)
    background_budget = max(0, eval_size - held)
    cells, sizes, full_sizes = _cells(pool, "background", seed, lossless_only=True)
    if cells and background_budget:
        takes = allocate(sizes, background_budget, floor)
        part = _materialize(cells, takes, full_sizes, tier)
        if not part.empty:
            parts.append(part)
    if not parts:
        raise RuntimeError("eval selection is empty")
    return pd.concat(parts, ignore_index=True)


def _flag_pilot(manifest: pd.DataFrame, pilot_size: int, first_tier: int) -> pd.DataFrame:
    first = manifest[manifest.tier <= first_tier]
    positives = first[(first.split == "eval") & (first.group == "positive")].sort_values("_rank", kind="mergesort")
    chosen = list(positives.head(min(20, len(positives))).volume_id)
    rest = first[~first.volume_id.isin(chosen)].sort_values("_rank", kind="mergesort")
    rest = rest.copy()
    rest["_rr"] = rest.groupby(["inspection_id", "split", "group"], sort=False).cumcount()
    rest = rest.sort_values(["_rr", "_rank"], kind="mergesort")
    chosen.extend(rest.head(max(0, pilot_size - len(chosen))).volume_id)
    flagged = manifest.copy()
    flagged["pilot"] = flagged.volume_id.isin(set(chosen))
    return flagged


def _cross_split_overlaps(manifest: pd.DataFrame) -> int:
    import numpy as np

    overlaps = 0
    for _, group in manifest.groupby("clip_id", sort=False):
        train = group.loc[group.split == "train", ["frame_start", "n_frames"]].to_numpy()
        eval_rows = group.loc[group.split == "eval", ["frame_start", "n_frames"]].to_numpy()
        if len(train) == 0 or len(eval_rows) == 0:
            continue
        order = eval_rows[:, 0].argsort()
        eval_rows = eval_rows[order]
        ends = eval_rows[:, 0] + eval_rows[:, 1]
        for start, n_frames in train:
            right = int(np.searchsorted(eval_rows[:, 0], start + n_frames, side="left"))
            if right and (ends[:right] > start).any():
                overlaps += 1
    return overlaps


def check_manifest(manifest: pd.DataFrame, meta: pd.DataFrame, *, max_frames: int) -> None:
    problems = []
    if manifest.volume_id.duplicated().any():
        problems.append("duplicate volume_id")
    pool = meta[meta.n_frames <= max_frames]
    merged = manifest.merge(
        pool[["volume_id", "split", "has_lossless"]],
        on="volume_id",
        suffixes=("", "_src"),
    )
    if len(merged) != len(manifest):
        problems.append("manifest volume missing from the pool")
    elif not (merged.split == merged.split_src).all():
        problems.append("split does not match the source")
    elif not merged.loc[merged.split == "eval", "has_lossless"].all():
        problems.append("eval volume without lossless frames")
    missing_clips = set(pool.clip_id) - set(manifest.clip_id)
    if missing_clips:
        problems.append(f"{len(missing_clips)} pool clips missing from the manifest")
    if _cross_split_overlaps(manifest):
        problems.append("a window overlaps another window across train and eval")
    train = manifest[manifest.split == "train"]
    seen: set[str] = set()
    for tier in sorted(train.tier.unique()):
        ids = set(train.loc[train.tier <= tier, "volume_id"])
        if not seen <= ids:
            problems.append(f"tier {tier} is not nested")
        seen = ids
    first_tier = int(manifest.tier.min())
    pilot = manifest[manifest.pilot]
    if not set(pilot.volume_id) <= set(manifest.loc[manifest.tier <= first_tier, "volume_id"]):
        problems.append("pilot is outside the first fetch")
    if set(pilot.inspection_id) != set(manifest.inspection_id):
        problems.append("pilot misses an inspection")
    if set(pilot.split) != {"train", "eval"}:
        problems.append("pilot misses a split")
    if problems:
        raise RuntimeError("; ".join(problems))


def build_manifest(
    meta: pd.DataFrame,
    *,
    train_tiers: tuple[int, ...] = (4000, 8000, 12000, 20000),
    eval_size: int = 4000,
    mix: tuple[float, float, float] = (0.30, 0.35, 0.35),
    max_frames: int = 330,
    pilot_size: int = 500,
    seed: int = 20261112,
    floor: int = 50,
) -> pd.DataFrame:
    pool = meta[meta.n_frames <= max_frames].copy()
    pool["group"] = pool.anomaly_type.map(group_of)
    pool["depth_bin"] = pool.depth_mm.map(depth_bin)
    pool["_rank"] = pool.volume_id.map(lambda volume_id: rank_of(str(volume_id), seed))
    tiers = tuple(sorted(train_tiers))
    train = _select_train(pool[pool.split == "train"], tiers, mix, seed, floor)
    eval_rows = _select_eval(pool[pool.split == "eval"], eval_size, tiers[0], seed, floor)
    manifest = _flag_pilot(pd.concat([train, eval_rows], ignore_index=True), pilot_size, tiers[0])
    manifest = manifest.sort_values(
        ["inspection_id", "clip_id", "frame_start", "volume_id"], kind="mergesort"
    ).reset_index(drop=True)
    check_manifest(manifest, meta, max_frames=max_frames)
    return manifest[COLUMNS]


def _has_lossless(value) -> bool:
    try:
        return value is not None and len(value) > 0
    except TypeError:
        return False


def load_catalog_metadata(catalog_root: str) -> pd.DataFrame:
    import lancedb

    connection = lancedb.connect(catalog_root)

    def table(name: str):
        return connection.open_table(name, namespace_path=NAMESPACE).to_lance()

    dataset = table("dataset").to_table(
        columns=["sample_id", "volume_id", "split", "anomaly_type"]
    ).to_pandas()
    volumes = table("volumes").to_table(
        columns=[
            "volume_id",
            "inspection_id",
            "clip_id",
            "frame_start",
            "n_frames",
            "lossless_frame_lengths",
        ]
    ).to_pandas()
    annotations = table("annotations").to_table(columns=["sample_id", "depth_mm"]).to_pandas()
    volumes["has_lossless"] = volumes["lossless_frame_lengths"].map(_has_lossless)
    rows = dataset.merge(
        volumes.drop(columns=["lossless_frame_lengths"]),
        on="volume_id",
        how="left",
        indicator=True,
    )
    missing = int((rows["_merge"] != "both").sum())
    if missing:
        raise RuntimeError(f"{missing} dataset rows have no volume")
    rows = rows.drop(columns=["_merge"]).merge(annotations, on="sample_id", how="left")
    return collapse_volumes(rows)


def sample_windows(manifest: pd.DataFrame, seed: int, per_inspection: int = 4) -> list[dict]:
    pilot = manifest[manifest.pilot].copy()
    pilot["_rank"] = pilot.volume_id.map(lambda volume_id: rank_of(str(volume_id), seed))
    windows = []
    for inspection, group in pilot.groupby("inspection_id", sort=True):
        picked = group.sort_values("_rank", kind="mergesort").head(per_inspection)
        if len(picked) < per_inspection:
            raise RuntimeError(f"{inspection} has {len(picked)} pilot volumes, need {per_inspection}")
        for row in picked.itertuples(index=False):
            windows.append(
                {
                    "inspection_id": row.inspection_id,
                    "clip_id": row.clip_id,
                    "frame_start": int(row.frame_start),
                    "n_frames": int(row.n_frames),
                    "volume_id": row.volume_id,
                }
            )
    return windows


def selection_report(manifest: pd.DataFrame) -> dict:
    train = manifest[manifest.split == "train"]
    report = {
        "volumes": int(len(manifest)),
        "frames": int(manifest.n_frames.sum()),
        "train": int(len(train)),
        "eval": int((manifest.split == "eval").sum()),
        "pilot": int(manifest.pilot.sum()),
        "clips": int(manifest.clip_id.nunique()),
        "inspections": sorted(manifest.inspection_id.unique()),
        "by_group": {split: manifest[manifest.split == split].group.value_counts().to_dict() for split in ("train", "eval")},
        "by_tier": {
            str(int(tier)): int((train.tier <= tier).sum()) for tier in sorted(train.tier.unique())
        },
        "estimated_raw_gib_at_107kib": round(float(manifest.n_frames.sum()) * 107 / 1024**2, 1),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog-root", default=DEFAULT_CATALOG)
    parser.add_argument("--work", type=Path, default=ROOT / "work")
    parser.add_argument("--seed", type=int, default=20261112)
    args = parser.parse_args()
    meta = load_catalog_metadata(args.catalog_root)
    manifest = build_manifest(meta, seed=args.seed)
    args.work.mkdir(parents=True, exist_ok=True)
    manifest_path = args.work / "manifest.parquet"
    manifest.to_parquet(manifest_path, index=False)
    report = selection_report(manifest)
    (args.work / "selection_report.json").write_text(json.dumps(report, indent=2) + "\n")
    windows = sample_windows(manifest, args.seed)
    (args.work / "sample_windows.json").write_text(json.dumps(windows, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"wrote {manifest_path}")


if __name__ == "__main__":
    main()
