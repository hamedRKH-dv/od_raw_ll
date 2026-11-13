"""Pair, hash, and count checks. No catalog and no chunk server."""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_paired_catalog import (  # noqa: E402
    blob_matches,
    count_problems,
    pair_problems,
    scope_frame,
)


def _vol(volume_id="v", frame_start=10, n_frames=4, clip_id="c", inspection_id="i"):
    return {
        "volume_id": volume_id,
        "frame_start": frame_start,
        "n_frames": n_frames,
        "clip_id": clip_id,
        "inspection_id": inspection_id,
        "split": "eval",
        "anomaly_type": "od_corrosion",
        "pilot": True,
        "tier": 4000,
    }


class ValidateTests(unittest.TestCase):
    def test_scope_pilot_and_tier(self) -> None:
        frame = pd.DataFrame(
            [
                {**_vol("a"), "pilot": True, "tier": 4000},
                {**_vol("b"), "pilot": False, "tier": 8000},
            ]
        )
        self.assertEqual(list(scope_frame(frame, "pilot").volume_id), ["a"])
        self.assertEqual(list(scope_frame(frame, "tier:4000").volume_id), ["a"])

    def test_pair_flags_disagreement(self) -> None:
        manifest = pd.DataFrame([_vol()])
        ll = pd.DataFrame([_vol()])
        raw = pd.DataFrame([_vol(n_frames=9)])
        problems = pair_problems(manifest, ll, raw)
        self.assertEqual(len(problems), 1)
        self.assertIn("n_frames", problems[0])

    def test_pair_ok_when_fields_agree(self) -> None:
        manifest = pd.DataFrame([_vol()])
        self.assertEqual(pair_problems(manifest, manifest, manifest), [])

    def test_blob_hash_and_shape(self) -> None:
        payload = np.arange(6, dtype=np.int16).tobytes()
        digest = hashlib.sha256(payload).hexdigest()
        self.assertTrue(blob_matches(payload, [3, 2], "int16", digest))
        self.assertFalse(blob_matches(payload, [2, 2], "int16", digest))
        self.assertFalse(blob_matches(payload + b"\x00", [3, 2], "int16", digest))

    def test_counts_match_cells_and_splits(self) -> None:
        manifest = pd.DataFrame([_vol("a"), {**_vol("b"), "split": "train", "anomaly_type": "nominal"}])
        self.assertEqual(count_problems(manifest, manifest), [])
        short = manifest.iloc[:1]
        self.assertTrue(count_problems(manifest, short))


if __name__ == "__main__":
    unittest.main()
