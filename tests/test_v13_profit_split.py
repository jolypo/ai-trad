from app.capital_engine import apply_realized_pnl, budget_state
from app.db import SessionLocal
from app.models import BotSettings, User
from app.security import hash_password
from app.trading import reset_for_new_day


def make_user(db, email="v13-profit@test.local"):
    u = User(name="V13", email=email, password_hash=hash_password("StrongPass123!"))
    db.add(u)
    db.flush()
    db.add(BotSettings(user_id=u.id, capital=100, current_bot_capital=100, cash_reserve_pct=0))
    db.commit()
    db.refresh(u)
    return u


def test_realized_pnl_is_persistently_split_by_engine():
    db = SessionLocal()
    try:
        u = make_user(db)
        apply_realized_pnl(u.settings, 12.50, "stocks")
        apply_realized_pnl(u.settings, -3.25, "options")
        db.commit()
        db.expire_all()
        s = db.query(BotSettings).filter_by(user_id=u.id).one()
        assert s.stocks_realized_pnl == 12.50
        assert s.options_realized_pnl == -3.25
        state = budget_state(db, u, broker_cash=1000)
        assert state["stocks_realized_pnl"] == 12.50
        assert state["options_realized_pnl"] == -3.25
        assert state["stocks_options_realized_pnl"] == 9.25
    finally:
        db.close()


def test_daily_reset_clears_both_engine_pnl_buckets():
    db = SessionLocal()
    try:
        u = make_user(db, "v13-reset@test.local")
        u.settings.session_day = "1900-01-01"
        u.settings.realized_pnl = 8
        u.settings.stocks_realized_pnl = 10
        u.settings.options_realized_pnl = -2
        reset_for_new_day(u.settings)
        assert u.settings.realized_pnl == 0
        assert u.settings.stocks_realized_pnl == 0
        assert u.settings.options_realized_pnl == 0
    finally:
        db.close()


def test_templates_use_separate_stock_option_profit_cards():
    stock = open("app/templates/dashboard.html", encoding="utf-8").read()
    options = open("app/templates/options.html", encoding="utf-8").read()
    capital = open("app/templates/capital.html", encoding="utf-8").read()
    assert "stocks_realized_pnl" in stock
    assert "options_realized_pnl" in options
    assert "أرباح الأسهم / العقود" in capital
    assert "stocks_options_realized_pnl" in capital
