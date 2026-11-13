"""Stage 2 prerequisite checks P1-P5 for the paired OD raw + LL build.

Read-only against the source catalog, the ID-surface buckets, and the chunk
server. Writes one JSON per check to work/prereq/. Run from the repo root:

    cache/venv/bin/python scripts/check_prerequisites.py all
    cache/venv/bin/python scripts/check_prerequisites.py p4

P1 frame numbering, P2 profile, P3 IQ upsample setting, P4 ID surface per
inspection, P5 stored-LL reconstruction tolerance. `all` stops after a P1 or P3
failure, because the pairing would be wrong.

Pass thresholds, fixed before the run:
  P4: at least 99.9% of pixels within 1 LSB of the stored lossless LL.
  P5: 99.9th-percentile reconstruction error within 1 LSB of the volume range.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from ilipy import Session
from ilipy.beamformer import BfImageType
from ilipy.channeldata import CaptureParams, ChannelDataReader, ImageProfile
from ilipy.probes import ProbeMapping
from ilipyutils.beamforming import Beamformer
from ilipyutils.codecs import Jp2Decoder
from ilipyutils.common.workflows.config import S3Location
from ilipyutils.configs import LL_DEFAULTS
from ilipyutils.lance.store import LanceCatalog

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT.parents[1] / "ILIWorkflows" / "workflows" / "od_corrosion_detection"
sys.path.insert(0, str(WORKFLOW))

from od_corrosion_detection.catalog_schemas import volumes_schema  # noqa: E402
from od_corrosion_detection.utils.ml_circle import get_ml_id_circle  # noqa: E402
from select_paired_subset import DEFAULT_CATALOG  # noqa: E402

NAMESPACE = ("kraken", "od_corrosion")
OUT = ROOT / "work" / "prereq"
WS = OUT / "ws"  # ml_circle downloads ID surfaces under ./tmp_ml_surface here
P4_THRESHOLD = 0.999
P5_THRESHOLD_LSB = 1.0

# ID-surface prefixes, from the Stage 2 listing on 13 Nov 2026. Each inspection
# has one candidate. A second candidate goes in the same list.
SURFACES = {
    "0A1OLRV96N3": [("dv-ilit0007", "track_runs/0A1OLRV96N3/ili_ml_surface/v1.2")],
    "0A49KLT3B7Y": [("dv-ilit0008", "track_runs/0A49KLT3B7Y/ili_ml_surface/v1.2")],
    "0ABP0TFUSH1": [("dv-ilit0010", "track_runs/0ABP0TFUSH1/ili_ml_surface/v1.2")],
    "0AGBXB4WEGN": [("dv-ilit0011", "track_runs/0AGBXB4WEGN/ili_ml_surface")],
    "0AP3OOMEPID": [("dv-ilit0013", "track_runs/0AP3OOMEPID/ili_ml_surface")],
}


def test_windows() -> dict[str, dict]:
    """Shortest eval positive per inspection; shortest eval volume if none."""
    manifest = pd.read_parquet(ROOT / "artifacts" / "manifest.parquet")
    evals = manifest[manifest.split == "eval"]
    windows = {}
    for inspection, rows in evals.groupby("inspection_id"):
        positives = rows[rows.anomaly_type == "od_corrosion"]
        pool = positives if len(positives) else rows
        row = pool.sort_values(["n_frames", "volume_id"]).iloc[0]
        windows[inspection] = {
            "volume_id": row.volume_id,
            "clip_id": row.clip_id,
            "frame_start": int(row.frame_start),
            "n_frames": int(row.n_frames),
            "anomaly_type": row.anomaly_type,
            "no_positive_in_eval": not len(positives),
        }
    return windows


def write_result(name: str, passed: bool, reason: str, **metrics) -> dict:
    result = {"check": name, "passed": bool(passed), "reason": reason, **metrics}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(f"{name}: {'PASS' if passed else 'FAIL'}  {reason}", flush=True)
    return result


def load_stored_lossless(catalog: LanceCatalog, volume_id: str):
    """Return the stored volume row and its decoded lossless uint16 frames."""
    dataset = catalog.open_table("volumes", volumes_schema()).to_lance()
    rows = dataset.to_table(
        columns=[
            "volume_id",
            "n_frames",
            "frame_h",
            "frame_w",
            "vol_vmin",
            "vol_vmax",
            "lossless_frame_offsets",
            "lossless_frame_lengths",
        ],
        filter=f"volume_id = '{volume_id}'",
        with_row_address=True,
        blob_handling="blobs_descriptions",
    ).to_pylist()
    if len(rows) != 1:
        raise ValueError(f"volume {volume_id!r}: {len(rows)} rows, expected 1")
    row = rows[0]
    offsets = [int(x) for x in row["lossless_frame_offsets"] or []]
    lengths = [int(x) for x in row["lossless_frame_lengths"] or []]
    if len(offsets) != int(row["n_frames"]):
        raise ValueError(f"volume {volume_id!r} has no complete lossless family")
    total = offsets[-1] + lengths[-1]
    payload = dataset.read_blob_ranges(
        "lossless_frame_bytes",
        [(int(row["_rowaddr"]), 0, total)],
        selector="addresses",
    )[0][2]
    payload = bytes(payload)
    frames = [payload[o : o + n] for o, n in zip(offsets, lengths, strict=True)]
    decoded = []
    decoder = Jp2Decoder()
    hw = (int(row["frame_h"]), int(row["frame_w"]))
    for start in range(0, len(frames), 32):
        decoded.append(np.asarray(decoder(frames[start : start + 32], expected_hw=hw).cpu().numpy()))
    return row, np.concatenate(decoded).astype(np.uint16)


def to_lsb(volume: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Quantize with the same formula the writer uses (`lance_writer._normalize_frames`)."""
    span = (vmax - vmin) or 1.0
    scaled = (volume.astype(np.float64) - vmin) / span
    return np.clip(np.round(scaled * 65535), 0, 65535).astype(np.int64)


def lsb_error(decoded: np.ndarray, fresh: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Per-pixel error in stored LSB units. Non-finite fresh pixels count as worst case."""
    if decoded.shape != fresh.shape:
        raise ValueError(f"shape mismatch: stored {decoded.shape}, fresh {fresh.shape}")
    finite = np.isfinite(fresh)
    error = np.abs(to_lsb(np.where(finite, fresh, vmin), vmin, vmax) - decoded.astype(np.int64))
    return np.where(finite, error, 65535)


def summarize(error: np.ndarray) -> dict:
    return {
        "exact_frac": float(np.mean(error == 0)),
        "within_1_frac": float(np.mean(error <= 1)),
        "max_lsb": int(error.max()),
        "p999_lsb": float(np.quantile(error, 0.999)),
    }


def open_clip(session: Session, inspection_id: str, clip_id: str):
    session.set_active_inspection(inspection_id)
    clip = session.get_clip_by_id(clip_id)
    beamformer = Beamformer(session, inspection_id, clip)
    standoff = ProbeMapping(session, clip).probe_config.carrier_arm.stand_off_mm
    radius = CaptureParams(session, clip).params.target_scan_radius_mm
    return clip, beamformer, standoff, radius


def surface_circles(inspection_id, clip_id, start, n, surface, standoff, radius):
    bucket, prefix = surface
    shutil.rmtree(Path("tmp_ml_surface") / inspection_id, ignore_errors=True)
    return get_ml_id_circle(
        id_surface_s3_location=S3Location(bucket=bucket, prefix=prefix),
        inspection_id=inspection_id,
        clip_id=clip_id,
        frame_index_range=(start, start + n),
        target_standoff_mm=standoff,
        target_scan_radius_mm=radius,
    )


def beamform_ll(beamformer, circles, start, n, iq=None) -> np.ndarray:
    """LL beamform, as `beamform_ll_window` does, plus the optional custom IQ hook."""
    radius_mm = float(np.nanmedian(np.asarray(circles)[:, 2]))
    volume = beamformer.beamform_compound_data(
        frame_range=(start, start + n),
        bf_type=BfImageType.OuterSurfaceDetect,
        circles=circles,
        bf_configs={**LL_DEFAULTS, "pipe_inner_radius_mm": radius_mm},
        custom_iq_frames=iq,
    )
    return np.asarray(volume.data, dtype=np.float32)


def check_p1(session, windows) -> dict:
    """Odometer ticks from the beamformer match the reader's frame headers."""
    bad = {}
    for inspection, w in windows.items():
        clip, beamformer, _, _ = open_clip(session, inspection, w["clip_id"])
        reader = ChannelDataReader(session, clip, ImageProfile.ZeroAngle)
        start, end = w["frame_start"], w["frame_start"] + w["n_frames"]
        beam_odo, _ = beamformer._get_odo_and_timer_ticks((start, end), ImageProfile.ZeroAngle)
        header_odo = [int(reader.get_frame_header(i).odometer_ticks.value) for i in range(start, end)]
        if beam_odo != header_odo or end > reader.frame_count:
            bad[inspection] = {
                "frames_in_clip": int(reader.frame_count),
                "odo_equal": beam_odo == header_odo,
                "beam_first": beam_odo[:3],
                "header_first": header_odo[:3],
            }
    passed = not bad
    return write_result(
        "p1",
        passed,
        "odometer ticks equal frame headers on all test windows"
        if passed
        else f"mismatch on {sorted(bad)}",
        mismatches=bad,
    )


def check_p2() -> dict:
    """LL image type maps to the zero-angle profile."""
    profile = Beamformer._image_mode_to_profile(BfImageType.OuterSurfaceDetect)
    passed = profile == ImageProfile.ZeroAngle
    return write_result("p2", passed, f"OuterSurfaceDetect -> {profile}", profile=str(profile))


def check_p3(session, windows) -> dict:
    """Which IQ upsample setting reproduces a normal beamform exactly."""
    matches = {}
    for inspection, w in windows.items():
        clip, beamformer, standoff, radius = open_clip(session, inspection, w["clip_id"])
        start, n = w["frame_start"], w["n_frames"]
        surface = SURFACES[inspection][0]
        circles = surface_circles(inspection, w["clip_id"], start, n, surface, standoff, radius)
        normal = beamform_ll(beamformer, circles, start, n)
        reader = ChannelDataReader(session, clip, ImageProfile.ZeroAngle)
        per_upsample = {}
        for upsample in (False, True):
            iq = reader.get_iq_frames(start, n, upsample=upsample)
            try:
                custom = beamform_ll(beamformer, circles, start, n, iq=iq)
            except RuntimeError as error:
                per_upsample[str(upsample)] = f"rejected: {error}"
                continue
            per_upsample[str(upsample)] = float(np.nanmax(np.abs(normal - custom)))
        matches[inspection] = per_upsample
    exact = {
        inspection: [flag for flag, diff in per.items() if diff == 0.0]
        for inspection, per in matches.items()
    }
    winners = {tuple(v) for v in exact.values()}
    passed = len(winners) == 1 and len(next(iter(winners))) == 1
    reason = (
        f"upsample={next(iter(winners))[0]} reproduces normal beamform on all windows"
        if passed
        else f"no single exact setting across windows: {exact}"
    )
    return write_result("p3", passed, reason, max_abs_diff=matches, exact_settings=exact)


def check_p4(session, windows, catalog) -> dict:
    """Pick the ID surface per inspection that reproduces the stored lossless LL."""
    results = {}
    for inspection, w in windows.items():
        row, decoded = load_stored_lossless(catalog, w["volume_id"])
        clip, beamformer, standoff, radius = open_clip(session, inspection, w["clip_id"])
        start, n = w["frame_start"], w["n_frames"]
        vmin, vmax = float(row["vol_vmin"]), float(row["vol_vmax"])
        candidates = []
        for surface in SURFACES[inspection]:
            circles = surface_circles(inspection, w["clip_id"], start, n, surface, standoff, radius)
            fresh = beamform_ll(beamformer, circles, start, n)
            candidates.append(
                {"bucket": surface[0], "prefix": surface[1], **summarize(lsb_error(decoded, fresh, vmin, vmax))}
            )
        best = max(candidates, key=lambda c: (c["within_1_frac"], c["exact_frac"]))
        results[inspection] = {
            "volume_id": w["volume_id"],
            "candidates": candidates,
            "winner": {"bucket": best["bucket"], "prefix": best["prefix"]},
            "passed": best["within_1_frac"] >= P4_THRESHOLD,
        }
    passed = all(r["passed"] for r in results.values())
    failed = sorted(k for k, r in results.items() if not r["passed"])
    reason = (
        "every inspection has a surface within 1 LSB on >= 99.9% of pixels"
        if passed
        else f"no matching surface for {failed}; see id_surfaces.json"
    )
    (OUT / "id_surfaces.json").write_text(json.dumps(results, indent=2) + "\n")
    return write_result("p4", passed, reason, inspections=results)


def check_p5(session, windows, catalog) -> dict:
    """Float-space reconstruction error on the P4 winners sets the tolerance."""
    id_surfaces = json.loads((OUT / "id_surfaces.json").read_text())
    per = {}
    for inspection, w in windows.items():
        if not id_surfaces[inspection]["passed"]:
            per[inspection] = {"skipped": "no P4 surface"}
            continue
        row, decoded = load_stored_lossless(catalog, w["volume_id"])
        clip, beamformer, standoff, radius = open_clip(session, inspection, w["clip_id"])
        winner = id_surfaces[inspection]["winner"]
        start, n = w["frame_start"], w["n_frames"]
        circles = surface_circles(
            inspection, w["clip_id"], start, n, (winner["bucket"], winner["prefix"]), standoff, radius
        )
        fresh = beamform_ll(beamformer, circles, start, n)
        vmin, vmax = float(row["vol_vmin"]), float(row["vol_vmax"])
        span = (vmax - vmin) or 1.0
        decoded_float = decoded.astype(np.float64) / 65535 * span + vmin
        rel = np.abs(fresh.astype(np.float64) - decoded_float) / span
        rel = rel[np.isfinite(rel)]
        per[inspection] = {
            "max_rel_err": float(rel.max()),
            "p999_rel_err_lsb": float(np.quantile(rel, 0.999) * 65535),
        }
    checked = {k: v for k, v in per.items() if "p999_rel_err_lsb" in v}
    passed = bool(checked) and all(v["p999_rel_err_lsb"] <= P5_THRESHOLD_LSB for v in checked.values())
    reason = (
        f"99.9th-percentile error <= {P5_THRESHOLD_LSB} LSB on {len(checked)} inspections"
        if passed
        else "error above 1 LSB on some inspection, or no surface passed P4"
    )
    return write_result("p5", passed, reason, per_inspection=per)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("check", choices=["p1", "p2", "p3", "p4", "p5", "all"])
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    WS.mkdir(parents=True, exist_ok=True)
    os.chdir(WS)

    windows = test_windows()
    (OUT / "test_windows.json").write_text(json.dumps(windows, indent=2) + "\n")
    session = Session(environment="prod")
    catalog = LanceCatalog(DEFAULT_CATALOG, namespace=NAMESPACE)

    steps = {
        "p1": lambda: check_p1(session, windows),
        "p2": check_p2,
        "p3": lambda: check_p3(session, windows),
        "p4": lambda: check_p4(session, windows, catalog),
        "p5": lambda: check_p5(session, windows, catalog),
    }
    names = list(steps) if args.check == "all" else [args.check]
    for name in names:
        result = steps[name]()
        if args.check == "all" and name in ("p1", "p3") and not result["passed"]:
            print(f"stopping: {name} failed, the pairing would be wrong", flush=True)
            break


if __name__ == "__main__":
    main()
