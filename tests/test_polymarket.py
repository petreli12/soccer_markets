"""Offline tests for Polymarket parsing, using synthetic Gamma/CLOB payloads shaped like the live API."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pm_edge.config import Config
from pm_edge.ingest.polymarket import (
    UnmappedEvent,
    is_full_time_result_event,
    map_market_outcome,
    market_result,
    parse_books,
    parse_event,
    parse_price_history,
    parse_ts,
    token_ids,
)
from pm_edge.models import Outcome, VenueMatch

HOME = {"name": "Arsenal FC", "alias": "Arsenal", "abbreviation": "ars", "ordering": "home"}
AWAY = {"name": "Coventry City FC", "abbreviation": "cov", "ordering": "away"}


def _market(mid: str, title: str, line: str | None, prices: str = '["0", "1"]', **kw: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": mid,
        "conditionId": f"0x{mid}",
        "groupItemTitle": title,
        "sportsMarketType": "moneyline",
        "version": "v1",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": prices,
        "clobTokenIds": json.dumps([f"{mid}-yes", f"{mid}-no"]),
        "positionIds": [f"{mid}-pos-yes", f"{mid}-pos-no"],
        "gameStartTime": "2026-08-21 19:00:00+00",
        "closed": True,
        "umaResolutionStatus": "resolved",
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.001,
        "feesEnabled": True,
        "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15},
    }
    if line is not None:
        m["marketMetadata"] = {"opticOddsSelectionLine": line}
    m.update(kw)
    return m


def _event(**kw: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": "814080",
        "slug": "epl-ars-cov-2026-08-21",
        "title": "Arsenal FC vs. Coventry City FC",
        "startTime": "2026-08-21T19:00:00Z",
        "closed": True,
        "negRiskMarketID": "0xneg",
        "teams": [HOME, AWAY],
        "markets": [
            _market("1", "Arsenal FC", "home", '["1", "0"]'),
            _market("2", "Draw (Arsenal FC vs. Coventry City FC)", "draw"),
            _market("3", "Coventry City FC", "away"),
        ],
    }
    e.update(kw)
    return e


def test_parse_ts_both_gamma_formats() -> None:
    assert parse_ts("2026-08-21T19:00:00Z") == parse_ts("2026-08-21 19:00:00+00")


def test_full_time_event_selected_and_siblings_ignored() -> None:
    assert is_full_time_result_event(_event(), "epl", "moneyline")
    assert not is_full_time_result_event(_event(slug="epl-ars-cov-2026-08-21-halftime-result"), "epl", "moneyline")
    assert not is_full_time_result_event(_event(slug="ucl-ars-cov-2026-08-21"), "epl", "moneyline")


def test_parse_event_maps_all_outcomes(cfg: Config) -> None:
    m = parse_event(_event(), "EPL", "moneyline", cfg)
    assert isinstance(m, VenueMatch)
    assert m.home_name == "Arsenal FC" and m.away_name == "Coventry City FC"
    assert m.kickoff_utc.isoformat() == "2026-08-21T19:00:00+00:00"
    assert {o: c.yes_id for o, c in m.contracts.items()} == {
        Outcome.HOME: "1-yes",
        Outcome.DRAW: "2-yes",
        Outcome.AWAY: "3-yes",
    }
    assert m.contracts[Outcome.HOME].no_id == "1-no"
    assert [m.contracts[o].result for o in (Outcome.HOME, Outcome.DRAW, Outcome.AWAY)] == ["yes", "no", "no"]
    assert m.contracts[Outcome.DRAW].min_order_size == 5
    assert m.contracts[Outcome.DRAW].tick_size == 0.001
    assert m.contracts[Outcome.DRAW].fee.rate == 0.05


def test_missing_metadata_falls_back_to_titles(cfg: Config) -> None:
    e = _event()
    for mk in e["markets"]:
        mk.pop("marketMetadata")
    e["markets"][0]["groupItemTitle"] = "Arsenal"  # alias also accepted
    m = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(m, VenueMatch)
    assert m.contracts[Outcome.HOME].yes_id == "1-yes"
    assert m.contracts[Outcome.AWAY].yes_id == "3-yes"


def test_metadata_title_contradiction_goes_to_review(cfg: Config) -> None:
    e = _event()
    e["markets"][0]["marketMetadata"]["opticOddsSelectionLine"] = "away"  # title says home
    out = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent) and "cannot map" in out.reason


def test_unresolvable_market_goes_to_review(cfg: Config) -> None:
    e = _event()
    e["markets"][2] = _market("3", "Some Other Team", None)
    out = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent)


def test_title_order_must_agree_with_teams_ordering(cfg: Config) -> None:
    out = parse_event(_event(title="Coventry City FC vs. Arsenal FC"), "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent) and "ordering" in out.reason


def test_missing_teams_goes_to_review(cfg: Config) -> None:
    out = parse_event(_event(teams=[]), "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent)


def test_kickoff_disagreement_goes_to_review(cfg: Config) -> None:
    e = _event()
    e["markets"][1]["gameStartTime"] = "2026-08-21 21:00:00+00"
    out = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent) and "gameStartTime" in out.reason


def test_duplicate_outcome_goes_to_review(cfg: Config) -> None:
    e = _event()
    e["markets"][2] = _market("3", "Arsenal FC", "home")
    out = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(out, UnmappedEvent) and "duplicate" in out.reason


def test_token_ids_by_version() -> None:
    m = _market("9", "Arsenal FC", "home")
    assert token_ids(m) == ("9-yes", "9-no")
    assert token_ids({**m, "version": "v2"}) == ("9-pos-yes", "9-pos-no")
    swapped = {**m, "outcomes": '["No", "Yes"]'}
    assert token_ids(swapped) == ("9-no", "9-yes")  # index-aligned: Yes is the second id
    assert token_ids({**m, "clobTokenIds": '["only-one"]'}) is None


def test_market_result_requires_resolution() -> None:
    assert market_result(_market("1", "x", "home", '["1", "0"]')) == "yes"
    assert market_result(_market("1", "x", "home", '["0.52", "0.48"]', closed=False)) is None
    assert market_result(_market("1", "x", "home", '["1", "0"]', umaResolutionStatus="proposed")) is None


def test_fees_disabled_means_zero_rate(cfg: Config) -> None:
    e = _event()
    for mk in e["markets"]:
        mk["feesEnabled"] = False
    m = parse_event(e, "EPL", "moneyline", cfg)
    assert isinstance(m, VenueMatch) and m.contracts[Outcome.HOME].fee.rate == 0.0


def test_map_outcome_ambiguous_alias_is_rejected() -> None:
    home = {**HOME, "alias": "City"}
    away = {**AWAY, "alias": "City"}
    assert map_market_outcome(_market("1", "City", None), home, away) is None


def test_parse_books_ignores_api_ordering(cfg: Config) -> None:
    m = parse_event(_event(), "EPL", "moneyline", cfg)
    assert isinstance(m, VenueMatch)
    c = m.contracts[Outcome.HOME]
    # Live API returns bids ascending and asks descending (best last); parser must sort.
    yes = {
        "timestamp": "1791745396451",
        "bids": [{"price": "0.01", "size": "100"}, {"price": "0.51", "size": "20"}, {"price": "0.52", "size": "83.37"}],
        "asks": [{"price": "0.99", "size": "1"}, {"price": "0.58", "size": "10"}, {"price": "0.55", "size": "325"}],
        "min_order_size": "5",
        "tick_size": "0.001",
        "last_trade_price": "0.530",
    }
    no = {
        "bids": [{"price": "0.40", "size": "50"}, {"price": "0.45", "size": "325"}],
        "asks": [{"price": "0.60", "size": "1"}, {"price": "0.48", "size": "83.37"}],
    }
    b = parse_books(c, yes, no, depth=2)
    assert b.best_yes_bid == 0.52 and b.best_yes_ask == 0.55 and b.best_no_ask == 0.48
    assert [lvl.price for lvl in b.yes_asks] == [0.55, 0.58]
    assert len(b.yes_bids) == 2
    assert b.last_trade_price == 0.53 and b.min_order_size == 5 and b.tick_size == 0.001
    assert b.midpoint == pytest.approx(0.535)
    assert b.ts_utc.year == 2026


def test_price_history_parsed_sorted_dedup() -> None:
    df = parse_price_history([{"t": 120, "p": 0.3}, {"t": 60, "p": 0.2}, {"t": 120, "p": 0.31}])
    assert list(df["price"]) == [0.2, 0.31]
    assert str(df["ts"].dt.tz) == "UTC"
