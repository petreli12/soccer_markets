"""Exploratory: Kalshi intra-venue overround on the EPL backfill (not production code).

For every pre-kickoff minute of every backfilled match, with all three legs quoted:
  YES bundle: buy YES on HOME, DRAW and AWAY at the ask; pays $1. Edge if cost < 1.
  NO bundle:  buy NO on all three at the ask (NO ask = 1 - YES bid); pays $2. Edge if cost < 2.

Fee-adjusted cost uses fees.py at a $100 reference notional. The bundle size is
N = floor(100 / raw bundle price, 0.01) contracts per leg. Each leg is one taker
order of N contracts, with Kalshi's per-order fee rounding. The cost is reported per
bundle: (sum of leg cost) / N.

Grid: 1-minute timestamps from the first candle up to kickoff. Each leg is forward-filled
without a staleness limit; docs/api_notes.md §1 shows a missing Kalshi candle means
no quote change.

Run:  source .venv/bin/activate && python notebooks/kalshi_overround_eda.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from pm_edge.config import get_config
from pm_edge.fees import FeeModel
from pm_edge.models import FeeParams, Venue

LEAGUE = "EPL"
OUTCOMES = ["HOME", "DRAW", "AWAY"]
OUT_DIR = Path(__file__).parent / "output"


def load(cfg) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = cfg.resolve(cfg.paths.raw_dir) / "kalshi"
    matches = pd.read_parquet(raw / f"matches_league={LEAGUE}.parquet")
    candles = pd.concat(
        pd.read_parquet(p) for p in sorted((raw / "candles" / f"league={LEAGUE}").glob("*.parquet"))
    )
    return matches, candles


def minute_grid(match_rows: pd.DataFrame, candles: pd.DataFrame) -> pd.DataFrame:
    """Wide per-minute frame for one match: forward-filled bid/ask per leg, plus 'real candle' flags."""
    kickoff = match_rows["kickoff_utc"].iloc[0]
    c = candles[candles["ts"] <= kickoff]
    if c.empty:
        return pd.DataFrame()
    idx = pd.date_range(c["ts"].min(), kickoff, freq="1min", name="ts")
    parts = {}
    for outcome in OUTCOMES:
        leg = c[c["outcome"] == outcome].set_index("ts").sort_index()
        leg = leg[~leg.index.duplicated(keep="last")].reindex(idx)
        parts[f"real_{outcome}"] = leg["yes_ask"].notna()
        # Minutes since this leg last printed a real candle (0 = real candle this minute).
        real_pos = np.where(leg["yes_ask"].notna(), np.arange(len(idx)), np.nan)
        parts[f"age_{outcome}"] = np.arange(len(idx)) - pd.Series(real_pos).ffill().to_numpy()
        # A real candle that ends a gap: the previous minute had no real candle for this leg.
        parts[f"resume_{outcome}"] = leg["yes_ask"].notna() & ~leg["yes_ask"].notna().shift(1, fill_value=True)
        ffilled = leg[["yes_bid", "yes_ask"]].ffill()
        parts[f"bid_{outcome}"] = ffilled["yes_bid"]
        parts[f"ask_{outcome}"] = ffilled["yes_ask"]
    g = pd.DataFrame(parts, index=idx).reset_index()
    g["venue_event_id"] = match_rows["venue_event_id"].iloc[0]
    g["title"] = match_rows["title"].iloc[0]
    g["kickoff_utc"] = kickoff
    g["minutes_to_kickoff"] = (kickoff - g["ts"]).dt.total_seconds() / 60
    return g.dropna(subset=[f"{s}_{o}" for s in ("bid", "ask") for o in OUTCOMES])


def bundle_cost(
    prices: list[float], payout: float, fee_model: FeeModel, params: list[FeeParams], notional: float
) -> float:
    """Fee-inclusive cost per bundle (one contract of each leg) at ``notional`` total outlay."""
    raw = sum(prices)
    if raw <= 0:
        return np.nan
    n = float(np.floor(notional / raw * 100) / 100)  # contracts per leg, 0.01-granular
    if n <= 0:
        return np.nan
    total = sum(fee_model.leg_cost(Venue.KALSHI, p, n, fp) for p, fp in zip(prices, params))
    return total / n


def episodes(flags: pd.DataFrame) -> pd.DataFrame:
    """Group consecutive profitable minutes (per match) into episodes."""
    rows = []
    for event_id, g in flags.groupby("venue_event_id"):
        g = g.sort_values("ts")
        new = g["ts"].diff().ne(pd.Timedelta(minutes=1))
        for _, ep in g.groupby(new.cumsum()):
            rows.append(
                {
                    "venue_event_id": event_id,
                    "title": ep["title"].iloc[0],
                    "start": ep["ts"].iloc[0],
                    "minutes_to_kickoff_at_start": round(ep["minutes_to_kickoff"].iloc[0]),
                    "duration_min": len(ep),
                    "best_net_edge": ep["net_edge"].max(),
                    "mean_net_edge": ep["net_edge"].mean(),
                }
            )
    return pd.DataFrame(rows)


def describe(series: pd.Series, threshold: float, label: str) -> dict:
    s = series.dropna()
    return {
        "series": label,
        "minutes": len(s),
        "min": s.min(),
        "p01": s.quantile(0.01),
        "p05": s.quantile(0.05),
        "p50": s.quantile(0.50),
        f"share_below_threshold": (s < threshold).mean(),
        "threshold": threshold,
    }


def main() -> None:
    cfg = get_config()
    fee_model = FeeModel(cfg.fees)
    notional = cfg.analysis.reference_notional_usd
    matches, candles = load(cfg)

    grids = []
    for event_id, rows in matches.groupby("venue_event_id"):
        g = minute_grid(rows, candles[candles["venue_event_id"] == event_id])
        if g.empty:
            continue
        by_outcome = rows.set_index("outcome")
        g.attrs["fees"] = [
            FeeParams(rate=by_outcome.loc[o, "fee_rate"], multiplier=by_outcome.loc[o, "fee_multiplier"])
            for o in OUTCOMES
        ]
        grids.append(g)

    frames = []
    for g in grids:
        fp = g.attrs["fees"]
        asks = g[[f"ask_{o}" for o in OUTCOMES]].to_numpy()
        no_asks = (1 - g[[f"bid_{o}" for o in OUTCOMES]].to_numpy()).round(6)
        g = g.copy()
        g.attrs = {}
        # Prices are on a 1c grid; round away float noise (0.34+0.33+0.33 -> 0.9999999).
        g["yes_sum"] = asks.sum(axis=1).round(6)
        g["no_sum"] = no_asks.sum(axis=1).round(6)
        g["yes_cost_fee"] = [bundle_cost(list(r), 1.0, fee_model, fp, notional) for r in asks]
        g["no_cost_fee"] = [bundle_cost(list(r), 2.0, fee_model, fp, notional) for r in no_asks]
        frames.append(g)
    df = pd.concat(frames, ignore_index=True)

    stats = pd.DataFrame(
        [
            describe(df["yes_sum"], 1.0, "(a) sum YES asks"),
            describe(df["no_sum"], 2.0, "(b) sum NO asks"),
            describe(df["yes_cost_fee"], 1.0, "(c) YES bundle incl. fees"),
            describe(df["no_cost_fee"], 2.0, "(c) NO bundle incl. fees"),
        ]
    )
    pd.set_option("display.width", 200)
    print(f"{df['venue_event_id'].nunique()} matches, {len(df):,} pre-kickoff minutes with all three legs quoted\n")
    print(stats.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # (d) fee-adjusted profitable minutes -> episodes
    yes_prof = df[df["yes_cost_fee"] < 1.0].assign(net_edge=lambda d: 1.0 - d["yes_cost_fee"], bundle="YES")
    no_prof = df[df["no_cost_fee"] < 2.0].assign(net_edge=lambda d: 2.0 - d["no_cost_fee"], bundle="NO")
    print("\n(d) fee-adjusted profitable episodes")
    for name, prof in (("YES", yes_prof), ("NO", no_prof)):
        eps = episodes(prof) if len(prof) else pd.DataFrame()
        print(f"  {name} bundle: {len(prof)} minutes, {len(eps)} episodes")
        if len(eps):
            print(eps.sort_values("start").to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    # (e) do sub-threshold sums cluster after gaps / on stale legs / at particular times?
    df["any_resume"] = df[[f"resume_{o}" for o in OUTCOMES]].any(axis=1)
    df["all_real"] = df[[f"real_{o}" for o in OUTCOMES]].all(axis=1)
    df["max_leg_age"] = df[[f"age_{o}" for o in OUTCOMES]].max(axis=1)
    df["mtk_bucket"] = pd.cut(
        df["minutes_to_kickoff"], [-1, 15, 120, 360, 1440, 2880], labels=["<15m", "15m-2h", "2h-6h", "6h-24h", "24h-48h"]
    )
    df["utc_hour"] = df["ts"].dt.hour
    sub = {"YES<1": df["yes_sum"] < 1.0, "NO<2": df["no_sum"] < 2.0}
    print("\n(e) artifact checks: share of minutes with each property, all vs sub-threshold")
    for label, mask in sub.items():
        if not mask.any():
            print(f"  {label}: no minutes")
            continue
        s = df[mask]
        print(
            f"  {label}: n={mask.sum()}  any leg just resumed from gap: {s['any_resume'].mean():.1%} (all {df['any_resume'].mean():.1%})"
            f" | all legs real candle: {s['all_real'].mean():.1%} (all {df['all_real'].mean():.1%})"
            f" | median max-leg age: {s['max_leg_age'].median():.0f}m (all {df['max_leg_age'].median():.0f}m)"
        )
        print(f"    by minutes-to-kickoff: {(s['mtk_bucket'].value_counts(normalize=True).sort_index().round(3)).to_dict()}")
        print(f"    all minutes          : {(df['mtk_bucket'].value_counts(normalize=True).sort_index().round(3)).to_dict()}")
        print(f"    matches: {s['venue_event_id'].nunique()}, top: {s['venue_event_id'].value_counts().head(5).to_dict()}")
        print(f"    raw sums seen: {s['yes_sum' if label.startswith('YES') else 'no_sum'].round(2).value_counts().sort_index().to_dict()}")

    OUT_DIR.mkdir(exist_ok=True)
    df.to_parquet(OUT_DIR / "kalshi_epl_bundle_minutes.parquet", index=False)
    stats.to_csv(OUT_DIR / "kalshi_epl_bundle_stats.csv", index=False)


if __name__ == "__main__":
    main()
