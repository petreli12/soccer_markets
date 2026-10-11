"""One-off scan of Polymarket's non-moneyline soccer markets (exploratory, not production).

For every open EPL and UCL Polymarket event, every market that is not the full-time moneyline is
scanned. That covers exact score, halftime result, corners, totals, spreads, first to score, player
props and so on. For each market it reports the first outcome token's book ("Yes", "Over", or a team
for spreads): best bid, best ask, spread, dollars at the best bid and best ask (price x size), and
Gamma's 24h volume.

Market type is the event slug suffix (``epl-ars-cov-2026-10-24-exact-score`` -> ``exact-score``) plus
Gamma's ``sportsMarketType``. "Thin but deep enough" means spread > 3c and >= $500 at the best price;
it is shown for the ask side and for both sides.

Run:  source .venv/bin/activate && python notebooks/pm_thin_market_scan.py
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from pm_edge.config import get_config
from pm_edge.ingest.polymarket import PolymarketIngest, parse_json_list

OUT_DIR = Path(__file__).parent / "output"
SPREAD_MIN = 0.03
USD_MIN = 500


def first_token(market: dict) -> tuple[str, str] | None:
    """Token id and label of the market's first outcome ("Yes", "Over", a team name, ...).

    Two-outcome markets have complementary books, so the first outcome's book shows the spread.
    """
    version = market.get("version") or "v1"
    ids = parse_json_list(market.get("positionIds") if version == "v2" else market.get("clobTokenIds"))
    labels = parse_json_list(market.get("outcomes"))
    if len(ids) != 2:
        return None
    return str(ids[0]), str(labels[0]) if labels else ""


def event_type(slug: str, sport: str) -> str:
    m = re.fullmatch(rf"{re.escape(sport)}-[a-z0-9]+-[a-z0-9]+-\d{{4}}-\d{{2}}-\d{{2}}(?:-(.+))?", slug)
    if not m:
        return "other"
    return m.group(1) or "(base event)"


async def scan() -> pd.DataFrame:
    cfg = get_config()
    rows = []
    async with PolymarketIngest(cfg) as p:
        for league in ("EPL", "UCL"):
            pm = cfg.league(league).polymarket
            async for ev in p.iter_events(pm.series_id, closed=False):
                etype = event_type(ev.get("slug", ""), pm.sport)
                for m in ev.get("markets") or []:
                    if m.get("closed") or not m.get("enableOrderBook", True):
                        continue
                    if m.get("sportsMarketType") == pm.market_type and etype == "(base event)":
                        continue
                    first = first_token(m)
                    if first is None:
                        continue
                    rows.append({
                        "league": league, "event_slug": ev.get("slug"), "event_type": etype,
                        "market_type": m.get("sportsMarketType") or "", "question": m.get("question"),
                        "condition_id": m.get("conditionId"), "yes_token": first[0], "outcome": first[1],
                        "volume_24h": float(m.get("volume24hr") or m.get("volume24hrClob") or 0),
                        "kickoff": ev.get("startTime"), "accepting_orders": m.get("acceptingOrders"),
                    })
        df = pd.DataFrame(rows)
        books = await p.fetch_raw_books(df["yes_token"].tolist())

    def best(side: list[dict], best_is_max: bool) -> tuple[float | None, float | None]:
        if not side:
            return None, None
        lvl = (max if best_is_max else min)(side, key=lambda x: float(x["price"]))
        return float(lvl["price"]), float(lvl["size"])

    out = []
    for r in df.itertuples():
        b = books.get(r.yes_token) or {}
        bid, bid_sz = best(b.get("bids") or [], True)
        ask, ask_sz = best(b.get("asks") or [], False)
        out.append({
            "best_bid": bid, "best_ask": ask, "spread": (ask - bid) if bid is not None and ask is not None else None,
            "bid_usd": bid * bid_sz if bid is not None else 0.0, "ask_usd": ask * ask_sz if ask is not None else 0.0,
            "two_sided": bid is not None and ask is not None,
        })
    return pd.concat([df, pd.DataFrame(out)], axis=1)


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    df = df.assign(
        wide=df["spread"] > SPREAD_MIN + 1e-9,
        wide_ask_deep=(df["spread"] > SPREAD_MIN + 1e-9) & (df["ask_usd"] >= USD_MIN),
        wide_both_deep=(df["spread"] > SPREAD_MIN + 1e-9) & (df["ask_usd"] >= USD_MIN) & (df["bid_usd"] >= USD_MIN),
    )
    g = df.groupby(["event_type", "market_type"])
    s = pd.DataFrame({
        "markets": g.size(),
        "two_sided": g["two_sided"].mean(),
        "spread_p50_c": g["spread"].median() * 100,
        "spread_p90_c": g["spread"].quantile(0.9) * 100,
        "ask_usd_p50": g["ask_usd"].median(),
        "vol24h_p50": g["volume_24h"].median(),
        "vol24h_sum": g["volume_24h"].sum(),
        "wide>3c": g["wide"].sum(),
        "wide&ask>=$500": g["wide_ask_deep"].sum(),
        "wide&both>=$500": g["wide_both_deep"].sum(),
    })
    return s.sort_values("markets", ascending=False)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    df = asyncio.run(scan())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M")
    df.to_csv(OUT_DIR / f"pm_thin_market_scan_{stamp}.csv", index=False)
    print(f"{df['event_slug'].nunique()} events, {len(df)} non-moneyline markets "
          f"({df.groupby('league').size().to_dict()}); two-sided books: {df['two_sided'].mean():.1%}")
    s = summarise(df)
    print(s.round(2).to_string())
    hits = df[(df["spread"] > SPREAD_MIN + 1e-9) & (df["ask_usd"] >= USD_MIN)].sort_values("volume_24h", ascending=False)
    print(f"\nmarkets with spread > 3c and >= ${USD_MIN} at best ask: {len(hits)} "
          f"(both sides >= ${USD_MIN}: {int((hits['bid_usd'] >= USD_MIN).sum())})")
    if len(hits):
        print(hits.head(25)[["league", "event_slug", "question", "best_bid", "best_ask", "spread", "bid_usd", "ask_usd",
                             "volume_24h"]].round(3).to_string(index=False))


if __name__ == "__main__":
    main()
