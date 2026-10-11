"""Offline tests for cross-venue matching, on synthetic events."""

from __future__ import annotations

import pandas as pd
import pytest

from pm_edge.config import Config
from pm_edge.matching import AliasTable, make_match_id, match_events, team_key

ALIASES = AliasTable(
    {
        "Arsenal": "Arsenal",
        "Arsenal FC": "Arsenal",
        "Coventry": "Coventry City",
        "Coventry City FC": "Coventry City",
        "Nottingham": "Nottingham Forest",
        "Nottingham Forest FC": "Nottingham Forest",
        "Manchester City": "Manchester City",
        "Manchester United": "Manchester United",
        "Be`er Sheva": "Hapoel Beer Sheva",
        "MH Hapoel Be'er Sheva": "Hapoel Beer Sheva",
        "Bodoe/Glimt": "Bodo/Glimt",
        "FK Bodø/Glimt": "Bodo/Glimt",
        "Fenerbahçe SK": "Fenerbahce",
        "Brighton & Hove Albion FC": "Brighton and Hove Albion",
    }
)
KO = pd.Timestamp("2026-08-21T19:00:00Z")


def _ev(venue: str, eid: str, home: str, away: str, ko: pd.Timestamp = KO, league: str = "EPL", **kw: str) -> dict:
    return {"venue": venue, "league": league, "venue_event_id": eid, "title": f"{home} vs {away}",
            "home_name": home, "away_name": away, "kickoff_utc": ko, **kw}


def _run(cfg: Config, *events: dict):
    return match_events(pd.DataFrame(list(events)), ALIASES, cfg)


def test_team_key_normalises_accents_punctuation_and_ampersand() -> None:
    assert team_key("Fenerbahçe SK") == "fenerbahce sk"
    assert team_key("FK Bodø/Glimt") == "fk bodo glimt"
    assert team_key("Be`er Sheva") == team_key("Be'er Sheva") == "beer sheva"
    assert team_key("Brighton & Hove Albion") == "brighton and hove albion"


def test_alias_table_rejects_conflicting_rows() -> None:
    with pytest.raises(ValueError):
        AliasTable({"City": "Manchester City", "city": "Leicester City"})


def test_nottingham_alias_pairs_across_venues(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K1", "Nottingham", "Arsenal"), _ev("polymarket", "P1", "Nottingham Forest FC", "Arsenal FC"))
    assert r.matches["match_id"].nunique() == 1
    assert r.matches["match_id"].iloc[0] == "EPL_2026-08-21_nottingham-forest_arsenal"
    assert set(r.matches["venue"]) == {"kalshi", "polymarket"}
    assert r.review.empty


def test_match_id_format() -> None:
    assert make_match_id("UCL", pd.Timestamp("2026-08-25T23:30:00Z"), "Hapoel Beer Sheva", "Bodo/Glimt") == (
        "UCL_2026-08-25_hapoel-beer-sheva_bodo-glimt"
    )


def test_kickoff_tolerance_boundary(cfg: Config) -> None:
    tol = cfg.matching.kickoff_tolerance_minutes
    inside = _run(cfg, _ev("kalshi", "K", "Arsenal", "Coventry"),
                  _ev("polymarket", "P", "Arsenal FC", "Coventry City FC", KO + pd.Timedelta(minutes=tol)))
    assert inside.matches["match_id"].nunique() == 1
    outside = _run(cfg, _ev("kalshi", "K", "Arsenal", "Coventry"),
                   _ev("polymarket", "P", "Arsenal FC", "Coventry City FC", KO + pd.Timedelta(minutes=tol + 1)))
    # Same teams, same UTC date, kickoffs too far apart: must not share a match_id, so neither is paired.
    assert outside.matches.empty
    assert set(outside.review["kind"]) == {"kickoff_mismatch"}
    assert set(outside.review["venue_event_id"]) == {"K", "P"}


def test_home_away_swap_is_never_paired(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K", "Arsenal", "Coventry"), _ev("polymarket", "P", "Coventry City FC", "Arsenal FC"))
    assert r.matches["match_id"].nunique() == 2
    assert set(r.review["kind"]) == {"home_away_swapped"}


def test_two_legged_tie_is_two_matches(cfg: Config) -> None:
    leg2 = KO + pd.Timedelta(days=7)
    r = _run(cfg,
             _ev("kalshi", "K1", "Be`er Sheva", "Bodoe/Glimt", league="UCL"),
             _ev("polymarket", "P1", "MH Hapoel Be'er Sheva", "FK Bodø/Glimt", league="UCL"),
             _ev("kalshi", "K2", "Bodoe/Glimt", "Be`er Sheva", leg2, league="UCL"),
             _ev("polymarket", "P2", "FK Bodø/Glimt", "MH Hapoel Be'er Sheva", leg2, league="UCL"))
    assert r.matches.groupby("match_id")["venue"].nunique().tolist() == [2, 2]
    assert r.review.empty


def test_fuzzy_below_threshold_is_never_accepted(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K", "Arsenl", "Coventry"), _ev("polymarket", "P", "Arsenal FC", "Coventry City FC"))
    # "arsenl" vs "arsenal" scores ~92 (accepted, flagged); force a weaker variant below 90:
    r2 = _run(cfg, _ev("kalshi", "K", "Arsnl Lndn", "Coventry"), _ev("polymarket", "P", "Arsenal FC", "Coventry City FC"))
    accepted = r.review[r.review["kind"] == "fuzzy_accepted"]
    assert len(accepted) == 1 and accepted["score"].iloc[0] >= cfg.matching.fuzzy_auto_accept_threshold
    assert r.matches["match_id"].nunique() == 1  # accepted, but visible in the review file
    unresolved = r2.review[r2.review["kind"] == "unresolved_team"]
    assert len(unresolved) == 1 and unresolved["score"].iloc[0] < cfg.matching.fuzzy_auto_accept_threshold
    assert "K" not in set(r2.matches["venue_event_id"])


def test_fuzzy_suggestion_recorded_between_thresholds(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K", "Manchester Utd FC", "Arsenal"), _ev("polymarket", "P", "Manchester United", "Arsenal FC"))
    row = r.review[r.review["kind"] == "unresolved_team"].iloc[0]
    assert cfg.matching.fuzzy_suggest_threshold <= row["score"] < cfg.matching.fuzzy_auto_accept_threshold
    assert row["suggestion"] == "Manchester United"
    assert "K" not in set(r.matches["venue_event_id"])


def test_city_vs_united_not_confused(cfg: Config) -> None:
    res = ALIASES.resolve("Manchester Citty", cfg.matching.fuzzy_suggest_threshold, cfg.matching.fuzzy_auto_accept_threshold)
    assert res.canonical == "Manchester City"


def test_venue_alias_column_used_as_exact_fallback(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K", "Arsenal", "Coventry"),
             _ev("polymarket", "P", "Arsenal Football Club Ltd", "Coventry City FC", home_alias="Arsenal"))
    assert r.matches["match_id"].nunique() == 1
    assert set(r.matches.loc[r.matches["venue"] == "polymarket", "home_method"]) == {"venue_alias"}


def test_duplicate_events_on_one_venue_go_to_review(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K1", "Arsenal", "Coventry"), _ev("kalshi", "K2", "Arsenal", "Coventry"),
             _ev("polymarket", "P", "Arsenal FC", "Coventry City FC"))
    assert set(r.review.loc[r.review["kind"] == "duplicate_venue_event", "venue_event_id"]) == {"K1", "K2"}
    assert set(r.matches["venue_event_id"]) == {"P"}


def test_unpaired_event_reported(cfg: Config) -> None:
    r = _run(cfg, _ev("kalshi", "K", "Arsenal", "Coventry"), _ev("polymarket", "P", "Manchester City", "Manchester United"))
    assert set(r.review["kind"]) == {"unpaired"}
    assert r.summary["matched_on_all_venues"] == 0
