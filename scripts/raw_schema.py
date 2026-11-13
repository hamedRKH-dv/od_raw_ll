"""Arrow schema for the raw_volumes table. Lives in RnD, not the workflow package."""

from __future__ import annotations

import pyarrow as pa
from lance.blob import blob_field


def raw_volumes_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("volume_id", pa.string()),
            pa.field("inspection_id", pa.string()),
            pa.field("clip_id", pa.string()),
            pa.field("frame_start", pa.int32()),
            pa.field("n_frames", pa.int32()),
            pa.field("raw_kind", pa.string()),  # "rf" | "iq"
            pa.field("upsample", pa.bool_()),
            pa.field("shape", pa.list_(pa.int32())),
            pa.field("dtype", pa.string()),
            pa.field("codec", pa.string()),  # "raw" | "zstd3"
            pa.field("decimation", pa.int32()),
            pa.field("content_sha256", pa.string()),
            pa.field("created_at", pa.timestamp("us", tz="UTC")),
            blob_field("raw_bytes", inline_size_threshold=0),
        ]
    )
