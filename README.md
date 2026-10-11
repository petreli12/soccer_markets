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
pm-edge collect-lite                      # live pre-kickoff snapshots -> data/live/venue=*/date=*/
```

## Running the collector

`collect-lite` covers every match listed on both Kalshi and Polymarket.
- **When:** from 3 h before kickoff until kickoff.
- **Books:** every 20 s, both venues' books (top 3 levels, all three outcomes).
- **Pinnacle:** every 10 min, h2h odds (1 Odds API credit per league per call). It stops calling below 50 remaining credits.
- **Output:** parquet, flushed every 2 min.
- **Logs:** `data/live/collector.log`, with a heartbeat every 10 min.
- **Stopping:** SIGTERM or Ctrl-C flushes buffered rows before exiting.

macOS, keeping the machine awake while it runs (lid-close sleep still stops it):

```bash
source .venv/bin/activate
nohup caffeinate -i pm-edge collect-lite >> data/live/collector.stdout 2>&1 &
tail -f data/live/collector.log          # watch
pkill -TERM -f "pm-edge collect-lite"   # stop
```

Linux (systemd), as `/etc/systemd/system/pm-edge-collector.service`:

```ini
[Unit]
Description=pm-edge collector-lite
After=network-online.target

[Service]
WorkingDirectory=/opt/pm-edge-soccer
ExecStart=/opt/pm-edge-soccer/.venv/bin/pm-edge collect-lite
Restart=always
RestartSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now pm-edge-collector` and `journalctl -u pm-edge-collector -f`.

After a matchday: `python notebooks/matchday_report.py --date 2026-10-12`. It reports executable cross-venue edges, divergence from Pinnacle, and whether Polymarket's history value is the midpoint or the last trade.

## Tests

```bash
pytest -m "not live"   # offline unit tests only
pytest                 # everything, including tests that hit live public APIs
```
