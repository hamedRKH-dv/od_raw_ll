"""Visuals for the P4 multi-window run: one grid per inspection plus a strip chart.

Reads the window list from artifacts/p4/more_windows.json and writes figures to
artifacts/p4/more/. Each grid row is one window's middle frame: stored LL, rebuilt
LL, and the difference in gray levels on a fixed 0 to 16 scale.

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
DIFF_MAX = 16


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    WS.mkdir(parents=True, exist_ok=True)
    os.chdir(WS)
    windows = json.loads((ROOT / "artifacts" / "p4" / "more_windows.json").read_text())
    session = Session(environment="prod")
    catalog = LanceCatalog(DEFAULT_CATALOG, namespace=NAMESPACE)
    per_inspection: dict[str, list[dict]] = {}
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
        error = lsb_error(stored_u16, fresh, vmin, vmax)
        mid = count // 2
        stats = summarize(error)
        per_inspection.setdefault(inspection, []).append(
            {
                **window,
                **stats,
                "passed": stats["within_1_frac"] >= P4_THRESHOLD,
                "_stored": stored[mid],
                "_fresh": fresh[mid],
                "_error": error[mid],
            }
        )
        print(f"{inspection} {window['anomaly_type']} {100 * stats['within_1_frac']:.1f}%", flush=True)

    for inspection, rows in per_inspection.items():
        rows.sort(key=lambda r: r["within_1_frac"])
        fig, axes = plt.subplots(len(rows), 3, figsize=(11, 2.1 * len(rows)), dpi=110, squeeze=False)
        for r, row in enumerate(rows):
            lo, hi = np.percentile(row["_stored"], [1, 99])
            for c, (image, title, clim, cmap) in enumerate(
                (
                    (row["_stored"], "stored LL", (lo, hi), "gray"),
                    (row["_fresh"], "rebuilt LL", (lo, hi), "gray"),
                    (row["_error"], "difference (gray levels)", (0, DIFF_MAX), "magma"),
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
        fig.suptitle(
            f"{inspection}: middle frame of each tested window (pictures). "
            f"Percent on each row is the whole window. Pass line 99.9%.",
            fontsize=10,
        )
        fig.tight_layout()
        fig.savefig(OUT / f"{inspection}_grid.jpg")
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4.6), dpi=130)
    order = list(per_inspection)
    for i, inspection in enumerate(order):
        for row in per_inspection[inspection]:
            x = row["within_1_frac"] * 100
            ax.scatter(x, i, s=34, color="tab:green" if row["passed"] else "tab:red", edgecolor="black", linewidth=0.4, zorder=3)
    ax.axvline(99.9, color="black", linestyle="--", linewidth=0.8, label="99.9% pass line")
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order)
    ax.set_xlim(0, 105)
    ax.set_xlabel("% of pixels within 1 gray level (whole window, one dot per window)")
    ax.set_title("All tested windows. Only the 0AP3OOMEPID dots reach the line.")
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "strip.jpg")
    plt.close(fig)

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
