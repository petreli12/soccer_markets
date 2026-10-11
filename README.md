# pm-edge-soccer

A read-only research tool. It measures whether pricing gaps between prediction markets (Kalshi, Polymarket) and sportsbooks on soccer matches are large, frequent, and liquid enough to support a paid alert product.

It **never places orders and needs no trading credentials**. The only secret is an optional The Odds API data key, kept in `.env`.

- API details, fees, and verified quirks: [`docs/api_notes.md`](docs/api_notes.md)
- Settings: [`config.yaml`](config.yaml)

## Setup

```bash
uv sync
source .venv/bin/activate
cp .env.example .env   # add ODDS_API_KEY when available
```

## Commands (so far)

```bash
pm-edge backfill --league EPL --days 60   # Kalshi + Polymarket (sportsbooks in a later step)
pm-edge backfill --league EPL --days 60 --venue polymarket   # one venue only
pm-edge match                             # pair events across venues -> data/processed/, data/unmatched_review.csv
pm-edge verify-kalshi-gaps --league EPL   # re-test "missing candle == no change"
```

## Tests

```bash
pytest -m "not live"   # offline unit tests only
pytest                 # everything, including tests that hit live public APIs
```
