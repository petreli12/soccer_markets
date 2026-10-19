from datetime import UTC, datetime, timedelta

import pandas as pd

from pm_edge.alerts import conditions, plan_messages, window_start

NOW = datetime(2026, 10, 19, 18, 30, tzinfo=UTC)
STALE = timedelta(minutes=10)


def test_window_start_only_when_a_match_is_active():
    s = pd.DataFrame({"match_id": ["a", "b"], "kickoff_utc": [pd.Timestamp("2026-10-19 19:00", tz="UTC"),
                                                             pd.Timestamp("2026-10-19 20:00", tz="UTC")]})
    assert window_start(s, NOW, 3, 0) == datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    assert window_start(s, datetime(2026, 10, 19, 15, 59, tzinfo=UTC), 3, 0) is None
    assert window_start(s, datetime(2026, 10, 19, 20, 0, tzinfo=UTC), 3, 0) is None


def test_stale_rows_counts_from_newest_part_or_window_start():
    assert conditions(NOW, None, None, {}, STALE) == {}
    assert conditions(NOW, NOW - timedelta(hours=1), NOW - timedelta(minutes=9), {}, STALE) == {}
    assert "stale_rows" in conditions(NOW, NOW - timedelta(hours=1), NOW - timedelta(minutes=10), {}, STALE)
    # window opened 5 min ago; parts from the previous window are old but that is not stale yet
    assert conditions(NOW, NOW - timedelta(minutes=5), NOW - timedelta(hours=5), {}, STALE) == {}
    assert "stale_rows" in conditions(NOW, NOW - timedelta(minutes=11), None, {}, STALE)


def test_venue_failure_needs_fresh_status_and_ten_minutes():
    ws, newest = NOW - timedelta(hours=1), NOW - timedelta(minutes=1)
    status = {"ts": NOW - timedelta(seconds=20), "venues": {
        "kalshi": {"failing_since": NOW - timedelta(minutes=10), "consecutive_failures": 30, "last_error": "x"},
        "polymarket": {"failing_since": NOW - timedelta(minutes=9)},
        "pinnacle": {"failing_since": None},
    }}
    assert set(conditions(NOW, ws, newest, status, STALE)) == {"venue:kalshi"}
    status["ts"] = NOW - timedelta(minutes=15)
    assert conditions(NOW, ws, newest, status, STALE) == {}


def test_plan_messages_alert_repeat_and_recover():
    repeat = timedelta(minutes=30)
    sent, st = plan_messages(NOW, {"stale_rows": "m"}, {}, repeat)
    assert sent == [("stale_rows", "alert", "m")]
    sent, st2 = plan_messages(NOW + timedelta(minutes=5), {"stale_rows": "m"}, st, repeat)
    assert sent == [] and st2 == st
    sent, st3 = plan_messages(NOW + timedelta(minutes=30), {"stale_rows": "m"}, st, repeat)
    assert [k for k, kind, _ in sent] == ["stale_rows"] and st3["stale_rows"]["since"] == st["stale_rows"]["since"]
    sent, st4 = plan_messages(NOW + timedelta(minutes=31), {}, st3, repeat)
    assert [(k, kind) for k, kind, _ in sent] == [("stale_rows", "recovered")] and st4 == {}
