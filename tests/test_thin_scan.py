from pm_edge.thin_scan import best_level, count_recent, event_type, first_token


def test_event_type_from_slug():
    assert event_type("ucl-ars-lil-2026-10-13", "ucl") == "(base event)"
    assert event_type("ucl-ars-lil-2026-10-13-total-corners", "ucl") == "total-corners"
    assert event_type("epl-ars-cov-2026-08-21-more-markets", "ucl") == "other"


def test_first_token_by_version():
    v1 = {"clobTokenIds": '["111", "222"]', "outcomes": '["Over", "Under"]'}
    v2 = {"version": "v2", "positionIds": '["333", "444"]', "clobTokenIds": '["x", "y"]', "outcomes": '["Yes", "No"]'}
    assert first_token(v1) == ("111", "Over")
    assert first_token(v2) == ("333", "Yes")
    assert first_token({"clobTokenIds": '["1"]'}) is None


def test_best_level_ignores_api_ordering():
    side = [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "3"}, {"price": "0.42", "size": "7"}]
    assert best_level(side, best_is_max=True) == (0.45, 3.0)
    assert best_level(side, best_is_max=False) == (0.40, 10.0)
    assert best_level([], True) == (None, None)


def test_count_recent_by_condition_id():
    trades = [
        {"conditionId": "a", "timestamp": 1000},
        {"conditionId": "a", "timestamp": 999},
        {"conditionId": "b", "timestamp": 1500},
        {"conditionId": "a", "timestamp": 500},
    ]
    assert count_recent(trades, since_ts=999) == {"a": 2, "b": 1}
