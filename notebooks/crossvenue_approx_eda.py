"""Exploratory, APPROXIMATE: Kalshi vs Polymarket cross-venue edges on matched EPL matches (not production).

Inputs: the backfilled Kalshi candles (real YES bid/ask) and Polymarket ``/prices-history`` (one price
``p`` per ~minute, no bid/ask). Matches are paired with the provisional join from
``step3_crossvenue_check.py``. Only minutes before kickoff are used.

Polymarket asks are approximated from ``p`` with an assumed half-spread ``h``:
    PM YES ask = p + h,  PM NO ask = (1 - p) + h     for h in {0.5, 1.0, 2.0} cents
Kalshi asks are real: YES ask = candle yes_ask close, NO ask = 1 - yes_bid close.

Per outcome and minute, two cross-venue pairs each pay exactly $1:
    A: Kalshi YES + Polymarket NO      B: Polymarket YES + Kalshi NO
Fee-adjusted cost per pair uses fees.py at a $100 reference notional. That buys
N = floor(100 / raw pair cost, 0.01) of each leg, with one taker order per leg and the per-order
rounding of each venue's fee. Net edge = 1 - (sum of leg cost) / N.

Grids: Kalshi is forward-filled without a staleness limit (verified). Polymarket at minute ``ts``
is the last history point at or before ``ts``, up to the configured 5-minute staleness, else NaN.
Polymarket points land ~15 s past the minute, so the sampled value can be up to ~45 s older than
Kalshi's; this biases the lag analysis toward "Polymarket trails" by up to ~1 minute.

Lag analysis: a "move" is |x(t) - x(t-2)| > 3c on either venue's price for an outcome (Kalshi mid,
Polymarket p). Moves on the same venue/outcome within 10 minutes of a previous one are merged. For
each move, the lag is the first k in [-2, 30] minutes such that the other venue has moved at least half
as far, in the same direction, from its own value at t-2. k <= 0 means the other venue moved at the
same time or first.

Run:  source .venv/bin/activate && python notebooks/crossvenue_approx_eda.py
"""

from __future__ import annotations

import math
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from kalshi_overround_eda import minute_grid  # noqa: E402
from step3_crossvenue_check import provisional_join  # noqa: E402

from pm_edge.config import get_config  # noqa: E402
from pm_edge.fees import FeeModel  # noqa: E402
from pm_edge.models import FeeParams, Venue  # noqa: E402

LEAGUE = "EPL"
OUTCOMES = ["HOME", "DRAW", "AWAY"]
HALF_SPREADS = [0.005, 0.010, 0.020]
BUCKETS = [(1440, 2880, "48h-24h"), (120, 1440, "24h-2h"), (60, 120, "2h-60m"), (0, 60, "60m-KO")]
MOVE_THRESHOLDS = [0.03, 0.02, 0.01]  # 3c is the spec; 2c and 1c are a sensitivity check
MOVE_WINDOW = 2
MOVE_COOLDOWN = 10
MAX_LAG = 30
OUT_DIR = Path(__file__).parent / "output"

cfg = get_config()
FEES = FeeModel(cfg.fees)
NOTIONAL = cfg.analysis.reference_notional_usd
LINEUP = next(w for w in cfg.analysis.focus_windows if w.name == "lineup")


@lru_cache(maxsize=None)
def _fee(venue: str, price: float, n: float, rate: float, mult: float, exp: float) -> float:
    return FEES.taker_fee(Venue(venue), price, n, FeeParams(rate=rate, multiplier=mult, exponent=exp))


def pair_net_edge(k_price: float, k_fee: FeeParams, p_price: float, p_fee: FeeParams) -> float:
    """Net edge per $1 payout of one Kalshi leg + one Polymarket leg at the reference notional."""
    if not (0 < k_price < 1 and 0 < p_price < 1):
        return math.nan
    k_price, p_price = round(k_price, 6), round(p_price, 6)
    raw = k_price + p_price
    n = math.floor(NOTIONAL / raw * 100) / 100
    if n < 5:  # Polymarket min_order_size; never binds at $100
        return math.nan
    cost = raw * n
    cost += _fee("kalshi", k_price, n, k_fee.rate, k_fee.multiplier, k_fee.exponent)
    cost += _fee("polymarket", p_price, n, p_fee.rate, p_fee.multiplier, p_fee.exponent)
    return 1 - cost / n


def bucket_of(mtk: pd.Series) -> pd.Series:
    out = pd.Series(pd.NA, index=mtk.index, dtype="object")
    for lo, hi, name in BUCKETS:
        out[(mtk > lo) & (mtk <= hi)] = name
    return out


def build_minutes(cfg) -> pd.DataFrame:
    """Long frame: one row per (match, outcome, pre-kickoff minute) with both venues' prices."""
    raw = cfg.resolve(cfg.paths.raw_dir)
    k = pd.read_parquet(raw / "kalshi" / f"matches_league={LEAGUE}.parquet")
    p = pd.read_parquet(raw / "polymarket" / f"matches_league={LEAGUE}.parquet")
    kc = pd.concat(pd.read_parquet(f) for f in (raw / "kalshi" / "candles" / f"league={LEAGUE}").glob("*.parquet"))
    joined, _ = provisional_join(k, p, cfg.matching.kickoff_tolerance_minutes)
    joined = joined[joined["status"] == "matched"]
    stale = pd.Timedelta(minutes=cfg.normalize.staleness_minutes.polymarket)

    frames = []
    for _, j in joined.iterrows():
        krows = k[k["venue_event_id"] == j["kalshi_event"]]
        g = minute_grid(krows, kc[kc["venue_event_id"] == j["kalshi_event"]])
        g = g[g["minutes_to_kickoff"] > 0]
        hist = pd.read_parquet(raw / "polymarket" / "history" / f"league={LEAGUE}" / f"{j['pm_event']}.parquet")
        prows = p[p["venue_event_id"] == j["pm_event"]].set_index("outcome")
        kfees = krows.set_index("outcome")
        for o in OUTCOMES:
            h = hist[hist["outcome"] == o].sort_values("ts")[["ts", "price"]]
            h["ts"] = h["ts"].astype("datetime64[us, UTC]")
            left = g[["ts", "minutes_to_kickoff", f"bid_{o}", f"ask_{o}"]].rename(
                columns={f"bid_{o}": "k_bid", f"ask_{o}": "k_ask"}
            )
            left["ts"] = left["ts"].astype("datetime64[us, UTC]")
            m = pd.merge_asof(left.sort_values("ts"), h, on="ts", direction="backward", tolerance=stale)
            m = m.rename(columns={"price": "p"})
            m["match"] = j["kalshi_title"]
            m["kalshi_event"] = j["kalshi_event"]
            m["outcome"] = o
            m["k_fee"] = [FeeParams(rate=kfees.loc[o, "fee_rate"], multiplier=kfees.loc[o, "fee_multiplier"])] * len(m)
            m["p_fee"] = [FeeParams(rate=prows.loc[o, "fee_rate"], exponent=prows.loc[o, "fee_exponent"])] * len(m)
            frames.append(m)
    df = pd.concat(frames, ignore_index=True).dropna(subset=["k_bid", "k_ask", "p"])
    df["k_mid"] = (df["k_bid"] + df["k_ask"]) / 2
    df["k_no_ask"] = (1 - df["k_bid"]).round(6)
    df["bucket"] = bucket_of(df["minutes_to_kickoff"])
    df["lineup"] = df["minutes_to_kickoff"].between(LINEUP.min_minutes, LINEUP.max_minutes)
    print(f"{joined.shape[0]} matched matches, {len(df):,} outcome-minutes pre-kickoff with both venues priced "
          f"({df.groupby(['kalshi_event', 'ts']).ngroups:,} match-minutes)")
    return df


def episodes(prof: pd.DataFrame) -> pd.Series:
    """Durations (minutes) of consecutive profitable runs per (match, outcome, direction)."""
    if prof.empty:
        return pd.Series(dtype=float)
    prof = prof.sort_values(["kalshi_event", "outcome", "direction", "ts"])
    key = prof[["kalshi_event", "outcome", "direction"]].astype(str).agg("|".join, axis=1)
    new = (prof["ts"].diff() != pd.Timedelta(minutes=1)) | (key != key.shift())
    return prof.groupby(new.cumsum()).size()


def edge_analysis(df: pd.DataFrame) -> list[dict]:
    rows = []
    for h in HALF_SPREADS:
        a = [pair_net_edge(ka, kf, 1 - p + h, pf) for ka, p, kf, pf in zip(df["k_ask"], df["p"], df["k_fee"], df["p_fee"])]
        b = [pair_net_edge(kn, kf, p + h, pf) for kn, p, kf, pf in zip(df["k_no_ask"], df["p"], df["k_fee"], df["p_fee"])]
        long = pd.concat(
            [df.assign(direction="K_YES+P_NO", net_edge=a), df.assign(direction="P_YES+K_NO", net_edge=b)],
            ignore_index=True,
        )
        prof = long[long["net_edge"] > 0]
        eps = episodes(prof)
        best = long.groupby(["kalshi_event", "outcome", "ts"])["net_edge"].max()
        row = {
            "h_cents": h * 100,
            "profitable_minutes": len(prof),
            "episodes": len(eps),
            "median_episode_min": eps.median() if len(eps) else np.nan,
            "max_episode_min": eps.max() if len(eps) else np.nan,
            "matches_with_episode": prof["kalshi_event"].nunique(),
            "best_edge_p50_c": best.quantile(0.50) * 100,
            "best_edge_p99_c": best.quantile(0.99) * 100,
            "best_edge_p999_c": best.quantile(0.999) * 100,
            "best_edge_max_c": best.max() * 100,
            "prof_edge_median_c": prof["net_edge"].median() * 100 if len(prof) else np.nan,
            "prof_ge_2c_minutes": int((prof["net_edge"] >= 0.02).sum()),
            "by_direction": prof["direction"].value_counts().to_dict(),
        }
        for _, _, name in BUCKETS:
            sub = prof[prof["bucket"] == name]
            row[f"min_{name}"] = len(sub)
        lu = prof[prof["lineup"]]
        row["min_lineup_75-45"] = len(lu)
        row["ep_lineup_75-45"] = len(episodes(lu))
        rows.append(row)
        long.loc[long["net_edge"] > 0].drop(columns=["k_fee", "p_fee"]).to_parquet(
            OUT_DIR / f"crossvenue_profitable_h{int(h * 1000)}.parquet", index=False
        )
    return rows


def mid_gap(df: pd.DataFrame) -> pd.DataFrame:
    gap = ((df["k_mid"] - df["p"]) * 100).round(4)
    parts = {"all": slice(None)}
    parts.update({name: df["bucket"] == name for _, _, name in BUCKETS})
    parts["lineup_75-45"] = df["lineup"]
    out = []
    for name, mask in parts.items():
        g = gap[mask]
        a = g.abs()
        out.append({"window": name, "n": len(g), "mean_signed_c": g.mean(), "abs_p50": a.median(), "abs_p90": a.quantile(0.9),
                    "abs_p99": a.quantile(0.99), "abs_max": a.max(), "share_ge_2c": (a >= 2).mean(),
                    "share_ge_3c": (a >= 3).mean(), "share_ge_4c": (a >= 4).mean()})
    by_outcome = df.assign(gap=gap.abs()).groupby("outcome")["gap"].agg(
        p50="median", p90=lambda s: s.quantile(0.9), share_ge_4c=lambda s: (s >= 4).mean()
    )
    print("\nRaw mid gap |Kalshi mid - Polymarket p| by outcome (cents):")
    print(by_outcome.round(3).to_string())
    return pd.DataFrame(out)


def lag_analysis(df: pd.DataFrame, threshold: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = []
    for (ev, o), g in df.groupby(["kalshi_event", "outcome"]):
        g = g.sort_values("ts").set_index("ts")
        full = pd.date_range(g.index.min(), g.index.max(), freq="1min")
        g = g.reindex(full)
        series = {"kalshi": g["k_mid"].to_numpy(), "polymarket": g["p"].to_numpy()}
        mtk = g["minutes_to_kickoff"].to_numpy()
        for lead, follow in (("kalshi", "polymarket"), ("polymarket", "kalshi")):
            x, y = series[lead], series[follow]
            last = -10**9
            for t in range(MOVE_WINDOW, len(x)):
                d = round(x[t] - x[t - MOVE_WINDOW], 6)  # 0.03 must not read as 0.0300000001
                if np.isnan(d) or abs(d) <= threshold or t - last < MOVE_COOLDOWN:
                    continue
                last = t
                base = y[t - MOVE_WINDOW]
                lag = None
                if not np.isnan(base):
                    for k in range(-MOVE_WINDOW, MAX_LAG + 1):
                        if t + k >= len(y) or np.isnan(y[t + k]):
                            continue
                        if round(np.sign(d) * (y[t + k] - base), 6) >= abs(d) / 2:
                            lag = k
                            break
                events.append({"match": ev, "outcome": o, "mover": lead, "ts": full[t], "move_c": d * 100,
                               "minutes_to_kickoff": mtk[t], "lag_min": lag, "follow_base_missing": np.isnan(base)})
    ev = pd.DataFrame(events)
    if ev.empty:
        return ev, ev
    ev["lineup"] = ev["minutes_to_kickoff"].between(LINEUP.min_minutes, LINEUP.max_minutes)
    ev["bucket"] = bucket_of(ev["minutes_to_kickoff"])

    def summarise(e: pd.DataFrame) -> dict:
        lag = e["lag_min"]
        return {"moves": len(e), "other_first_or_same(k<=0)": (lag <= 0).mean(), "k=1": (lag == 1).mean(),
                "k=2-5": lag.between(2, 5).mean(), "k=6-30": lag.between(6, 30).mean(), "never<=30": lag.isna().mean(),
                "median_lag_when_followed": lag[lag > 0].median()}

    groups = [("all", ev)] + [("lineup_75-45", ev[ev["lineup"]])] + [(b, ev[ev["bucket"] == b]) for _, _, b in BUCKETS]
    summary = pd.DataFrame([{"window": w, "mover": m, **summarise(e[e["mover"] == m])}
                            for w, e in groups for m in ("kalshi", "polymarket") if (e["mover"] == m).any()])
    return ev, summary


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    df = build_minutes(cfg)

    print("\n=== APPROXIMATE cross-venue edges (Polymarket asks = p +/- h) ===")
    edges = pd.DataFrame(edge_analysis(df))
    print(edges.T.to_string())

    print("\n=== Raw mid gap |Kalshi mid - Polymarket p| (cents, before fees) ===")
    print(mid_gap(df).round(3).to_string(index=False))

    for th in MOVE_THRESHOLDS:
        print(f"\n=== Lag analysis: moves >{th * 100:.0f}c within {MOVE_WINDOW} min, other venue's time to move half as far ===")
        ev, summary = lag_analysis(df, th)
        if ev.empty:
            print("  no moves")
            continue
        print(summary.round(3).to_string(index=False))
        ev.to_csv(OUT_DIR / f"crossvenue_lag_events_{int(th * 100)}c.csv", index=False)
        print(f"  matches with >=1 move: {ev['match'].nunique()}; follower base missing: {ev['follow_base_missing'].sum()}")
        lu = ev[ev["lineup"]].sort_values("ts")
        if len(lu):
            print("  lineup-window moves:")
            print(lu[["match", "outcome", "mover", "ts", "minutes_to_kickoff", "move_c", "lag_min"]].to_string(index=False))
    edges.to_csv(OUT_DIR / "crossvenue_edges_summary.csv", index=False)


if __name__ == "__main__":
    main()
