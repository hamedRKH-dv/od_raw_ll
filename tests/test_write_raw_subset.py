"""Writer contract: schema, content hash, resume, low-disk stop. Fake reader, no chunk server."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from raw_schema import raw_volumes_schema  # noqa: E402
from write_raw_subset import write_volumes  # noqa: E402


def _manifest(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "volume_id": f"v{i}",
                "inspection_id": "insp",
                "clip_id": "clip",
                "frame_start": 100 + i * 10,
                "n_frames": 2,
            }
            for i in range(n)
        ]
    )


class _Reader:
    def __init__(self) -> None:
        self.queued: list[tuple[int, int]] = []

    def queue_frames(self, start: int, count: int, prefetcher) -> None:
        self.queued.append((start, count))

    def get_iq_frames(self, start: int, count: int, upsample: bool = False) -> np.ndarray:
        return np.full((count, 2, 3, 2), start, dtype=np.int16)

    def get_iq_decimation(self) -> int:
        return 3


def _payload(start: int, count: int = 2) -> bytes:
    return np.ascontiguousarray(np.full((count, 2, 3, 2), start, dtype=np.int16)).tobytes()


class WriteRawSubsetTest(unittest.TestCase):
    def test_schema_names(self) -> None:
        names = raw_volumes_schema().names
        self.assertEqual(
            names,
            [
                "volume_id",
                "inspection_id",
                "clip_id",
                "frame_start",
                "n_frames",
                "raw_kind",
                "upsample",
                "shape",
                "dtype",
                "codec",
                "decimation",
                "content_sha256",
                "created_at",
                "raw_bytes",
            ],
        )

    def _write(self, dest: Path, manifest: pd.DataFrame, *, free: int = 100 * 2**30):
        reader = _Reader()
        log = dest / "write_raw_log.jsonl"
        report = write_volumes(
            dest / "catalog",
            manifest,
            open_reader=lambda inspection, clip: reader,
            kind="iq",
            upsample=False,
            free_bytes=lambda _path: free,
            log_path=log,
        )
        return report, reader, log

    def test_hash_matches_stored_bytes(self) -> None:
        import tempfile

        from ilipyutils.lance.store import LanceCatalog

        from raw_schema import raw_volumes_schema
        from select_paired_subset import NAMESPACE

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            report, reader, _log = self._write(dest, _manifest(2))
            self.assertEqual(report["inserted"], 2)
            self.assertFalse(report["stopped"])
            self.assertEqual(reader.queued, [(100, 2), (110, 2)])
            catalog = LanceCatalog(str(dest / "catalog"), namespace=tuple(NAMESPACE))
            table = catalog.open_table("raw_volumes", raw_volumes_schema())
            ds = table.to_lance()
            rows = ds.to_table(
                columns=["volume_id", "content_sha256", "codec", "decimation", "shape", "dtype"],
                with_row_address=True,
            ).to_pylist()
            self.assertEqual(len(rows), 2)
            for row, start in zip(sorted(rows, key=lambda r: r["volume_id"]), (100, 110)):
                blob = ds.take_blobs("raw_bytes", addresses=[int(row["_rowaddr"])])[0]
                data = blob.read()
                self.assertEqual(row["content_sha256"], hashlib.sha256(_payload(start)).hexdigest())
                self.assertEqual(hashlib.sha256(data).hexdigest(), row["content_sha256"])
                self.assertEqual(data, _payload(start))
                self.assertEqual(row["codec"], "raw")
                self.assertEqual(row["decimation"], 3)
                self.assertEqual(row["dtype"], "int16")
                self.assertEqual(list(row["shape"]), [2, 2, 3, 2])

    def test_resume_skips_existing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            first, _, _ = self._write(dest, _manifest(1))
            second, reader, _ = self._write(dest, _manifest(2))
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["inserted"], 1)
            self.assertEqual(reader.queued, [(110, 2)])

    def test_low_disk_writes_nothing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            report, reader, log = self._write(dest, _manifest(2), free=1)
            self.assertEqual(report["inserted"], 0)
            self.assertEqual(report["stopped"], "low_disk")
            self.assertEqual(reader.queued, [])
            text = log.read_text() if log.exists() else ""
            self.assertEqual(text, "")

    def test_log_line_per_volume(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            _, _, log = self._write(dest, _manifest(1))
            lines = [json.loads(line) for line in log.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["volume_id"], "v0")
            self.assertGreater(lines[0]["bytes"], 0)
            self.assertGreaterEqual(lines[0]["seconds"], 0)


if __name__ == "__main__":
    unittest.main()
