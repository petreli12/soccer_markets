"""Offline tests for Kalshi parsing and outcome mapping, using small synthetic fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from pm_edge.config import Config
from pm_edge.ingest.kalshi import (
    UnmappedEvent,
    candle_gap_report,
    parse_candles,
    parse_event,
    parse_orderbook,
    resolve_fee_params,
)
from pm_edge.models import FeeParams, Outcome, VenueMatch

FEE = FeeParams(rate=0.07, multiplier=1.0)


def _market(suffix: str, label: str, result: str = "") -> dict[str, Any]:
    return {
        "ticker": f"KXEPLGAME-26OCT25MUNBOU-{suffix}",
        "yes_sub_title": label,
        "result": result,
        "expected_expiration_time": "2026-10-25T17:00:00Z",  # kickoff + 3h: must NOT be used
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def _event(**overrides: Any) -> dict[str, Any]:
    ev = {
        "event_ticker": "KXEPLGAME-26OCT25MUNBOU",
        "series_ticker": "KXEPLGAME",
        "title": "Manchester United vs Bournemouth",
        "sub_title": "MUN vs BOU (Oct 25)",
    }
    ev.update(overrides)
    return ev


MILESTONE = {"start_date": "2026-10-25T14:00:00Z", "details": {"status": "not_started"}}
MARKETS = [_market("MUN", "Manchester United"), _market("BOU", "Bournemouth"), _market("TIE", "Tie")]


def test_maps_three_markets_and_uses_milestone_kickoff() -> None:
    m = parse_event(_event(), MARKETS, MILESTONE, "EPL", FEE)
    assert isinstance(m, VenueMatch)
    assert m.home_name == "Manchester United" and m.away_name == "Bournemouth"
    assert m.contracts[Outcome.HOME].market_id.endswith("-MUN")
    assert m.contracts[Outcome.AWAY].market_id.endswith("-BOU")
    assert m.contracts[Outcome.DRAW].market_id.endswith("-TIE")
    assert m.kickoff_utc == datetime(2026, 10, 25, 14, 0, tzinfo=UTC)
    assert m.is_complete(["HOME", "DRAW", "AWAY"])


def test_abbreviation_mapping_works_when_label_is_a_short_name() -> None:
    # Kalshi labels sometimes differ from the title (e.g. "Man Utd"); the ticker suffix still decides.
    markets = [_market("MUN", "Man Utd"), _market("BOU", "Bournemouth"), _market("TIE", "Tie")]
    m = parse_event(_event(), markets, MILESTONE, "EPL", FEE)
    assert isinstance(m, VenueMatch)
    assert m.contracts[Outcome.HOME].label == "Man Utd"


def test_contradicting_suffix_and_label_goes_to_review() -> None:
    markets = [_market("MUN", "Bournemouth"), _market("BOU", "Manchester United"), _market("TIE", "Tie")]
    out = parse_event(_event(), markets, MILESTONE, "EPL", FEE)
    assert isinstance(out, UnmappedEvent)
    assert "cannot map" in out.reason


def test_missing_milestone_is_not_guessed_from_expiration() -> None:
    out = parse_event(_event(), MARKETS, None, "EPL", FEE)
    assert isinstance(out, UnmappedEvent)
    assert "kickoff" in out.reason


def test_bad_title_goes_to_review() -> None:
    out = parse_event(_event(title="Manchester United v. Bournemouth odds"), MARKETS, MILESTONE, "EPL", FEE)
    assert isinstance(out, UnmappedEvent)


def test_duplicate_outcome_goes_to_review() -> None:
    markets = [*MARKETS, _market("TIE", "Tie")]
    out = parse_event(_event(), markets, MILESTONE, "EPL", FEE)
    assert isinstance(out, UnmappedEvent)


def test_event_fee_override_wins(cfg: Config) -> None:
    series = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    assert resolve_fee_params(series, _event(), cfg).multiplier == 1.0
    ev = _event(fee_type_override="quadratic", fee_multiplier_override=0.5)
    assert resolve_fee_params(series, ev, cfg).multiplier == 0.5
    with pytest.raises(ValueError):
        resolve_fee_params({"fee_type": "flat", "fee_multiplier": 1}, _event(), cfg)


def test_orderbook_derives_asks_from_opposite_bids() -> None:
    ob = {
        "yes_dollars": [["0.4900", "185.00"], ["0.5300", "200.00"], ["0.5200", "3147.00"]],
        "no_dollars": [["0.4200", "3647.00"], ["0.4300", "460.84"]],
    }
    book = parse_orderbook("T", ob, datetime(2026, 10, 11, tzinfo=UTC), depth=5)
    assert [lv.price for lv in book.yes_bids] == [0.53, 0.52, 0.49]
    assert book.best_yes_ask == pytest.approx(0.57)  # 1 - best NO bid 0.43
    assert book.yes_asks[0].size == pytest.approx(460.84)
    assert book.best_no_ask == pytest.approx(0.47)  # 1 - best YES bid 0.53
    assert book.midpoint == pytest.approx(0.55)


def test_orderbook_depth_and_empty_side() -> None:
    ob = {"yes_dollars": [[f"0.{i:02d}00", "1.00"] for i in range(1, 9)], "no_dollars": []}
    book = parse_orderbook("T", ob, datetime(2026, 10, 11, tzinfo=UTC), depth=5)
    assert len(book.yes_bids) == 5 and book.yes_bids[0].price == pytest.approx(0.08)
    assert book.yes_asks == [] and book.best_yes_ask is None and book.midpoint is None


def test_candles_live_and_historical_schemas_agree() -> None:
    live = [
        {
            "end_period_ts": 1791682500,
            "yes_bid": {"open_dollars": "0.3000", "close_dollars": "0.3100"},
            "yes_ask": {"open_dollars": "0.3200", "close_dollars": "0.3300"},
            "price": {"close_dollars": None},
            "volume_fp": "5.96",
            "open_interest_fp": "100.00",
        }
    ]
    hist = [
        {
            "end_period_ts": 1791682500,
            "yes_bid": {"open": "0.3000", "close": "0.3100"},
            "yes_ask": {"open": "0.3200", "close": "0.3300"},
            "price": {"close": None},
            "volume": "5.96",
            "open_interest": "100.00",
        }
    ]
    a, b = parse_candles(live, historical=False), parse_candles(hist, historical=True)
    pd.testing.assert_frame_equal(a, b)
    row = a.iloc[0]
    assert row["ts"] == pd.Timestamp(1791682500, unit="s", tz="UTC")
    assert (row["yes_bid"], row["yes_ask"], row["volume"]) == (0.31, 0.33, 5.96)
    assert pd.isna(row["trade_close"])


def _candle(ts: int, bid_open: str, bid: str, ask_open: str, ask: str) -> dict[str, Any]:
    return {
        "end_period_ts": ts,
        "yes_bid": {"open_dollars": bid_open, "close_dollars": bid},
        "yes_ask": {"open_dollars": ask_open, "close_dollars": ask},
        "price": {},
        "volume_fp": "0.00",
        "open_interest_fp": "0.00",
    }


def test_gap_report_counts_unchanged_books_and_trades_in_gaps() -> None:
    t0 = 1_791_000_000
    candles = parse_candles(
        [
            _candle(t0, "0.30", "0.30", "0.32", "0.32"),
            _candle(t0 + 240, "0.30", "0.31", "0.32", "0.32"),  # 3 missing minutes, book unchanged
            _candle(t0 + 360, "0.29", "0.29", "0.33", "0.33"),  # 1 missing minute, book changed
        ],
        historical=False,
    )
    # One trade inside the first gap; one at t0+180.5 belongs to the t0+240 candle window, not the gap.
    report = candle_gap_report(candles, [t0 + 100.0, t0 + 180.5])
    assert report == {"gaps": 2, "missing_minutes": 4, "bid_unchanged": 1, "ask_unchanged": 1, "gaps_with_trades": 1}
