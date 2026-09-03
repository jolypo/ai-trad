from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import BotSettings, Trade, User
from app.options_engine import _advance_option_trailing


def make_user(db):
    u = User(name="Trail", email="trail-v16@test.local", password_hash="x")
    db.add(u); db.flush()
    db.add(BotSettings(user_id=u.id, capital=1000, current_bot_capital=1000))
    db.commit(); db.refresh(u)
    return u


def test_option_trailing_activates_at_profit_trigger_and_does_not_sell_at_trigger():
    db = SessionLocal()
    try:
        u = make_user(db)
        t = Trade(
            user_id=u.id, symbol="AAPL270115C00320000", side="BUY", engine="options",
            qty=1, entry=2.00, stop_loss=1.40, take_profit=None, status="OPEN",
            trailing_enabled=True, trailing_active=False, trailing_activation_price=3.00,
            trailing_high_watermark=2.00, trailing_distance_pct=20,
        )
        db.add(t); db.commit()
        reason, changed = _advance_option_trailing(db, u, t, 3.00)
        assert reason is None
        assert changed is True
        assert t.trailing_active is True
        assert t.trailing_stop_price == 2.40
        assert t.stop_loss == 2.40
        assert t.trailing_activated_at is not None
    finally:
        db.close()


def test_option_trailing_moves_only_up_and_triggers_on_pullback():
    db = SessionLocal()
    try:
        u = make_user(db)
        t = Trade(
            user_id=u.id, symbol="AAPL270115C00320000", side="BUY", engine="options",
            qty=1, entry=2.00, stop_loss=2.40, take_profit=None, status="OPEN",
            trailing_enabled=True, trailing_active=True, trailing_activation_price=3.00,
            trailing_high_watermark=3.00, trailing_stop_price=2.40, trailing_distance_pct=20,
            trailing_activated_at=datetime.now(timezone.utc),
        )
        db.add(t); db.commit()
        reason, _ = _advance_option_trailing(db, u, t, 4.00)
        assert reason is None
        assert t.trailing_high_watermark == 4.00
        assert t.trailing_stop_price == 3.20
        # A pullback above the stop must not lower it.
        reason, _ = _advance_option_trailing(db, u, t, 3.50)
        assert reason is None
        assert t.trailing_high_watermark == 4.00
        assert t.trailing_stop_price == 3.20
        # Crossing the persistent trailing stop triggers an exit.
        reason, _ = _advance_option_trailing(db, u, t, 3.19)
        assert reason == "ALPACA_OPTION_TRAILING_STOP"
        assert t.trailing_stop_price == 3.20
    finally:
        db.close()


def test_option_trailing_activation_never_moves_stop_below_breakeven():
    db = SessionLocal()
    try:
        u = make_user(db)
        t = Trade(
            user_id=u.id, symbol="QQQ270115C00500000", side="BUY", engine="options",
            qty=1, entry=2.00, stop_loss=1.40, status="OPEN",
            trailing_enabled=True, trailing_active=False, trailing_activation_price=2.20,
            trailing_high_watermark=2.00, trailing_distance_pct=30,
        )
        db.add(t); db.commit()
        reason, _ = _advance_option_trailing(db, u, t, 2.20)
        assert reason is None
        assert t.trailing_active is True
        assert t.trailing_stop_price == 2.00
    finally:
        db.close()


def test_options_ui_exposes_trailing_controls_only_on_options_page():
    options = open("app/templates/options.html", encoding="utf-8").read()
    dashboard = open("app/templates/dashboard.html", encoding="utf-8").read()
    assert 'name="options_exit_mode"' in options
    assert 'name="trailing_activation_pct"' in options
    assert 'name="trailing_distance_pct"' in options
    assert 'name="trailing_activation_pct"' not in dashboard


def test_manage_option_positions_closes_once_when_trailing_stop_breaks(monkeypatch):
    from app import options_engine
    db = SessionLocal(); calls=[]
    try:
        u = make_user(db)
        t = Trade(
            user_id=u.id, symbol="NVDA270115C00200000", side="BUY", engine="options",
            qty=1, entry=2.00, stop_loss=3.20, status="OPEN", indicators="{}",
            trailing_enabled=True, trailing_active=True, trailing_activation_price=3.00,
            trailing_high_watermark=4.00, trailing_stop_price=3.20, trailing_distance_pct=20,
        )
        db.add(t); db.commit()
        monkeypatch.setattr(options_engine.alpaca_broker, "positions", lambda:[{"symbol":t.symbol,"qty":"1","current_price":"3.10"}])
        def close(symbol, qty=None, percentage=None):
            calls.append((symbol, qty)); return {"id":"trail-exit-1","status":"filled","qty":"1","filled_qty":"1","filled_avg_price":"3.08"}
        monkeypatch.setattr(options_engine.alpaca_broker, "close_position", close)
        monkeypatch.setattr(options_engine.alpaca_broker, "wait_for_terminal_order", lambda oid, timeout_seconds=8:{"id":oid,"status":"filled","filled_qty":"1","filled_avg_price":"3.08"})
        options_engine.manage_option_positions(db, u); db.refresh(t)
        assert len(calls) == 1
        assert t.status == "CLOSED"
        assert round(t.pnl, 2) == 108.00
        assert "TRAILING_STOP" in t.reason
    finally:
        db.close()


def test_fixed_option_take_profit_remains_supported():
    db = SessionLocal()
    try:
        u = make_user(db)
        t = Trade(user_id=u.id,symbol="MSFT270115C00400000",side="BUY",engine="options",qty=1,entry=2,stop_loss=1.4,take_profit=3,status="OPEN",trailing_enabled=False)
        db.add(t); db.commit()
        reason, changed = _advance_option_trailing(db, u, t, 3.20)
        assert reason is None and changed is False
        assert t.take_profit == 3
    finally:
        db.close()
