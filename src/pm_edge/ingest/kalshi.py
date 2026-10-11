"""Kalshi public market data: soccer events/markets, 1-minute candlesticks, and order book snapshots.

Kalshi lists each match as one mutually-exclusive event with three binary markets whose ticker
suffixes are the home abbreviation, the away abbreviation, and ``TIE`` (docs/api_notes.md §1).
No endpoint here requires authentication.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from pm_edge.config import Config, LeagueConfig
from pm_edge.http import ApiClient
from pm_edge.ingest.common import matches_to_frame
from pm_edge.models import BinaryBook, FeeParams, Outcome, PriceLevel, Venue, VenueContract, VenueMatch

log = logging.getLogger(__name__)

TIE_SUFFIX = "TIE"
_SUBTITLE_ABBR = re.compile(r"^\s*(\S+)\s+vs\.?\s+(\S+)")
_TITLE_SPLIT = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)

CANDLE_COLUMNS = ["ts", "yes_bid", "yes_ask", "yes_bid_open", "yes_ask_open", "trade_close", "volume", "open_interest"]


@dataclass
class UnmappedEvent:
    """A Kalshi event that could not be mapped to HOME/DRAW/AWAY with certainty."""

    league: str
    event_ticker: str
    title: str
    reason: str


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit tested offline)
# ---------------------------------------------------------------------------


def to_float(value: Any) -> float | None:
    """Parse a Kalshi fixed-point string (or number) into a float; empty/None -> None."""
    if value is None or value == "":
        return None
    return float(value)


def parse_ts(value: str | None) -> datetime | None:
    """Parse an RFC3339 timestamp into an aware UTC datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def split_title(title: str) -> tuple[str, str] | None:
    """Split an event title of the form ``"{Home} vs {Away}"``."""
    parts = _TITLE_SPLIT.split(title.strip())
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0].strip(), parts[1].strip()


def subtitle_abbreviations(sub_title: str) -> tuple[str, str] | None:
    """Extract ``(home_abbr, away_abbr)`` from a sub-title like ``"MUN vs BOU (Oct 25)"``."""
    m = _SUBTITLE_ABBR.match(sub_title or "")
    return (m.group(1).upper(), m.group(2).upper()) if m else None


def map_market_outcome(
    market: dict[str, Any], home: str, away: str, home_abbr: str | None, away_abbr: str | None
) -> Outcome | None:
    """Map one Kalshi market to an outcome, or ``None`` if signals disagree or are missing.

    Primary signal is the ticker suffix (``TIE`` or a team abbreviation from the event sub-title).
    It is cross-checked against ``yes_sub_title``; any contradiction returns ``None`` so the event
    goes to manual review instead of being guessed.
    """
    suffix = market["ticker"].rsplit("-", 1)[-1].upper()
    label = (market.get("yes_sub_title") or "").strip().casefold()

    by_label: Outcome | None = None
    if label in {"tie", "draw"}:
        by_label = Outcome.DRAW
    elif label == home.casefold():
        by_label = Outcome.HOME
    elif label == away.casefold():
        by_label = Outcome.AWAY

    by_suffix: Outcome | None = None
    if suffix == TIE_SUFFIX:
        by_suffix = Outcome.DRAW
    elif home_abbr and suffix == home_abbr:
        by_suffix = Outcome.HOME
    elif away_abbr and suffix == away_abbr:
        by_suffix = Outcome.AWAY

    if by_suffix and by_label and by_suffix != by_label:
        return None
    return by_suffix or by_label


def resolve_fee_params(series: dict[str, Any], event: dict[str, Any], cfg: Config) -> FeeParams:
    """Combine config rate with the series fee multiplier and any event-level override."""
    fee_type = event.get("fee_type_override") or series.get("fee_type") or "quadratic"
    if fee_type == "flat":
        raise ValueError(f"Flat Kalshi fee type is not modelled (event {event.get('event_ticker')})")
    multiplier = event.get("fee_multiplier_override")
    if multiplier is None:
        multiplier = series.get("fee_multiplier", cfg.fees.kalshi.default_multiplier)
    return FeeParams(rate=cfg.fees.kalshi.taker_rate, multiplier=float(multiplier))


def parse_event(
    event: dict[str, Any],
    markets: list[dict[str, Any]],
    milestone: dict[str, Any] | None,
    league: str,
    fee: FeeParams,
) -> VenueMatch | UnmappedEvent:
    """Build a :class:`VenueMatch` from a Kalshi event, its markets, and its milestone."""
    ticker = event["event_ticker"]
    title = event.get("title", "")

    def unmapped(reason: str) -> UnmappedEvent:
        return UnmappedEvent(league=league, event_ticker=ticker, title=title, reason=reason)

    teams = split_title(title)
    if teams is None:
        return unmapped("title is not 'Home vs Away'")
    home, away = teams
    abbrs = subtitle_abbreviations(event.get("sub_title", ""))
    home_abbr, away_abbr = abbrs if abbrs else (None, None)

    # Kickoff comes from the milestone. Market expected_expiration_time is kickoff + 3h; never use it.
    kickoff = parse_ts(milestone.get("start_date")) if milestone else None
    if kickoff is None:
        return unmapped("no milestone start_date (kickoff unknown)")

    contracts: dict[Outcome, VenueContract] = {}
    for m in markets:
        outcome = map_market_outcome(m, home, away, home_abbr, away_abbr)
        if outcome is None:
            return unmapped(f"cannot map market {m['ticker']} ({m.get('yes_sub_title')!r})")
        if outcome in contracts:
            return unmapped(f"duplicate {outcome} mapping ({m['ticker']})")
        contracts[outcome] = VenueContract(
            outcome=outcome,
            market_id=m["ticker"],
            yes_id=m["ticker"],
            no_id=m["ticker"],
            label=m.get("yes_sub_title", ""),
            fee=fee,
            tick_size=_tick_size(m),
            result=m.get("result") or None,
        )

    return VenueMatch(
        venue=Venue.KALSHI,
        league=league,
        venue_event_id=ticker,
        title=title,
        home_name=home,
        away_name=away,
        kickoff_utc=kickoff,
        status=(milestone or {}).get("details", {}).get("status", ""),
        contracts=contracts,
        extra={"series_ticker": event.get("series_ticker", ""), "sub_title": event.get("sub_title", "")},
    )


def _tick_size(market: dict[str, Any]) -> float:
    ranges = market.get("price_ranges") or []
    steps = [float(r["step"]) for r in ranges if r.get("step")]
    return min(steps) if steps else 0.01


def _ohlc(block: dict[str, Any] | None, field: str, historical: bool) -> float | None:
    if not block:
        return None
    return to_float(block.get(field if historical else f"{field}_dollars"))


def parse_candles(candles: list[dict[str, Any]], historical: bool) -> pd.DataFrame:
    """Normalise live or historical candlesticks to one row per minute.

    ``ts`` is the candle's ``end_period_ts`` (inclusive end of the ``(ts-60s, ts]`` window), so the
    close values are the state of the book at ``ts``. The two endpoints use different field names.
    """
    rows = []
    for c in candles:
        rows.append(
            {
                "ts": pd.Timestamp(c["end_period_ts"], unit="s", tz="UTC"),
                "yes_bid": _ohlc(c.get("yes_bid"), "close", historical),
                "yes_ask": _ohlc(c.get("yes_ask"), "close", historical),
                "yes_bid_open": _ohlc(c.get("yes_bid"), "open", historical),
                "yes_ask_open": _ohlc(c.get("yes_ask"), "open", historical),
                "trade_close": _ohlc(c.get("price"), "close", historical),
                "volume": to_float(c.get("volume" if historical else "volume_fp")),
                "open_interest": to_float(c.get("open_interest" if historical else "open_interest_fp")),
            }
        )
    df = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
    return df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)


def _levels(raw: list[list[str]], invert: bool, depth: int | None) -> list[PriceLevel]:
    """Best-first price levels; ``invert`` turns opposite-side bids into asks (price -> 1 - price)."""
    levels = sorted(((float(p), float(s)) for p, s in raw), key=lambda x: x[0], reverse=True)
    if depth is not None:
        levels = levels[:depth]
    return [PriceLevel(price=round(1.0 - p, 6) if invert else p, size=s) for p, s in levels]


def parse_orderbook(ticker: str, orderbook_fp: dict[str, Any], ts: datetime, depth: int | None = None) -> BinaryBook:
    """Build an executable book from Kalshi's bids-only order book.

    YES asks are implied by NO bids (YES ask = 1 - NO bid, same size), and vice versa.
    """
    yes_raw = orderbook_fp.get("yes_dollars") or []
    no_raw = orderbook_fp.get("no_dollars") or []
    return BinaryBook(
        venue=Venue.KALSHI,
        market_id=ticker,
        ts_utc=ts,
        yes_bids=_levels(yes_raw, invert=False, depth=depth),
        no_bids=_levels(no_raw, invert=False, depth=depth),
        yes_asks=_levels(no_raw, invert=True, depth=depth),
        no_asks=_levels(yes_raw, invert=True, depth=depth),
    )


def candle_gap_report(candles: pd.DataFrame, trade_times: Iterable[float]) -> dict[str, int]:
    """Check whether missing 1-minute candles mean "no change" (docs/api_notes.md §1).

    For every gap between consecutive candles, counts whether bid/ask were unchanged across the gap
    and whether any trade happened strictly inside the missing minutes.
    """
    ts = [int(t.timestamp()) for t in candles["ts"]]
    trades = sorted(trade_times)
    out = {"gaps": 0, "missing_minutes": 0, "bid_unchanged": 0, "ask_unchanged": 0, "gaps_with_trades": 0}
    for i in range(len(ts) - 1):
        step = (ts[i + 1] - ts[i]) // 60
        if step <= 1:
            continue
        out["gaps"] += 1
        out["missing_minutes"] += step - 1
        a, b = candles.iloc[i], candles.iloc[i + 1]
        out["bid_unchanged"] += int(a["yes_bid"] == b["yes_bid_open"])
        out["ask_unchanged"] += int(a["yes_ask"] == b["yes_ask_open"])
        # Missing minutes span (ts[i], ts[i+1] - 60]; trades are compared with fractional seconds.
        lo = bisect.bisect_right(trades, float(ts[i]))
        hi = bisect.bisect_right(trades, float(ts[i + 1] - 60))
        out["gaps_with_trades"] += int(hi > lo)
    return out


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class KalshiIngest:
    """Read-only Kalshi market data client."""

    def __init__(self, cfg: Config, api: ApiClient | None = None) -> None:
        self.cfg = cfg
        vc = cfg.venues.kalshi
        self.api = api or ApiClient(vc.base_url, vc.max_requests_per_second)
        self._series_cache: dict[str, dict[str, Any]] = {}
        self._cutoff: datetime | None = None

    async def __aenter__(self) -> KalshiIngest:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.api.aclose()

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        """Series metadata including ``fee_type`` and ``fee_multiplier``."""
        if series_ticker not in self._series_cache:
            data = await self.api.get_json(f"/series/{series_ticker}")
            self._series_cache[series_ticker] = data["series"]
        return self._series_cache[series_ticker]

    async def market_settled_cutoff(self) -> datetime:
        """Markets settled before this time are only served by the /historical endpoints."""
        if self._cutoff is None:
            data = await self.api.get_json("/historical/cutoff")
            self._cutoff = parse_ts(data["market_settled_ts"])
        assert self._cutoff is not None
        return self._cutoff

    async def iter_events(
        self, series_ticker: str, status: str | None = None, min_close_ts: int | None = None
    ) -> AsyncIterator[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
        """Yield ``(event, milestones_by_event_ticker)`` across all pages."""
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "with_nested_markets": "true",
                "with_milestones": "true",
                "limit": 200,
            }
            if status:
                params["status"] = status
            if min_close_ts:
                params["min_close_ts"] = min_close_ts
            if cursor:
                params["cursor"] = cursor
            data = await self.api.get_json("/events", params=params)
            milestones = {t: m for m in data.get("milestones") or [] for t in m.get("primary_event_tickers", [])}
            for event in data.get("events", []):
                yield event, milestones
            cursor = data.get("cursor") or ""
            if not cursor:
                return

    async def historical_markets_for_event(self, event_ticker: str) -> list[dict[str, Any]]:
        """Archived markets of an event (nested markets are omitted once archived)."""
        data = await self.api.get_json("/historical/markets", params={"event_ticker": event_ticker, "limit": 1000})
        return data.get("markets", [])

    async def list_matches(
        self,
        league_name: str,
        statuses: Iterable[str | None] = (None,),
        kickoff_from: datetime | None = None,
        kickoff_to: datetime | None = None,
    ) -> tuple[list[VenueMatch], list[UnmappedEvent]]:
        """List a league's matches, mapped to HOME/DRAW/AWAY, filtered by kickoff window."""
        league: LeagueConfig = self.cfg.league(league_name)
        series_ticker = league.kalshi.series_ticker
        series = await self.get_series(series_ticker)
        # close_time is ~kickoff + 48h, so this bound never drops a match that kicked off in range.
        min_close = int(kickoff_from.timestamp()) if kickoff_from else None

        matches: dict[str, VenueMatch] = {}
        unmapped: list[UnmappedEvent] = []
        for status in statuses:
            async for event, milestones in self.iter_events(series_ticker, status, min_close):
                ticker = event["event_ticker"]
                if ticker in matches:
                    continue
                markets = event.get("markets") or await self.historical_markets_for_event(ticker)
                try:
                    fee = resolve_fee_params(series, event, self.cfg)
                except ValueError as exc:
                    unmapped.append(UnmappedEvent(league_name, ticker, event.get("title", ""), str(exc)))
                    continue
                parsed = parse_event(event, markets, milestones.get(ticker), league_name, fee)
                if isinstance(parsed, UnmappedEvent):
                    unmapped.append(parsed)
                    continue
                if not parsed.is_complete(league.outcomes):
                    unmapped.append(UnmappedEvent(league_name, ticker, parsed.title, "missing outcome markets"))
                    continue
                if kickoff_from and parsed.kickoff_utc < kickoff_from:
                    continue
                if kickoff_to and parsed.kickoff_utc > kickoff_to:
                    continue
                matches[ticker] = parsed
        return sorted(matches.values(), key=lambda m: m.kickoff_utc), unmapped

    async def fetch_candles(
        self,
        series_ticker: str,
        ticker: str,
        start: datetime,
        end: datetime,
        *,
        archived: bool,
    ) -> pd.DataFrame:
        """1-minute candles for ``ticker`` over ``[start, end]``, chunked to the 5,000-candle limit."""
        step = timedelta(minutes=self.cfg.venues.kalshi.max_candles_per_request - 1)
        path = (
            f"/historical/markets/{ticker}/candlesticks"
            if archived
            else f"/series/{series_ticker}/markets/{ticker}/candlesticks"
        )
        frames = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + step, end)
            data = await self.api.get_json(
                path,
                params={
                    "start_ts": int(chunk_start.timestamp()),
                    "end_ts": int(chunk_end.timestamp()),
                    "period_interval": 1,
                },
            )
            frames.append(parse_candles(data.get("candlesticks") or [], historical=archived))
            chunk_start = chunk_end + timedelta(seconds=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=CANDLE_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        return df.sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)

    async def fetch_match_candles(self, match: VenueMatch) -> pd.DataFrame:
        """Candles for every contract of a match over the configured window around kickoff."""
        n = self.cfg.normalize
        start = match.kickoff_utc - timedelta(hours=n.window_before_kickoff_hours)
        end = match.kickoff_utc + timedelta(hours=n.window_after_kickoff_hours)
        cutoff = await self.market_settled_cutoff()
        # A market settles after the match; if kickoff + window is already before the cutoff it is archived.
        archived = end < cutoff
        series_ticker = match.extra["series_ticker"]

        async def fetch(ticker: str, use_archive: bool) -> pd.DataFrame:
            try:
                return await self.fetch_candles(series_ticker, ticker, start, end, archived=use_archive)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return pd.DataFrame(columns=CANDLE_COLUMNS)
                raise

        async def one(contract: VenueContract) -> pd.DataFrame:
            # The cutoff test is approximate (settlement lags kickoff), so fall back to the other tier.
            df = await fetch(contract.market_id, archived)
            if df.empty:
                df = await fetch(contract.market_id, not archived)
            df.insert(0, "outcome", contract.outcome.value)
            df.insert(0, "market_id", contract.market_id)
            return df

        frames = await asyncio.gather(*(one(c) for c in match.contracts.values()))
        df = pd.concat(frames, ignore_index=True)
        df.insert(0, "venue_event_id", match.venue_event_id)
        return df

    async def fetch_orderbooks(self, tickers: list[str], depth: int | None = None) -> list[BinaryBook]:
        """Order book snapshots for many markets via the batch endpoint (``tickers`` repeated)."""
        size = self.cfg.venues.kalshi.orderbook_batch_size
        books: list[BinaryBook] = []
        for i in range(0, len(tickers), size):
            batch = tickers[i : i + size]
            data = await self.api.get_json("/markets/orderbooks", params=[("tickers", t) for t in batch])
            ts = datetime.now(UTC)
            for ob in data.get("orderbooks", []):
                books.append(parse_orderbook(ob["ticker"], ob.get("orderbook_fp") or {}, ts, depth))
        return books

    async def fetch_trade_times(self, ticker: str, start: datetime, end: datetime) -> list[float]:
        """Unix timestamps (fractional seconds) of all public trades in a window."""
        out: list[float] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {
                "ticker": ticker,
                "min_ts": int(start.timestamp()),
                "max_ts": int(end.timestamp()),
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            data = await self.api.get_json("/markets/trades", params=params)
            out.extend(parse_ts(t["created_time"]).timestamp() for t in data.get("trades", []))  # type: ignore[union-attr]
            cursor = data.get("cursor") or ""
            if not cursor:
                return out


# ---------------------------------------------------------------------------
# Backfill entry point
# ---------------------------------------------------------------------------


async def backfill(cfg: Config, league_name: str, days: int, now: datetime | None = None) -> dict[str, int]:
    """Pull every finished match in the last ``days`` days and write matches + candles to parquet."""
    now = now or datetime.now(UTC)
    raw = cfg.resolve(cfg.paths.raw_dir) / "kalshi"
    candle_dir = raw / "candles" / f"league={league_name}"
    candle_dir.mkdir(parents=True, exist_ok=True)

    async with KalshiIngest(cfg) as k:
        matches, unmapped = await k.list_matches(
            league_name, statuses=("settled", "closed"), kickoff_from=now - timedelta(days=days), kickoff_to=now
        )
        matches_to_frame(matches).to_parquet(raw / f"matches_league={league_name}.parquet", index=False)
        _write_unmapped(unmapped, raw / f"unmapped_league={league_name}.csv")

        written = 0
        for m in matches:
            df = await k.fetch_match_candles(m)
            if df.empty:
                log.warning("no candles for %s", m.venue_event_id)
                continue
            df.to_parquet(candle_dir / f"{m.venue_event_id}.parquet", index=False)
            written += 1
            log.info("kalshi %s %s: %d candle rows", league_name, m.venue_event_id, len(df))
    return {"matches": len(matches), "unmapped": len(unmapped), "candle_files": written}


def _write_unmapped(unmapped: list[UnmappedEvent], path: Path) -> None:
    pd.DataFrame([u.__dict__ for u in unmapped], columns=["league", "event_ticker", "title", "reason"]).to_csv(
        path, index=False
    )
