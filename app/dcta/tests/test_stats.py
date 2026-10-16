"""The interval and aggregation helpers the eval report is built on."""
import pytest

from dcta.eval.stats import mean_ci95, mean_sd, pass_k, rate_per_trial, wilson_ci


def test_wilson_empty_sample():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_matches_known_value():
    lo, hi = wilson_ci(18, 20)
    assert lo == pytest.approx(0.699, abs=1e-3)
    assert hi == pytest.approx(0.972, abs=1e-3)


def test_wilson_perfect_score_is_not_a_zero_width_interval():
    """20/20 does not mean 100%: the normal approximation would report [1, 1]."""
    lo, hi = wilson_ci(20, 20)
    assert hi == pytest.approx(1.0)
    assert 0.8 < lo < 0.9


def test_wilson_stays_inside_the_unit_interval():
    for s, n in [(0, 5), (5, 5), (1, 3)]:
        lo, hi = wilson_ci(s, n)
        assert 0.0 <= lo <= hi <= 1.0


def test_mean_ci_collapses_when_undefined():
    assert mean_ci95([]) == (0.0, 0.0, 0.0)
    assert mean_ci95([4.2]) == (4.2, 4.2, 4.2)


def test_mean_ci_brackets_the_mean():
    mu, lo, hi = mean_ci95([1.0, 2.0, 3.0, 4.0])
    assert mu == 2.5
    assert lo < mu < hi


def test_mean_sd():
    assert mean_sd([]) == (0.0, 0.0)
    assert mean_sd([0.5]) == (0.5, 0.0)
    mu, sd = mean_sd([0.8, 1.0])
    assert mu == pytest.approx(0.9) and sd > 0


_ROWS = [
    {"case_id": "A", "trial": 1, "success": 1},
    {"case_id": "B", "trial": 1, "success": 1},
    {"case_id": "A", "trial": 2, "success": 1},
    {"case_id": "B", "trial": 2, "success": 0},
]


def test_rate_is_taken_per_trial():
    assert rate_per_trial(_ROWS, "success", trials=2) == [1.0, 0.5]


def test_pass_k_requires_every_trial():
    """B passed once out of twice: a 75% row-level rate, but only half the cases
    are dependable."""
    assert pass_k(_ROWS) == 0.5
    assert pass_k([]) == 0.0
