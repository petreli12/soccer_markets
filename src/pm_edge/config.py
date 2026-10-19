"""Configuration loading: non-secret settings from config.yaml, secrets from .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Secrets(BaseSettings):
    """Secrets read from the environment or .env. Only data-subscription keys, never trading credentials."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    odds_api_key: str | None = None
    ntfy_topic: str | None = None


class KalshiLeague(BaseModel):
    series_ticker: str


class PolymarketLeague(BaseModel):
    series_id: str
    sport: str
    market_type: str = "moneyline"


class OddsApiLeague(BaseModel):
    sport_key: str


class LeagueConfig(BaseModel):
    enabled: bool = True
    sport: str
    outcomes: list[str]
    kalshi: KalshiLeague
    polymarket: PolymarketLeague
    odds_api: OddsApiLeague


class KalshiVenue(BaseModel):
    base_url: str
    max_requests_per_second: float = 10
    max_candles_per_request: int = 5000
    orderbook_batch_size: int = 100


class PolymarketVenue(BaseModel):
    gamma_url: str
    clob_url: str
    data_url: str = "https://data-api.polymarket.com"
    max_requests_per_second: float = 20
    books_batch_size: int = 100
    prices_history_fidelity_minutes: int = 1


class UsExCrossCheck(BaseModel):
    enabled: bool = False
    bookmakers: list[str] = Field(default_factory=lambda: ["kalshi", "polymarket"])


class OddsApiVenue(BaseModel):
    base_url: str
    bookmakers: list[str]
    sharp_bookmaker: str = "pinnacle"
    odds_format: str = "decimal"
    credit_floor: int = 25
    live_poll_interval_seconds: int = 1800
    us_ex_crosscheck: UsExCrossCheck = Field(default_factory=UsExCrossCheck)


class Venues(BaseModel):
    kalshi: KalshiVenue
    polymarket: PolymarketVenue
    odds_api: OddsApiVenue


class KalshiFees(BaseModel):
    formula: str = "quadratic"
    taker_rate: float = 0.07
    default_multiplier: float = 1.0
    rounding_increment_usd: float = 0.01


class PolymarketFees(BaseModel):
    formula: str = "quadratic_exponent"
    default_taker_rate: float = 0.05
    default_exponent: float = 1.0
    rounding_decimals: int = 5
    apply_taker_rebate: bool = False


class Fees(BaseModel):
    kalshi: KalshiFees
    polymarket: PolymarketFees


class Devig(BaseModel):
    method: str = "power"
    fallback: str = "multiplicative"
    power_max_iter: int = 100
    power_tolerance: float = 1e-10


class Matching(BaseModel):
    fuzzy_suggest_threshold: int = 80
    fuzzy_auto_accept_threshold: int = 90
    kickoff_tolerance_minutes: int = 120
    team_aliases_path: str = "data/team_aliases.csv"
    review_path: str = "data/unmatched_review.csv"


class Staleness(BaseModel):
    kalshi_historical: float | None = None
    kalshi_live: float | None = 5
    polymarket: float | None = 5
    sportsbook: float | None = 5


class Normalize(BaseModel):
    resample: str = "1min"
    window_before_kickoff_hours: float = 48
    window_after_kickoff_hours: float = 3
    staleness_minutes: Staleness = Field(default_factory=Staleness)


class SharpConfig(BaseModel):
    max_pinnacle_age_minutes: float = 5
    divergence_threshold: float = 0.03


class TimeBucket(BaseModel):
    name: str
    min_minutes: float | None = None
    max_minutes: float | None = None


class PmHistorySemantics(BaseModel):
    min_matched_minutes: int = 500
    match_tolerance: float = 0.0005


class Analysis(BaseModel):
    gap_thresholds: list[float]
    reference_notional_usd: float = 100
    sharp: SharpConfig = Field(default_factory=SharpConfig)
    time_buckets: list[TimeBucket]
    focus_windows: list[TimeBucket] = Field(default_factory=list)
    headline_bucket: str = "2h-kickoff"
    exclude_inplay_from_decision: bool = True
    pm_history_semantics: PmHistorySemantics = Field(default_factory=PmHistorySemantics)


class Collector(BaseModel):
    poll_interval_seconds: int = 20
    lookahead_hours: float = 48
    inplay_tail_hours: float = 3
    orderbook_depth: int = 5
    live_dir: str = "data/live"


class CollectorLite(BaseModel):
    alert_stale_minutes: float = 10
    alert_repeat_minutes: float = 30
    window_before_kickoff_hours: float = 3
    window_after_kickoff_hours: float = 0
    book_poll_seconds: int = 20
    orderbook_depth: int = 3
    odds_poll_seconds: int = 600
    odds_bookmakers: list[str] = Field(default_factory=lambda: ["pinnacle"])
    odds_credit_floor: int = 50
    schedule_refresh_seconds: int = 1800
    schedule_lookahead_hours: float = 48
    flush_seconds: int = 120
    live_dir: str = "data/live"


class Decision(BaseModel):
    min_executable_episodes_per_matchday: float = 3
    executable_threshold: float = 0.02
    min_median_episode_minutes: float = 2
    min_median_executable_usd: float = 200
    sharp_divergence_threshold: float = 0.03
    min_share_matches_with_sharp_divergence: float = 0.20


class Paths(BaseModel):
    raw_dir: str = "data/raw"
    processed_dir: str = "data/processed"
    report_path: str = "reports/edge_report.html"


class Config(BaseModel):
    """Top-level typed view of config.yaml."""

    leagues: dict[str, LeagueConfig]
    venues: Venues
    fees: Fees
    devig: Devig = Field(default_factory=Devig)
    matching: Matching = Field(default_factory=Matching)
    normalize: Normalize = Field(default_factory=Normalize)
    analysis: Analysis
    collector: Collector = Field(default_factory=Collector)
    collector_lite: CollectorLite = Field(default_factory=CollectorLite)
    decision: Decision = Field(default_factory=Decision)
    paths: Paths = Field(default_factory=Paths)

    def league(self, name: str) -> LeagueConfig:
        """Return the config for an enabled league, raising a clear error otherwise."""
        try:
            cfg = self.leagues[name]
        except KeyError as exc:
            raise KeyError(f"Unknown league {name!r}; configured: {sorted(self.leagues)}") from exc
        if not cfg.enabled:
            raise ValueError(f"League {name!r} is disabled in config.yaml")
        return cfg

    def resolve(self, relative: str) -> Path:
        """Resolve a config path relative to the project root."""
        return PROJECT_ROOT / relative


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate config.yaml."""
    with open(path or DEFAULT_CONFIG_PATH) as fh:
        return Config.model_validate(yaml.safe_load(fh))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Cached default config."""
    return load_config()


def get_secrets() -> Secrets:
    """Load secrets from the environment / .env."""
    return Secrets()
