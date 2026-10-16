"""Interval and aggregation helpers for the eval harness.

Reporting discipline these encode:
  - **Proportions get a Wilson 95% CI** — the right interval for the small-n,
    near-1 rates a golden set produces.
  - **Continuous metrics get mean ± 1.96·SE**, reported alongside the median,
    since Wilson is undefined off [0, 1] and latency/cost are right-skewed.
  - **Deterministic metrics run n=1.** Repeating a check that returns the same
    answer every time measures nothing; confidence comes from the set's size.
  - **Stochastic metrics run k trials.** Anything with an LLM in the loop varies
    run to run, so a single run is not a result: take the rate per trial, then
    mean ± sd ACROSS trials, and report pass^k next to it.
"""

from __future__ import annotations

import math
import statistics


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion.

    Preferred over the normal approximation for the small-n, near-1 proportions
    an eval golden set produces. Returns (low, high); (0.0, 0.0) for n == 0.
    """
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def mean_ci95(vals: list[float]) -> tuple[float, float, float]:
    """(mean, lo, hi) — normal-approximation 95% CI on the mean of a continuous metric.

    With n<2 the interval is undefined, so it collapses to the point estimate
    rather than inventing a width.

    Report it alongside the median, not instead of it: a tool loop or a retry
    doubles a single case, so the mean is the quantity a CI describes while the
    median is the quantity to headline.
    """
    n = len(vals)
    if n == 0:
        return (0.0, 0.0, 0.0)
    mu = statistics.mean(vals)
    if n < 2:
        return (mu, mu, mu)
    se = statistics.stdev(vals) / math.sqrt(n)
    return (mu, mu - 1.96 * se, mu + 1.96 * se)


def mean_sd(vals: list[float]) -> tuple[float, float]:
    """(mean, sample sd); sd is 0.0 for fewer than two values."""
    if not vals:
        return (0.0, 0.0)
    return (statistics.mean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0)


def rate_per_trial(rows: list[dict], key: str, trials: int) -> list[float]:
    """The rate of `key` (a 0/1 field) within each trial, as a list of length `trials`.

    Averaging every row together would understate the variance: rows within one
    trial share that trial's luck. Rate-per-trial first, then mean/sd over trials,
    is the honest reduction for a stochastic pipeline.
    """
    out = []
    for t in range(1, trials + 1):
        sub = [r for r in rows if r["trial"] == t]
        out.append(sum(r[key] for r in sub) / len(sub) if sub else 0.0)
    return out


def pass_k(rows: list[dict], key: str = "success", id_key: str = "case_id") -> float:
    """pass^k: the share of cases that succeeded in EVERY trial.

    The metric that matters for a bank — an answer that is right 2 times in 3 is
    not deployable.
    """
    by_case: dict[str, list[int]] = {}
    for r in rows:
        by_case.setdefault(r[id_key], []).append(r[key])
    if not by_case:
        return 0.0
    return sum(1 for v in by_case.values() if all(v)) / len(by_case)
