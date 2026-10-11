"""Taker fee models. Formulas and parameters come from config.yaml and per-market metadata.

See docs/api_notes.md §1 (Kalshi) and §2 (Polymarket) for sources.
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from pm_edge.config import Fees
from pm_edge.models import FeeParams, Venue

CONTRACT_STEP = Decimal("0.01")


def _d(x: float) -> Decimal:
    """Decimal from a float's shortest repr, so 0.07 is exactly 0.07 (avoids 1.7500000000000002).

    ``float()`` first: numpy 2 scalars repr as ``np.float64(0.07)``.
    """
    return Decimal(repr(float(x)))


def kalshi_taker_fee(price: float, contracts: float, multiplier: float, rate: float, increment: float) -> float:
    """Kalshi taker fee for ONE order: round_up(M * rate * C * P * (1 - P)) to ``increment`` dollars."""
    _check_price(price)
    p = _d(price)
    raw = _d(multiplier) * _d(rate) * _d(contracts) * p * (1 - p)
    if raw <= 0:
        return 0.0
    inc = _d(increment)
    return float((raw / inc).to_integral_value(rounding=ROUND_CEILING) * inc)


def polymarket_taker_fee(price: float, shares: float, rate: float, exponent: float, decimals: int) -> float:
    """Polymarket taker fee for ONE order: C * rate * (p * (1 - p)) ** exponent, rounded to ``decimals``."""
    _check_price(price)
    p = _d(price)
    base = p * (1 - p)
    curve = base ** int(exponent) if float(exponent).is_integer() else _d(float(base) ** exponent)
    raw = _d(shares) * _d(rate) * curve
    return float(raw.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


def size_for_notional(notional_usd: float, price: float) -> float:
    """Contracts/shares purchasable for ``notional_usd`` at ``price`` (before fees), floored to 0.01.

    Kalshi contracts are 0.01-granular; Polymarket sizes are quoted to 0.01 in books.
    """
    _check_price(price)
    if price <= 0:
        return 0.0
    raw = _d(notional_usd) / _d(price)
    return float(raw.quantize(CONTRACT_STEP, rounding=ROUND_FLOOR))


class FeeModel:
    """Applies config-driven fee formulas to a single order (leg)."""

    def __init__(self, fees: Fees) -> None:
        self.fees = fees

    def default_params(self, venue: Venue) -> FeeParams:
        """Fee parameters to use when a market does not publish its own."""
        if venue is Venue.KALSHI:
            k = self.fees.kalshi
            return FeeParams(rate=k.taker_rate, multiplier=k.default_multiplier)
        if venue is Venue.POLYMARKET:
            p = self.fees.polymarket
            return FeeParams(rate=p.default_taker_rate, exponent=p.default_exponent)
        return FeeParams(rate=0.0)

    def taker_fee(self, venue: Venue, price: float, size: float, params: FeeParams) -> float:
        """Fee in dollars for one taker order of ``size`` contracts at ``price``."""
        if size <= 0:
            return 0.0
        if venue is Venue.KALSHI:
            k = self.fees.kalshi
            return kalshi_taker_fee(price, size, params.multiplier, params.rate, k.rounding_increment_usd)
        if venue is Venue.POLYMARKET:
            p = self.fees.polymarket
            return polymarket_taker_fee(price, size, params.rate, params.exponent, p.rounding_decimals)
        raise ValueError(f"No taker fee model for venue {venue}")

    def leg_cost(self, venue: Venue, price: float, size: float, params: FeeParams) -> float:
        """Total dollars paid to buy ``size`` contracts at ``price``, including the taker fee."""
        return price * size + self.taker_fee(venue, price, size, params)

    def fee_per_contract(self, venue: Venue, price: float, size: float, params: FeeParams) -> float:
        """Effective fee per contract at a given order size (captures per-order rounding)."""
        if size <= 0:
            return math.nan
        return self.taker_fee(venue, price, size, params) / size


def _check_price(price: float) -> None:
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be in [0, 1], got {price}")
