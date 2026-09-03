from app.capital_engine import budget_state
from app.db import Base, SessionLocal, engine
from app.models import BotSettings, Trade, User
from app.security import hash_password


def _u(db, email):
    u=db.query(User).filter_by(email=email).first()
    if u: return u
    u=User(name='V5',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u); db.flush(); db.add(BotSettings(user_id=u.id,capital=500,cash_reserve_pct=0,allocation_mode='fixed',stocks_allocation_pct=60,options_allocation_pct=40)); db.commit(); db.refresh(u); return u


def test_fixed_capital_split_keeps_engine_buckets_separate():
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_u(db,'v5split@test.local')
        state=budget_state(db,u,broker_cash=1000)
        assert state['allocation']['stocks_cap']==300
        assert state['allocation']['options_cap']==200
        assert state['available']==500
    finally: db.close()


def test_dynamic_mode_shares_one_global_budget_without_double_counting():
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_u(db,'v5dynamic@test.local'); u.settings.allocation_mode='dynamic'; db.commit()
        db.add(Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=1,entry=200,status='OPEN')); db.commit()
        state=budget_state(db,u,broker_cash=800)
        assert state['available']==300
        assert state['allocation']['options_available']==300
    finally: db.close()
