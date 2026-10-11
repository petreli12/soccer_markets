"""The Odds API v4: live h2h odds (read-only, API key from .env).

Credits: one call costs 1 credit per market per region (up to 10 bookmakers count as one region).
Every response carries ``x-requests-remaining``; :class:`OddsIngest` exposes it and refuses to call
below the configured floor.
"""

from __future__ import annotations

import logging
from typing import Any

from pm_edge.config import Config, get_secrets
from pm_edge.http import ApiClient
from pm_edge.models import Outcome

log = logging.getLogger(__name__)


class CreditFloorReached(RuntimeError):
    """Raised instead of calling the API once remaining credits fall below the floor."""


def map_h2h_outcome(name: str, home_team: str, away_team: str) -> Outcome | None:
    """Outcome names are the exact ``home_team``/``away_team`` strings or ``"Draw"`` (verified); order varies."""
    if name == "Draw":
        return Outcome.DRAW
    if name == home_team:
        return Outcome.HOME
    if name == away_team:
        return Outcome.AWAY
    return None


def parse_h2h(events: list[dict[str, Any]], league: str) -> list[dict[str, Any]]:
    """Flatten ``/odds`` events to one row per (event, bookmaker, outcome)."""
    rows = []
    for ev in events:
        for book in ev.get("bookmakers") or []:
            for market in book.get("markets") or []:
                if market.get("key") != "h2h":
                    continue
                for o in market.get("outcomes") or []:
                    outcome = map_h2h_outcome(o.get("name", ""), ev["home_team"], ev["away_team"])
                    rows.append({
                        "league": league,
                        "odds_event_id": ev["id"],
                        "home_team": ev["home_team"],
                        "away_team": ev["away_team"],
                        "commence_time": ev["commence_time"],
                        "bookmaker": book["key"],
                        "last_update": market.get("last_update") or book.get("last_update"),
                        "outcome_name": o.get("name"),
                        "outcome": outcome.value if outcome else None,
                        "price": float(o["price"]),
                    })
    return rows


class OddsIngest:
    """Minimal Odds API client for live h2h odds."""

    def __init__(self, cfg: Config, api: ApiClient | None = None, credit_floor: int | None = None) -> None:
        key = get_secrets().odds_api_key
        if not key:
            raise RuntimeError("ODDS_API_KEY is not set in .env")
        self._key = key
        self.cfg = cfg
        self.api = api or ApiClient(cfg.venues.odds_api.base_url, rate_per_second=1)
        self.credit_floor = cfg.venues.odds_api.credit_floor if credit_floor is None else credit_floor
        self.remaining: int | None = None
        self.used: int | None = None

    async def __aenter__(self) -> OddsIngest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.api.aclose()

    async def h2h(self, league_name: str, bookmakers: list[str]) -> list[dict[str, Any]]:
        """Live h2h odds for a league's sport key, flattened. Raises :class:`CreditFloorReached` at the floor."""
        if self.remaining is not None and self.remaining < self.credit_floor:
            raise CreditFloorReached(f"{self.remaining} credits left (< floor {self.credit_floor})")
        sport_key = self.cfg.league(league_name).odds_api.sport_key
        data = await self.api.get_json(
            f"/v4/sports/{sport_key}/odds",
            params={
                "apiKey": self._key,
                "markets": "h2h",
                "bookmakers": ",".join(bookmakers),
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )
        headers = self.api.last_response.headers if self.api.last_response is not None else {}
        if "x-requests-remaining" in headers:
            self.remaining = int(float(headers["x-requests-remaining"]))
            self.used = int(float(headers.get("x-requests-used", 0)))
        log.info("odds %s: %d events, credits remaining %s", league_name, len(data), self.remaining)
        return parse_h2h(data, league_name)
