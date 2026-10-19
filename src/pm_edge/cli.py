"""pm-edge command line interface."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import typer

from pm_edge.config import Config, load_config
from pm_edge import matching
from pm_edge.ingest import kalshi, polymarket

app = typer.Typer(no_args_is_help=True, help="Read-only research on prediction-market vs sportsbook pricing gaps.")


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def backfill(
    league: str = typer.Option(..., help="League key from config.yaml, e.g. EPL"),
    days: int = typer.Option(60, help="Look back this many days of kickoffs"),
    venue: list[str] = typer.Option(["kalshi", "polymarket"], help="Venues to backfill (repeatable)"),
) -> None:
    """Backfill historical data for finished matches. Sportsbooks are added in a later build step."""
    cfg = load_config()
    runners = {"kalshi": kalshi.backfill, "polymarket": polymarket.backfill}
    unknown = set(venue) - set(runners)
    if unknown:
        raise typer.BadParameter(f"unknown venue(s): {sorted(unknown)}; choose from {sorted(runners)}")
    summary = {v: asyncio.run(runners[v](cfg, league, days)) for v in venue}
    typer.echo(json.dumps(summary, indent=2))


@app.command()
def match(
    league: list[str] = typer.Option(None, help="League(s) to match; default: every enabled league"),
) -> None:
    """Pair ingested events across venues; writes data/processed/matches_league=*.parquet and the review file."""
    cfg = load_config()
    leagues = league or [name for name, lc in cfg.leagues.items() if lc.enabled]
    summary = matching.run(cfg, leagues)
    typer.echo(json.dumps(summary, indent=2, default=str))
    typer.echo(f"review file: {cfg.matching.review_path}")


@app.command("collect-lite")
def collect_lite(
    league: list[str] = typer.Option(None, help="League(s) to collect; default: every enabled league"),
    log_file: str = typer.Option("data/live/collector.log", help="Rotating log file (also logs to stderr)"),
) -> None:
    """Pre-kickoff live snapshots (books every 20 s, Pinnacle every 10 min) for matched matches."""
    from logging.handlers import RotatingFileHandler

    from pm_edge import collector_lite

    cfg = load_config()
    path = cfg.resolve(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    leagues = league or [name for name, lc in cfg.leagues.items() if lc.enabled]
    asyncio.run(collector_lite.run(cfg, leagues))


@app.command()
def fixtures(
    date_from: str = typer.Option(..., "--from", help="UTC date YYYY-MM-DD (kickoff on/after)"),
    date_to: str = typer.Option(..., "--to", help="UTC date YYYY-MM-DD (kickoff on/before, inclusive)"),
    league: list[str] = typer.Option(None, help="League(s); default: every enabled league"),
    pinnacle: bool = typer.Option(True, help="Check Pinnacle h2h availability (1 Odds API credit per league)"),
) -> None:
    """Upcoming fixtures paired across Kalshi + Polymarket, with Pinnacle availability."""
    import pandas as pd

    from pm_edge.collector_lite import CollectorLite

    cfg = load_config()
    leagues = league or [name for name, lc in cfg.leagues.items() if lc.enabled]
    start = pd.Timestamp(date_from, tz="UTC")
    end = pd.Timestamp(date_to, tz="UTC") + pd.Timedelta(days=1)
    hours = max(0.0, (end - pd.Timestamp.now(tz="UTC")).total_seconds() / 3600)
    cfg = cfg.model_copy(update={"collector_lite": cfg.collector_lite.model_copy(
        update={"schedule_lookahead_hours": hours})})

    async def go() -> list[dict]:
        c = CollectorLite(cfg, leagues)
        pin: dict[str, dict] = {}
        try:
            await c.refresh_schedule()
            if pinnacle and c.odds is not None:
                for lg in leagues:
                    await c.poll_odds(lg, [])
                for r in c.buffer.rows.get("pinnacle", []):
                    if r["match_id"]:
                        p = pin.setdefault(r["match_id"], {"outcomes": set(), "last_update": r["last_update"]})
                        p["outcomes"].add(r["outcome"])
        finally:
            await asyncio.gather(c.kalshi.__aexit__(), c.poly.__aexit__(), return_exceptions=True)
            if c.odds is not None:
                await c.odds.__aexit__()
        rows = []
        for m in sorted(c.schedule.values(), key=lambda s: (s.kickoff_utc, s.league, s.match_id)):
            if not start <= pd.Timestamp(m.kickoff_utc) < end:
                continue
            p = pin.get(m.match_id)
            rows.append({"league": m.league, "kickoff_utc": f"{m.kickoff_utc:%Y-%m-%d %H:%M}", "home": m.home,
                         "away": m.away, "kalshi": "kalshi" in m.venues, "polymarket": "polymarket" in m.venues,
                         "pinnacle": bool(p) and len(p["outcomes"]) == 3,
                         "pin_last_update": f"{p['last_update']:%m-%d %H:%M}" if p else "-"})
        return rows

    rows = asyncio.run(go())
    if not rows:
        typer.echo("no paired fixtures in range")
        return
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    typer.echo(df.to_string(index=False))
    typer.echo(f"\n{len(df)} fixtures; all three sources: {int((df.kalshi & df.polymarket & df.pinnacle).sum())}; "
               f"by league {df.groupby('league').size().to_dict()}")


@app.command()
def watchdog(
    stale_minutes: float = typer.Option(None, help="Override collector_lite.alert_stale_minutes"),
    test: bool = typer.Option(False, help="Send one test notification and exit"),
) -> None:
    """One watchdog check of collect-lite output; push ntfy alerts. Run it every minute."""
    from pm_edge.alerts import notify, run_watchdog
    from pm_edge.config import Secrets

    logging.getLogger().setLevel(logging.INFO)
    if test:
        ok = notify(Secrets().ntfy_topic, "pm-edge test", "Test notification from pm-edge watchdog.")
        raise typer.Exit(0 if ok else 1)
    for key, kind, msg in run_watchdog(load_config(), stale_minutes=stale_minutes):
        typer.echo(f"{kind} {key}: {msg}")


@app.command("thin-scan")
def thin_scan(
    league: list[str] = typer.Option(None, help="League(s); default: every enabled league"),
    kickoff_within_hours: float = typer.Option(None, help="Only events kicking off within this many hours from now"),
    spread_min: float = typer.Option(0.03, help="'Wide' means spread above this (dollars)"),
    usd_min: float = typer.Option(500, help="Minimum dollars at the best price"),
    trade_window_hours: float = typer.Option(3, help="Count public trades in this many past hours"),
    top: int = typer.Option(25, help="Rows of wide-and-deep markets to print"),
) -> None:
    """Scan Polymarket non-moneyline soccer markets: spreads, depth, 24h volume, recent trades."""
    import pandas as pd

    from pm_edge import thin_scan as ts

    cfg = load_config()
    leagues = league or [name for name, lc in cfg.leagues.items() if lc.enabled]
    df, summary, path = ts.run(cfg, leagues, kickoff_within_hours, cfg.resolve(cfg.paths.processed_dir) / "thin_scan",
                               spread_min, usd_min, trade_window_hours)
    if df.empty:
        typer.echo("no markets matched")
        return
    tcol = f"trades_{trade_window_hours:g}h"
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    typer.echo(f"{df['event_slug'].nunique()} events, {len(df)} markets {df.groupby('league').size().to_dict()}; "
               f"two-sided {df['two_sided'].mean():.1%}; 24h volume ${df['volume_24h'].sum():,.0f}; "
               f"{tcol} {int(df[tcol].sum())} trades in {int((df[tcol] > 0).sum())} markets")
    typer.echo(summary.round(2).to_string())
    wide = df["spread"] > spread_min + 1e-9
    hits = df[wide & (df["ask_usd"] >= usd_min)].sort_values([tcol, "volume_24h"], ascending=False)
    both = int((hits["bid_usd"] >= usd_min).sum())
    typer.echo(f"\nspread > {spread_min * 100:g}c and >= ${usd_min:g} at best ask: {len(hits)} markets "
               f"(both sides >= ${usd_min:g}: {both})")
    if len(hits):
        typer.echo(hits.head(top)[["league", "event_slug", "question", "best_bid", "best_ask", "spread", "bid_usd",
                                   "ask_usd", "volume_24h", tcol]].round(3).to_string(index=False))
    typer.echo(f"\nfull table: {path}")


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
