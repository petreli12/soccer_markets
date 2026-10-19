"""Push alerts for collect-lite via ntfy.sh, and the out-of-process watchdog (``pm-edge watchdog``).

The collector writes ``<live_dir>/status.json`` on every loop iteration: per-venue
``failing_since`` (first failure of the current unbroken run of failures, ``None`` when the last
call succeeded), its stats and its active matches. It also sends the window-close summary itself.

The watchdog runs separately (launchd ``StartInterval`` / cron / systemd timer, once a minute), so
it still alerts when the collector is dead. Using only files on disk it checks, while a collection
window is open according to the latest ``venue=schedule`` snapshot:

* ``stale_rows``: no new book parquet part (Kalshi or Polymarket) for ``alert_stale_minutes``,
  counted from the later of the newest part's mtime and the window start;
* ``venue:<name>``: ``failing_since`` older than ``alert_stale_minutes`` in a status file that
  is itself fresh.

Each condition alerts once, repeats every ``alert_repeat_minutes`` while it persists, and sends one
"recovered" message when it clears. State is kept in ``<live_dir>/watchdog_state.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from pm_edge.config import Config, Secrets

log = logging.getLogger(__name__)

NTFY_URL = "https://ntfy.sh"
BOOK_VENUES = ("kalshi", "polymarket")


def notify(topic: str | None, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """POST one ntfy message. Never raises: alerting must not take the collector down."""
    if not topic:
        log.warning("ntfy: NTFY_TOPIC not set; would have sent %r", title)
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        r = httpx.post(f"{NTFY_URL}/{topic}", content=message.encode(), headers=headers, timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("ntfy: send failed: %r", exc)
        return False


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, default=str, indent=1))
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def latest_schedule(live_dir: Path) -> pd.DataFrame:
    """Matches in the most recent schedule snapshot (one row per match_id)."""
    dirs = sorted((live_dir / "venue=schedule").glob("date=*"))[-2:]
    files = [f for d in dirs for f in d.glob("*.parquet")]
    if not files:
        return pd.DataFrame(columns=["match_id", "kickoff_utc"])
    s = pd.concat((pd.read_parquet(f, columns=["ts_poll", "match_id", "kickoff_utc"]) for f in files),
                  ignore_index=True)
    return s[s["ts_poll"] == s["ts_poll"].max()].drop_duplicates("match_id")


def window_start(schedule: pd.DataFrame, now: datetime, before_h: float, after_h: float) -> datetime | None:
    """Start of the open collection window (earliest start among active matches), or None."""
    ko = pd.to_datetime(schedule["kickoff_utc"], utc=True)
    start, end = ko - pd.Timedelta(hours=before_h), ko + pd.Timedelta(hours=after_h)
    now_ts = pd.Timestamp(now)
    act = (start <= now_ts) & (now_ts < end)
    return start[act].min().to_pydatetime() if act.any() else None


def newest_part_mtime(live_dir: Path) -> datetime | None:
    newest = None
    for v in BOOK_VENUES:
        for d in sorted((live_dir / f"venue={v}").glob("date=*"))[-2:]:
            for f in d.glob("*.parquet"):
                m = f.stat().st_mtime
                newest = m if newest is None or m > newest else newest
    return datetime.fromtimestamp(newest, UTC) if newest is not None else None


def conditions(
    now: datetime, win_start: datetime | None, newest: datetime | None, status: dict[str, Any], stale: timedelta
) -> dict[str, str]:
    """Active alert conditions -> message. Empty when no window is open."""
    if win_start is None:
        return {}
    out: dict[str, str] = {}
    last = max(t for t in (newest, win_start) if t is not None)
    if now - last >= stale:
        st = status.get("ts")
        alive = f"collector status last written {st}" if st else "no collector status file"
        out["stale_rows"] = (f"No new book rows for {(now - last).total_seconds() / 60:.0f} min during an open window "
                             f"(newest part {newest:%H:%M:%S}Z; {alive})." if newest else
                             f"No book rows written since the window opened at {win_start:%H:%M}Z ({alive}).")
    st_ts = pd.Timestamp(status["ts"]) if status.get("ts") else None
    if st_ts is not None and now - st_ts.to_pydatetime() < stale:
        for venue, v in (status.get("venues") or {}).items():
            since = v.get("failing_since")
            if since and now - pd.Timestamp(since).to_pydatetime() >= stale:
                out[f"venue:{venue}"] = (f"{venue} has failed every call since {pd.Timestamp(since):%H:%M:%S}Z "
                                         f"({v.get('consecutive_failures', '?')} in a row); last error: "
                                         f"{v.get('last_error', '?')}")
    return out


def plan_messages(
    now: datetime, active: dict[str, str], state: dict[str, Any], repeat: timedelta
) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    """(key, kind, message) to send, where kind is 'alert' or 'recovered', plus the new state."""
    sent: list[tuple[str, str, str]] = []
    new_state: dict[str, Any] = {}
    for key, msg in active.items():
        prev = state.get(key)
        if prev is None or now - pd.Timestamp(prev["last_sent"]).to_pydatetime() >= repeat:
            sent.append((key, "alert", msg))
            new_state[key] = {"since": prev["since"] if prev else now.isoformat(), "last_sent": now.isoformat()}
        else:
            new_state[key] = prev
    for key, prev in state.items():
        if key not in active:
            sent.append((key, "recovered", f"{key} recovered (alerting since {prev['since']})."))
    return sent, new_state


def run_watchdog(cfg: Config, now: datetime | None = None, stale_minutes: float | None = None) -> list[tuple[str, str, str]]:
    lc = cfg.collector_lite
    live = cfg.resolve(lc.live_dir)
    now = now or datetime.now(UTC)
    stale = timedelta(minutes=stale_minutes if stale_minutes is not None else lc.alert_stale_minutes)
    ws = window_start(latest_schedule(live), now, lc.window_before_kickoff_hours, lc.window_after_kickoff_hours)
    active = conditions(now, ws, newest_part_mtime(live), read_json(live / "status.json"), stale)
    state_path = live / "watchdog_state.json"
    msgs, new_state = plan_messages(now, active, read_json(state_path), timedelta(minutes=lc.alert_repeat_minutes))
    topic = Secrets().ntfy_topic
    for key, kind, msg in msgs:
        if kind == "alert":
            notify(topic, f"pm-edge ALERT: {key}", msg, priority="high", tags="warning")
        else:
            notify(topic, f"pm-edge recovered: {key}", msg, tags="white_check_mark")
        log.info("watchdog %s %s: %s", kind, key, msg)
    write_json_atomic(state_path, new_state)
    return msgs
