"""Thin-market scan: Polymarket non-moneyline soccer markets (read-only, ``pm-edge thin-scan``).

For every open EPL/UCL Polymarket event, optionally limited to kickoffs within N hours, every
market except the full-time moneyline is scanned: exact score, halftime, corners, totals, spreads,
first to score, player props and so on. Per market, using the first outcome token's book ("Yes",
"Over", or a team for spreads), it reports:

* best bid, best ask, spread, and dollars (price x size) at the best bid and best ask;
* Gamma ``volume24hr``;
* the number of public trades in the last ``trade_window_hours`` (3), from data-api ``/trades``.
  That endpoint takes comma-separated condition ids, returns trades newest first, and is paged
  until trades are older than the window.

Market type = event slug suffix (``...-exact-score``) + Gamma ``sportsMarketType``. The headline
filter is spread > 3c with >= $500 at the best ask, and separately at both best bid and ask.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from pm_edge.config import Config
from pm_edge.ingest.polymarket import PolymarketIngest, parse_json_list, parse_ts

log = logging.getLogger(__name__)

TRADES_PAGE = 1000


def event_type(slug: str, sport: str) -> str:
    m = re.fullmatch(rf"{re.escape(sport)}-[a-z0-9]+-[a-z0-9]+-\d{{4}}-\d{{2}}-\d{{2}}(?:-(.+))?", slug)
    if not m:
        return "other"
    return m.group(1) or "(base event)"


def first_token(market: dict[str, Any]) -> tuple[str, str] | None:
    """Token id and label of a two-outcome market's first outcome (books are complementary)."""
    version = market.get("version") or "v1"
    ids = parse_json_list(market.get("positionIds") if version == "v2" else market.get("clobTokenIds"))
    labels = parse_json_list(market.get("outcomes"))
    if len(ids) != 2:
        return None
    return str(ids[0]), str(labels[0]) if labels else ""


def best_level(side: list[dict[str, str]], best_is_max: bool) -> tuple[float | None, float | None]:
    if not side:
        return None, None
    lvl = (max if best_is_max else min)(side, key=lambda x: float(x["price"]))
    return float(lvl["price"]), float(lvl["size"])


def count_recent(trades: list[dict[str, Any]], since_ts: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        if int(t.get("timestamp", 0)) >= since_ts:
            out[t["conditionId"]] = out.get(t["conditionId"], 0) + 1
    return out


async def recent_trade_counts(p: PolymarketIngest, condition_ids: list[str], since: datetime) -> dict[str, int]:
    """Trades per market since ``since``, one batched, paged request series per id group."""
    since_ts = int(since.timestamp())
    counts: dict[str, int] = {}
    offset = 0
    while True:
        page = await p.data.get_json(
            "/trades", params={"market": ",".join(condition_ids), "limit": TRADES_PAGE, "offset": offset}
        )
        for cid, n in count_recent(page, since_ts).items():
            counts[cid] = counts.get(cid, 0) + n
        if len(page) < TRADES_PAGE or int(page[-1].get("timestamp", 0)) < since_ts:
            return counts
        offset += TRADES_PAGE


async def scan(
    cfg: Config, leagues: list[str], kickoff_within_hours: float | None = None, trade_window_hours: float = 3
) -> pd.DataFrame:
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=kickoff_within_hours) if kickoff_within_hours is not None else None
    rows: list[dict[str, Any]] = []
    async with PolymarketIngest(cfg) as p:
        for league in leagues:
            pm = cfg.league(league).polymarket
            async for ev in p.iter_events(pm.series_id, closed=False):
                kickoff = parse_ts(ev.get("startTime")) or parse_ts(ev.get("endDate"))
                if horizon is not None and (kickoff is None or not now <= kickoff <= horizon):
                    continue
                etype = event_type(ev.get("slug", ""), pm.sport)
                for m in ev.get("markets") or []:
                    if m.get("closed") or not m.get("enableOrderBook", True):
                        continue
                    if etype == "(base event)" and m.get("sportsMarketType") == pm.market_type:
                        continue
                    tok = first_token(m)
                    if tok is None:
                        continue
                    rows.append({
                        "league": league, "event_slug": ev.get("slug"), "event_type": etype,
                        "market_type": m.get("sportsMarketType") or "", "question": m.get("question"),
                        "condition_id": m.get("conditionId"), "token": tok[0], "outcome": tok[1],
                        "kickoff_utc": kickoff, "volume_24h": float(m.get("volume24hr") or 0),
                    })
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        books = await p.fetch_raw_books(df["token"].tolist())
        since = now - timedelta(hours=trade_window_hours)
        groups = [g["condition_id"].tolist() for _, g in df.groupby("event_slug")]
        results = await asyncio.gather(*(recent_trade_counts(p, ids, since) for ids in groups))
    trades = {cid: n for r in results for cid, n in r.items()}

    extra = []
    for r in df.itertuples():
        b = books.get(r.token) or {}
        bid, bid_sz = best_level(b.get("bids") or [], True)
        ask, ask_sz = best_level(b.get("asks") or [], False)
        extra.append({
            "best_bid": bid, "best_ask": ask,
            "spread": round(ask - bid, 6) if bid is not None and ask is not None else None,
            "bid_usd": bid * bid_sz if bid is not None else 0.0,
            "ask_usd": ask * ask_sz if ask is not None else 0.0,
            "two_sided": bid is not None and ask is not None,
            f"trades_{trade_window_hours:g}h": trades.get(r.condition_id, 0),
        })
    return pd.concat([df, pd.DataFrame(extra)], axis=1)


def summarise(df: pd.DataFrame, spread_min: float, usd_min: float, trades_col: str) -> pd.DataFrame:
    wide = df["spread"] > spread_min
    df = df.assign(
        wide=wide,
        wide_ask_deep=wide & (df["ask_usd"] >= usd_min),
        wide_both_deep=wide & (df["ask_usd"] >= usd_min) & (df["bid_usd"] >= usd_min),
        traded=df[trades_col] > 0,
    )
    g = df.groupby(["event_type", "market_type"])
    s = pd.DataFrame({
        "markets": g.size(),
        "two_sided": g["two_sided"].mean(),
        "spread_p50_c": g["spread"].median() * 100,
        "spread_p90_c": g["spread"].quantile(0.9) * 100,
        "ask_usd_p50": g["ask_usd"].median(),
        "vol24h_sum": g["volume_24h"].sum(),
        "vol24h_p50": g["volume_24h"].median(),
        trades_col: g[trades_col].sum(),
        "share_traded": g["traded"].mean(),
        "wide>3c": g["wide"].sum(),
        "wide&ask>=$": g["wide_ask_deep"].sum(),
        "wide&both>=$": g["wide_both_deep"].sum(),
    })
    return s.sort_values("markets", ascending=False)


def run(
    cfg: Config,
    leagues: list[str],
    kickoff_within_hours: float | None,
    out_dir: Path,
    spread_min: float = 0.03,
    usd_min: float = 500,
    trade_window_hours: float = 3,
) -> tuple[pd.DataFrame, pd.DataFrame, Path | None]:
    df = asyncio.run(scan(cfg, leagues, kickoff_within_hours, trade_window_hours))
    if df.empty:
        return df, pd.DataFrame(), None
    trades_col = f"trades_{trade_window_hours:g}h"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"thin_scan_{datetime.now(UTC):%Y%m%dT%H%M}Z.csv"
    df.to_csv(path, index=False)
    return df, summarise(df, spread_min + 1e-9, usd_min, trades_col), path
