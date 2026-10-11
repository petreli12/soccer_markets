"""Exploratory: Kalshi vs Polymarket coverage and spreads for EPL (not production code).

A. Provisional join of the backfilled Kalshi and Polymarket matches. This is a sanity check before
   step 4 (matching.py); it accepts a pair only if
   - kickoffs agree within ``matching.kickoff_tolerance_minutes``, and
   - each side's team agrees by an exact signal: Kalshi name == Polymarket name/alias/name minus " FC",
     or Kalshi ticker suffix == Polymarket abbreviation.
   Everything else is listed for review. For accepted pairs, both venues' settled results must agree
   outcome by outcome, which independently checks both HOME/DRAW/AWAY mappings.
B. Polymarket history coverage per match (points, pre-kickoff gaps).
C. Spread comparison. Polymarket has no historical bid/ask (``/prices-history`` is one price), so
   the comparison uses live books: open EPL matches on both venues, snapshotted at the same instants.

Run:  source .venv/bin/activate && python notebooks/step3_crossvenue_check.py [--snapshots 6 --interval 20]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import pandas as pd

from pm_edge.config import get_config
from pm_edge.ingest.kalshi import KalshiIngest
from pm_edge.ingest.polymarket import PolymarketIngest
from pm_edge.models import VenueMatch

LEAGUE = "EPL"


def _names(name: str, alias: str = "") -> set[str]:
    out = {name.casefold(), alias.casefold()} - {""}
    out |= {n.removesuffix(" fc").strip() for n in out}
    return out


def team_signal(k_name: str, k_abbr: str, p_name: str, p_alias: str, p_abbr: str) -> str | None:
    """Which exact signal ties a Kalshi team to a Polymarket team, or ``None``."""
    if k_name.casefold() in _names(p_name, p_alias):
        return "name"
    if k_abbr and p_abbr and k_abbr.casefold() == p_abbr.casefold():
        return "abbr"
    return None


def provisional_join(k: pd.DataFrame, p: pd.DataFrame, tol_min: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per Kalshi match: the Polymarket event it pairs with, or the reason it does not."""
    km = k.groupby("venue_event_id").first().reset_index()
    abbrs = k.assign(abbr=k["market_id"].str.rsplit("-", n=1).str[-1]).pivot(
        index="venue_event_id", columns="outcome", values="abbr"
    )
    pm = p.groupby("venue_event_id").first().reset_index()
    rows = []
    for _, r in km.iterrows():
        dt = (pm["kickoff_utc"] - r["kickoff_utc"]).abs().dt.total_seconds() / 60
        cands = pm[dt <= tol_min]
        best = None
        for _, c in cands.iterrows():
            hs = team_signal(r["home_name"], abbrs.loc[r["venue_event_id"], "HOME"], c["home_name"], c["home_alias"], c["home_abbr"])
            as_ = team_signal(r["away_name"], abbrs.loc[r["venue_event_id"], "AWAY"], c["away_name"], c["away_alias"], c["away_abbr"])
            if hs and as_:
                best = (c, hs, as_)
                break
        if best is None:
            rows.append({"kalshi_event": r["venue_event_id"], "kalshi_title": r["title"], "status": "review",
                         "reason": f"{len(cands)} Polymarket events within {tol_min} min, none with exact team agreement"})
            continue
        c, hs, as_ = best
        rows.append({
            "kalshi_event": r["venue_event_id"], "kalshi_title": r["title"], "status": "matched",
            "pm_event": c["venue_event_id"], "pm_slug": c["slug"], "pm_title": c["title"],
            "kickoff_diff_min": (c["kickoff_utc"] - r["kickoff_utc"]).total_seconds() / 60,
            "home_signal": hs, "away_signal": as_,
        })
    joined = pd.DataFrame(rows)

    res_k = k.pivot(index="venue_event_id", columns="outcome", values="result")
    res_p = p.pivot(index="venue_event_id", columns="outcome", values="result")
    m = joined[joined["status"] == "matched"]
    checks = []
    for _, r in m.iterrows():
        a, b = res_k.loc[r["kalshi_event"]], res_p.loc[r["pm_event"]]
        checks.append({"kalshi_event": r["kalshi_event"], "agree": bool((a == b).all()),
                       "kalshi": a.to_dict(), "polymarket": b.to_dict()})
    return joined, pd.DataFrame(checks)


def history_coverage(p: pd.DataFrame, hist: pd.DataFrame) -> pd.DataFrame:
    ko = p.groupby("venue_event_id")["kickoff_utc"].first()
    out = []
    for (ev, oc), g in hist.groupby(["venue_event_id", "outcome"]):
        pre = g[g["ts"] < ko[ev]].sort_values("ts")
        gaps = pre["ts"].diff().dt.total_seconds().div(60).dropna()
        out.append({"venue_event_id": ev, "outcome": oc, "points": len(g), "pre_points": len(pre),
                    "first_minutes_before_ko": (ko[ev] - pre["ts"].min()).total_seconds() / 60 if len(pre) else None,
                    "pre_gaps_over_5min": int((gaps > 5).sum()), "max_pre_gap_min": gaps.max() if len(gaps) else None})
    return pd.DataFrame(out)


async def spread_snapshots(n: int, interval: float) -> pd.DataFrame:
    """Snapshot best bid/ask on both venues for currently open EPL matches."""
    cfg = get_config()
    now = datetime.now(UTC)
    async with KalshiIngest(cfg) as k, PolymarketIngest(cfg) as p:
        km, _ = await k.list_matches(LEAGUE, statuses=("open",), kickoff_from=now)
        pmm, _ = await p.list_matches(LEAGUE, closed=False, kickoff_from=now)
        pairs = _pair_live(km, pmm, cfg.matching.kickoff_tolerance_minutes)
        print(f"\nC. live: {len(km)} open Kalshi, {len(pmm)} open Polymarket EPL matches, {len(pairs)} paired")
        rows = []
        for i in range(n):
            kb = await k.fetch_orderbooks([c.market_id for m, _ in pairs for c in m.contracts.values()])
            pb = await p.fetch_orderbooks([pm for _, pm in pairs])
            ts = datetime.now(UTC)
            kbm, pbm = {b.market_id: b for b in kb}, {b.market_id: b for b in pb}
            for km_, pm_ in pairs:
                mtk = (km_.kickoff_utc - ts).total_seconds() / 60
                for o, c in km_.contracts.items():
                    for venue, book in (("kalshi", kbm.get(c.market_id)), ("polymarket", pbm.get(pm_.contracts[o].market_id))):
                        if book and book.best_yes_bid is not None and book.best_yes_ask is not None:
                            rows.append({"snap": i, "match": km_.title, "venue": venue, "outcome": o.value,
                                         "minutes_to_kickoff": mtk, "bid": book.best_yes_bid, "ask": book.best_yes_ask,
                                         "spread": book.best_yes_ask - book.best_yes_bid,
                                         "ask_size": book.yes_asks[0].size})
            if i < n - 1:
                await asyncio.sleep(interval)
    return pd.DataFrame(rows)


def _pair_live(km: list[VenueMatch], pmm: list[VenueMatch], tol: int) -> list[tuple[VenueMatch, VenueMatch]]:
    pairs = []
    for a in km:
        abbr = {o.value: c.market_id.rsplit("-", 1)[-1] for o, c in a.contracts.items()}
        for b in pmm:
            if abs((a.kickoff_utc - b.kickoff_utc).total_seconds()) / 60 > tol:
                continue
            if team_signal(a.home_name, abbr["HOME"], b.home_name, b.extra["home_alias"], b.extra["home_abbr"]) and team_signal(
                a.away_name, abbr["AWAY"], b.away_name, b.extra["away_alias"], b.extra["away_abbr"]
            ):
                pairs.append((a, b))
                break
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=int, default=6)
    ap.add_argument("--interval", type=float, default=20)
    args = ap.parse_args()
    cfg = get_config()
    raw = cfg.resolve(cfg.paths.raw_dir)
    k = pd.read_parquet(raw / "kalshi" / f"matches_league={LEAGUE}.parquet")
    p = pd.read_parquet(raw / "polymarket" / f"matches_league={LEAGUE}.parquet")
    hist = pd.concat(pd.read_parquet(f) for f in (raw / "polymarket" / "history" / f"league={LEAGUE}").glob("*.parquet"))
    pd.set_option("display.width", 220)

    joined, checks = provisional_join(k, p, cfg.matching.kickoff_tolerance_minutes)
    print(f"A. Kalshi matches: {k['venue_event_id'].nunique()}, Polymarket matches: {p['venue_event_id'].nunique()}")
    print(joined["status"].value_counts().to_string())
    print("team signals used:", pd.concat([joined["home_signal"], joined["away_signal"]]).value_counts().to_dict())
    print("kickoff diff (min):", joined["kickoff_diff_min"].describe()[["min", "max"]].to_dict())
    print(f"settled results agree outcome-by-outcome: {checks['agree'].sum()}/{len(checks)}")
    review = joined[joined["status"] == "review"]
    if len(review):
        print(review[["kalshi_event", "kalshi_title", "reason"]].to_string(index=False))
    unpaired_pm = set(p["venue_event_id"]) - set(joined.get("pm_event", pd.Series(dtype=str)).dropna())
    print("Polymarket events with no Kalshi partner:", sorted(p[p["venue_event_id"].isin(unpaired_pm)]["slug"].unique()))
    print("name pairs that needed the abbreviation signal:")
    abbr_rows = joined[(joined["home_signal"] == "abbr") | (joined["away_signal"] == "abbr")]
    print(abbr_rows[["kalshi_title", "pm_title"]].to_string(index=False) if len(abbr_rows) else "  none")

    cov = history_coverage(p, hist)
    print(f"\nB. Polymarket history: {len(hist):,} points; per outcome market: pre-kickoff points median "
          f"{cov['pre_points'].median():.0f}, starts median {cov['first_minutes_before_ko'].median():.0f} min before KO; "
          f"pre-kickoff gaps >5 min: {cov['pre_gaps_over_5min'].sum()} total, max gap {cov['max_pre_gap_min'].max():.0f} min")

    kc = pd.concat(pd.read_parquet(f) for f in (raw / "kalshi" / "candles" / f"league={LEAGUE}").glob("*.parquet"))
    kc = kc.merge(k[["venue_event_id", "kickoff_utc"]].drop_duplicates(), on="venue_event_id")
    kc["mtk"] = (kc["kickoff_utc"] - kc["ts"]).dt.total_seconds() / 60
    kc["spread"] = kc["yes_ask"] - kc["yes_bid"]
    print(f"   Kalshi historical spread (candle rows, T-48h..T+3h): mean {kc['spread'].mean()*100:.2f}c, "
          f"pre-kickoff mean {kc.loc[kc['mtk'] > 0, 'spread'].mean()*100:.2f}c, median {kc['spread'].median()*100:.1f}c")

    snaps = asyncio.run(spread_snapshots(args.snapshots, args.interval))
    if snaps.empty:
        print("   no paired open matches with two-sided books")
        return
    snaps["horizon"] = pd.cut(snaps["minutes_to_kickoff"], [0, 2880, 1e9], labels=["<=48h", ">48h"])
    summary = snaps.groupby(["horizon", "venue"], observed=True)["spread"].agg(["mean", "median", "count"])
    summary[["mean", "median"]] *= 100
    print(summary.round(2).to_string())
    per_match = snaps.groupby(["match", "venue"])["spread"].mean().unstack().mul(100).round(2)
    print(per_match.to_string())
    print("top-of-book ask size (contracts/shares), median:", snaps.groupby("venue")["ask_size"].median().round(0).to_dict())


if __name__ == "__main__":
    main()
