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
pm-edge thin-scan --league UCL --kickoff-within-hours 36   # Polymarket non-moneyline books, 24h volume, trades in last 3h
```

`thin-scan` prints a summary by market type plus the markets with a spread over 3c and at least $500 at the best ask. The full per-market table goes to `data/processed/thin_scan/thin_scan_<UTC stamp>.csv`.

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

### On a small Ubuntu VPS

The collector is one process making a few requests a second, so 1 vCPU / 1 GB RAM is enough. It writes roughly tens of MB of parquet per matchday. The examples assume the default `ubuntu` login user and the repo at `~/pm-edge-soccer`; adjust both to taste.

**1. Install uv and clone** (uv installs Python 3.11 itself, so no system Python is needed):

```bash
sudo apt update && sudo apt install -y git rsync
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env        # or log out and back in
git clone https://github.com/petreli12/soccer_markets.git ~/pm-edge-soccer   # private repo: use a token or deploy key
cd ~/pm-edge-soccer && uv sync
```

**2. Copy `.env` from the Mac.** It is never committed, so it has to be copied by hand:

```bash
scp .env ubuntu@VPS:~/pm-edge-soccer/.env      # run on the Mac, from the repo root
ssh ubuntu@VPS chmod 600 ~/pm-edge-soccer/.env
```

`data/team_aliases.csv` comes with the clone. No other data is needed, because `collect-lite` builds its own schedule from the live APIs.

**3. Smoke test**, then press Ctrl-C:

```bash
source .venv/bin/activate && pm-edge collect-lite   # expect "schedule: N paired matches" within ~30 s
```

**4. Run under systemd.** Create `/etc/systemd/system/pm-edge-collector.service`:

```ini
[Unit]
Description=pm-edge collector-lite (read-only market data)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/pm-edge-soccer
ExecStart=/home/ubuntu/pm-edge-soccer/.venv/bin/pm-edge collect-lite
Restart=on-failure
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pm-edge-collector   # also starts on reboot
systemctl status pm-edge-collector
journalctl -u pm-edge-collector -f              # stdout/stderr; tracebacks land here
tail -f ~/pm-edge-soccer/data/live/collector.log
sudo systemctl stop pm-edge-collector           # SIGTERM -> final flush, exits 0, not restarted
```

`Restart=on-failure` restarts the collector after a crash or non-zero exit, but not after `systemctl stop`. `StartLimitIntervalSec=0` means it never gives up restarting. A restart loses at most the last 2 minutes of buffered rows.

All timestamps are UTC, so the VPS timezone doesn't matter. The clock does: `timedatectl` should show `System clock synchronized: yes` (Ubuntu's default `systemd-timesyncd` handles this).

**5. Switching from the Mac.** Run only one collector at a time. Two collectors would write duplicate rows and spend Odds API credits twice. Stop the Mac collector (`pkill -TERM -f "pm-edge collect-lite"`) before starting the VPS one.

**6. Copying the parquet data back** (run on the Mac, from the repo root):

```bash
rsync -avz ubuntu@VPS:pm-edge-soccer/data/live/ data/live/          # incremental; safe to rerun
rsync -avz --exclude '*.log*' --exclude '*.stdout' ubuntu@VPS:pm-edge-soccer/data/live/ data/live/   # data only
# or a one-off copy without rsync:
scp -r ubuntu@VPS:pm-edge-soccer/data/live/venue=* data/live/
```

Part files have unique names (`part-<timestamp>-<random>.parquet`), so copying merges with whatever the Mac already collected and never overwrites it. Don't use `--delete`. Copying while the collector runs is fine, because files are written once and never modified.

After a matchday: `python notebooks/matchday_report.py --date 2026-10-12`. It reports executable cross-venue edges, divergence from Pinnacle, and whether Polymarket's history value is the midpoint or the last trade. It ends with the run's decision metric: the share of matches where either venue's mid was at least 3c from a fresh Pinnacle price in the final 3 h, and whether the gap's direction (by favourite, draw, underdog) is consistent across matches.

## Tests

```bash
pytest -m "not live"   # offline unit tests only
pytest                 # everything, including tests that hit live public APIs
```
