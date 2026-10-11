"""Matchday report on collector-lite data (exploratory, not production).

  a. EXECUTABLE cross-venue edges from real asks on both venues. Per outcome and 20 s tick:
     Kalshi YES + Polymarket NO, and Polymarket YES + Kalshi NO, each paying $1. Size for $100 total:
     N = floor(100 / (best ask A + best ask B), 0.01) per leg. Each leg walks the stored top-3 levels.
     Fees come from fees.py per level filled (each level is a separate fill), so Kalshi's per-fill
     rounding applies; this is conservative by at most a cent per extra level. Polymarket
     min_order_size is enforced. Also reported: the edge and dollar size at the largest size both best
     levels support.
  b. Sharp divergence: each venue's mid, (best bid + best ask) / 2, minus the Pinnacle power-de-vigged
     probability. Uses the venue snapshot nearest each Pinnacle poll (within 30 s). A comparison is
     discarded if Pinnacle's last_update is older than analysis.sharp.max_pinnacle_age_minutes; discards
     are counted. The lineup window (75-45 min) is reported separately.
  c. Polymarket /prices-history vs live book: each history point is compared with the latest live
     snapshot of the same token at or before it (within 30 s): midpoint vs last_trade_price.

Run:  source .venv/bin/activate && python notebooks/matchday_report.py [--date 2026-10-12 ...] [--live-dir data/live]
"""

from __future__ import annotations

import argparse
import asyncio
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from pm_edge.config import get_config
from pm_edge.fees import FeeModel
from pm_edge.ingest.polymarket import PolymarketIngest
from pm_edge.models import FeeParams, Venue

cfg = get_config()
FEES = FeeModel(cfg.fees)
NOTIONAL = cfg.analysis.reference_notional_usd
LINEUP = next(w for w in cfg.analysis.focus_windows if w.name == "lineup")
DEPTH = cfg.collector_lite.orderbook_depth
TOL = pd.Timedelta(seconds=30)


def load(live_dir: str, dates: list[str] | None) -> dict[str, pd.DataFrame]:
    root = cfg.resolve(live_dir)
    out = {}
    for v in ("kalshi", "polymarket", "pinnacle", "schedule"):
        p = root / f"venue={v}"
        out[v] = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    sched = out["schedule"].sort_values("ts_poll").drop_duplicates(["match_id", "venue", "outcome"], keep="last")
    if dates:
        keep = sched[sched["kickoff_utc"].dt.strftime("%Y-%m-%d").isin(dates)]["match_id"].unique()
        sched = sched[sched["match_id"].isin(keep)]
    out["schedule"] = sched
    for v in ("kalshi", "polymarket", "pinnacle"):
        if not out[v].empty:
            out[v] = out[v][out[v]["match_id"].isin(sched["match_id"])]
    return out


def fill(levels: list[tuple[float, float]], n: float) -> list[tuple[float, float]] | None:
    """Walk best-first (price, size) levels for ``n`` contracts; ``None`` if depth is insufficient."""
    out, left = [], n
    for px, sz in levels:
        if left <= 1e-9:
            break
        if px is None or sz is None or (isinstance(px, float) and math.isnan(px)):
            break
        q = min(sz, left)
        out.append((px, q))
        left -= q
    return out if left <= 1e-9 else None


def leg_cost(venue: Venue, fills: list[tuple[float, float]], fp: FeeParams) -> float:
    return sum(px * q + FEES.taker_fee(venue, px, round(q, 2), fp) for px, q in fills)


def levels(row: pd.Series, side: str) -> list[tuple[float, float]]:
    return [(row[f"{side}_px_{i}"], row[f"{side}_sz_{i}"]) for i in range(1, DEPTH + 1)
            if pd.notna(row[f"{side}_px_{i}"])]


def executable(d: dict[str, pd.DataFrame]) -> pd.DataFrame:
    sched = d["schedule"].set_index(["match_id", "venue", "outcome"])
    k = d["kalshi"].set_index(["ts_poll", "match_id", "outcome"])
    p = d["polymarket"].set_index(["ts_poll", "match_id", "outcome"])
    both = k.index.intersection(p.index)
    rows = []
    for key in both:
        kr, pr = k.loc[key], p.loc[key]
        _, mid, o = key
        kf = sched.loc[(mid, "kalshi", o)]
        pf = sched.loc[(mid, "polymarket", o)]
        kfp = FeeParams(rate=kf["fee_rate"], multiplier=kf["fee_multiplier"])
        pfp = FeeParams(rate=pf["fee_rate"], exponent=pf["fee_exponent"])
        pmin = float(pr["min_order_size"] or pf["min_order_size"] or 0)
        for direction, kside, pside in (("K_YES+P_NO", "yes_ask", "no_ask"), ("P_YES+K_NO", "no_ask", "yes_ask")):
            kl, pl = levels(kr, kside), levels(pr, pside)
            if not kl or not pl:
                continue
            raw = kl[0][0] + pl[0][0]
            n = math.floor(NOTIONAL / raw * 100) / 100
            kfill, pfill = fill(kl, n), fill(pl, n)
            ref_edge = np.nan
            if kfill and pfill and n >= pmin:
                ref_edge = 1 - (leg_cost(Venue.KALSHI, kfill, kfp) + leg_cost(Venue.POLYMARKET, pfill, pfp)) / n
            top = math.floor(min(kl[0][1], pl[0][1]) * 100) / 100
            top_edge = np.nan
            if top >= max(pmin, 0.01):
                top_edge = 1 - (leg_cost(Venue.KALSHI, [(kl[0][0], top)], kfp)
                                + leg_cost(Venue.POLYMARKET, [(pl[0][0], top)], pfp)) / top
            rows.append({"ts_poll": key[0], "match_id": mid, "outcome": o, "direction": direction,
                         "minutes_to_kickoff": kr["minutes_to_kickoff"], "raw_cost": raw,
                         "ref_fillable": kfill is not None and pfill is not None, "net_edge_ref": ref_edge,
                         "top_size": top, "net_edge_top": top_edge, "top_usd": top * raw})
    return pd.DataFrame(rows)


def episodes(prof: pd.DataFrame, poll_s: int) -> pd.Series:
    if prof.empty:
        return pd.Series(dtype=float)
    prof = prof.sort_values(["match_id", "outcome", "direction", "ts_poll"])
    key = prof["match_id"] + prof["outcome"] + prof["direction"]
    new = (prof["ts_poll"].diff() > pd.Timedelta(seconds=poll_s * 1.5)) | (key != key.shift())
    return prof.groupby(new.cumsum())["ts_poll"].agg(lambda s: (s.max() - s.min()).total_seconds() / 60 + poll_s / 60)


def report_executable(ex: pd.DataFrame) -> None:
    print("\n=== a. EXECUTABLE cross-venue edges (real asks, fees, $100) ===")
    if ex.empty:
        print("  no ticks with both venues")
        return
    print(f"  ticks x outcomes x directions: {len(ex):,}; matches: {ex['match_id'].nunique()}; "
          f"$100 fillable within top {DEPTH}: {ex['ref_fillable'].mean():.1%}")
    for col, label in (("net_edge_ref", "$100 reference"), ("net_edge_top", "best-level max size")):
        s = ex[col].dropna()
        prof = ex[ex[col] > 0]
        eps = episodes(prof, cfg.collector_lite.book_poll_seconds)
        print(f"  {label}: profitable ticks {len(prof)}, episodes {len(eps)}, median duration "
              f"{eps.median() if len(eps) else float('nan'):.1f} min; net edge p50 {s.median() * 100:.2f}c, "
              f"p99 {s.quantile(0.99) * 100:.2f}c, max {s.max() * 100:.2f}c")
    prof = ex[ex["net_edge_top"] > 0]
    if len(prof):
        print("  profitable at best level:")
        print(prof.sort_values("net_edge_top", ascending=False).head(10)[
            ["match_id", "outcome", "direction", "ts_poll", "minutes_to_kickoff", "net_edge_top", "top_size", "top_usd"]
        ].to_string(index=False))
    print("  raw cost (best asks, before fees) p1/p50:", ex["raw_cost"].quantile([0.01, 0.5]).round(4).tolist())


def sharp(d: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if d["pinnacle"].empty:
        return pd.DataFrame()
    pin = d["pinnacle"].dropna(subset=["match_id", "p_devig"])
    if pin.empty:
        return pd.DataFrame()
    max_age = pd.Timedelta(minutes=cfg.analysis.sharp.max_pinnacle_age_minutes)
    out = []
    for venue in ("kalshi", "polymarket"):
        b = d[venue].copy()
        if b.empty:
            continue
        b["mid"] = (b["best_yes_bid"] + b["best_yes_ask"]) / 2
        left = pin[["ts_poll", "match_id", "outcome", "p_devig", "last_update"]].sort_values("ts_poll")
        right = b[["ts_poll", "match_id", "outcome", "mid", "minutes_to_kickoff"]].sort_values("ts_poll")
        right = right.rename(columns={"ts_poll": "ts_book"})
        m = pd.merge_asof(left, right, left_on="ts_poll", right_on="ts_book", by=["match_id", "outcome"],
                          direction="nearest", tolerance=TOL)
        m["venue"] = venue
        m["pin_age_min"] = (m["ts_poll"] - m["last_update"]).dt.total_seconds() / 60
        m["fresh"] = (m["ts_poll"] - m["last_update"]) <= max_age
        m["gap"] = m["mid"] - m["p_devig"]
        out.append(m)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def report_sharp(sh: pd.DataFrame) -> None:
    print("\n=== b. Sharp divergence: venue mid - Pinnacle de-vigged (power) ===")
    if sh.empty:
        print("  no Pinnacle comparisons")
        return
    has_book = sh["mid"].notna()
    disc = has_book & ~sh["fresh"]
    print(f"  comparisons with a venue snapshot: {has_book.sum()}; discarded (Pinnacle last_update > "
          f"{cfg.analysis.sharp.max_pinnacle_age_minutes} min): {disc.sum()} ({disc.sum() / max(has_book.sum(), 1):.1%}); "
          f"Pinnacle age median {sh.loc[has_book, 'pin_age_min'].median():.1f} min")
    ok = sh[has_book & sh["fresh"]].copy()
    ok["lineup"] = ok["minutes_to_kickoff"].between(LINEUP.min_minutes, LINEUP.max_minutes)
    for name, g in [("all", ok), ("lineup 75-45", ok[ok["lineup"]])]:
        for venue, gv in g.groupby("venue"):
            a = gv["gap"].abs() * 100
            print(f"  {name:12s} {venue:10s} n={len(gv):5d} signed mean {gv['gap'].mean() * 100:+.2f}c | |gap| p50 "
                  f"{a.median():.2f}c p90 {a.quantile(0.9):.2f}c max {a.max():.2f}c | >3c {(a > 3).mean():.1%}")
    per_match = ok.assign(over=ok["gap"].abs() > cfg.analysis.sharp.divergence_threshold).groupby(["venue", "match_id"])["over"].any()
    print("  share of matches with any fresh comparison >3c:", per_match.groupby("venue").mean().round(3).to_dict())


async def history_vs_live(d: dict[str, pd.DataFrame]) -> pd.DataFrame:
    sched = d["schedule"][d["schedule"]["venue"] == "polymarket"]
    live = d["polymarket"]
    if live.empty:
        return pd.DataFrame()
    out = []
    async with PolymarketIngest(cfg) as p:
        for (mid, o), s in sched.groupby(["match_id", "outcome"]):
            lv = live[(live["match_id"] == mid) & (live["outcome"] == o)].sort_values("ts_fetch")
            if lv.empty:
                continue
            start, end = lv["ts_fetch"].min() - timedelta(minutes=1), lv["ts_fetch"].max() + timedelta(minutes=1)
            h = await p.fetch_price_history(s["yes_id"].iloc[0], start.to_pydatetime(), end.to_pydatetime())
            if h.empty:
                continue
            h["ts"] = h["ts"].astype("datetime64[us, UTC]")
            lv = lv.assign(ts_fetch=lv["ts_fetch"].astype("datetime64[us, UTC]"))
            m = pd.merge_asof(h.sort_values("ts"), lv[["ts_fetch", "midpoint", "last_trade_price", "best_yes_bid", "best_yes_ask"]],
                              left_on="ts", right_on="ts_fetch", direction="backward", tolerance=TOL)
            m["match_id"], m["outcome"] = mid, o
            out.append(m)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def report_history(hv: pd.DataFrame) -> None:
    print("\n=== c. Polymarket /prices-history vs live book ===")
    if hv.empty:
        print("  no overlap")
        return
    m = hv.dropna(subset=["midpoint"])
    tol = cfg.analysis.pm_history_semantics.match_tolerance
    for col in ("midpoint", "last_trade_price"):
        diff = (m["price"] - m[col]).abs()
        print(f"  vs {col:16s}: n={diff.notna().sum()}, exact (<= {tol}) {(diff <= tol).mean():.1%}, MAE {diff.mean() * 100:.3f}c")
    inside = (m["price"] >= m["best_yes_bid"] - 1e-9) & (m["price"] <= m["best_yes_ask"] + 1e-9)
    print(f"  history price inside live bid-ask: {inside.mean():.1%}; points without a live snapshot within 30 s: "
          f"{hv['midpoint'].isna().sum()}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", help="kickoff date(s) UTC, YYYY-MM-DD; default all collected")
    ap.add_argument("--live-dir", default=cfg.collector_lite.live_dir)
    ap.add_argument("--skip-history", action="store_true")
    args = ap.parse_args()
    pd.set_option("display.width", 220)
    d = load(args.live_dir, args.date)
    sched = d["schedule"].drop_duplicates("match_id")
    print(f"matches: {len(sched)} ({', '.join(sorted(sched['match_id']))})")
    print(f"rows: kalshi {len(d['kalshi']):,}, polymarket {len(d['polymarket']):,}, pinnacle {len(d['pinnacle']):,}")
    for v in ("kalshi", "polymarket"):
        if not d[v].empty:
            sp = (d[v]["best_yes_ask"] - d[v]["best_yes_bid"]) * 100
            print(f"  {v} spread mean {sp.mean():.2f}c, median {sp.median():.2f}c; best-ask size median "
                  f"{d[v]['yes_ask_sz_1'].median():.0f}")
    report_executable(executable(d))
    report_sharp(sharp(d))
    if not args.skip_history:
        report_history(asyncio.run(history_vs_live(d)))


if __name__ == "__main__":
    main()
