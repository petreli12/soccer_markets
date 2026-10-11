"""Helpers shared by the venue ingest modules."""

from __future__ import annotations

import pandas as pd

from pm_edge.models import VenueMatch


def matches_to_frame(matches: list[VenueMatch]) -> pd.DataFrame:
    """One row per (match, outcome) contract, suitable for parquet and for matching.py."""
    rows = []
    for m in matches:
        for c in m.contracts.values():
            rows.append(
                {
                    "venue": m.venue.value,
                    "league": m.league,
                    "venue_event_id": m.venue_event_id,
                    "title": m.title,
                    "home_name": m.home_name,
                    "away_name": m.away_name,
                    "kickoff_utc": pd.Timestamp(m.kickoff_utc),
                    "status": m.status,
                    "outcome": c.outcome.value,
                    "market_id": c.market_id,
                    "yes_id": c.yes_id,
                    "no_id": c.no_id,
                    "label": c.label,
                    "fee_rate": c.fee.rate,
                    "fee_multiplier": c.fee.multiplier,
                    "fee_exponent": c.fee.exponent,
                    "min_order_size": c.min_order_size,
                    "tick_size": c.tick_size,
                    "result": c.result,
                }
            )
    return pd.DataFrame(rows)
