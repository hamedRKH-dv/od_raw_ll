"""One-line progress with a rate projection. Stdlib only."""

from __future__ import annotations

import sys
import time


def format_line(
    done: int,
    total: int,
    elapsed_s: float,
    *,
    label: str,
    detail: str = "",
) -> str:
    rate = elapsed_s / done if done else 0.0
    eta = rate * (total - done) if done else 0.0
    width = 20
    filled = int(width * done / total) if total else 0
    bar = "#" * filled + "-" * (width - filled)
    tail = f"  {detail}" if detail else ""
    return f"[{bar}] {done}/{total}  {label}  {elapsed_s:.0f}s  eta {eta:.0f}s{tail}"


class Progress:
    def __init__(self, total: int, label: str) -> None:
        self.total = total
        self.label = label
        self.done = 0
        self.started = time.perf_counter()

    def advance(self, n: int = 1, detail: str = "") -> str:
        self.done += n
        line = format_line(
            self.done,
            self.total,
            time.perf_counter() - self.started,
            label=self.label,
            detail=detail,
        )
        print(line, file=sys.stderr, flush=True)
        return line

    def tick(self, detail: str = "") -> str:
        return self.advance(1, detail)
