"""Statistical confidence intervals used throughout learning/ and analytics/ -
real implementations (Wilson score, Clopper-Pearson via the beta distribution,
and a generic bootstrap), not approximations."""
import numpy as np
from scipy import stats


def wilson_ci(p_hat: float, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion - better-behaved than the
    naive normal approximation for small n or p near 0/1."""
    if n == 0:
        return 0.0, 1.0
    z = stats.norm.ppf(1 - (1 - confidence) / 2)
    denom = 1 + z ** 2 / n
    center = (p_hat + z ** 2 / (2 * n)) / denom
    half_width = (z * math_sqrt(p_hat * (1 - p_hat) / n + z ** 2 / (4 * n ** 2))) / denom
    return max(0.0, center - half_width), min(1.0, center + half_width)


def math_sqrt(x: float) -> float:
    return x ** 0.5 if x > 0 else 0.0


def clopper_pearson_ci(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Exact binomial CI via the Beta distribution - the most conservative of
    the three, good for small samples (e.g. a new rule with only 12 fires)."""
    if n == 0:
        return 0.0, 1.0
    alpha = 1 - confidence
    lower = 0.0 if successes == 0 else stats.beta.ppf(alpha / 2, successes, n - successes + 1)
    upper = 1.0 if successes == n else stats.beta.ppf(1 - alpha / 2, successes + 1, n - successes)
    return float(lower), float(upper)


def bootstrap_ci(samples: list[float], stat_fn=np.mean, n_resamples: int = 2000,
                  confidence: float = 0.95, seed: int = 42) -> tuple[float, float]:
    """Generic bootstrap CI for any statistic (mean, median, Sharpe, etc.) - use
    when the underlying distribution isn't a simple proportion (e.g. P&L %)."""
    if len(samples) == 0:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    arr = np.array(samples, dtype=float)
    stats_resampled = np.array([
        stat_fn(rng.choice(arr, size=len(arr), replace=True)) for _ in range(n_resamples)
    ])
    alpha = 1 - confidence
    lower = np.percentile(stats_resampled, 100 * alpha / 2)
    upper = np.percentile(stats_resampled, 100 * (1 - alpha / 2))
    return float(lower), float(upper)


def two_proportion_z_test(successes_a: int, n_a: int, successes_b: int, n_b: int) -> dict:
    """Two-proportion z-test - used by champion_challenger.py to decide whether
    the challenger's win rate is statistically different from the champion's."""
    if n_a == 0 or n_b == 0:
        return {"z": 0.0, "p_value": 1.0, "significant": False}
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)
    se = math_sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return {"z": 0.0, "p_value": 1.0, "significant": False}
    z = (p_a - p_b) / se
    p_value = 2 * (1 - stats.norm.cdf(abs(z)))
    return {"z": float(z), "p_value": float(p_value), "significant": p_value < 0.05,
            "p_a": p_a, "p_b": p_b}
