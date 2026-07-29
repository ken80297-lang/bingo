from desktop.core.backtest_engine import run_readonly_backtest
from desktop.core.replay_engine import ReplayEngine


def test_replay_uses_source_issue_and_next_target_issue_only():
    engine = ReplayEngine()
    records = engine.load(limit=20)

    for record in records:
        if not record.prediction:
            continue
        assert int(record.prediction.target_issue) == int(record.prediction.source_issue) + 1
        if record.source_draw:
            assert record.source_draw.issue == record.prediction.source_issue
        if record.target_draw:
            assert record.target_draw.issue == record.prediction.target_issue


def test_backtest_reports_no_lookahead_bias():
    summary = run_readonly_backtest(limit=20)

    assert summary["look_ahead_bias"] is False
    assert summary["total_predictions"] >= summary["valid_simulations"]

