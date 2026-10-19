"""Maker vs taker execution backtest on Kalshi EPL/UCL match markets (exploratory, read-only, APPROXIMATE).

Question: a bettor wants $100 of YES on one outcome (HOME/DRAW/AWAY). Is posting a resting bid
(maker) cheaper than lifting the ask (taker), after the cost of falling back to a taker order?

Data
  * Kalshi 1-minute candles (bid/ask OHLC) from the backfill: data/raw/kalshi/candles/.
  * Kalshi public trades, backfilled here to data/raw/kalshi/trades/league=L/{event}.parquet for
    [kickoff - 24h, kickoff]: ``GET /markets/trades`` (no auth); trades created before the
    exchange's ``trades_created_ts`` cutoff come from ``GET /historical/trades``.
  * Polymarket public trades (data-api ``/trades?eventId=``) to data/raw/polymarket/trades/, for a
    volume comparison only. The simulated bettor trades on Kalshi.

Simulation (per match x entry time T-3h/T-2h/T-1h x outcome)
  * N = floor($100 / ask at entry, 0.01) contracts; the same N for every strategy, so all
    strategies end with an identical position and identical settlement payoff. Net savings is
    therefore exactly the difference in all-in cost.
  * Taker: N at the entry ask, fee round_up(0.07 * N * P(1-P)).
  * Maker A: rest N at the entry best bid b, with Q contracts queued ahead. Every later trade
    printed at yes_price <= b consumes the queue; our fills begin once Q is consumed (a print below
    b means the level was swept, so everything left fills). Maker fee round_up(0.0175 * F * b(1-b)).
  * Maker B: if the spread is >= 2c, rest at b + 1 tick instead, as a new price level with no queue;
    otherwise identical to A.
  * Fallback: whatever is unfilled at T-10min is bought at the T-10min ask with the taker fee.
  * Markouts (adverse selection): Kalshi mid at fill + 5/15/60 min (capped at kickoff) and at
    kickoff, minus fill price, quantity-weighted. Negative = the market moved against the fill.

Queue Q: historical book depth is not public. Candles have prices only. Real displayed bid sizes
exist only in collector-lite snapshots, and none overlap the backfilled matches. Q is therefore
a scan: 0 (front of the queue, the optimistic bound), then 1k, 10k contracts, then the live-
snapshot median best-bid size at that entry time. The live median is the spec's "displayed size"
assumption. All results are APPROXIMATE.

Run:  source .venv/bin/activate && python notebooks/maker_backtest.py [--refresh-trades] [--skip-pm]
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_overround_eda import minute_grid  # noqa: E402

from pm_edge.config import get_config  # noqa: E402
from pm_edge.fees import kalshi_taker_fee, size_for_notional  # noqa: E402
from pm_edge.http import ApiClient  # noqa: E402

cfg = get_config()
RAW = cfg.resolve(cfg.paths.raw_dir)
PROCESSED = cfg.resolve(cfg.paths.processed_dir)
LEAGUES = ["EPL", "UCL"]
OUTCOMES = ["HOME", "DRAW", "AWAY"]
ENTRY_MIN = [180, 120, 60]
FALLBACK_MIN = 10
MARKOUT_MIN = [5, 15, 60]
NOTIONAL = 100.0
TAKER_RATE, MAKER_RATE = 0.07, 0.0175
INCREMENT = cfg.fees.kalshi.rounding_increment_usd
TICK = 0.01
TRADES_BEFORE_H = 24
FIXED_QUEUES = [0.0, 1_000.0, 10_000.0]


# -- trades backfill ---------------------------------------------------------------------------

def pairs() -> pd.DataFrame:
    out = []
    for lg in LEAGUES:
        m = pd.read_parquet(PROCESSED / f"matches_league={lg}.parquet")
        w = m.pivot_table(index=["match_id", "kickoff_utc", "league"], columns="venue", values="venue_event_id",
                          aggfunc="first").dropna().reset_index()
        out.append(w)
    p = pd.concat(out, ignore_index=True)
    return p[p["kickoff_utc"] < pd.Timestamp.now(tz="UTC")].sort_values("kickoff_utc").reset_index(drop=True)


async def kalshi_trades(api: ApiClient, path: str, ticker: str, lo: int, hi: int) -> list[dict]:
    rows, cursor = [], ""
    while True:
        params = {"ticker": ticker, "min_ts": lo, "max_ts": hi, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = await api.get_json(path, params=params)
        rows.extend(data.get("trades") or [])
        cursor = data.get("cursor") or ""
        if not cursor:
            return rows


async def backfill_kalshi(p: pd.DataFrame, refresh: bool) -> dict[str, int]:
    k = pd.concat(pd.read_parquet(RAW / "kalshi" / f"matches_league={lg}.parquet") for lg in LEAGUES)
    stats = {"events": 0, "cached": 0, "trades": 0, "historical_calls": 0}
    async with ApiClient(cfg.venues.kalshi.base_url, cfg.venues.kalshi.max_requests_per_second) as api:
        cutoff = pd.Timestamp((await api.get_json("/historical/cutoff"))["trades_created_ts"])
        for r in p.itertuples():
            out = RAW / "kalshi" / "trades" / f"league={r.league}" / f"{r.kalshi}.parquet"
            if out.exists() and not refresh:
                stats["cached"] += 1
                continue
            lo = int((r.kickoff_utc - timedelta(hours=TRADES_BEFORE_H)).timestamp())
            hi = int(r.kickoff_utc.timestamp())
            rows = []
            for m in k[k["venue_event_id"] == r.kalshi].itertuples():
                got = await kalshi_trades(api, "/markets/trades", m.market_id, lo, hi)
                if pd.Timestamp(lo, unit="s", tz="UTC") < cutoff:
                    got += await kalshi_trades(api, "/historical/trades", m.market_id, lo, hi)
                    stats["historical_calls"] += 1
                rows += [{**t, "outcome": m.outcome} for t in got]
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.drop_duplicates("trade_id")
                df = pd.DataFrame({
                    "trade_id": df["trade_id"], "ticker": df["ticker"], "outcome": df["outcome"],
                    "ts": pd.to_datetime(df["created_time"], utc=True, format="ISO8601"),
                    "yes_price": df["yes_price_dollars"].astype(float), "count": df["count_fp"].astype(float),
                    "taker_outcome_side": df["taker_outcome_side"], "is_block_trade": df["is_block_trade"],
                }).sort_values("ts")
            else:
                df = pd.DataFrame(columns=["trade_id", "ticker", "outcome", "ts", "yes_price", "count",
                                           "taker_outcome_side", "is_block_trade"])
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            stats["events"] += 1
            stats["trades"] += len(df)
    return stats


async def backfill_polymarket(p: pd.DataFrame, refresh: bool) -> dict[str, int]:
    pm = pd.concat(pd.read_parquet(RAW / "polymarket" / f"matches_league={lg}.parquet") for lg in LEAGUES)
    outcome_of = dict(zip(pm["market_id"], pm["outcome"]))
    stats = {"events": 0, "cached": 0, "trades": 0, "split_windows": 0}
    async with ApiClient(cfg.venues.polymarket.data_url, 10) as api:
        for r in p.itertuples():
            out = RAW / "polymarket" / "trades" / f"league={r.league}" / f"{r.polymarket}.parquet"
            if out.exists() and not refresh:
                stats["cached"] += 1
                continue
            windows = [(int((r.kickoff_utc - timedelta(hours=TRADES_BEFORE_H)).timestamp()),
                        int(r.kickoff_utc.timestamp()))]
            rows = []
            while windows:
                lo, hi = windows.pop()
                page, offset, got = 10_000, 0, []
                while True:
                    data = await api.get_json("/trades", params={"eventId": r.polymarket, "start": lo, "end": hi,
                                                                 "limit": page, "offset": offset})
                    got += data
                    if len(data) < page:
                        break
                    offset += page
                    if offset > 10_000:  # offset budget exhausted: split the window and redo it
                        mid = (lo + hi) // 2
                        windows += [(lo, mid), (mid + 1, hi)]
                        stats["split_windows"] += 1
                        got = None
                        break
                if got is not None:
                    rows += got
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.drop_duplicates(["transactionHash", "asset", "side", "size", "price"])
                df = pd.DataFrame({
                    "condition_id": df["conditionId"], "outcome": df["conditionId"].map(outcome_of),
                    "token_outcome": df["outcome"], "side": df["side"], "price": df["price"].astype(float),
                    "size": df["size"].astype(float), "ts": pd.to_datetime(df["timestamp"], unit="s", utc=True),
                })
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            stats["events"] += 1
            stats["trades"] += len(df)
    return stats


# -- simulation --------------------------------------------------------------------------------

def maker_fills(ts: np.ndarray, px: np.ndarray, qty: np.ndarray, price: float, queue: float, n: float
                ) -> list[tuple[int, float]]:
    """(trade index, contracts filled) for a resting bid of ``n`` at ``price`` behind ``queue``.

    Trades (time-ordered) printed at or below ``price`` consume the queue, then our order. A print
    strictly below ``price`` means the level was swept: everything left fills on that trade.
    """
    fills, ahead, left = [], queue, n
    for i in range(len(ts)):
        if left <= 1e-9:
            break
        if px[i] > price + 1e-9:
            continue
        if px[i] < price - 1e-9:
            fills.append((i, left))
            break
        q = qty[i]
        take = min(q, ahead)
        ahead -= take
        q -= take
        if q > 1e-9:
            f = min(q, left)
            fills.append((i, f))
            left -= f
    return fills


def asof(grid_ts: np.ndarray, values: np.ndarray, t: np.datetime64) -> float:
    i = np.searchsorted(grid_ts, t, side="right") - 1
    return float(values[i]) if i >= 0 else math.nan


def fee(rate: float, price: float, n: float) -> float:
    return kalshi_taker_fee(price, round(n, 2), 1.0, rate, INCREMENT) if n > 1e-9 else 0.0


def live_queue_medians() -> dict[int, float]:
    p = cfg.resolve(cfg.collector_lite.live_dir) / "venue=kalshi"
    if not p.exists():
        return {}
    d = pd.read_parquet(p, columns=["minutes_to_kickoff", "yes_bid_sz_1", "match_id"])
    out = {}
    for e in ENTRY_MIN:
        s = d.loc[(d["minutes_to_kickoff"] - e).abs() <= 10, "yes_bid_sz_1"].dropna()
        if len(s):
            out[e] = float(s.median())
    return out


def simulate(p: pd.DataFrame, queues: dict[str, dict[int, float]]) -> pd.DataFrame:
    rows = []
    for lg in LEAGUES:
        k = pd.read_parquet(RAW / "kalshi" / f"matches_league={lg}.parquet")
        for r in p[p["league"] == lg].itertuples():
            cf = RAW / "kalshi" / "candles" / f"league={lg}" / f"{r.kalshi}.parquet"
            tf = RAW / "kalshi" / "trades" / f"league={lg}" / f"{r.kalshi}.parquet"
            if not cf.exists() or not tf.exists():
                continue
            g = minute_grid(k[k["venue_event_id"] == r.kalshi], pd.read_parquet(cf))
            if g.empty:
                continue
            trades = pd.read_parquet(tf)
            gts = g["ts"].to_numpy(dtype="datetime64[ns]")
            ko = np.datetime64(r.kickoff_utc.tz_convert(None), "ns")
            fb_t = ko - np.timedelta64(FALLBACK_MIN, "m")
            for e in ENTRY_MIN:
                t0 = ko - np.timedelta64(e, "m")
                mids0 = {o: (asof(gts, g[f"bid_{o}"].to_numpy(), t0) + asof(gts, g[f"ask_{o}"].to_numpy(), t0)) / 2
                         for o in OUTCOMES}
                fav = "HOME" if mids0["HOME"] >= mids0["AWAY"] else "AWAY"
                for o in OUTCOMES:
                    bid_v, ask_v = g[f"bid_{o}"].to_numpy(), g[f"ask_{o}"].to_numpy()
                    mid_v = (bid_v + ask_v) / 2
                    b0, a0, a_fb = asof(gts, bid_v, t0), asof(gts, ask_v, t0), asof(gts, ask_v, fb_t)
                    if not (0 < b0 < a0 < 1) or not (0 < a_fb < 1):
                        continue
                    n = size_for_notional(NOTIONAL, a0)
                    taker_cpc = (a0 * n + fee(TAKER_RATE, a0, n)) / n
                    tw = trades[(trades["outcome"] == o) & (trades["ts"] > pd.Timestamp(t0, tz="UTC"))
                                & (trades["ts"] <= pd.Timestamp(fb_t, tz="UTC"))]
                    tts = tw["ts"].dt.tz_convert(None).to_numpy(dtype="datetime64[ns]")
                    tpx, tq = tw["yes_price"].to_numpy(), tw["count"].to_numpy()
                    spread = round(a0 - b0, 4)
                    role = "draw" if o == "DRAW" else ("favourite" if o == fav else "underdog")
                    base = {"match_id": r.match_id, "league": lg, "entry_min": e, "outcome": o, "role": role,
                            "n": n, "bid0": b0, "ask0": a0, "spread_c": spread * 100, "ask_fb": a_fb,
                            "taker_cpc": taker_cpc, "trades_window": len(tw), "volume_window": float(tq.sum())}
                    for qname, qmap in queues.items():
                        q = qmap.get(e, math.nan)
                        if math.isnan(q):
                            continue
                        for strat in ("A", "B"):
                            price, queue = b0, q
                            if strat == "B" and spread >= 0.02 - 1e-9:
                                price, queue = round(b0 + TICK, 4), 0.0
                            fills = maker_fills(tts, tpx, tq, price, queue, n)
                            filled = sum(f for _, f in fills)
                            rest = max(n - filled, 0.0)
                            cost = price * filled + fee(MAKER_RATE, price, filled) + a_fb * rest + fee(TAKER_RATE, a_fb, rest)
                            mk = {}
                            if filled > 1e-9:
                                for h in MARKOUT_MIN:
                                    mk[f"mo{h}_c"] = sum(
                                        f * (asof(gts, mid_v, min(tts[i] + np.timedelta64(h, "m"), ko)) - price)
                                        for i, f in fills) / filled * 100
                                mk["moKO_c"] = (asof(gts, mid_v, ko) - price) * 100
                            rows.append({**base, "queue": qname, "queue_size": queue, "strategy": strat,
                                         "post_px": price, "filled_frac": filled / n, "full_fill": rest <= 1e-9,
                                         "cpc": cost / n, "savings_c": (taker_cpc - cost / n) * 100,
                                         "savings_pct_stake": (taker_cpc * n - cost) / NOTIONAL * 100, **mk})
                    # taker markouts for context (same horizons from the entry time)
                    for h in MARKOUT_MIN:
                        base[f"taker_mo{h}_c"] = (asof(gts, mid_v, min(t0 + np.timedelta64(h, "m"), ko)) - a0) * 100
                    base["taker_moKO_c"] = (asof(gts, mid_v, ko) - a0) * 100
                    rows.append({**base, "queue": "-", "queue_size": math.nan, "strategy": "taker", "post_px": a0,
                                 "filled_frac": 1.0, "full_fill": True, "cpc": taker_cpc, "savings_c": 0.0,
                                 "savings_pct_stake": 0.0})
    return pd.DataFrame(rows)


# -- reporting ---------------------------------------------------------------------------------

def summary_table(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    def agg(g: pd.DataFrame) -> pd.Series:
        f = g[g["filled_frac"] > 0]
        w = f["filled_frac"] * f["n"]
        mo = {f"mo{h}_c": np.average(f[f"mo{h}_c"], weights=w) if len(f) else np.nan for h in MARKOUT_MIN}
        mo["moKO_c"] = np.average(f["moKO_c"], weights=w) if len(f) else np.nan
        return pd.Series({
            "orders": len(g), "full_fill_rate": g["full_fill"].mean(), "filled_frac": g["filled_frac"].mean(),
            "cost_c": g["cpc"].mean() * 100, "taker_c": g["taker_cpc"].mean() * 100,
            "savings_c": g["savings_c"].mean(), "savings_p10_c": g["savings_c"].quantile(0.1),
            "savings_pct": g["savings_pct_stake"].mean(), **mo,
        })
    return df.groupby(by, sort=False).apply(agg, include_groups=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-trades", action="store_true")
    ap.add_argument("--skip-pm", action="store_true", help="skip the Polymarket trades backfill")
    args = ap.parse_args()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    p = pairs()
    print(f"matched matches with kickoff in the past: {len(p)} {p.groupby('league').size().to_dict()}")
    print("Kalshi trades backfill:", asyncio.run(backfill_kalshi(p, args.refresh_trades)))
    if not args.skip_pm:
        print("Polymarket trades backfill:", asyncio.run(backfill_polymarket(p, args.refresh_trades)))

    live = live_queue_medians()
    queues = {f"Q={int(q):,}": {e: q for e in ENTRY_MIN} for q in FIXED_QUEUES}
    if live:
        queues["Q=live median"] = live
    print("queue ahead scanned:", {k: {e: round(v) for e, v in m.items()} for k, m in queues.items()},
          f"(live median from {cfg.collector_lite.live_dir} snapshots within 10 min of each entry time)")

    df = simulate(p, queues)
    tk = df[df["strategy"] == "taker"]
    mk = df[df["strategy"] != "taker"]
    print(f"\norders simulated per strategy/queue: {len(tk)} ({tk['match_id'].nunique()} matches x 3 entries x 3 outcomes,"
          f" minus missing quotes); entry spread: 1c {(tk['spread_c'].round() == 1).mean():.0%}, "
          f">=2c {(tk['spread_c'] >= 1.999).mean():.0%}; Kalshi contracts traded entry..T-10m per order: median "
          f"{tk['volume_window'].median():,.0f}")
    print("\n=== APPROXIMATE: maker vs taker, $100 YES, all entries/outcomes (cents per contract) ===")
    print(summary_table(mk, ["queue", "strategy"]).round(3).to_string())
    t = tk[[f"taker_mo{h}_c" for h in MARKOUT_MIN] + ["taker_moKO_c"]].mean().round(3).to_dict()
    print(f"taker markouts (mid after entry - ask paid, c): {t}; taker all-in cost {tk['taker_cpc'].mean() * 100:.2f}c")
    for q in [k for k in queues if k in ("Q=0", "Q=live median")]:
        print(f"\n--- {q}: by entry time ---")
        print(summary_table(mk[mk["queue"] == q], ["strategy", "entry_min"]).round(3).to_string())
        print(f"--- {q}: by outcome role ---")
        print(summary_table(mk[mk["queue"] == q], ["strategy", "role"]).round(3).to_string())

    pm_dir = RAW / "polymarket" / "trades"
    if pm_dir.exists() and not args.skip_pm:
        rows = []
        for r in p.itertuples():
            f = pm_dir / f"league={r.league}" / f"{r.polymarket}.parquet"
            if f.exists():
                d = pd.read_parquet(f)
                if not d.empty:
                    d = d[d["ts"] > r.kickoff_utc - timedelta(hours=3)]
                rows.append({"match_id": r.match_id, "pm_trades_3h": len(d), "pm_usd_3h": float((d["price"] * d["size"]).sum()) if len(d) else 0.0})
        pmv = pd.DataFrame(rows)
        kv = tk[tk["entry_min"] == 180].groupby("match_id")[["trades_window", "volume_window"]].sum()
        both = pmv.set_index("match_id").join(kv, how="inner")
        print(f"\nPolymarket vs Kalshi, T-3h..kickoff per match (median): PM trades {both['pm_trades_3h'].median():.0f}, "
              f"PM ${both['pm_usd_3h'].median():,.0f} | Kalshi trades {both['trades_window'].median():.0f}, "
              f"Kalshi contracts {both['volume_window'].median():,.0f} (to T-10m)")

    out = Path(__file__).parent / "output"
    out.mkdir(exist_ok=True)
    df.to_csv(out / "maker_backtest_orders.csv", index=False)

    main_q = "Q=live median" if "Q=live median" in queues else "Q=10,000"
    s = summary_table(mk, ["queue", "strategy"])
    best = s["savings_c"].max()
    print("\n=== SUMMARY (APPROXIMATE: queue position is estimated, not observed) ===")
    lines = [
        f"1. {tk['match_id'].nunique()} Kalshi EPL/UCL matches, {len(tk)} $100 YES orders per strategy (T-3h/2h/1h x 3 outcomes).",
        f"2. Taker baseline all-in {tk['taker_cpc'].mean() * 100:.2f}c/contract; entry spread is 1c in {(tk['spread_c'].round() == 1).mean():.0%} of orders.",
        f"3. Strategy B differs from A only when the spread is >=2c: {(tk['spread_c'] >= 1.999).mean():.0%} of orders.",
        f"4. Front of queue (Q=0, optimistic bound): A full-fill {s.loc[('Q=0', 'A'), 'full_fill_rate']:.0%}, net {s.loc[('Q=0', 'A'), 'savings_c']:+.2f}c; "
        f"B full-fill {s.loc[('Q=0', 'B'), 'full_fill_rate']:.0%}, net {s.loc[('Q=0', 'B'), 'savings_c']:+.2f}c per contract.",
        f"5. {main_q} (spec assumption; {int(s.loc[(main_q, 'A'), 'orders'])} orders, entries with live size data only): A full-fill {s.loc[(main_q, 'A'), 'full_fill_rate']:.0%}, net {s.loc[(main_q, 'A'), 'savings_c']:+.2f}c; "
        f"B net {s.loc[(main_q, 'B'), 'savings_c']:+.2f}c ({s.loc[(main_q, 'B'), 'savings_pct']:+.2f}% of stake).",
        f"6. Maker-fill markouts (mid - fill; +0.5c = no move at a 1c spread): Q=0 {s.loc[('Q=0', 'A'), 'mo5_c']:+.2f}c at +5m, "
        f"{s.loc[('Q=0', 'A'), 'moKO_c']:+.2f}c at KO; {main_q} {s.loc[(main_q, 'A'), 'mo5_c']:+.2f}c / {s.loc[(main_q, 'A'), 'moKO_c']:+.2f}c.",
        f"7. Taker markout at kickoff {tk['taker_moKO_c'].mean():+.2f}c vs ask paid; the maker gives up fills when the price runs away and pays the T-10m ask instead.",
        f"8. Net savings already include fallback costs and adverse selection: every strategy ends with the same N contracts, so payoffs are identical.",
        f"9. Best case across scanned queues: {best:+.2f}c/contract ({s['savings_pct'].max():+.2f}% of stake).",
        f"10. Verdict: maker execution {'DOES' if s.loc[(main_q, 'A'), 'savings_c'] >= 1 or s.loc[(main_q, 'B'), 'savings_c'] >= 1 else 'does NOT'} "
        f"save >=1c/contract net under {main_q}; at Q=0 it {'does' if max(s.loc[('Q=0', 'A'), 'savings_c'], s.loc[('Q=0', 'B'), 'savings_c']) >= 1 else 'does not'}.",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
