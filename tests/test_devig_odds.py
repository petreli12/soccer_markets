from __future__ import annotations

import pytest

from pm_edge.config import Config
from pm_edge.devig import devig, multiplicative, power
from pm_edge.ingest.odds import map_h2h_outcome, parse_h2h
from pm_edge.models import Outcome

ODDS = [3.2, 3.59, 2.26]  # Coventry / Draw / Newcastle, Pinnacle 2026-10-11


def test_multiplicative_sums_to_one_and_keeps_ratios() -> None:
    p = multiplicative(ODDS)
    assert sum(p) == pytest.approx(1.0)
    assert p[0] / p[2] == pytest.approx(ODDS[2] / ODDS[0])


def test_power_sums_to_one_and_shades_longshots_more() -> None:
    p = power(ODDS)
    m = multiplicative(ODDS)
    assert p is not None and sum(p) == pytest.approx(1.0, abs=1e-9)
    # Favourite (lowest odds) gets more probability under power than multiplicative; longest shot gets less.
    assert p[2] > m[2]
    assert p[1] < m[1]


def test_power_on_fair_book_is_identity() -> None:
    p = power([2.0, 4.0, 4.0])
    assert p == pytest.approx([0.5, 0.25, 0.25])


def test_power_handles_underround() -> None:
    p = power([2.1, 4.2, 4.2])
    assert p is not None and sum(p) == pytest.approx(1.0, abs=1e-9)


def test_devig_uses_config_and_reports_method(cfg: Config) -> None:
    p, method = devig(ODDS, cfg.devig)
    assert method == "power" and sum(p) == pytest.approx(1.0, abs=1e-9)


def test_invalid_odds_rejected() -> None:
    with pytest.raises(ValueError):
        multiplicative([1.0, 3.0, 3.0])


def test_h2h_outcome_mapping_by_name_not_position() -> None:
    assert map_h2h_outcome("Chelsea", "Everton", "Chelsea") is Outcome.AWAY
    assert map_h2h_outcome("Draw", "Everton", "Chelsea") is Outcome.DRAW
    assert map_h2h_outcome("Everton", "Everton", "Chelsea") is Outcome.HOME
    assert map_h2h_outcome("Tie", "Everton", "Chelsea") is None


def test_parse_h2h_rows() -> None:
    ev = [{"id": "e1", "home_team": "Everton", "away_team": "Chelsea", "commence_time": "2026-10-17T11:30:00Z",
           "bookmakers": [{"key": "pinnacle", "last_update": "2026-10-11T19:33:47Z", "markets": [
               {"key": "h2h", "last_update": "2026-10-11T19:33:47Z", "outcomes": [
                   {"name": "Chelsea", "price": 2.16}, {"name": "Everton", "price": 3.28}, {"name": "Draw", "price": 3.76}]}]}]}]
    rows = parse_h2h(ev, "EPL")
    assert [r["outcome"] for r in rows] == ["AWAY", "HOME", "DRAW"]
    assert rows[0]["last_update"] == "2026-10-11T19:33:47Z"
