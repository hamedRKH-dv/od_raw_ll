"""Visuals for the P4 multi-window run: one grid per inspection plus a strip chart.

Reads the window list from artifacts/p4/more_windows.json and writes figures to
artifacts/p4/more/. Also refreshes the five single-window figures in
artifacts/p4/figures/.

Four inspections were copied from a legacy store that kept each frame as float16
in 0..1. The difference shown for those is the rebuild after that same rounding.
0AP3OOMEPID was written at full precision, so its difference is not rounded.

    cache/venv/bin/python scripts/render_p4_more.py
"""

import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from ilipy import Session  # noqa: E402
from ilipyutils.lance.store import LanceCatalog  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_prerequisites import (  # noqa: E402
    DEFAULT_CATALOG,
    NAMESPACE,
    P4_THRESHOLD,
    SURFACES,
    WS,
    beamform_ll,
    load_stored_lossless,
    lsb_error,
    open_clip,
    summarize,
    surface_circles,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "p4" / "more"
FIG = ROOT / "artifacts" / "p4" / "figures"
DIFF_MAX = 16
# These four were ingested from the legacy Zarr, which stored frames as float16.
LEGACY_FLOAT16 = frozenset({"0A1OLRV96N3", "0A49KLT3B7Y", "0ABP0TFUSH1", "0AGBXB4WEGN"})
# The one window per inspection drawn in the report.
REPORT_VOLUMES = frozenset(
    {
        "1074d0fd-6f4d-5246-8db5-3dfa44c2961d",
        "0005b152-9eef-51e5-9915-210204d6ba05",
        "e7959c75-ceed-519e-a855-a3deafeb5aaa",
        "4e53520e-966f-5de1-b866-f7e32d488a39",
        "7d0f544b-fedd-58a4-b777-d7fbe9ad9d4a",
    }
)
ORDER = ["0AP3OOMEPID", "0A1OLRV96N3", "0A49KLT3B7Y", "0ABP0TFUSH1", "0AGBXB4WEGN"]


def as_stored(fresh: np.ndarray, vmin: float, vmax: float, inspection: str) -> np.ndarray:
    """Round a rebuild the way that inspection's stored LL was saved."""
    if inspection not in LEGACY_FLOAT16:
        return fresh
    span = (vmax - vmin) or 1.0
    unit = (fresh.astype(np.float64) - vmin) / span
    return unit.astype(np.float16).astype(np.float64) * span + vmin


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    FIG.mkdir(parents=True, exist_ok=True)
    WS.mkdir(parents=True, exist_ok=True)
    os.chdir(WS)
    windows = json.loads((ROOT / "artifacts" / "p4" / "more_windows.json").read_text())
    session = Session(environment="prod")
    catalog = LanceCatalog(DEFAULT_CATALOG, namespace=NAMESPACE)
    per_inspection: dict[str, list[dict]] = {}
    report_rows: dict[str, dict] = {}
    for window in windows:
        inspection = window["inspection_id"]
        row, stored_u16 = load_stored_lossless(catalog, window["volume_id"])
        vmin, vmax = float(row["vol_vmin"]), float(row["vol_vmax"])
        span = (vmax - vmin) or 1.0
        stored = stored_u16.astype(np.float64) / 65535.0 * span + vmin
        _, beamformer, standoff, radius = open_clip(session, inspection, window["clip_id"])
        start, count = window["frame_start"], window["n_frames"]
        circles = surface_circles(
            inspection, window["clip_id"], start, count, SURFACES[inspection][0], standoff, radius
        )
        fresh = beamform_ll(beamformer, circles, start, count)
        raw = summarize(lsb_error(stored_u16, fresh, vmin, vmax))
        compared = as_stored(fresh, vmin, vmax, inspection)
        error = lsb_error(stored_u16, compared, vmin, vmax)
        mid = count // 2
        stats = summarize(error)
        entry = {
            **window,
            **stats,
            "raw_within_1_frac": raw["within_1_frac"],
            "raw_max_lsb": raw["max_lsb"],
            "float16": inspection in LEGACY_FLOAT16,
            "passed": stats["within_1_frac"] >= P4_THRESHOLD,
            "vol_vmax": vmax,
            "_stored": stored[mid],
            "_fresh": compared[mid],
            "_error": error[mid],
            "_within": (error.reshape(count, -1) <= 1).mean(axis=1),
            "_mid_max": int(error[mid].max()),
        }
        per_inspection.setdefault(inspection, []).append(entry)
        if window["volume_id"] in REPORT_VOLUMES:
            report_rows[inspection] = entry
        print(
            f"{inspection} {window['anomaly_type']} raw {100 * raw['within_1_frac']:.1f}%"
            f"  compared {100 * stats['within_1_frac']:.1f}%",
            flush=True,
        )

    for inspection, rows in per_inspection.items():
        rows.sort(key=lambda r: r["within_1_frac"])
        fig, axes = plt.subplots(len(rows), 3, figsize=(11, 2.1 * len(rows)), dpi=110, squeeze=False)
        for r, row in enumerate(rows):
            lo, hi = np.percentile(row["_stored"], [1, 99])
            for c, (image, title, clim, cmap) in enumerate(
                (
                    (row["_stored"], "stored LL", (lo, hi), "gray"),
                    (
                        row["_fresh"],
                        "rebuilt, then float16" if row["float16"] else "rebuilt LL",
                        (lo, hi),
                        "gray",
                    ),
                    (
                        row["_error"],
                        "difference after float16" if row["float16"] else "difference (gray levels)",
                        (0, DIFF_MAX),
                        "magma",
                    ),
                )
            ):
                ax = axes[r, c]
                shown = ax.imshow(image, cmap=cmap, vmin=clim[0], vmax=clim[1], aspect="auto")
                ax.set_xticks([])
                ax.set_yticks([])
                if r == 0:
                    ax.set_title(title, fontsize=9)
                if c == 0:
                    ax.set_ylabel(
                        f"{row['anomaly_type']}\n{row['clip_id']}\n{100 * row['within_1_frac']:.1f}%",
                        fontsize=8,
                        rotation=0,
                        ha="right",
                        va="center",
                    )
                if c == 2:
                    fig.colorbar(shown, ax=ax, fraction=0.046, pad=0.02)
        rounding = (
            "Difference is the rebuild after the same float16 rounding the stored image used."
            if inspection in LEGACY_FLOAT16
            else "This inspection was stored at full precision, so the difference is not rounded."
        )
        fig.suptitle(
            f"{inspection}: middle frame of each window. Percent is the whole window. Pass line 99.9%.\n{rounding}",
            fontsize=10,
        )
        fig.tight_layout()
        fig.savefig(OUT / f"{inspection}_grid.jpg")
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=130)
    order = [name for name in ORDER if name in per_inspection]
    for i, inspection in enumerate(order):
        for row in per_inspection[inspection]:
            x = row["within_1_frac"] * 100
            ax.scatter(x, i, s=34, color="tab:green" if row["passed"] else "tab:red", edgecolor="black", linewidth=0.4, zorder=3)
    ax.axvline(99.9, color="black", linestyle="--", linewidth=0.8, label="99.9% pass line")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order)
    ax.set_xlim(0, 105)
    ax.set_xlabel("% of pixels within 1 gray level (whole window, one dot per window)")
    ax.set_title("After the rounding each inspection was stored with. Green reaches 99.9%.")
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "strip.jpg")
    plt.close(fig)

    for inspection, entry in report_rows.items():
        lo, hi = np.percentile(entry["_stored"], [1, 99])
        mid = entry["n_frames"] // 2
        within = entry["_within"]
        fig, axes = plt.subplots(1, 3, figsize=(12.2, 4.4), dpi=130)
        for ax, image, title, clim in (
            (axes[0], entry["_stored"], "stored lossless LL", (lo, hi)),
            (axes[1], entry["_fresh"], "rebuilt, then float16" if entry["float16"] else "rebuilt LL", (lo, hi)),
            (axes[2], entry["_error"], "difference, gray levels", (0, DIFF_MAX)),
        ):
            shown = ax.imshow(
                image, cmap="gray" if clim[0] != 0 else "magma", vmin=clim[0], vmax=clim[1], aspect="auto"
            )
            ax.set_title(title)
            ax.set_xlabel("depth pixel")
            ax.set_ylabel("angle pixel")
            fig.colorbar(shown, ax=ax, fraction=0.046, pad=0.04)
        fig.suptitle(
            f"{inspection}   middle frame {entry['frame_start'] + mid}"
            f"   this frame {100 * float(within[mid]):.1f}% within 1 level\n"
            f"whole window {100 * entry['within_1_frac']:.1f}% within 1 level"
            f"    before rounding {100 * entry['raw_within_1_frac']:.1f}%"
            f"    worst pixel {entry['max_lsb']} levels"
        )
        fig.tight_layout()
        fig.savefig(FIG / f"{inspection}.jpg")
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=130)
    for inspection in order:
        if inspection in report_rows:
            within = report_rows[inspection]["_within"]
            ax.plot(np.arange(len(within)), 100 * np.asarray(within), label=inspection, linewidth=1.4)
    ax.axhline(99.9, color="black", linewidth=0.8, linestyle="--", label="99.9% pass line")
    ax.set_ylim(0, 105)
    ax.set_xlabel("frame inside the window")
    ax.set_ylabel("% of pixels within 1 gray level")
    ax.set_title("Same comparison as the pairs. Legacy inspections are rounded to float16 first.")
    ax.legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    fig.savefig(FIG / "per_frame.jpg")
    plt.close(fig)
    (FIG / "summary.json").write_text(
        json.dumps(
            {
                inspection: {k: v for k, v in entry.items() if not k.startswith("_")}
                for inspection, entry in report_rows.items()
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    summary = {
        inspection: [
            {k: v for k, v in row.items() if not k.startswith("_")} for row in rows
        ]
        for inspection, rows in per_inspection.items()
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()
