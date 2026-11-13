"""Metric checks for check_prerequisites: exact, 1-LSB, and non-finite cases."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_prerequisites import lsb_error, summarize, to_lsb  # noqa: E402

VMIN, VMAX = 0.0, 1.0
decoded = np.full((2, 3, 3), 1000, dtype=np.uint16)
exact_fresh = np.full((2, 3, 3), 1000 / 65535, dtype=np.float32)

err = lsb_error(decoded, exact_fresh, VMIN, VMAX)
assert err.max() == 0, err.max()
assert summarize(err)["exact_frac"] == 1.0

one_off = exact_fresh.copy()
one_off[0, 0, 0] = (1001) / 65535  # one LSB away
err = lsb_error(decoded, one_off, VMIN, VMAX)
assert err.max() == 1 and summarize(err)["within_1_frac"] == 1.0
assert summarize(err)["exact_frac"] < 1.0

nan_fresh = exact_fresh.copy()
nan_fresh[1, 1, 1] = np.nan
err = lsb_error(decoded, nan_fresh, VMIN, VMAX)
assert err.max() == 65535 and summarize(err)["within_1_frac"] < 1.0

assert to_lsb(np.array([0.0, 1.0]), VMIN, VMAX).tolist() == [0, 65535]
print("test_prereq_metrics: ok")
