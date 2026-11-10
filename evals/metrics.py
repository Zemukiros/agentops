"""Statistics for the eval report. Pure functions, unit-tested."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def wilson(successes: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion (good at small n and near 0/1)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100); None for no data."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def precision_recall(pairs: Sequence[tuple[bool, bool]]) -> dict[str, float | int | None]:
    """pairs of (predicted_defect, actual_defect)."""
    tp = sum(1 for p, a in pairs if p and a)
    fp = sum(1 for p, a in pairs if p and not a)
    fn = sum(1 for p, a in pairs if not p and a)
    tn = sum(1 for p, a in pairs if not p and not a)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall}


def paired_lift(
    pairs: Sequence[tuple[float, float]], *, seed: int = 7, iters: int = 5000
) -> dict[str, float | int | None]:
    """Mean of (on - off) over paired runs, with a seeded bootstrap 95% interval."""
    if not pairs:
        return {"n": 0, "lift": None, "low": None, "high": None}
    diffs = [on - off for on, off in pairs]
    mean = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(iters))
    return {
        "n": len(diffs),
        "lift": mean,
        "low": boots[int(0.025 * iters)],
        "high": boots[int(0.975 * iters) - 1],
    }
