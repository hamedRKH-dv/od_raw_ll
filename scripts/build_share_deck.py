"""Shareable deck for the raw + LL paired-dataset plan.

Numbers are computed from catalog_census.json and the same assumptions as
estimate.py. Nothing on a slide is typed in by hand.

    python3 scripts/od_raw_ll/build_share_deck.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

sys.path.insert(0, str(Path.home() / ".cursor/skills/building-pptx-slides/scripts"))
import pptx_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parent
CENSUS = ROOT / "catalog_census.json"
OUT = ROOT / "od_raw_ll_plan.pptx"

# Same assumptions as estimate.py. Labeled as assumed on the slides.
RAW_KIB_PER_FRAME = 107.0
CHUNK_FRAMES = 512
DOWNLOAD_FPS = (225.0, 1000.0)  # slow, fast
OVERHEAD_S = (1.0, 0.3)  # slow, fast
S3_MB_PER_S = (50.0, 150.0)


def _load() -> dict:
    census = json.loads(CENSUS.read_text())
    volumes = census["volumes"]
    frames = census["total_frames"]
    mean = frames / volumes
    positives = census["positives"]
    dataset = census["dataset"]
    negatives = dataset - positives
    chunks = 1 + (mean - 1) / CHUNK_FRAMES
    downloaded = chunks * CHUNK_FRAMES
    per_frame = census["lossy_bytes"] / frames
    ratio = (RAW_KIB_PER_FRAME * 1024) / per_frame

    def subset(count: int) -> dict:
        n_frames = count * mean
        raw = n_frames * RAW_KIB_PER_FRAME * 1024
        ll = n_frames * per_frame
        hours = []
        for fps, overhead in zip(DOWNLOAD_FPS, OVERHEAD_S):
            hours.append(count * (downloaded / fps + overhead) / 3600)
        slow, fast = max(hours), min(hours)
        io = [(raw + ll) / (rate * 1e6) / 60 for rate in S3_MB_PER_S]
        return {
            "count": count,
            "frames": n_frames,
            "raw_gib": raw / 1024**3,
            "ll_gb": ll / 1e9,
            "slow_h": slow,
            "fast_h": fast,
            "io_slow_min": max(io),
            "io_fast_min": min(io),
        }

    kept = positives + round(0.30 * negatives)
    return {
        "catalog": census["catalog"],
        "volumes": volumes,
        "dataset": dataset,
        "positives": positives,
        "negatives": negatives,
        "frames": frames,
        "mean": mean,
        "median": census["n_frames_median"],
        "max_frames": census["n_frames_max"],
        "clips": census["clips"],
        "ll_gb": census["lossy_bytes"] / 1e9,
        "kb_per_frame": per_frame / 1e3,
        "hw": census["frame_hw"][0],
        "mode": census["mode"][0],
        "codec": census["codec"][0],
        "by_split": census["by_split"],
        "inspections": census["per_inspection"],
        "chunks": chunks,
        "downloaded": downloaded,
        "raw_over_ll": ratio,
        "thirty": subset(round(0.30 * volumes)),
        "tenk": subset(10_000),
        "full": subset(volumes),
        "kept": subset(kept),
        "kept_count": kept,
        "kept_pct": 100.0 * kept / dataset,
        "tenk_pct": 100.0 * 10_000 / volumes,
        "flat_positives": round(0.30 * positives),
        "flat_eval_positives": round(0.30 * census["by_split"]["eval"]["positives"]),
    }


def _footer(slide, page: str) -> None:
    kit.text(
        slide,
        "OD raw + LL",
        kit.MARGIN,
        Inches(7.08),
        Inches(4.0),
        Inches(0.24),
        size=11,
        color=kit.MUTED,
        name="Footer",
    )
    kit.text(
        slide,
        page,
        Inches(8.6),
        Inches(7.08),
        Inches(4.03),
        Inches(0.24),
        size=11,
        color=kit.MUTED,
        align=PP_ALIGN.RIGHT,
        name="FooterPage",
    )


def _metric(slide, left, top, width, value, label) -> None:
    kit.panel(slide, left, top, width, Inches(1.45), name=f"Metric {label}")
    kit.text(
        slide,
        value,
        left + Inches(0.16),
        top + Inches(0.16),
        width - Inches(0.32),
        Inches(0.62),
        size=28,
        color=kit.ORANGE,
        bold=True,
        name=f"MetricValue {label}",
    )
    kit.text(
        slide,
        label,
        left + Inches(0.16),
        top + Inches(0.86),
        width - Inches(0.32),
        Inches(0.42),
        size=13,
        color=kit.TEXT,
        name=f"MetricLabel {label}",
    )


def _hours(row: dict) -> str:
    return f"{row['fast_h']:.1f} to {row['slow_h']:.1f} h"


def _workers(row: dict, n: int) -> str:
    return f"{row['fast_h'] / n:.1f} to {row['slow_h'] / n:.1f} h"


def slide_title(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "Pair raw with the LL catalog")
    kit.text(
        slide,
        "OD corrosion  ·  shareable plan  ·  12 Nov 2026",
        kit.MARGIN,
        Inches(0.42),
        Inches(11.9),
        Inches(0.28),
        size=12,
        color=kit.MUTED,
        bold=True,
        name="Kicker",
    )
    kit.text(
        slide,
        "\n".join(
            [
                "Beamforming is the expensive step for OD corrosion.",
                "The LL volumes already exist, and the raw channel data",
                "they came from is still on the chunk server.",
                "",
                "This is a plan to store both for the same frames,",
                "then learn a model that skips beamforming at inference.",
            ]
        ),
        kit.MARGIN,
        Inches(1.85),
        kit.CONTENT_W,
        Inches(2.3),
        size=20,
        name="Lead",
    )
    labels = [
        (f"{facts['volumes']:,}", "LL volumes today"),
        (f"{facts['frames'] / 1e6:.2f} M", "frames in those volumes"),
        (f"{facts['ll_gb']:.1f} GB", "LL stored, lossy JP2"),
        (f"{facts['clips']}", "clips"),
    ]
    for index, (value, label) in enumerate(labels):
        left = kit.MARGIN + Emu(int((kit.CARD_W + kit.CARD_GAP) * index))
        _metric(slide, left, Inches(4.55), kit.CARD_W, value, label)
    kit.text(
        slide,
        "Counted from the mahsa OD catalog on 12 Nov 2026. Metadata only, no frame bytes.",
        kit.MARGIN,
        Inches(6.2),
        kit.CONTENT_W,
        Inches(0.3),
        size=13,
        color=kit.MUTED,
        name="Source",
    )
    _footer(slide, "1")


def slide_proposal(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "What was proposed")
    steps = [
        ("1", "Full LL catalog", f"{facts['volumes']:,} volumes", "already beamformed"),
        ("2", "Take a subset", "30%, or 10k", "the two differ"),
        ("3", "Fetch the raw", "same frame window", "from chunk server"),
        ("4", "New catalog", "raw next to the LL", "no re-beamform"),
    ]
    top = Inches(1.75)
    for index, (num, head, line_a, line_b) in enumerate(steps):
        left = kit.MARGIN + Emu(int((kit.CARD_W + kit.CARD_GAP) * index))
        kit.panel(slide, left, top, kit.CARD_W, Inches(1.85), name=f"Step {num}")
        kit.text(
            slide,
            num,
            left + Inches(0.16),
            top + Inches(0.12),
            kit.CARD_W - Inches(0.32),
            Inches(0.32),
            size=14,
            color=kit.ORANGE,
            bold=True,
            name=f"StepNum {num}",
        )
        kit.text(
            slide,
            head,
            left + Inches(0.16),
            top + Inches(0.46),
            kit.CARD_W - Inches(0.32),
            Inches(0.36),
            size=15,
            color=kit.TEXT,
            bold=True,
            name=f"StepHead {num}",
        )
        kit.text(
            slide,
            f"{line_a}\n{line_b}",
            left + Inches(0.16),
            top + Inches(0.92),
            kit.CARD_W - Inches(0.32),
            Inches(0.75),
            size=13,
            color=kit.MUTED,
            name=f"StepBody {num}",
        )
    gap = Inches(0.28)
    width = Emu(int((kit.CONTENT_W - gap) / 2))
    y = Inches(3.85)
    height = Inches(2.95)
    kit.panel(slide, kit.MARGIN, y, width, height, name="Option I")
    kit.text(
        slide,
        "I    Shared embedding",
        kit.MARGIN + Inches(0.24),
        y + Inches(0.2),
        width - Inches(0.48),
        Inches(0.36),
        size=18,
        color=kit.ORANGE,
        bold=True,
        name="OptionITitle",
    )
    kit.text(
        slide,
        "\n".join(
            [
                "Raw and LL go into one embedding.",
                "A VAE. The board has a question",
                "mark on this one.",
                "",
                "Done when a raw window finds",
                "its own LL window in that space.",
            ]
        ),
        kit.MARGIN + Inches(0.24),
        y + Inches(0.7),
        width - Inches(0.48),
        Inches(2.0),
        size=15,
        name="OptionIBody",
    )
    right = kit.MARGIN + width + gap
    kit.panel(slide, right, y, width, height, name="Option II")
    kit.text(
        slide,
        "II   Distillation",
        right + Inches(0.24),
        y + Inches(0.2),
        width - Inches(0.48),
        Inches(0.36),
        size=18,
        color=kit.ORANGE,
        bold=True,
        name="OptionIITitle",
    )
    kit.text(
        slide,
        "\n".join(
            [
                "The LL model teaches a raw model.",
                "Suggested first: the LL model",
                "already ships in production.",
                "",
                "Done when the raw model matches",
                "it on eval, with no beamforming.",
            ]
        ),
        right + Inches(0.24),
        y + Inches(0.7),
        width - Inches(0.48),
        Inches(2.0),
        size=15,
        name="OptionIIBody",
    )
    _footer(slide, "2")


def slide_catalog(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "The catalog today")
    train = facts["by_split"]["train"]
    ev = facts["by_split"]["eval"]
    height, width = facts["hw"]
    kit.text(
        slide,
        (
            f"Mode {facts['mode']}, {facts['codec'].upper()}, {height} by {width}.  "
            f"Train {train['rows']:,} ({train['positives']:,} positives).  "
            f"Eval {ev['rows']:,} ({ev['positives']:,} positives)."
        ),
        kit.MARGIN,
        Inches(1.7),
        kit.CONTENT_W,
        Inches(0.36),
        size=15,
        name="CatalogLine",
    )
    rows = [("Inspection", "Volumes", "Clips", "Frames", "Share")]
    ordered = sorted(facts["inspections"].items(), key=lambda item: -item[1]["volumes"])
    for inspection_id, info in ordered:
        share = 100.0 * info["volumes"] / facts["volumes"]
        rows.append(
            (
                inspection_id,
                f"{int(info['volumes']):,}",
                f"{int(info['clips']):,}",
                f"{int(info['frames']):,}",
                f"{share:.1f}%",
            )
        )
    kit.table(
        slide,
        rows,
        kit.MARGIN,
        Inches(2.25),
        kit.CONTENT_W,
        col_ratios=[26, 16, 12, 20, 12],
        row_h=Inches(0.48),
    )
    kit.text(
        slide,
        (
            f"Median window {facts['median']:.0f} frames, "
            f"mean {facts['mean']:.0f}, longest {facts['max_frames']:,}.  "
            f"LL is {facts['kb_per_frame']:.1f} kB per frame."
        ),
        kit.MARGIN,
        Inches(4.85),
        kit.CONTENT_W,
        Inches(0.4),
        size=15,
        color=kit.MUTED,
        name="WindowLine",
    )
    kit.text(
        slide,
        (
            f"{facts['dataset']:,} dataset rows, {facts['volumes']:,} volumes, "
            f"{facts['positives']:,} positives."
        ),
        kit.MARGIN,
        Inches(5.45),
        kit.CONTENT_W,
        Inches(0.8),
        size=15,
        name="RowNote",
    )
    _footer(slide, "3")


def slide_subset(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "30% and 10k differ")
    cards = [
        (
            f"{facts['thirty']['count']:,}",
            "30% of the volumes",
            f"{facts['thirty']['raw_gib']:.0f} GiB of raw\n"
            f"{_hours(facts['thirty'])} on 1 worker",
        ),
        (
            "10,000",
            f"{facts['tenk_pct']:.0f}% of the volumes",
            f"{facts['tenk']['raw_gib']:.0f} GiB of raw\n"
            f"{_hours(facts['tenk'])} on 1 worker",
        ),
        (
            f"{facts['kept_count']:,}",
            "if every positive is kept",
            f"all {facts['positives']:,} positives\nplus 30% of the negatives",
        ),
    ]
    gap = Inches(0.28)
    width = Emu(int((kit.CONTENT_W - 2 * gap) / 3))
    for index, (value, label, body) in enumerate(cards):
        left = kit.MARGIN + Emu(int((width + gap) * index))
        kit.panel(slide, left, Inches(1.8), width, Inches(2.55), name=f"Choice {index}")
        kit.text(
            slide,
            value,
            left + Inches(0.22),
            Inches(1.98),
            width - Inches(0.44),
            Inches(0.7),
            size=32,
            color=kit.ORANGE,
            bold=True,
            name=f"ChoiceValue {index}",
        )
        kit.text(
            slide,
            label,
            left + Inches(0.22),
            Inches(2.72),
            width - Inches(0.44),
            Inches(0.4),
            size=15,
            color=kit.TEXT,
            bold=True,
            name=f"ChoiceLabel {index}",
        )
        kit.text(
            slide,
            body,
            left + Inches(0.22),
            Inches(3.22),
            width - Inches(0.44),
            Inches(0.9),
            size=14,
            color=kit.MUTED,
            name=f"ChoiceBody {index}",
        )
    kit.text(
        slide,
        "\n".join(
            [
                "The whiteboard writes both figures. They are not the same cut.",
                "",
                f"A flat 30% keeps about {facts['flat_positives']:,} positives,",
                f"and about {facts['flat_eval_positives']} of the "
                f"{facts['by_split']['eval']['positives']} in eval.",
                "Keeping every positive, plus 30% of the negatives,",
                f"is {facts['kept_count']:,} samples. "
                f"That is {facts['kept_pct']:.0f}% of the dataset.",
            ]
        ),
        kit.MARGIN,
        Inches(4.6),
        kit.CONTENT_W,
        Inches(2.1),
        size=16,
        name="SubsetNote",
    )
    _footer(slide, "4")


def slide_estimate(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "How long the prep takes")
    kit.text(
        slide,
        "Volume counts are measured. Raw size and read speed are assumed. See the next slide.",
        kit.MARGIN,
        Inches(1.68),
        kit.CONTENT_W,
        Inches(0.34),
        size=14,
        color=kit.MUTED,
        name="EstimateCaveat",
    )
    rows = [
        ("Subset", "Volumes", "Raw", "One worker", "Eight workers"),
        (
            "30%",
            f"{facts['thirty']['count']:,}",
            f"{facts['thirty']['raw_gib']:.0f} GiB",
            _hours(facts["thirty"]),
            _workers(facts["thirty"], 8),
        ),
        (
            "10,000",
            "10,000",
            f"{facts['tenk']['raw_gib']:.0f} GiB",
            _hours(facts["tenk"]),
            _workers(facts["tenk"], 8),
        ),
        (
            "Full catalog",
            f"{facts['full']['count']:,}",
            f"{facts['full']['raw_gib']:.0f} GiB",
            _hours(facts["full"]),
            _workers(facts["full"], 8),
        ),
    ]
    bottom = kit.table(
        slide,
        rows,
        kit.MARGIN,
        Inches(2.15),
        kit.CONTENT_W,
        col_ratios=[18, 16, 14, 24, 24],
        row_h=Inches(0.5),
    )
    kit.text(
        slide,
        "\n".join(
            [
                f"A mean window is {facts['mean']:.0f} frames and touches "
                f"{facts['chunks']:.2f} chunks,",
                f"so about {facts['downloaded']:.0f} frames are downloaded to keep "
                f"{facts['mean']:.0f}. Time follows the volume count.",
                "",
                f"Copying the LL and uploading the raw, for the 30% cut, is "
                f"{facts['thirty']['io_fast_min']:.0f} to "
                f"{facts['thirty']['io_slow_min']:.0f} min",
                f"at an assumed {S3_MB_PER_S[0]:.0f} to {S3_MB_PER_S[1]:.0f} MB/s. "
                f"The LL copy itself is only {facts['thirty']['ll_gb']:.1f} GB.",
                "",
                f"Uncompressed raw is about {facts['raw_over_ll']:.0f}x the LL bytes "
                "for the same frames.",
            ]
        ),
        kit.MARGIN,
        bottom + Inches(0.28),
        kit.CONTENT_W,
        Inches(2.2),
        size=16,
        name="EstimateNotes",
    )
    _footer(slide, "5")


def slide_assumed(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "These numbers are assumed")
    rows = [
        ("Input", "Value used", "Where it comes from"),
        ("Raw per frame", f"{RAW_KIB_PER_FRAME:.0f} KiB", "Prefetch report, 1,024-frame span"),
        ("Chunk length", f"{CHUNK_FRAMES} frames", "Same report: 1,024 is two chunks"),
        ("Slow download", f"{DOWNLOAD_FPS[0]:.0f} frames/s", "Pod rate the prefetcher beat. A floor"),
        ("Fast download", f"{DOWNLOAD_FPS[1]:.0f} frames/s", "A guess"),
        ("Per volume", f"{OVERHEAD_S[1]:.1f} to {OVERHEAD_S[0]:.1f} s", "A guess, added to the download"),
        ("S3 rate", f"{S3_MB_PER_S[0]:.0f} to {S3_MB_PER_S[1]:.0f} MB/s", "A guess"),
    ]
    bottom = kit.table(
        slide,
        rows,
        kit.MARGIN,
        Inches(1.75),
        kit.CONTENT_W,
        col_ratios=[22, 24, 48],
        row_h=Inches(0.42),
    )
    kit.text(
        slide,
        "\n".join(
            [
                "Not measured yet. A script reads 16 real windows, 4 per inspection,",
                "and reports KiB per frame, cold seconds, and how well raw compresses.",
                "It needs an ilipy login, so it has not been run. A laptop is slower",
                "than a pod, so those seconds are an upper bound.",
                "",
                "The catalog timestamps cannot fill this in. All "
                f"{facts['volumes']:,} created_at values",
                "fall in the same half-second. That is a migration stamp.",
            ]
        ),
        kit.MARGIN,
        bottom + Inches(0.25),
        kit.CONTENT_W,
        Inches(2.0),
        size=15,
        name="AssumedNote",
    )
    _footer(slide, "6")


def slide_decisions(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "Decide these four first")
    items = [
        (
            "D1    What raw means",
            "RF frames, the input beamforming reads. A raw model can then skip it.",
            "IQ is the other raw form. Complex beamform (ll_cx) is still beamformed.",
        ),
        (
            "D2    How many",
            f"30% is {facts['thirty']['count']:,} volumes. "
            f"10,000 volumes is {100 * 10000 / facts['volumes']:.0f}% of the catalog.",
            f"Keeping every positive plus 30% of the negatives is {facts['kept_count']:,} samples.",
        ),
        (
            "D3    How the subset is drawn",
            "By inspection and anomaly type, fixed seed, so a second run matches the first.",
            f"A flat 30% keeps about {facts['flat_positives']:,} positives, "
            f"about {facts['flat_eval_positives']} of them in eval.",
        ),
        (
            "D4    How raw is stored",
            "Uncompressed, or lossless. Decide once the 16-window run reports compression.",
            "Train JPEG 2000 is lossy and is a 441 by 201 image, so it does not fit raw.",
        ),
    ]
    top = Inches(1.72)
    height = Inches(1.2)
    gap = Inches(0.1)
    for index, (head, line_a, line_b) in enumerate(items):
        y = top + Emu(int((height + gap) * index))
        kit.panel(slide, kit.MARGIN, y, kit.CONTENT_W, height, name=f"Decision {index}")
        kit.text(
            slide,
            head,
            kit.MARGIN + Inches(0.22),
            y + Inches(0.1),
            kit.CONTENT_W - Inches(0.44),
            Inches(0.3),
            size=14,
            color=kit.ORANGE,
            bold=True,
            name=f"DecisionHead {index}",
        )
        kit.text(
            slide,
            f"{line_a}\n{line_b}",
            kit.MARGIN + Inches(0.22),
            y + Inches(0.44),
            kit.CONTENT_W - Inches(0.44),
            Inches(0.68),
            size=14,
            name=f"DecisionBody {index}",
        )
    _footer(slide, "7")


def slide_order(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "Order of work")
    ev = facts["by_split"]["eval"]
    rows = [
        ("#", "Step", "Finished when"),
        ("0", "Time 16 raw windows", "KiB per frame and seconds replace the assumptions"),
        ("1", "Write the subset manifest", "A second run writes the same file. Splits stay"),
        ("2", "Copy those LL volumes", "20 volumes are byte-identical to the source"),
        ("3", "Write the raw windows", "A 50-volume trial, then the rest. Resumable"),
        ("4", "Check every pair", "Stored raw matches a fresh chunk-server read"),
        ("5", "Paired loader", "One item is LL, raw, and the label, same frames"),
        (
            "6",
            "Distill raw from the LL model",
            f"Student vs teacher on the {ev['rows']:,} eval samples",
        ),
    ]
    bottom = kit.table(
        slide,
        rows,
        kit.MARGIN,
        Inches(1.72),
        kit.CONTENT_W,
        col_ratios=[6, 40, 54],
        row_h=Inches(0.44),
    )
    kit.text(
        slide,
        "\n".join(
            [
                "Code goes in od_corrosion_detection, on a new branch off master.",
                "Raw gets its own table, keyed by the LL volume id. That id hashes",
                "the beamform mode, so a raw mode would not join back to its LL row.",
            ]
        ),
        kit.MARGIN,
        bottom + Inches(0.2),
        kit.CONTENT_W,
        Inches(1.05),
        size=14,
        color=kit.MUTED,
        name="OrderNote",
    )
    _footer(slide, "8")


def slide_ask(prs, facts) -> None:
    slide = kit.blank(prs)
    kit.title(slide, "Limits, and the ask")
    cards = [
        (
            "No shared pixels",
            "Raw is channel by time.\nLL is angle by radius.\nBoxes do not carry over.\nFrame labels do.",
        ),
        (
            "Raw is the bulk",
            f"About {facts['raw_over_ll']:.0f}x the LL, per frame.\n"
            f"30% is about {facts['thirty']['raw_gib']:.0f} GiB raw.\n"
            "Uncompressed, and unmeasured.\nThis sets storage and loading.",
        ),
        (
            "Eval is small",
            f"{facts['by_split']['eval']['positives']} positives in eval.\n"
            f"A flat 30% leaves about {facts['flat_eval_positives']}.\n"
            "Keeping every positive\navoids that cut.",
        ),
        (
            "Train LL is lossy",
            "Only eval has lossless LL.\nTrain LL is lossy JPEG 2000.\nA student of the train set\ninherits that loss.",
        ),
    ]
    gap = Inches(0.22)
    width = Emu(int((kit.CONTENT_W - gap) / 2))
    height = Inches(2.05)
    for index, (head, body) in enumerate(cards):
        col = index % 2
        row = index // 2
        left = kit.MARGIN + Emu(int((width + gap) * col))
        top = Inches(1.72) + Emu(int((height + gap) * row))
        kit.panel(slide, left, top, width, height, name=f"Risk {index}")
        kit.text(
            slide,
            head,
            left + Inches(0.22),
            top + Inches(0.14),
            width - Inches(0.44),
            Inches(0.34),
            size=16,
            color=kit.ORANGE,
            bold=True,
            name=f"RiskHead {index}",
        )
        kit.text(
            slide,
            body,
            left + Inches(0.22),
            top + Inches(0.54),
            width - Inches(0.44),
            Inches(1.4),
            size=14,
            name=f"RiskBody {index}",
        )
    _footer(slide, "9")


def main() -> None:
    facts = _load()
    prs = kit.new_presentation()
    slide_title(prs, facts)
    slide_proposal(prs, facts)
    slide_catalog(prs, facts)
    slide_subset(prs, facts)
    slide_estimate(prs, facts)
    slide_assumed(prs, facts)
    slide_decisions(prs, facts)
    slide_order(prs, facts)
    slide_ask(prs, facts)
    prs.save(OUT)
    thirty = facts["thirty"]
    print(f"wrote {OUT}")
    print(
        f"volumes={facts['volumes']} frames={facts['frames']} "
        f"positives={facts['positives']} clips={facts['clips']} "
        f"ll_gb={facts['ll_gb']:.2f} mean_frames={facts['mean']:.2f}"
    )
    print(
        f"30%={thirty['count']} raw_gib={thirty['raw_gib']:.1f} "
        f"one_worker={_hours(thirty)} eight={_workers(thirty, 8)} "
        f"downloaded_frames={facts['downloaded']:.1f} "
        f"raw_over_ll={facts['raw_over_ll']:.2f} kept={facts['kept_count']}"
    )


if __name__ == "__main__":
    main()
