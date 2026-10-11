"""Polymarket public market data: Gamma metadata, CLOB order books, and CLOB price history.

Each match is a neg-risk Gamma event with three binary Yes/No moneyline markets, one per outcome
(docs/api_notes.md §2). Only the full-time result event is used; halftime, exact-score and other
sibling events for the same fixture are ignored. No endpoint here requires authentication.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from pm_edge.config import Config, LeagueConfig
from pm_edge.http import ApiClient
from pm_edge.ingest.common import matches_to_frame
from pm_edge.models import BinaryBook, FeeParams, Outcome, PriceLevel, Venue, VenueContract, VenueMatch

log = logging.getLogger(__name__)

HISTORY_COLUMNS = ["venue_event_id", "market_id", "outcome", "ts", "price"]
_TITLE_SPLIT = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)
_SELECTION_LINE = {"home": Outcome.HOME, "draw": Outcome.DRAW, "away": Outcome.AWAY}


@dataclass
class UnmappedEvent:
    """A Polymarket full-time result event that could not be mapped to HOME/DRAW/AWAY with certainty."""

    league: str
    event_id: str
    slug: str
    title: str
    reason: str


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit tested offline)
# ---------------------------------------------------------------------------


def parse_json_list(value: Any) -> list[Any]:
    """Gamma encodes some arrays as JSON strings (``'["Yes", "No"]'``); accept either form."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return list(json.loads(value))


def parse_ts(value: str | None) -> datetime | None:
    """Parse Gamma timestamps: ``2026-08-21T19:00:00Z`` and ``2026-08-21 19:00:00+00``."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", v):
        v += ":00"
    return datetime.fromisoformat(v).astimezone(UTC)


def is_full_time_result_event(event: dict[str, Any], sport: str, market_type: str) -> bool:
    """True for the base fixture event (``epl-ars-cov-2026-08-21``), not its sibling prop events."""
    slug = event.get("slug") or ""
    return re.fullmatch(rf"{re.escape(sport)}-[a-z0-9]+-[a-z0-9]+-\d{{4}}-\d{{2}}-\d{{2}}", slug) is not None and any(
        m.get("sportsMarketType") == market_type for m in event.get("markets") or []
    )


def token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
    """``(yes_token, no_token)`` chosen by market ``version``: v1 ``clobTokenIds``, v2 ``positionIds``."""
    version = market.get("version") or "v1"
    ids = parse_json_list(market.get("positionIds") if version == "v2" else market.get("clobTokenIds"))
    outcomes = [str(o).casefold() for o in parse_json_list(market.get("outcomes"))]
    if len(ids) != 2:
        return None
    if outcomes and outcomes != ["yes", "no"]:
        if sorted(outcomes) != ["no", "yes"]:
            return None
        ids = [ids[outcomes.index("yes")], ids[outcomes.index("no")]]
    return str(ids[0]), str(ids[1])


def home_away_teams(event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Home and away entries of ``teams[]`` by their ``ordering`` field."""
    teams = {t.get("ordering"): t for t in event.get("teams") or []}
    if "home" in teams and "away" in teams:
        return teams["home"], teams["away"]
    return None


def _team_names(team: dict[str, Any]) -> set[str]:
    return {str(team[k]).strip().casefold() for k in ("name", "alias") if team.get(k)}


def map_market_outcome(market: dict[str, Any], home: dict[str, Any], away: dict[str, Any]) -> Outcome | None:
    """Map one moneyline market to an outcome, or ``None`` if signals disagree or are missing.

    Primary signal: ``marketMetadata.opticOddsSelectionLine`` (home/draw/away). Fallback (absent on
    some UCL markets): ``groupItemTitle`` starting with "Draw", or equal to a team's name/alias.
    Both are computed; a contradiction returns ``None`` so the event goes to review.
    """
    line = ((market.get("marketMetadata") or {}).get("opticOddsSelectionLine") or "").casefold()
    by_meta = _SELECTION_LINE.get(line)

    title = (market.get("groupItemTitle") or "").strip().casefold()
    by_title: Outcome | None = None
    if title.startswith("draw"):
        by_title = Outcome.DRAW
    elif title in _team_names(home) and title not in _team_names(away):
        by_title = Outcome.HOME
    elif title in _team_names(away) and title not in _team_names(home):
        by_title = Outcome.AWAY

    if by_meta and by_title and by_meta != by_title:
        return None
    return by_meta or by_title


def market_result(market: dict[str, Any]) -> str | None:
    """``"yes"``/``"no"`` once a market has resolved (``outcomePrices`` is ``["1","0"]`` or ``["0","1"]``)."""
    if not market.get("closed") or market.get("umaResolutionStatus") != "resolved":
        return None
    prices = [float(p) for p in parse_json_list(market.get("outcomePrices"))]
    if prices == [1.0, 0.0]:
        return "yes"
    if prices == [0.0, 1.0]:
        return "no"
    return None


def fee_params(market: dict[str, Any], cfg: Config) -> FeeParams:
    """Per-market ``feeSchedule`` (rate, exponent) with config defaults; zero when fees are disabled."""
    pm = cfg.fees.polymarket
    if market.get("feesEnabled") is False:
        return FeeParams(rate=0.0, exponent=pm.default_exponent)
    sched = market.get("feeSchedule") or {}
    return FeeParams(
        rate=float(sched.get("rate", pm.default_taker_rate)),
        exponent=float(sched.get("exponent", pm.default_exponent)),
    )


def parse_event(event: dict[str, Any], league: str, market_type: str, cfg: Config) -> VenueMatch | UnmappedEvent:
    """Build a :class:`VenueMatch` from a full-time result event."""
    title = event.get("title", "")

    def unmapped(reason: str) -> UnmappedEvent:
        return UnmappedEvent(league, str(event.get("id", "")), event.get("slug", ""), title, reason)

    teams = home_away_teams(event)
    if teams is None:
        return unmapped("teams[] lacks home/away ordering")
    home, away = teams
    split = [p.strip() for p in _TITLE_SPLIT.split(title.strip())]
    if len(split) != 2 or split[0].casefold() not in _team_names(home) or split[1].casefold() not in _team_names(away):
        return unmapped("title 'Home vs. Away' disagrees with teams[] ordering")

    kickoff = parse_ts(event.get("startTime"))
    markets = [m for m in event.get("markets") or [] if m.get("sportsMarketType") == market_type]
    game_starts = {parse_ts(m.get("gameStartTime")) for m in markets} - {None}
    if kickoff is None and len(game_starts) == 1:
        kickoff = game_starts.pop()
    if kickoff is None:
        return unmapped("no startTime/gameStartTime (kickoff unknown)")
    if any(abs((g - kickoff).total_seconds()) > 60 for g in game_starts if g):
        return unmapped("event startTime disagrees with market gameStartTime")

    contracts: dict[Outcome, VenueContract] = {}
    for m in markets:
        outcome = map_market_outcome(m, home, away)
        if outcome is None:
            return unmapped(f"cannot map market {m.get('id')} ({m.get('groupItemTitle')!r})")
        if outcome in contracts:
            return unmapped(f"duplicate {outcome} mapping (market {m.get('id')})")
        ids = token_ids(m)
        if ids is None:
            return unmapped(f"market {m.get('id')} has no Yes/No token pair")
        contracts[outcome] = VenueContract(
            outcome=outcome,
            market_id=m.get("conditionId") or str(m.get("id")),
            yes_id=ids[0],
            no_id=ids[1],
            label=m.get("groupItemTitle", ""),
            fee=fee_params(m, cfg),
            min_order_size=float(m.get("orderMinSize") or 0.0),
            tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
            result=market_result(m),
        )

    status = "closed" if event.get("closed") else ("live" if event.get("live") else "open")
    return VenueMatch(
        venue=Venue.POLYMARKET,
        league=league,
        venue_event_id=str(event["id"]),
        title=title,
        home_name=str(home.get("name", "")),
        away_name=str(away.get("name", "")),
        kickoff_utc=kickoff,
        status=status,
        contracts=contracts,
        extra={
            "slug": event.get("slug", ""),
            "home_alias": str(home.get("alias") or ""),
            "away_alias": str(away.get("alias") or ""),
            "home_abbr": str(home.get("abbreviation") or ""),
            "away_abbr": str(away.get("abbreviation") or ""),
            "neg_risk_market_id": event.get("negRiskMarketID") or "",
            "score": event.get("score") or "",
        },
    )


def _levels(raw: list[dict[str, str]], best_is_max: bool, depth: int | None) -> list[PriceLevel]:
    """Best-first levels. The live API's ordering differs from the docs, so never trust it: sort."""
    levels = sorted(((float(x["price"]), float(x["size"])) for x in raw), key=lambda x: x[0], reverse=best_is_max)
    if depth is not None:
        levels = levels[:depth]
    return [PriceLevel(price=p, size=s) for p, s in levels]


def parse_books(contract: VenueContract, yes_book: dict[str, Any], no_book: dict[str, Any], depth: int | None) -> BinaryBook:
    """Combine the Yes-token and No-token CLOB books of one outcome market."""
    ts_ms = yes_book.get("timestamp") or no_book.get("timestamp")
    ts = datetime.fromtimestamp(int(ts_ms) / 1000, UTC) if ts_ms else datetime.now(UTC)
    ltp = yes_book.get("last_trade_price")
    return BinaryBook(
        venue=Venue.POLYMARKET,
        market_id=contract.market_id,
        ts_utc=ts,
        yes_bids=_levels(yes_book.get("bids") or [], best_is_max=True, depth=depth),
        yes_asks=_levels(yes_book.get("asks") or [], best_is_max=False, depth=depth),
        no_bids=_levels(no_book.get("bids") or [], best_is_max=True, depth=depth),
        no_asks=_levels(no_book.get("asks") or [], best_is_max=False, depth=depth),
        last_trade_price=float(ltp) if ltp not in (None, "") else None,
        min_order_size=float(yes_book.get("min_order_size") or contract.min_order_size),
        tick_size=float(yes_book.get("tick_size") or contract.tick_size),
    )


def last_trade_yes(trade: dict[str, Any]) -> tuple[float, datetime, str] | None:
    """YES-equivalent price, time and token of a data-api trade (a NO trade at p is YES at 1 - p).

    Needed because the CLOB book's ``last_trade_price`` is market-level and quoted in whichever token
    traded last (verified 2026-10-11: both books show the NO trade price 0.69 while YES is 0.30/0.31).
    """
    outcome = str(trade.get("outcome", "")).casefold()
    if outcome not in {"yes", "no"} or trade.get("price") is None:
        return None
    price = float(trade["price"])
    ts = datetime.fromtimestamp(int(trade["timestamp"]), UTC)
    return (price if outcome == "yes" else round(1 - price, 6)), ts, outcome


def parse_price_history(history: list[dict[str, Any]]) -> pd.DataFrame:
    """``/prices-history`` points to ``ts, price``. Points are ~1/min but not minute-aligned."""
    df = pd.DataFrame(
        {"ts": [pd.Timestamp(int(p["t"]), unit="s", tz="UTC") for p in history], "price": [float(p["p"]) for p in history]}
    )
    return df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class PolymarketIngest:
    """Read-only Polymarket client (Gamma + CLOB)."""

    def __init__(self, cfg: Config, gamma: ApiClient | None = None, clob: ApiClient | None = None) -> None:
        self.cfg = cfg
        vc = cfg.venues.polymarket
        self.gamma = gamma or ApiClient(vc.gamma_url, vc.max_requests_per_second)
        self.clob = clob or ApiClient(vc.clob_url, vc.max_requests_per_second)
        self.data = ApiClient(vc.data_url, min(vc.max_requests_per_second, 10))

    async def __aenter__(self) -> PolymarketIngest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.gather(self.gamma.aclose(), self.clob.aclose(), self.data.aclose())

    async def fetch_last_trades(self, condition_ids: list[str]) -> dict[str, tuple[float, datetime, str]]:
        """Latest public trade per market as ``(yes_equivalent_price, time, token)``."""

        async def one(cid: str) -> tuple[str, tuple[float, datetime, str] | None]:
            trades = await self.data.get_json("/trades", params={"market": cid, "limit": 1})
            return cid, last_trade_yes(trades[0]) if trades else None

        results = await asyncio.gather(*(one(c) for c in condition_ids))
        return {cid: t for cid, t in results if t is not None}

    async def iter_events(
        self,
        series_id: str,
        closed: bool | None = None,
        end_date_min: datetime | None = None,
        end_date_max: datetime | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every event of a series. ``endDate`` equals kickoff for match events."""
        offset = 0
        while True:
            params: dict[str, Any] = {"series_id": series_id, "limit": page_size, "offset": offset}
            if closed is not None:
                params["closed"] = str(closed).lower()
            if end_date_min:
                params["end_date_min"] = end_date_min.strftime("%Y-%m-%dT%H:%M:%SZ")
            if end_date_max:
                params["end_date_max"] = end_date_max.strftime("%Y-%m-%dT%H:%M:%SZ")
            page = await self.gamma.get_json("/events", params=params)
            for event in page:
                yield event
            if len(page) < page_size:
                return
            offset += page_size

    async def list_matches(
        self,
        league_name: str,
        closed: bool | None = None,
        kickoff_from: datetime | None = None,
        kickoff_to: datetime | None = None,
    ) -> tuple[list[VenueMatch], list[UnmappedEvent]]:
        """A league's full-time result events, mapped to HOME/DRAW/AWAY, filtered by kickoff window."""
        league: LeagueConfig = self.cfg.league(league_name)
        pm = league.polymarket
        matches: dict[str, VenueMatch] = {}
        unmapped: list[UnmappedEvent] = []
        async for event in self.iter_events(pm.series_id, closed, kickoff_from, kickoff_to):
            if not is_full_time_result_event(event, pm.sport, pm.market_type):
                continue
            parsed = parse_event(event, league_name, pm.market_type, self.cfg)
            if isinstance(parsed, UnmappedEvent):
                unmapped.append(parsed)
                continue
            if not parsed.is_complete(league.outcomes):
                unmapped.append(
                    UnmappedEvent(league_name, parsed.venue_event_id, event.get("slug", ""), parsed.title, "missing outcome markets")
                )
                continue
            if kickoff_from and parsed.kickoff_utc < kickoff_from:
                continue
            if kickoff_to and parsed.kickoff_utc > kickoff_to:
                continue
            matches[parsed.venue_event_id] = parsed
        return sorted(matches.values(), key=lambda m: m.kickoff_utc), unmapped

    async def fetch_price_history(self, token_id: str, start: datetime, end: datetime) -> pd.DataFrame:
        """``/prices-history`` for one token over ``[start, end]`` at the configured fidelity."""
        data = await self.clob.get_json(
            "/prices-history",
            params={
                "market": token_id,
                "startTs": int(start.timestamp()),
                "endTs": int(end.timestamp()),
                "fidelity": self.cfg.venues.polymarket.prices_history_fidelity_minutes,
            },
        )
        return parse_price_history(data.get("history") or [])

    async def fetch_match_history(self, match: VenueMatch) -> pd.DataFrame:
        """Yes-token price history for every outcome over the configured window around kickoff.

        Only the Yes token is fetched: verified that No-token history is exactly ``1 - p`` at every
        shared timestamp (docs/api_notes.md §2).
        """
        n = self.cfg.normalize
        start = match.kickoff_utc - timedelta(hours=n.window_before_kickoff_hours)
        end = match.kickoff_utc + timedelta(hours=n.window_after_kickoff_hours)

        async def one(c: VenueContract) -> pd.DataFrame:
            df = await self.fetch_price_history(c.yes_id, start, end)
            df.insert(0, "outcome", c.outcome.value)
            df.insert(0, "market_id", c.market_id)
            return df

        frames = await asyncio.gather(*(one(c) for c in match.contracts.values()))
        df = pd.concat(frames, ignore_index=True)
        df.insert(0, "venue_event_id", match.venue_event_id)
        return df[HISTORY_COLUMNS]

    async def fetch_raw_books(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Raw CLOB books keyed by token id, via the read-only ``POST /books`` batch lookup."""
        size = self.cfg.venues.polymarket.books_batch_size
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(token_ids), size):
            batch = token_ids[i : i + size]
            data = await self.clob.post_json_readonly("/books", [{"token_id": t} for t in batch])
            for book in data or []:
                out[str(book.get("asset_id"))] = book
        return out

    async def fetch_orderbooks(self, matches: list[VenueMatch], depth: int | None = None) -> list[BinaryBook]:
        """One :class:`BinaryBook` per outcome contract, combining its Yes and No token books."""
        contracts = [c for m in matches for c in m.contracts.values()]
        raw = await self.fetch_raw_books([t for c in contracts for t in (c.yes_id, c.no_id)])
        books = []
        for c in contracts:
            if c.yes_id in raw and c.no_id in raw:
                books.append(parse_books(c, raw[c.yes_id], raw[c.no_id], depth))
        return books


# ---------------------------------------------------------------------------
# Backfill entry point
# ---------------------------------------------------------------------------


async def backfill(cfg: Config, league_name: str, days: int, now: datetime | None = None) -> dict[str, int]:
    """Pull every finished match in the last ``days`` days and write matches + price history to parquet."""
    now = now or datetime.now(UTC)
    raw = cfg.resolve(cfg.paths.raw_dir) / "polymarket"
    hist_dir = raw / "history" / f"league={league_name}"
    hist_dir.mkdir(parents=True, exist_ok=True)

    async with PolymarketIngest(cfg) as p:
        matches, unmapped = await p.list_matches(
            league_name, closed=True, kickoff_from=now - timedelta(days=days), kickoff_to=now
        )
        frame = matches_to_frame(matches)
        extras = pd.DataFrame([{"venue_event_id": m.venue_event_id, **m.extra} for m in matches])
        if not frame.empty:
            frame = frame.merge(extras, on="venue_event_id", how="left")
        frame.to_parquet(raw / f"matches_league={league_name}.parquet", index=False)
        _write_unmapped(unmapped, raw / f"unmapped_league={league_name}.csv")

        written = 0
        for m in matches:
            df = await p.fetch_match_history(m)
            if df.empty:
                log.warning("no price history for %s (%s)", m.venue_event_id, m.extra.get("slug"))
                continue
            df.to_parquet(hist_dir / f"{m.venue_event_id}.parquet", index=False)
            written += 1
            log.info("polymarket %s %s: %d history rows", league_name, m.extra.get("slug"), len(df))
    return {"matches": len(matches), "unmapped": len(unmapped), "history_files": written}


def _write_unmapped(unmapped: list[UnmappedEvent], path: Path) -> None:
    pd.DataFrame([u.__dict__ for u in unmapped], columns=["league", "event_id", "slug", "title", "reason"]).to_csv(
        path, index=False
    )
