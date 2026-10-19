"""Collector-lite: pre-kickoff live snapshots for matched EPL/UCL matches (read-only).

For every match listed on both Kalshi and Polymarket and paired by :mod:`pm_edge.matching`, from
``collector_lite.window_before_kickoff_hours`` before kickoff until kickoff:

* every ``book_poll_seconds`` (20 s): best bid/ask and the top ``orderbook_depth`` (3) levels of
  YES and NO books for all three outcomes on both venues, plus Polymarket midpoint and
  last_trade_price. The last trade comes from data-api ``/trades``, converted to YES terms; the book's
  own ``last_trade_price`` is market-level and quoted in whichever token traded last, so it is kept
  only as ``book_last_trade_raw``. Both venues are fetched concurrently and share one ``ts_poll`` per tick.
* every ``odds_poll_seconds`` (10 min) per league with an active match: Pinnacle h2h from the Odds API
  with power de-vig. Remaining credits are read from response headers, and odds polling stops for
  good below ``odds_credit_floor``.

Output (append-only parquet, flushed every ``flush_seconds``)::

    data/live/venue=kalshi|polymarket|pinnacle|schedule/date=YYYY-MM-DD/part-<utc>-<rand>.parquet

``venue=schedule`` holds the contract metadata (token ids, fees, min order size) of each match each
time the schedule is refreshed. Every venue call is isolated: a failure is logged and the loop moves on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from pm_edge.alerts import notify, write_json_atomic
from pm_edge.config import Config, Secrets
from pm_edge.devig import devig
from pm_edge.ingest.common import matches_to_frame
from pm_edge.ingest.kalshi import KalshiIngest
from pm_edge.ingest.odds import CreditFloorReached, OddsIngest
from pm_edge.ingest.polymarket import PolymarketIngest
from pm_edge.matching import AliasTable, match_events
from pm_edge.models import BinaryBook, Outcome, PriceLevel, VenueMatch

log = logging.getLogger(__name__)


@dataclass
class ScheduledMatch:
    match_id: str
    league: str
    kickoff_utc: datetime
    home: str
    away: str
    venues: dict[str, VenueMatch]


class ParquetBuffer:
    """In-memory rows per venue, appended to date-partitioned parquet on :meth:`flush`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def add(self, venue: str, rows: list[dict[str, Any]]) -> None:
        self.rows.setdefault(venue, []).extend(rows)

    def pending(self) -> int:
        return sum(len(r) for r in self.rows.values())

    def flush(self) -> int:
        written = 0
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        for venue, rows in list(self.rows.items()):
            if not rows:
                continue
            df = pd.DataFrame(rows)
            for date, part in df.groupby(df["ts_poll"].dt.strftime("%Y-%m-%d")):
                d = self.root / f"venue={venue}" / f"date={date}"
                d.mkdir(parents=True, exist_ok=True)
                part.to_parquet(d / f"part-{stamp}-{secrets.token_hex(3)}.parquet", index=False)
            written += len(rows)
            self.rows[venue] = []
        return written


def _levels(prefix: str, levels: list[PriceLevel], depth: int) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for i in range(depth):
        lvl = levels[i] if i < len(levels) else None
        out[f"{prefix}_px_{i + 1}"] = lvl.price if lvl else None
        out[f"{prefix}_sz_{i + 1}"] = lvl.size if lvl else None
    return out


def book_row(
    ts_poll: datetime, ts_fetch: datetime, m: ScheduledMatch, outcome: Outcome, book: BinaryBook, depth: int
) -> dict[str, Any]:
    """One snapshot row: best prices, top-``depth`` levels per side, Polymarket extras."""
    best_no_bid = book.no_bids[0].price if book.no_bids else None
    return {
        "ts_poll": ts_poll,
        "ts_fetch": ts_fetch,
        "book_ts": book.ts_utc,
        "venue": book.venue.value,
        "league": m.league,
        "match_id": m.match_id,
        "kickoff_utc": m.kickoff_utc,
        "minutes_to_kickoff": (m.kickoff_utc - ts_poll).total_seconds() / 60,
        "outcome": outcome.value,
        "market_id": book.market_id,
        "best_yes_bid": book.best_yes_bid,
        "best_yes_ask": book.best_yes_ask,
        "best_no_bid": best_no_bid,
        "best_no_ask": book.best_no_ask,
        **_levels("yes_bid", book.yes_bids, depth),
        **_levels("yes_ask", book.yes_asks, depth),
        **_levels("no_bid", book.no_bids, depth),
        **_levels("no_ask", book.no_asks, depth),
        "last_trade_price": book.last_trade_price,
        "midpoint": book.midpoint,
        "min_order_size": book.min_order_size,
        "tick_size": book.tick_size,
    }


class CollectorLite:
    def __init__(self, cfg: Config, leagues: list[str]) -> None:
        self.cfg = cfg
        self.lc = cfg.collector_lite
        self.leagues = leagues
        self.aliases = AliasTable.from_csv(cfg.resolve(cfg.matching.team_aliases_path))
        self.buffer = ParquetBuffer(cfg.resolve(self.lc.live_dir))
        self.schedule: dict[str, ScheduledMatch] = {}
        self.kalshi = KalshiIngest(cfg)
        self.poly = PolymarketIngest(cfg)
        try:
            self.odds: OddsIngest | None = OddsIngest(cfg, credit_floor=self.lc.odds_credit_floor)
        except RuntimeError as exc:
            log.error("odds polling disabled: %s", exc)
            self.odds = None
        self.stop = asyncio.Event()
        self.stats = {"ticks": 0, "book_rows": 0, "odds_calls": 0, "errors": 0}
        self.health: dict[str, dict[str, Any]] = {
            v: {"failing_since": None, "consecutive_failures": 0, "last_ok": None, "last_error": None}
            for v in ("kalshi", "polymarket", "pinnacle")
        }
        self.window: dict[str, Any] | None = None
        self.topic = Secrets().ntfy_topic
        self.status_path = cfg.resolve(self.lc.live_dir) / "status.json"

    def mark(self, venue: str, error: BaseException | None) -> None:
        h = self.health[venue]
        now = datetime.now(UTC)
        if error is None:
            h.update(failing_since=None, consecutive_failures=0, last_ok=now)
        else:
            h["failing_since"] = h["failing_since"] or now
            h["consecutive_failures"] += 1
            h["last_error"] = repr(error)[:200]
        if self.window is not None and error is not None:
            self.window["errors"] += 1

    # -- window tracking / alerts -----------------------------------------------------------

    def track_window(self, active: list[ScheduledMatch], now: datetime) -> str | None:
        """Update the open window; return its summary text when it has just closed."""
        if active:
            if self.window is None:
                self.window = {"start": now, "matches": set(), "polls": 0, "errors": 0, "odds_calls": 0}
                log.info("window opened")
            self.window["matches"].update(m.match_id for m in active)
            return None
        if self.window is None:
            return None
        w, self.window = self.window, None
        mins = (now - w["start"]).total_seconds() / 60
        credits = self.odds.remaining if self.odds else "disabled"
        return (f"Window {w['start']:%m-%d %H:%M}-{now:%H:%M}Z ({mins:.0f} min): {len(w['matches'])} matches "
                f"({', '.join(sorted(w['matches']))}); polls {w['polls']}; errors {w['errors']}; "
                f"Pinnacle calls {w['odds_calls']}; credits remaining {credits}")

    def write_status(self, active: list[ScheduledMatch]) -> None:
        try:
            write_json_atomic(self.status_path, {
                "ts": datetime.now(UTC), "pid": os.getpid(), "active": [m.match_id for m in active],
                "stats": self.stats, "venues": self.health,
                "credits": self.odds.remaining if self.odds else None,
            })
        except OSError as exc:
            log.warning("status write failed: %r", exc)

    # -- schedule ---------------------------------------------------------------------------

    async def refresh_schedule(self) -> None:
        now = datetime.now(UTC)
        kick_from = now - timedelta(hours=self.lc.window_after_kickoff_hours)
        kick_to = now + timedelta(hours=self.lc.schedule_lookahead_hours)
        venue_matches: dict[tuple[str, str], VenueMatch] = {}
        failed: set[str] = set()
        for league in self.leagues:
            for venue, call in (
                ("kalshi", lambda lg=league: self.kalshi.list_matches(lg, ("open",), kick_from, kick_to)),
                ("polymarket", lambda lg=league: self.poly.list_matches(lg, False, kick_from, kick_to)),
            ):
                try:
                    found, unmapped = await call()
                except Exception:
                    log.exception("schedule: %s %s listing failed; keeping previous schedule for it", venue, league)
                    failed.add(venue)
                    continue
                for u in unmapped:
                    log.warning("schedule: %s %s unmapped event %s", venue, league, u)
                for vm in found:
                    venue_matches[(venue, vm.venue_event_id)] = vm
        if failed:
            for sm in self.schedule.values():
                for v in failed:
                    if v in sm.venues:
                        venue_matches[(v, sm.venues[v].venue_event_id)] = sm.venues[v]
        if not venue_matches:
            return

        events = pd.DataFrame([
            {"venue": v, "league": vm.league, "venue_event_id": eid, "title": vm.title, "home_name": vm.home_name,
             "away_name": vm.away_name, "kickoff_utc": pd.Timestamp(vm.kickoff_utc),
             "home_alias": vm.extra.get("home_alias", ""), "away_alias": vm.extra.get("away_alias", "")}
            for (v, eid), vm in venue_matches.items()
        ])
        result = match_events(events, self.aliases, self.cfg)
        for _, r in result.review.iterrows():
            if r["kind"] != "unpaired":
                log.warning("schedule review: %s %s %s %s", r["venue"], r["venue_event_id"], r["kind"], r["detail"])
        new: dict[str, ScheduledMatch] = {}
        for match_id, grp in result.matches.groupby("match_id"):
            if set(grp["venue"]) != {"kalshi", "polymarket"}:
                continue
            first = grp.iloc[0]
            new[match_id] = ScheduledMatch(
                match_id=match_id, league=first["league"], kickoff_utc=first["kickoff_utc"].to_pydatetime(),
                home=first["home"], away=first["away"],
                venues={r["venue"]: venue_matches[(r["venue"], r["venue_event_id"])] for _, r in grp.iterrows()},
            )
        self.schedule = new
        ts = datetime.now(UTC)
        frames = []
        for sm in new.values():
            for vm in sm.venues.values():
                f = matches_to_frame([vm])
                f.insert(0, "match_id", sm.match_id)
                frames.append(f)
        if frames:
            sched = pd.concat(frames, ignore_index=True)
            sched.insert(0, "ts_poll", ts)
            self.buffer.add("schedule", sched.to_dict("records"))
        upcoming = sorted(new.values(), key=lambda s: s.kickoff_utc)
        log.info("schedule: %d paired matches in next %.0fh; next: %s", len(new), self.lc.schedule_lookahead_hours,
                 ", ".join(f"{s.match_id}@{s.kickoff_utc:%m-%d %H:%M}" for s in upcoming[:4]))

    def active(self, now: datetime) -> list[ScheduledMatch]:
        before = timedelta(hours=self.lc.window_before_kickoff_hours)
        after = timedelta(hours=self.lc.window_after_kickoff_hours)
        return [m for m in self.schedule.values() if m.kickoff_utc - before <= now < m.kickoff_utc + after]

    def next_window_start(self, now: datetime) -> datetime | None:
        before = timedelta(hours=self.lc.window_before_kickoff_hours)
        starts = [m.kickoff_utc - before for m in self.schedule.values() if m.kickoff_utc - before > now]
        return min(starts) if starts else None

    # -- polling ----------------------------------------------------------------------------

    async def poll_books(self, active: list[ScheduledMatch]) -> None:
        ts_poll = datetime.now(UTC)
        depth = self.lc.orderbook_depth

        async def kalshi() -> list[dict[str, Any]]:
            by_ticker = {c.market_id: (m, o) for m in active for o, c in m.venues["kalshi"].contracts.items()}
            books = await self.kalshi.fetch_orderbooks(list(by_ticker), depth=depth)
            ts = datetime.now(UTC)
            return [book_row(ts_poll, ts, *by_ticker[b.market_id], b, depth) for b in books if b.market_id in by_ticker]

        async def poly() -> list[dict[str, Any]]:
            vms = [m.venues["polymarket"] for m in active]
            by_market = {c.market_id: (m, o) for m in active for o, c in m.venues["polymarket"].contracts.items()}
            books, trades = await asyncio.gather(
                self.poly.fetch_orderbooks(vms, depth=depth),
                self.poly.fetch_last_trades(list(by_market)),
                return_exceptions=True,
            )
            if isinstance(books, BaseException):
                raise books
            if isinstance(trades, BaseException):
                log.warning("polymarket last trades failed: %r", trades)
                trades = {}
            ts = datetime.now(UTC)
            rows = []
            for b in books:
                if b.market_id not in by_market:
                    continue
                row = book_row(ts_poll, ts, *by_market[b.market_id], b, depth)
                # Book field is market-level and in whichever token traded last; keep it raw.
                row["book_last_trade_raw"] = row.pop("last_trade_price")
                t = trades.get(b.market_id)
                row["last_trade_price"] = t[0] if t else None
                row["last_trade_ts"] = t[1] if t else None
                row["last_trade_token"] = t[2] if t else None
                rows.append(row)
            return rows

        for venue, result in zip(("kalshi", "polymarket"), await asyncio.gather(kalshi(), poly(), return_exceptions=True)):
            if isinstance(result, BaseException):
                self.stats["errors"] += 1
                self.mark(venue, result)
                log.error("books: %s poll failed: %r", venue, result)
                continue
            self.mark(venue, None)
            self.buffer.add(venue, result)
            self.stats["book_rows"] += len(result)
        self.stats["ticks"] += 1
        if self.window is not None:
            self.window["polls"] += 1

    async def poll_odds(self, league: str, active: list[ScheduledMatch]) -> None:
        if self.odds is None:
            return
        ts_poll = datetime.now(UTC)
        try:
            rows = await self.odds.h2h(league, self.lc.odds_bookmakers)
        except CreditFloorReached as exc:
            log.error("odds polling stopped: %s", exc)
            self.odds = None
            return
        except Exception as exc:
            self.stats["errors"] += 1
            self.mark("pinnacle", exc)
            log.error("odds: %s poll failed: %r", league, exc)
            return
        self.mark("pinnacle", None)
        self.stats["odds_calls"] += 1
        if self.window is not None:
            self.window["odds_calls"] += 1
        tol = timedelta(minutes=self.cfg.matching.kickoff_tolerance_minutes)
        by_teams = {(m.home, m.away): m for m in self.schedule.values() if m.league == league}
        df = pd.DataFrame(rows)
        if df.empty:
            return
        out = []
        for (eid, book), g in df.groupby(["odds_event_id", "bookmaker"]):
            first = g.iloc[0]
            home, away = self.aliases.lookup(first["home_team"]), self.aliases.lookup(first["away_team"])
            commence = pd.Timestamp(first["commence_time"])
            sm = by_teams.get((home, away))
            match_id = sm.match_id if sm and abs(commence - pd.Timestamp(sm.kickoff_utc)) <= tol else None
            probs: dict[str, float] = {}
            method = None
            if set(g["outcome"].dropna()) == {"HOME", "DRAW", "AWAY"}:
                p, method = devig(g["price"].tolist(), self.cfg.devig)
                probs = dict(zip(g["outcome"], p))
            for _, r in g.iterrows():
                out.append({
                    **r.to_dict(), "ts_poll": ts_poll, "match_id": match_id,
                    "last_update": pd.Timestamp(r["last_update"]) if r["last_update"] else pd.NaT,
                    "commence_time": commence, "implied": 1 / r["price"],
                    "p_devig": probs.get(r["outcome"]), "devig_method": method,
                    "credits_remaining": self.odds.remaining if self.odds else None,
                })
        self.buffer.add("pinnacle", out)
        log.info("odds %s: %d rows (%d mapped to active schedule), credits remaining %s",
                 league, len(out), sum(1 for r in out if r["match_id"]), self.odds.remaining if self.odds else "-")

    # -- main loop --------------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        mono = time.monotonic
        next_sched = next_book = next_flush = next_beat = mono()
        next_odds: dict[str, float] = {}
        try:
            while not self.stop.is_set():
                t = mono()
                if t >= next_sched:
                    try:
                        await self.refresh_schedule()
                    except Exception:
                        self.stats["errors"] += 1
                        log.exception("schedule refresh failed")
                    next_sched = t + self.lc.schedule_refresh_seconds
                now = datetime.now(UTC)
                active = self.active(now)
                summary = self.track_window(active, now)
                if summary:
                    log.info("window closed: %s", summary)
                    await asyncio.to_thread(notify, self.topic, "pm-edge window closed", summary, "default",
                                            "soccer")
                if active and t >= next_book:
                    next_book = t + self.lc.book_poll_seconds
                    await self.poll_books(active)
                for league in {m.league for m in active}:
                    if t >= next_odds.get(league, 0):
                        next_odds[league] = t + self.lc.odds_poll_seconds
                        await self.poll_odds(league, [m for m in active if m.league == league])
                for league in list(next_odds):
                    if league not in {m.league for m in active}:
                        del next_odds[league]  # next window starts with a fresh odds call
                if t >= next_flush:
                    n = self.buffer.flush()
                    if n:
                        log.debug("flushed %d rows", n)
                    next_flush = t + self.lc.flush_seconds
                self.write_status(active)
                if t >= next_beat:
                    nxt = self.next_window_start(now)
                    log.info("heartbeat: %d active, %d scheduled, next window %s, stats %s, credits %s",
                             len(active), len(self.schedule), f"{nxt:%m-%d %H:%M}Z" if nxt else "-", self.stats,
                             self.odds.remaining if self.odds else "disabled")
                    next_beat = t + 600
                wake = min(next_sched, next_flush, next_beat, next_book if active else next_sched)
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=max(0.5, min(wake - mono(), 20)))
                except TimeoutError:
                    pass
        finally:
            n = self.buffer.flush()
            log.info("collector stopping; final flush %d rows; stats %s", n, self.stats)
            await asyncio.gather(self.kalshi.__aexit__(), self.poly.__aexit__(), return_exceptions=True)
            if self.odds is not None:
                await self.odds.__aexit__()


async def run(cfg: Config, leagues: list[str]) -> None:
    await CollectorLite(cfg, leagues).run()
