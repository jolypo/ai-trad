import json

from app.capital_engine import budget_state
from app.db import Base, SessionLocal, engine
from app.models import BotSettings, Trade, User
from app.security import hash_password
import app.trading as trading


def _user(db, email):
    u=db.query(User).filter_by(email=email).first()
    if u: return u
    u=User(name='V63',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u); db.flush(); db.add(BotSettings(user_id=u.id,capital=500,cash_reserve_pct=0,max_position_allocation_pct=100)); db.commit(); db.refresh(u); return u


def test_capital_ready_plans_are_deterministic():
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'v63-plan@test.local')
        u.settings.allocation_mode='balanced'; db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['allocation']['stocks_cap']==250
        assert b['allocation']['options_cap']==250
        u.settings.allocation_mode='stocks_focus'; db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['allocation']['stocks_cap']==350
        assert b['allocation']['options_cap']==150
        u.settings.allocation_mode='opportunity'; db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['allocation']['stocks_available']==500
        assert b['allocation']['options_available']==500
    finally: db.close()


def test_partial_close_accepted_pending_is_not_reported_as_failure(monkeypatch):
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'v63-pending@test.local')
        t=Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=4,entry=100,stop_loss=95,take_profit=110,status='OPEN',reason='TEST',data_source='ALPACA_PAPER')
        db.add(t); db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_phase',lambda:'regular')
        monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':'AAPL','qty':'4'}])
        monkeypatch.setattr(trading.alpaca_broker,'cancel_open_orders_for_symbol',lambda symbol:[])
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_no_open_orders',lambda symbol,timeout_seconds=4:True)
        monkeypatch.setattr(trading.alpaca_broker,'close_position',lambda symbol,qty=None,percentage=None:{'id':'oid-1','status':'accepted'})
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=12:{'id':oid,'status':'accepted','filled_qty':'0'})
        result=trading.close_user_position_partial(db,u,'AAPL',percentage=50)
        assert getattr(result,'_close_submission_status')=='pending'
        db.refresh(t)
        payload=json.loads(t.indicators or '{}')
        assert payload['pending_manual_close']['order_id']=='oid-1'
        assert t.status=='OPEN' and t.qty==4
    finally: db.close()


def test_after_hours_manual_sell_uses_extended_limit_when_quote_exists(monkeypatch):
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'v63-extended@test.local')
        t=Trade(user_id=u.id,symbol='MSFT',side='BUY',engine='stocks',qty=2,entry=500,stop_loss=490,take_profit=520,status='OPEN',reason='TEST',data_source='ALPACA_PAPER')
        db.add(t); db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_phase',lambda:'afterhours')
        monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':'MSFT','qty':'2'}])
        monkeypatch.setattr(trading.alpaca_broker,'cancel_open_orders_for_symbol',lambda symbol:[])
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_no_open_orders',lambda symbol,timeout_seconds=4:True)
        monkeypatch.setattr(trading.market_data,'latest_quotes',lambda symbols,feed=None:{'MSFT':{'bp':505,'ap':506}})
        captured={}
        def submit(**kwargs):
            captured.update(kwargs); return {'id':'oid-2','status':'accepted'}
        monkeypatch.setattr(trading.alpaca_broker,'submit_extended_hours_sell',submit)
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=12:{'id':oid,'status':'accepted','filled_qty':'0'})
        result=trading.close_user_position_partial(db,u,'MSFT',percentage=100)
        assert getattr(result,'_close_submission_status')=='pending'
        assert captured['symbol']=='MSFT' and captured['qty']==2
        assert captured['limit_price']>0
    finally: db.close()
