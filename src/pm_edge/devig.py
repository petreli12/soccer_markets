"""Remove the bookmaker margin from decimal odds.

Power method: find k with sum((1/o_i) ** k) = 1 and use p_i = (1/o_i) ** k. It shades longshots more
than favourites, which fits soccer 1X2 better than proportional scaling. If the solve fails, fall back
to multiplicative: p_i = (1/o_i) / sum(1/o_j).
"""

from __future__ import annotations

from collections.abc import Sequence

from pm_edge.config import Devig


def implied(odds: Sequence[float]) -> list[float]:
    if any(o <= 1.0 for o in odds):
        raise ValueError(f"decimal odds must be > 1, got {list(odds)}")
    return [1.0 / o for o in odds]


def multiplicative(odds: Sequence[float]) -> list[float]:
    q = implied(odds)
    s = sum(q)
    return [x / s for x in q]


def power(odds: Sequence[float], max_iter: int = 100, tol: float = 1e-10) -> list[float] | None:
    """Power de-vig by bisection on k; ``None`` if it does not converge."""
    q = implied(odds)
    if abs(sum(q) - 1.0) < tol:
        return q

    def total(k: float) -> float:
        return sum(x**k for x in q)

    # total(k) is decreasing in k because every q < 1; overround (>1) needs k > 1, underround k < 1.
    lo, hi = (1.0, 2.0) if sum(q) > 1 else (0.5, 1.0)
    while total(hi) > 1.0:
        hi *= 2
        if hi > 1e3:
            return None
    while total(lo) < 1.0:
        lo /= 2
        if lo < 1e-3:
            return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            k = (lo + hi) / 2
            return [x**k for x in q]
    return None


def devig(odds: Sequence[float], cfg: Devig) -> tuple[list[float], str]:
    """De-vigged probabilities and the method actually used."""
    if cfg.method == "power":
        p = power(odds, cfg.power_max_iter, cfg.power_tolerance)
        if p is not None:
            return p, "power"
        if cfg.fallback != "multiplicative":
            raise ValueError("power de-vig failed and no multiplicative fallback configured")
    return multiplicative(odds), "multiplicative"
