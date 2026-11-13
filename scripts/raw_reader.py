"""Read one raw window from a ChannelDataReader. No beamformer."""

from __future__ import annotations

import numpy as np


def read_raw_window(
    reader,
    frame_start: int,
    n_frames: int,
    *,
    kind: str,
    upsample: bool,
) -> np.ndarray:
    """Return RF or IQ frames for [frame_start, frame_start + n_frames)."""
    if kind == "rf":
        return reader.get_rf_frames(int(frame_start), int(n_frames))
    if kind == "iq":
        return reader.get_iq_frames(int(frame_start), int(n_frames), upsample=bool(upsample))
    raise ValueError(f"kind must be 'rf' or 'iq', got {kind!r}")
