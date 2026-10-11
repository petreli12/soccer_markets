from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from pm_edge.collector_lite import ParquetBuffer, ScheduledMatch, book_row
from pm_edge.models import BinaryBook, Outcome, PriceLevel, Venue


def _book() -> BinaryBook:
    lv = lambda *ps: [PriceLevel(price=p, size=10 * (i + 1)) for i, p in enumerate(ps)]  # noqa: E731
    return BinaryBook(venue=Venue.POLYMARKET, market_id="0xabc", ts_utc=datetime(2026, 10, 12, 16, tzinfo=UTC),
                      yes_bids=lv(0.40, 0.39), yes_asks=lv(0.41, 0.42, 0.43, 0.44), no_bids=lv(0.59), no_asks=lv(0.60),
                      last_trade_price=0.405, min_order_size=5, tick_size=0.001)


def test_book_row_levels_and_extras() -> None:
    ko = datetime(2026, 10, 12, 19, tzinfo=UTC)
    m = ScheduledMatch("EPL_2026-10-12_coventry-city_newcastle-united", "EPL", ko, "Coventry City", "Newcastle United", {})
    ts = ko - timedelta(minutes=90)
    r = book_row(ts, ts, m, Outcome.HOME, _book(), depth=3)
    assert r["minutes_to_kickoff"] == 90
    assert (r["best_yes_bid"], r["best_yes_ask"], r["best_no_ask"]) == (0.40, 0.41, 0.60)
    assert r["yes_ask_px_3"] == 0.43 and "yes_ask_px_4" not in r
    assert r["yes_bid_px_3"] is None and r["yes_bid_sz_2"] == 20
    assert r["midpoint"] == 0.405 and r["last_trade_price"] == 0.405


def test_buffer_partitions_by_venue_and_date(tmp_path: Path) -> None:
    buf = ParquetBuffer(tmp_path)
    t1 = datetime(2026, 10, 12, 23, 59, 50, tzinfo=UTC)
    buf.add("kalshi", [{"ts_poll": t1, "x": 1}, {"ts_poll": t1 + timedelta(seconds=20), "x": 2}])
    assert buf.pending() == 2
    assert buf.flush() == 2 and buf.pending() == 0
    files = sorted(p.relative_to(tmp_path).parts[:2] for p in tmp_path.rglob("*.parquet"))
    assert files == [("venue=kalshi", "date=2026-10-12"), ("venue=kalshi", "date=2026-10-13")]
    assert len(pd.read_parquet(tmp_path / "venue=kalshi")) == 2
