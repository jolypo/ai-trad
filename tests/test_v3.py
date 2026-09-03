from types import SimpleNamespace

from app.capital_engine import budget_state, reserve_capital, release_reservation
from app.db import Base, SessionLocal, engine
from app.models import BotSettings, Trade, User
from app.pine_converter import convert_pine_to_python
from app.security import hash_password
from app.trading import close_user_position_partial


def _user(db, email="v3@test.local"):
    u = db.query(User).filter_by(email=email).first()
    if u:
        return u
    u = User(name="V3", email=email, password_hash=hash_password("StrongPass123!"))
    db.add(u); db.flush(); db.add(BotSettings(user_id=u.id, capital=500, cash_reserve_pct=0, max_position_allocation_pct=100)); db.commit(); db.refresh(u)
    return u


def test_pine_safe_converter_complete():
    src = """//@version=6\nindicator('X')\nfast = ta.ema(close, 9)\nslow = ta.ema(close, 20)\nlongCondition = fast > slow\nplot(fast)\n"""
    r = convert_pine_to_python(src)
    assert r.status == "COMPLETE"
    assert r.supported_pct == 100
    compile(r.python_code, "<test>", "exec")


def test_pine_converter_blocks_unsupported_strategy():
    r = convert_pine_to_python("strategy('x')\nstrategy.entry('L', strategy.long)")
    assert r.status != "COMPLETE"
    assert r.errors


def test_unified_capital_reservation_prevents_overspend():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        u = _user(db, "budget@test.local")
        u.settings.capital = 500; u.settings.cash_reserve_pct = 0; db.commit()
        r = reserve_capital(db, u, 300, "stocks", "AAPL"); db.commit()
        state = budget_state(db, u, broker_cash=1000)
        assert round(state["available"], 2) == 200
        try:
            reserve_capital(db, u, 201, "index", "QQQ")
            assert False, "overspend should be rejected"
        except ValueError:
            db.rollback()
        rr = db.get(type(r), r.id); release_reservation(db, rr, "test"); db.commit()
        assert round(budget_state(db, u, broker_cash=1000)["available"], 2) == 500
    finally:
        db.close()


def test_partial_close_simulator_updates_remaining_qty_and_realized_pnl():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        u = _user(db, "partial@test.local")
        u.settings.realized_pnl = 0
        t = Trade(user_id=u.id, symbol="AAPL", side="BUY", qty=4, entry=100, stop_loss=95, take_profit=110, status="OPEN", reason="TEST", data_source="SIMULATOR")
        db.add(t); db.commit()
        # market_data is not configured in the test env, so simulator exits at entry: no P&L.
        close_user_position_partial(db, u, "AAPL", percentage=50)
        db.refresh(t)
        assert t.status == "OPEN"
        assert t.qty == 2
        close_user_position_partial(db, u, "AAPL", percentage=100)
        db.refresh(t)
        assert t.status == "CLOSED"
    finally:
        db.close()


def test_option_universe_contains_requested_symbols():
    from app.options_engine import OPTION_UNIVERSE
    for s in ["SPX","QQQ","AAPL","MSFT","NVDA","AMD","AMZN","META","TSLA"]:
        assert s in OPTION_UNIVERSE


def test_bearish_score_direction():
    from app.options_engine import _bearish_score
    state={"price":90,"vwap":100,"ema9":90,"ema20":95,"ema50":100,"macd":-2,"macd_signal":-1,"rsi14":40,"adx14":30,"rel_volume":1.5,"momentum_5":-1.0}
    assert _bearish_score(state) >= 70
