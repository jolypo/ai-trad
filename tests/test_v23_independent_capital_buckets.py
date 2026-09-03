from app.capital_engine import apply_realized_pnl, budget_state, set_capital_plan
from app.db import SessionLocal
from app.models import BotSettings, User
from app.security import hash_password
from app.trading import reset_for_new_day


def make_user(db, email="v23@test.local"):
    u = User(name="V23", email=email, password_hash=hash_password("StrongPass123!"))
    db.add(u)
    db.flush()
    db.add(BotSettings(
        user_id=u.id,
        capital=2000,
        current_bot_capital=2000,
        cash_reserve_pct=0,
        allocation_mode="balanced",
        stocks_allocation_pct=50,
        options_allocation_pct=50,
        stocks_target_capital=1000,
        options_target_capital=1000,
        stocks_current_capital=1000,
        options_current_capital=1000,
    ))
    db.commit()
    db.refresh(u)
    return u


def test_stock_loss_does_not_reduce_options_bucket():
    db = SessionLocal()
    try:
        u = make_user(db)
        apply_realized_pnl(u.settings, -2.50, "stocks")
        db.commit()
        state = budget_state(db, u, broker_cash=10000)
        assert state["current"] == 1997.50
        assert state["allocation"]["stocks_current"] == 997.50
        assert state["allocation"]["options_current"] == 1000.00
        assert state["allocation"]["stocks_cap"] == 997.50
        assert state["allocation"]["options_cap"] == 1000.00
    finally:
        db.close()


def test_new_day_does_not_rebalance_engine_current_buckets():
    db = SessionLocal()
    try:
        u = make_user(db, "v23-reset@test.local")
        apply_realized_pnl(u.settings, -2.50, "stocks")
        u.settings.session_day = "1900-01-01"
        reset_for_new_day(u.settings)
        db.commit()
        state = budget_state(db, u, broker_cash=10000)
        assert state["allocation"]["stocks_current"] == 997.50
        assert state["allocation"]["options_current"] == 1000.00
        assert state["stocks_realized_pnl"] == 0
        assert state["options_realized_pnl"] == 0
    finally:
        db.close()


def test_options_loss_is_isolated_from_stocks():
    db = SessionLocal()
    try:
        u = make_user(db, "v23-options@test.local")
        apply_realized_pnl(u.settings, -100, "options")
        state = budget_state(db, u, broker_cash=10000)
        assert state["allocation"]["stocks_current"] == 1000
        assert state["allocation"]["options_current"] == 900
        assert state["current"] == 1900
    finally:
        db.close()


def test_profit_restores_only_same_engine_then_becomes_engine_excess():
    db = SessionLocal()
    try:
        u = make_user(db, "v23-recovery@test.local")
        apply_realized_pnl(u.settings, -100, "stocks")
        apply_realized_pnl(u.settings, 50, "options")
        state = budget_state(db, u, broker_cash=10000)
        # options was already at target, so its profit must not heal stock drawdown
        assert state["allocation"]["stocks_current"] == 900
        assert state["allocation"]["options_current"] == 1000
        assert state["allocation"]["options_excess"] == 50
        assert state["current"] == 1900
        apply_realized_pnl(u.settings, 40, "stocks")
        state = budget_state(db, u, broker_cash=10000)
        assert state["allocation"]["stocks_current"] == 940
        assert state["allocation"]["options_current"] == 1000
    finally:
        db.close()


def test_explicit_reallocation_preserves_engine_drawdown():
    db = SessionLocal()
    try:
        u = make_user(db, "v23-realloc@test.local")
        apply_realized_pnl(u.settings, -25, "stocks")
        set_capital_plan(u.settings, 2000, "stocks_focus", 70, 30)
        state = budget_state(db, u, broker_cash=10000)
        assert state["allocation"]["stocks_target"] == 1400
        assert state["allocation"]["stocks_current"] == 1375
        assert state["allocation"]["options_target"] == 600
        assert state["allocation"]["options_current"] == 600
        assert state["current"] == 1975
    finally:
        db.close()
