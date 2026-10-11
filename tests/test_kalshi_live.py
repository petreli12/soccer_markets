"""Live test: pull one real, finished EPL match from Kalshi's public API (skip with -m "not live")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm_edge.config import Config
from pm_edge.ingest.kalshi import KalshiIngest
from pm_edge.models import Outcome

pytestmark = pytest.mark.live


async def test_pull_one_epl_match(cfg: Config) -> None:
    now = datetime.now(UTC)
    async with KalshiIngest(cfg) as k:
        matches, _ = await k.list_matches("EPL", statuses=("settled",), kickoff_from=now - timedelta(days=14))
        assert matches, "expected at least one settled EPL match in the last 14 days"
        match = matches[-1]

        assert set(match.contracts) == {Outcome.HOME, Outcome.DRAW, Outcome.AWAY}
        assert match.contracts[Outcome.DRAW].market_id.endswith("-TIE")
        assert match.kickoff_utc < now
        # Exactly one outcome settles YES.
        assert sorted(c.result for c in match.contracts.values()) == ["no", "no", "yes"]

        candles = await k.fetch_match_candles(match)
        assert set(candles["outcome"]) == {"HOME", "DRAW", "AWAY"}
        window_start = match.kickoff_utc - timedelta(hours=cfg.normalize.window_before_kickoff_hours)
        window_end = match.kickoff_utc + timedelta(hours=cfg.normalize.window_after_kickoff_hours)
        assert candles["ts"].min() >= window_start and candles["ts"].max() <= window_end
        quoted = candles.dropna(subset=["yes_bid", "yes_ask"])
        assert len(quoted) > 100
        assert (quoted["yes_bid"] <= quoted["yes_ask"]).all()

        # Pre-kickoff, the three YES asks should sum to at least ~$1 (no free money on the mid book).
        pre = quoted[quoted["ts"] < match.kickoff_utc]
        last = pre.sort_values("ts").groupby("outcome").tail(1)
        assert last["yes_ask"].sum() >= 0.97


async def test_orderbook_snapshot_for_open_epl_match(cfg: Config) -> None:
    now = datetime.now(UTC)
    async with KalshiIngest(cfg) as k:
        matches, _ = await k.list_matches("EPL", statuses=("open",), kickoff_from=now)
        assert matches, "expected at least one open EPL match"
        tickers = [c.market_id for c in matches[0].contracts.values()]
        books = await k.fetch_orderbooks(tickers, depth=cfg.collector.orderbook_depth)
        assert {b.market_id for b in books} == set(tickers)
        for b in books:
            assert len(b.yes_asks) <= cfg.collector.orderbook_depth
            if b.best_yes_bid is not None and b.best_yes_ask is not None:
                assert b.best_yes_bid < b.best_yes_ask
