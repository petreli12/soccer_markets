from __future__ import annotations

import pytest

from pm_edge.config import Config
from pm_edge.fees import FeeModel, kalshi_taker_fee, polymarket_taker_fee, size_for_notional
from pm_edge.models import FeeParams, Venue


# Kalshi fee schedule table (7 Jul 2026): fee for 1 and for 100 contracts, M = 1.
@pytest.mark.parametrize(
    ("price", "fee_1", "fee_100"),
    [(0.01, 0.01, 0.07), (0.10, 0.01, 0.63), (0.25, 0.02, 1.32), (0.50, 0.02, 1.75), (0.90, 0.01, 0.63), (0.99, 0.01, 0.07)],
)
def test_kalshi_matches_published_table(price: float, fee_1: float, fee_100: float) -> None:
    assert kalshi_taker_fee(price, 1, 1.0, 0.07, 0.01) == pytest.approx(fee_1)
    assert kalshi_taker_fee(price, 100, 1.0, 0.07, 0.01) == pytest.approx(fee_100)


def test_kalshi_rounding_is_per_order_and_upward() -> None:
    # 3 contracts at 0.50: raw 0.0525 -> 0.06 as one order, but 3 separate orders cost 3 * 0.02.
    assert kalshi_taker_fee(0.50, 3, 1.0, 0.07, 0.01) == pytest.approx(0.06)
    assert 3 * kalshi_taker_fee(0.50, 1, 1.0, 0.07, 0.01) == pytest.approx(0.06)
    assert kalshi_taker_fee(0.50, 10, 1.0, 0.07, 0.01) == pytest.approx(0.18)  # raw 0.175
    # Centicent increment (direct members) keeps more precision.
    assert kalshi_taker_fee(0.50, 10, 1.0, 0.07, 0.0001) == pytest.approx(0.175)


def test_kalshi_multiplier_scales_fee() -> None:
    assert kalshi_taker_fee(0.50, 100, 0.5, 0.07, 0.01) == pytest.approx(0.88)  # raw 0.875


def test_kalshi_exact_multiple_not_bumped() -> None:
    # 0.07 * 100 * 0.2 * 0.8 = 1.12 exactly; float noise must not round it up to 1.13.
    assert kalshi_taker_fee(0.20, 100, 1.0, 0.07, 0.01) == pytest.approx(1.12)


# Polymarket sports table (rate 0.05), 100 shares.
@pytest.mark.parametrize(("price", "fee"), [(0.10, 0.45), (0.30, 1.05), (0.50, 1.25), (0.70, 1.05), (0.99, 0.0495)])
def test_polymarket_sports_table(price: float, fee: float) -> None:
    assert polymarket_taker_fee(price, 100, 0.05, 1.0, 5) == pytest.approx(fee)


def test_polymarket_rounds_to_5dp_and_tiny_fees_vanish() -> None:
    assert polymarket_taker_fee(0.05, 100, 0.05, 1.0, 5) == pytest.approx(0.2375)
    assert polymarket_taker_fee(0.999, 0.01, 0.05, 1.0, 5) == 0.0


def test_polymarket_exponent_generalisation() -> None:
    assert polymarket_taker_fee(0.50, 100, 0.05, 2.0, 5) == pytest.approx(100 * 0.05 * 0.25**2)


def test_size_for_notional_floors_to_cents_of_a_contract() -> None:
    assert size_for_notional(100, 0.33) == pytest.approx(303.03)
    assert size_for_notional(100, 0.50) == pytest.approx(200.0)


def test_fee_model_uses_config(cfg: Config) -> None:
    fm = FeeModel(cfg.fees)
    k = fm.default_params(Venue.KALSHI)
    p = fm.default_params(Venue.POLYMARKET)
    assert fm.taker_fee(Venue.KALSHI, 0.5, 100, k) == pytest.approx(1.75)
    assert fm.taker_fee(Venue.POLYMARKET, 0.5, 100, p) == pytest.approx(1.25)
    assert fm.leg_cost(Venue.KALSHI, 0.5, 100, k) == pytest.approx(51.75)
    assert fm.taker_fee(Venue.KALSHI, 0.5, 0, k) == 0.0


def test_price_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        kalshi_taker_fee(1.2, 1, 1.0, 0.07, 0.01)
    with pytest.raises(ValueError):
        polymarket_taker_fee(-0.1, 1, 0.05, 1.0, 5)


def test_sportsbook_has_no_taker_fee_model(cfg: Config) -> None:
    with pytest.raises(ValueError):
        FeeModel(cfg.fees).taker_fee(Venue.SPORTSBOOK, 0.5, 1, FeeParams(rate=0))
