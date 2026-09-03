from datetime import datetime
from types import SimpleNamespace

from app import trading, options_engine


def _settings(**kw):
    base = dict(
        start_mode="scheduled",
        scheduled_days="0,1,2,3,4",
        start_delay_minutes=0,
        auto_stop_before_close_minutes=5,
        active=False,
        locked=False,
        stocks_risk_locked=False,
        session_started_at=datetime(2026, 8, 31, 14, 0),
        options_start_mode="scheduled",
        options_scheduled_days="0,1,2,3,4",
        options_start_delay_minutes=0,
        options_auto_stop_before_close_minutes=5,
        options_bot_active=False,
        options_risk_locked=False,
        options_session_started_at=datetime(2026, 8, 31, 14, 0),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _mock_market(monkeypatch, module, hour, minute, weekday=0, is_open=True):
    class FakeNow:
        def __init__(self):
            self.hour = hour
            self.minute = minute
        def weekday(self):
            return weekday
    monkeypatch.setattr(module, "market_now", lambda: FakeNow())
    monkeypatch.setattr(module, "market_is_open", lambda: is_open)


def test_stock_scheduled_restart_resumes_inside_window_even_with_old_session_timestamp(monkeypatch):
    s = _settings()
    _mock_market(monkeypatch, trading, 13, 15)
    assert trading.schedule_should_start(s) is False  # normal loop avoids duplicate starts
    assert trading.stock_schedule_should_resume_after_restart(s) is True


def test_stock_restart_does_not_resume_manual_or_after_cutoff(monkeypatch):
    s = _settings(start_mode="manual")
    _mock_market(monkeypatch, trading, 13, 15)
    assert trading.stock_schedule_should_resume_after_restart(s) is False

    s = _settings(start_mode="scheduled", auto_stop_before_close_minutes=5)
    _mock_market(monkeypatch, trading, 15, 56)
    assert trading.stock_schedule_should_resume_after_restart(s) is False
    assert trading.schedule_should_start(s) is False


def test_options_scheduled_restart_resumes_independently(monkeypatch):
    s = _settings(start_mode="manual", options_start_mode="scheduled")
    _mock_market(monkeypatch, options_engine, 12, 0)
    assert options_engine.options_schedule_should_start(s) is False
    assert options_engine.options_schedule_should_resume_after_restart(s) is True


def test_options_restart_respects_delay_and_close_cutoff(monkeypatch):
    s = _settings(options_start_delay_minutes=30, options_auto_stop_before_close_minutes=10)
    _mock_market(monkeypatch, options_engine, 9, 45)
    assert options_engine.options_schedule_should_resume_after_restart(s) is False

    _mock_market(monkeypatch, options_engine, 10, 5)
    assert options_engine.options_schedule_should_resume_after_restart(s) is True

    _mock_market(monkeypatch, options_engine, 15, 51)
    assert options_engine.options_schedule_should_resume_after_restart(s) is False


def test_closed_market_never_auto_resumes(monkeypatch):
    s = _settings()
    _mock_market(monkeypatch, trading, 12, 0, is_open=False)
    _mock_market(monkeypatch, options_engine, 12, 0, is_open=False)
    assert trading.stock_schedule_should_resume_after_restart(s) is False
    assert options_engine.options_schedule_should_resume_after_restart(s) is False
