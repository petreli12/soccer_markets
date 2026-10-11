"""Cross-venue match identification.

A match is identified by ``match_id = {league}_{kickoff date UTC}_{home}_{away}``, built from canonical
team names taken from ``data/team_aliases.csv``. Rules:

* Team names are resolved through the alias table after a light normalisation (case, accents,
  punctuation, ``&`` → ``and``). The table is the source of truth.
* A name that is not in the table goes to rapidfuzz against every known alias. A score at or above
  ``matching.fuzzy_auto_accept_threshold`` (90) is accepted, but it is still written to the review file,
  so it is never silent. A score at or above ``fuzzy_suggest_threshold`` (80) is only written as a
  suggestion; the event stays unmatched. ``token_sort_ratio`` is used rather than ``WRatio``, because
  WRatio's partial matching scores "Inter" against "Inter Milan B" highly.
* Events from different venues pair only if both canonical teams agree, home and away the same way
  round, and the kickoffs are within ``matching.kickoff_tolerance_minutes``.
  A pair with home and away swapped is never paired; it is flagged for review.

Outcome mapping across venues: each venue ingest maps its contracts to HOME/DRAW/AWAY relative to
that venue's own home team (docs/api_notes.md §1, §2). Pairing requires the same canonical home team
on every venue, so a venue's HOME contract is the match's HOME outcome without further translation.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from pm_edge.config import Config

log = logging.getLogger(__name__)

REVIEW_COLUMNS = [
    "league", "venue", "venue_event_id", "kind", "detail", "raw_home", "raw_away", "kickoff_utc", "suggestion", "score",
]
MATCH_COLUMNS = [
    "match_id", "league", "kickoff_utc", "home", "away", "venue", "venue_event_id", "venue_title",
    "venue_kickoff_utc", "kickoff_diff_min", "home_method", "away_method",
]
_TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l"})


def team_key(name: str) -> str:
    """Normalised lookup key: ``"Fenerbahçe SK"`` → ``"fenerbahce sk"``, ``"Be`er Sheva"`` → ``"beer sheva"``."""
    s = unicodedata.normalize("NFKD", name.translate(_TRANSLIT))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s = s.replace("&", " and ")
    s = re.sub(r"['`’]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def slug(name: str) -> str:
    return team_key(name).replace(" ", "-")


def make_match_id(league: str, kickoff: pd.Timestamp, home: str, away: str) -> str:
    """``EPL_2026-08-21_arsenal_coventry-city``."""
    return f"{league}_{kickoff.tz_convert('UTC').date().isoformat()}_{slug(home)}_{slug(away)}"


@dataclass
class TeamResolution:
    canonical: str | None
    method: str  # "alias" | "fuzzy" | "unresolved"
    score: float = 100.0
    suggestion: str | None = None
    matched_alias: str | None = None


class AliasTable:
    """Maps every known team-name variant to one canonical name."""

    def __init__(self, rows: dict[str, str]) -> None:
        self._by_key: dict[str, str] = {}
        self._alias_by_key: dict[str, str] = {}
        for alias, canonical in rows.items():
            key = team_key(alias)
            prev = self._by_key.get(key)
            if prev is not None and prev != canonical:
                raise ValueError(f"alias {alias!r} maps to both {prev!r} and {canonical!r}")
            self._by_key[key] = canonical
            self._alias_by_key[key] = alias
        for canonical in set(rows.values()):
            self._by_key.setdefault(team_key(canonical), canonical)
            self._alias_by_key.setdefault(team_key(canonical), canonical)

    @classmethod
    def from_csv(cls, path: Path) -> AliasTable:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        missing = {"alias", "canonical"} - set(df.columns)
        if missing:
            raise ValueError(f"{path} lacks columns {sorted(missing)}")
        return cls(dict(zip(df["alias"].str.strip(), df["canonical"].str.strip())))

    @property
    def canonicals(self) -> set[str]:
        return set(self._by_key.values())

    def lookup(self, name: str) -> str | None:
        return self._by_key.get(team_key(name))

    def resolve(self, name: str, suggest_at: float, accept_at: float) -> TeamResolution:
        """Exact alias lookup, else a fuzzy candidate that is accepted only at ``accept_at`` or above."""
        hit = self.lookup(name)
        if hit is not None:
            return TeamResolution(hit, "alias")
        best = process.extractOne(team_key(name), list(self._by_key), scorer=fuzz.token_sort_ratio)
        if best is None:
            return TeamResolution(None, "unresolved", 0.0)
        key, score, _ = best
        canonical = self._by_key[key]
        if score >= accept_at:
            return TeamResolution(canonical, "fuzzy", score, canonical, self._alias_by_key[key])
        return TeamResolution(None, "unresolved", score, canonical if score >= suggest_at else None, self._alias_by_key[key])


@dataclass
class MatchResult:
    matches: pd.DataFrame
    review: pd.DataFrame
    summary: dict[str, object] = field(default_factory=dict)


def venue_events(contract_rows: pd.DataFrame) -> pd.DataFrame:
    """Collapse an ingest ``matches_league=*.parquet`` frame (one row per contract) to one row per event."""
    cols = ["venue", "league", "venue_event_id", "title", "home_name", "away_name", "kickoff_utc"]
    extra = [c for c in ("home_alias", "away_alias") if c in contract_rows.columns]
    return contract_rows[cols + extra].drop_duplicates("venue_event_id").reset_index(drop=True)


def match_events(events: pd.DataFrame, aliases: AliasTable, cfg: Config) -> MatchResult:
    """Pair events across venues. ``events`` has one row per venue event (see :func:`venue_events`)."""
    m = cfg.matching
    tol = pd.Timedelta(minutes=m.kickoff_tolerance_minutes)
    review: list[dict[str, object]] = []

    def flag(row: pd.Series, kind: str, detail: str, suggestion: str | None = None, score: float | None = None) -> None:
        review.append({
            "league": row["league"], "venue": row["venue"], "venue_event_id": row["venue_event_id"], "kind": kind,
            "detail": detail, "raw_home": row["home_name"], "raw_away": row["away_name"],
            "kickoff_utc": row["kickoff_utc"], "suggestion": suggestion, "score": score,
        })

    resolved = []
    for _, row in events.iterrows():
        sides = {}
        ok = True
        for side in ("home", "away"):
            res = aliases.resolve(row[f"{side}_name"], m.fuzzy_suggest_threshold, m.fuzzy_auto_accept_threshold)
            alias_col = f"{side}_alias"
            if res.canonical is None and alias_col in row and pd.notna(row[alias_col]) and row[alias_col]:
                alt = aliases.lookup(str(row[alias_col]))
                if alt is not None:
                    res = TeamResolution(alt, "venue_alias")
            if res.method == "fuzzy":
                flag(row, "fuzzy_accepted", f"{side} {row[f'{side}_name']!r} -> {res.canonical!r} via {res.matched_alias!r}",
                     res.canonical, res.score)
            if res.canonical is None:
                ok = False
                flag(row, "unresolved_team", f"{side} {row[f'{side}_name']!r} not in alias table"
                     + (f"; closest alias {res.matched_alias!r}" if res.matched_alias else ""), res.suggestion, res.score)
            sides[side] = res
        if ok:
            resolved.append({**row.to_dict(), "home": sides["home"].canonical, "away": sides["away"].canonical,
                             "home_method": sides["home"].method, "away_method": sides["away"].method})

    res_df = pd.DataFrame(resolved)
    out_rows: list[dict[str, object]] = []
    if res_df.empty:
        return MatchResult(pd.DataFrame(columns=MATCH_COLUMNS), pd.DataFrame(review, columns=REVIEW_COLUMNS))

    for (league, home, away), grp in res_df.groupby(["league", "home", "away"], sort=False):
        grp = grp.sort_values("kickoff_utc")
        clusters: list[list[pd.Series]] = []
        for _, row in grp.iterrows():
            if clusters and row["kickoff_utc"] - clusters[-1][0]["kickoff_utc"] <= tol:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        # Clusters that would share a match_id (same teams, same UTC date, kickoffs beyond tolerance)
        # must not be merged by id: send every event involved to review.
        ids = [make_match_id(league, min(r["kickoff_utc"] for r in c), home, away) for c in clusters]
        clashing = {i for i in ids if ids.count(i) > 1}
        for cluster, cid in zip(clusters, ids):
            if cid not in clashing:
                continue
            for r in cluster:
                flag(r, "kickoff_mismatch", f"{home} v {away} listed more than once on {cid.split('_')[1]} "
                     f"with kickoffs more than {m.kickoff_tolerance_minutes} min apart")
        clusters = [c for c, cid in zip(clusters, ids) if cid not in clashing]
        for cluster in clusters:
            by_venue: dict[str, list[pd.Series]] = {}
            for row in cluster:
                by_venue.setdefault(row["venue"], []).append(row)
            keep = []
            for venue, rows in by_venue.items():
                if len(rows) > 1:
                    for r in rows:
                        flag(r, "duplicate_venue_event", f"{len(rows)} {venue} events for {home} v {away} within tolerance")
                else:
                    keep.append(rows[0])
            if not keep:
                continue
            kickoff = min(r["kickoff_utc"] for r in keep)
            match_id = make_match_id(league, kickoff, home, away)
            for r in keep:
                out_rows.append({
                    "match_id": match_id, "league": league, "kickoff_utc": kickoff, "home": home, "away": away,
                    "venue": r["venue"], "venue_event_id": r["venue_event_id"], "venue_title": r["title"],
                    "venue_kickoff_utc": r["kickoff_utc"],
                    "kickoff_diff_min": (r["kickoff_utc"] - kickoff).total_seconds() / 60,
                    "home_method": r["home_method"], "away_method": r["away_method"],
                })

    matches = pd.DataFrame(out_rows, columns=MATCH_COLUMNS)
    venues = sorted(events["venue"].unique())
    per_match = matches.groupby("match_id")["venue"].nunique()
    singles = matches[matches["match_id"].isin(per_match[per_match < len(venues)].index)]

    # Explain matches missing on some venue: swapped home/away, kickoff outside tolerance, or just not listed.
    for _, r in singles.iterrows():
        others = res_df[(res_df["league"] == r["league"]) & (res_df["venue"] != r["venue"])]
        swapped = others[(others["home"] == r["away"]) & (others["away"] == r["home"])
                         & ((others["kickoff_utc"] - r["venue_kickoff_utc"]).abs() <= tol)]
        same = others[(others["home"] == r["home"]) & (others["away"] == r["away"])]
        row = events[(events["venue"] == r["venue"]) & (events["venue_event_id"] == r["venue_event_id"])].iloc[0]
        missing = sorted(set(venues) - set(matches.loc[matches["match_id"] == r["match_id"], "venue"]))
        if len(swapped):
            flag(row, "home_away_swapped", f"{missing} list {r['away']} v {r['home']} at a matching kickoff",
                 str(swapped["venue_event_id"].iloc[0]))
        elif len(same):
            gap = ((same["kickoff_utc"] - r["venue_kickoff_utc"]).abs().min()).total_seconds() / 60
            flag(row, "kickoff_mismatch", f"same teams on {missing} but nearest kickoff is {gap:.0f} min away",
                 str(same.iloc[((same["kickoff_utc"] - r["venue_kickoff_utc"]).abs()).argmin()]["venue_event_id"]))
        else:
            flag(row, "unpaired", f"no event on {missing} for {r['home']} v {r['away']}")

    summary = {
        "venues": venues,
        "events_per_venue": events["venue"].value_counts().to_dict(),
        "matches": int(per_match.size),
        "matched_on_all_venues": int((per_match == len(venues)).sum()),
        "review_rows": len(review),
    }
    return MatchResult(matches, pd.DataFrame(review, columns=REVIEW_COLUMNS), summary)


def run(cfg: Config, leagues: list[str], venues: tuple[str, ...] = ("kalshi", "polymarket")) -> dict[str, object]:
    """Match every league's ingested events; write processed match tables and one review file."""
    aliases = AliasTable.from_csv(cfg.resolve(cfg.matching.team_aliases_path))
    raw = cfg.resolve(cfg.paths.raw_dir)
    out_dir = cfg.resolve(cfg.paths.processed_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, object] = {}
    reviews = []
    for league in leagues:
        frames = []
        for venue in venues:
            path = raw / venue / f"matches_league={league}.parquet"
            if path.exists():
                frames.append(venue_events(pd.read_parquet(path)))
            else:
                log.warning("no %s ingest for %s (%s)", venue, league, path)
        if not frames:
            continue
        result = match_events(pd.concat(frames, ignore_index=True), aliases, cfg)
        result.matches.to_parquet(out_dir / f"matches_league={league}.parquet", index=False)
        reviews.append(result.review)
        summaries[league] = result.summary
    review = pd.concat(reviews, ignore_index=True) if reviews else pd.DataFrame(columns=REVIEW_COLUMNS)
    review.to_csv(cfg.resolve(cfg.matching.review_path), index=False)
    return summaries
