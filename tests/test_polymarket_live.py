"""Live test: pull one real, finished EPL match from Polymarket's public APIs (skip with -m "not live")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pm_edge.config import Config
from pm_edge.ingest.polymarket import PolymarketIngest
from pm_edge.models import Outcome

pytestmark = pytest.mark.live


async def test_pull_one_epl_match(cfg: Config) -> None:
    now = datetime.now(UTC)
    async with PolymarketIngest(cfg) as p:
        matches, unmapped = await p.list_matches("EPL", closed=True, kickoff_from=now - timedelta(days=14), kickoff_to=now)
        assert matches, f"expected a closed EPL match in the last 14 days (unmapped: {unmapped})"
        match = matches[-1]
        assert set(match.contracts) == {Outcome.HOME, Outcome.DRAW, Outcome.AWAY}
        assert sorted(c.result or "" for c in match.contracts.values()) == ["no", "no", "yes"]

        hist = await p.fetch_match_history(match)
        assert set(hist["outcome"]) == {"HOME", "DRAW", "AWAY"}
        assert hist["price"].between(0, 1).all()
        pre = hist[hist["ts"] < match.kickoff_utc].sort_values("ts").groupby("outcome").tail(1)
        assert pre["price"].sum() == pytest.approx(1.0, abs=0.08)


async def test_orderbook_snapshot_for_open_epl_match(cfg: Config) -> None:
    now = datetime.now(UTC)
    async with PolymarketIngest(cfg) as p:
        matches, _ = await p.list_matches("EPL", closed=False, kickoff_from=now)
        assert matches, "expected at least one open EPL match"
        books = await p.fetch_orderbooks(matches[:1], depth=cfg.collector.orderbook_depth)
        assert {b.market_id for b in books} == {c.market_id for c in matches[0].contracts.values()}
        for b in books:
            assert len(b.yes_asks) <= cfg.collector.orderbook_depth
            if b.best_yes_bid is not None and b.best_yes_ask is not None:
                assert b.best_yes_bid < b.best_yes_ask
