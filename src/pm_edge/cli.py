"""pm-edge command line interface."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import typer

from pm_edge.config import Config, load_config
from pm_edge.ingest import kalshi

app = typer.Typer(no_args_is_help=True, help="Read-only research on prediction-market vs sportsbook pricing gaps.")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def backfill(
    league: str = typer.Option(..., help="League key from config.yaml, e.g. EPL"),
    days: int = typer.Option(60, help="Look back this many days of kickoffs"),
) -> None:
    """Backfill historical data for finished matches. Kalshi only until later build steps add other venues."""
    cfg = load_config()
    summary = asyncio.run(kalshi.backfill(cfg, league, days))
    typer.echo(json.dumps({"kalshi": summary}, indent=2))


@app.command("verify-kalshi-gaps")
def verify_kalshi_gaps(
    league: str = typer.Option("EPL"),
    matches: int = typer.Option(4, help="Number of recent settled matches to test"),
) -> None:
    """Re-test that a missing Kalshi 1-min candle means no book change and no trades."""
    cfg = load_config()
    typer.echo(json.dumps(asyncio.run(_verify_gaps(cfg, league, matches)), indent=2))


async def _verify_gaps(cfg: Config, league: str, n: int) -> dict[str, int]:
    total: dict[str, int] = {}
    now = datetime.now(UTC)
    async with kalshi.KalshiIngest(cfg) as k:
        found, _ = await k.list_matches(league, statuses=("settled",), kickoff_from=now - timedelta(days=30))
        for m in found[-n:]:
            start = m.kickoff_utc - timedelta(hours=cfg.normalize.window_before_kickoff_hours)
            end = m.kickoff_utc + timedelta(hours=cfg.normalize.window_after_kickoff_hours)
            for c in m.contracts.values():
                candles = await k.fetch_candles(m.extra["series_ticker"], c.market_id, start, end, archived=False)
                trades = await k.fetch_trade_times(c.market_id, start, end)
                for key, val in kalshi.candle_gap_report(candles, trades).items():
                    total[key] = total.get(key, 0) + val
    return total


if __name__ == "__main__":
    app()
