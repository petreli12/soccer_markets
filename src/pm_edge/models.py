"""Shared pydantic data models used across venues."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"
    SPORTSBOOK = "sportsbook"


class Outcome(StrEnum):
    HOME = "HOME"
    DRAW = "DRAW"
    AWAY = "AWAY"


class FeeParams(BaseModel):
    """Venue fee parameters attached to a specific market (resolved from series/event/market metadata)."""

    rate: float
    multiplier: float = 1.0
    exponent: float = 1.0


class VenueContract(BaseModel):
    """One outcome of a match on one prediction venue.

    Both venues list a separate binary market per outcome. ``yes_id`` buys the outcome,
    ``no_id`` buys against it. For Kalshi both sides live on one ticker, so ``no_id`` equals
    ``yes_id``; for Polymarket they are distinct CLOB token ids.
    """

    outcome: Outcome
    market_id: str
    yes_id: str
    no_id: str
    label: str
    fee: FeeParams
    min_order_size: float = 0.0
    tick_size: float = 0.01
    result: str | None = None


class VenueMatch(BaseModel):
    """A single match as listed on one venue, with contracts mapped to HOME/DRAW/AWAY."""

    venue: Venue
    league: str
    venue_event_id: str
    title: str
    home_name: str
    away_name: str
    kickoff_utc: datetime
    status: str
    contracts: dict[Outcome, VenueContract] = Field(default_factory=dict)
    extra: dict[str, str] = Field(default_factory=dict)

    def is_complete(self, outcomes: list[str]) -> bool:
        """True when every configured outcome has a mapped contract."""
        return all(Outcome(o) in self.contracts for o in outcomes)


class PriceLevel(BaseModel):
    price: float
    size: float


class BinaryBook(BaseModel):
    """Executable view of a binary market: asks to buy YES and asks to buy NO, best first."""

    venue: Venue
    market_id: str
    ts_utc: datetime
    yes_bids: list[PriceLevel]
    yes_asks: list[PriceLevel]
    no_bids: list[PriceLevel]
    no_asks: list[PriceLevel]
    last_trade_price: float | None = None
    min_order_size: float = 0.0
    tick_size: float | None = None

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def best_no_ask(self) -> float | None:
        return self.no_asks[0].price if self.no_asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return (self.best_yes_bid + self.best_yes_ask) / 2
