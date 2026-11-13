"""read_raw_window dispatches RF vs IQ and keeps the reader's array."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from raw_reader import read_raw_window  # noqa: E402


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_rf_frames(self, start: int, count: int) -> np.ndarray:
        self.calls.append(("rf", start, count))
        return np.full((count, 2, 3), start, dtype=np.int16)

    def get_iq_frames(self, start: int, count: int, upsample: bool = False) -> np.ndarray:
        self.calls.append(("iq", start, count, upsample))
        return np.full((count, 2, 3, 2), start, dtype=np.int16)


class ReadRawWindowTest(unittest.TestCase):
    def test_iq_without_upsample(self) -> None:
        reader = _Reader()
        got = read_raw_window(reader, 10, 4, kind="iq", upsample=False)
        self.assertEqual(got.shape, (4, 2, 3, 2))
        self.assertEqual(got.dtype, np.int16)
        self.assertEqual(reader.calls, [("iq", 10, 4, False)])

    def test_rf(self) -> None:
        reader = _Reader()
        got = read_raw_window(reader, 3, 2, kind="rf", upsample=False)
        self.assertEqual(got.shape, (2, 2, 3))
        self.assertEqual(reader.calls, [("rf", 3, 2)])

    def test_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ValueError):
            read_raw_window(_Reader(), 0, 1, kind="bm", upsample=False)


if __name__ == "__main__":
    unittest.main()
