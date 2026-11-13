"""Sampler invariants on a fake catalog. No S3."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import select_paired_subset as sampler  # noqa: E402


def _rows(specs: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(specs)
    frame["depth_mm"] = frame["depth_mm"] if "depth_mm" in frame else float("nan")
    frame["has_lossless"] = frame["has_lossless"] if "has_lossless" in frame else True
    return frame


def _catalog() -> pd.DataFrame:
    rows = []
    for split, per_type in (("train", 80), ("eval", 12)):
        for inspection in ("I0", "I1"):
            for anomaly, depth in (
                ("od_corrosion", 2.4),
                ("lamination", None),
                ("nominal", None),
            ):
                n = per_type if split == "train" else (12 if anomaly != "nominal" else 30)
                for i in range(n):
                    rows.append(
                        {
                            "sample_id": f"{split}-{inspection}-{anomaly}-{i}",
                            "volume_id": f"v-{split}-{inspection}-{anomaly}-{i}",
                            "inspection_id": inspection,
                            "clip_id": f"{inspection}-c{i % 2}",
                            "frame_start": (0 if split == "train" else 1_000_000) + i * 400,
                            "n_frames": 80,
                            "split": split,
                            "anomaly_type": anomaly,
                            "depth_mm": None if depth is None else depth - (i % 5) * 0.4,
                            "has_lossless": not (
                                split == "eval" and anomaly == "od_corrosion" and inspection == "I0" and i == 0
                            ),
                        }
                    )
    rows.append(
        {
            "sample_id": "long",
            "volume_id": "v-long",
            "inspection_id": "I0",
            "clip_id": "I0-c0",
            "frame_start": 0,
            "n_frames": 400,
            "split": "train",
            "anomaly_type": "nominal",
            "depth_mm": None,
            "has_lossless": True,
        }
    )
    return sampler.collapse_volumes(_rows(rows))


class AllocateTest(unittest.TestCase):
    def test_sqrt_prefers_large_cells_without_giving_them_everything(self) -> None:
        sizes = {("a", "od"): 100, ("b", "od"): 10_000}
        alloc = sampler.allocate(sizes, 1_000, floor=50)
        self.assertEqual(sum(alloc.values()), 1_000)
        self.assertLess(alloc[("a", "od")], alloc[("b", "od")])
        self.assertGreater(alloc[("a", "od")], 50)
        self.assertLessEqual(alloc[("b", "od")], 10_000)

    def test_takes_are_monotonic_in_budget(self) -> None:
        sizes = {("a", "t"): 30, ("b", "t"): 200, ("c", "t"): 5_000}
        previous = {key: 0 for key in sizes}
        for budget in (100, 250, 800, 2_000):
            alloc = sampler.allocate(sizes, budget, floor=20)
            for key, taken in alloc.items():
                self.assertGreaterEqual(taken, previous[key])
                self.assertLessEqual(taken, sizes[key])
            previous = alloc


class CollapseTest(unittest.TestCase):
    def test_merges_same_class_and_keeps_max_depth(self) -> None:
        rows = _rows(
            [
                {
                    "sample_id": "s2",
                    "volume_id": "v",
                    "inspection_id": "I0",
                    "clip_id": "c",
                    "frame_start": 10,
                    "n_frames": 20,
                    "split": "train",
                    "anomaly_type": "od_corrosion",
                    "depth_mm": 0.4,
                    "has_lossless": True,
                },
                {
                    "sample_id": "s1",
                    "volume_id": "v",
                    "inspection_id": "I0",
                    "clip_id": "c",
                    "frame_start": 10,
                    "n_frames": 20,
                    "split": "train",
                    "anomaly_type": "od_corrosion",
                    "depth_mm": 2.5,
                    "has_lossless": True,
                },
            ]
        )
        collapsed = sampler.collapse_volumes(rows)
        self.assertEqual(list(collapsed.sample_ids)[0], ["s1", "s2"])
        self.assertEqual(collapsed.depth_mm.iloc[0], 2.5)

    def test_rejects_mixed_class_on_one_volume(self) -> None:
        rows = _rows(
            [
                {
                    "sample_id": "s1",
                    "volume_id": "v",
                    "inspection_id": "I0",
                    "clip_id": "c",
                    "frame_start": 0,
                    "n_frames": 10,
                    "split": "train",
                    "anomaly_type": "od_corrosion",
                    "depth_mm": 1.0,
                    "has_lossless": True,
                },
                {
                    "sample_id": "s2",
                    "volume_id": "v",
                    "inspection_id": "I0",
                    "clip_id": "c",
                    "frame_start": 0,
                    "n_frames": 10,
                    "split": "train",
                    "anomaly_type": "nominal",
                    "depth_mm": None,
                    "has_lossless": True,
                },
            ]
        )
        with self.assertRaises(RuntimeError):
            sampler.collapse_volumes(rows)


class ManifestTest(unittest.TestCase):
    def _build(self, meta: pd.DataFrame) -> pd.DataFrame:
        return sampler.build_manifest(
            meta,
            train_tiers=(200, 400),
            eval_size=80,
            mix=(0.30, 0.35, 0.35),
            max_frames=330,
            pilot_size=40,
            seed=20261112,
            floor=5,
        )

    def test_rules(self) -> None:
        meta = _catalog()
        shuffled = meta.sample(frac=1, random_state=1).reset_index(drop=True)
        first = self._build(meta)
        second = self._build(shuffled)
        pd.testing.assert_frame_equal(first.reset_index(drop=True), second.reset_index(drop=True))

        self.assertFalse(first.volume_id.duplicated().any())
        self.assertNotIn("v-long", set(first.volume_id))
        self.assertNotIn("v-eval-I0-od_corrosion-0", set(first.volume_id))
        self.assertIn("v-eval-I0-od_corrosion-1", set(first.volume_id))
        self.assertTrue((first.split == first.volume_id.map(lambda v: "eval" if "-eval-" in v else "train")).all())

        train = first[first.split == "train"]
        for tier in (200, 400):
            smaller = set(train.loc[train.tier <= tier, "volume_id"])
            self.assertTrue(smaller <= set(train.loc[train.tier <= 400, "volume_id"]))
        self.assertEqual(set(train.loc[train.tier <= 200, "volume_id"]) < set(train.volume_id), True)

        counts = train.group.value_counts().to_dict()
        self.assertEqual(counts, {"positive": 120, "confuser": 140, "background": 140})
        self.assertTrue((train.cell_take <= train.cell_size).all())
        self.assertTrue((first.loc[first.split == "eval", "tier"] == 200).all())
        self.assertTrue(set(first.loc[first.pilot, "volume_id"]) <= set(first.loc[first.tier <= 200, "volume_id"]))
        self.assertEqual(int(first.pilot.sum()), 40)
        self.assertEqual(set(first.loc[first.pilot, "inspection_id"]), {"I0", "I1"})
        self.assertEqual(set(first.loc[first.pilot, "split"]), {"train", "eval"})
        deep = first[(first.group == "positive") & (first.depth_bin == "(2.0, 10.0]")]
        self.assertFalse(deep.empty)
        sampler.check_manifest(first, meta, max_frames=330)

    def test_slice_is_pilot_or_cumulative_tier(self) -> None:
        meta = _catalog()
        manifest = self._build(meta)
        pilot = sampler.slice_manifest(manifest, pilot=True)
        first = sampler.slice_manifest(manifest, tier=200)
        self.assertTrue(pilot.pilot.all())
        self.assertTrue(set(pilot.volume_id) <= set(first.volume_id))
        self.assertEqual(int((manifest.tier <= 200).sum()), len(first))
        with self.assertRaises(ValueError):
            sampler.slice_manifest(manifest, pilot=True, tier=200)

    def test_overlap_across_splits_fails(self) -> None:
        meta = _catalog()
        manifest = self._build(meta)
        extra = manifest.iloc[0].copy()
        other = manifest[manifest.split != extra.split].iloc[0]
        extra.volume_id = "overlap"
        extra.clip_id = other.clip_id
        extra.frame_start = int(other.frame_start)
        extra.n_frames = int(other.n_frames)
        extra.split = "train" if other.split == "eval" else "eval"
        broken = pd.concat([manifest, pd.DataFrame([extra])], ignore_index=True)
        with self.assertRaises(RuntimeError):
            sampler.check_manifest(broken, meta, max_frames=330)


if __name__ == "__main__":
    unittest.main()
