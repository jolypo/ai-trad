from types import SimpleNamespace
import json
import pytest

from app import trading
from app.db import SessionLocal
from app.models import User, BotSettings, Trade
from app.security import hash_password


def make_user(db,email='v20@test.local',capital=1000):
    u=User(name='V20',email=email,password_hash=hash_password('password123'))
    db.add(u); db.flush(); db.add(BotSettings(user_id=u.id, capital=capital, current_bot_capital=capital)); db.commit(); db.refresh(u); return u


def prepare(db,u):
    s=u.settings
    s.active=True; s.allocation_mode='stocks_only'; s.cash_reserve_pct=0
    s.max_position_allocation_pct=100; s.risk_per_trade_pct=100
    s.stocks_exit_mode='trailing'; s.stocks_trailing_distance_pct=2.0
    db.commit()


def test_stock_trailing_distance_drives_risk_stop():
    db=SessionLocal()
    try:
        u=make_user(db,'v20-risk@test.local'); prepare(db,u)
        sig=SimpleNamespace(price=100.0,stop=95.0,target=110.0)
        out=trading._apply_exit_plan(u,sig)
        assert out.stop==98.0
    finally: db.close()


def test_whole_share_entry_arms_native_broker_trailing(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'v20-entry@test.local'); prepare(db,u)
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:10000)
        monkeypatch.setattr(trading.alpaca_broker,'submit_market_buy',lambda **kw:{'id':'e1','status':'new','client_order_id':kw['client_order_id']})
        def terminal(oid,timeout_seconds=8):
            return {'id':oid,'status':'filled','filled_qty':'3','filled_avg_price':'100.00','client_order_id':'entry'}
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',terminal)
        seen={}
        def submit_trailing(**kw):
            seen.update(kw); return {'id':'ts1','status':'new','trail_percent':str(kw['trail_percent'])}
        monkeypatch.setattr(trading.alpaca_broker,'submit_trailing_stop_sell',submit_trailing)
        monkeypatch.setattr(trading.alpaca_broker,'confirm_open_order',lambda oid,timeout_seconds=4:{'id':oid,'status':'new','hwm':'100.20','stop_price':'98.20'})
        sig=SimpleNamespace(symbol='AAPL',price=100.0,stop=99.0,target=102.0,score=90.0,to_dict=lambda:{})
        t=trading._open_from_signal(db,u,sig)
        assert t is not None
        assert t.reason=='ALPACA_STOCK_TRAILING_PROTECTED'
        assert t.qty==3 and t.entry==100.0
        assert t.take_profit is None
        assert t.stop_loss==98.0
        assert seen['qty']==3 and seen['trail_percent']==2.0 and seen['time_in_force']=='gtc'
        payload=json.loads(t.indicators)
        assert payload['stock_trailing_order_id']=='ts1'
        assert payload['protection_mode']=='alpaca-native-trailing'
        assert payload['broker_protection_confirmed'] is True
    finally: db.close()


def test_broker_trailing_fill_closes_trade(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'v20-fill@test.local'); prepare(db,u)
        t=Trade(user_id=u.id,symbol='MSFT',side='BUY',engine='stocks',qty=2,entry=100,status='OPEN',pnl=0,
                stop_loss=98,take_profit=None,reason='ALPACA_STOCK_TRAILING_PROTECTED',data_source='ALPACA_PAPER_ORDER',
                indicators=json.dumps({'stock_trailing_order_id':'ts2','broker_protection_confirmed':True}))
        db.add(t); db.commit(); db.refresh(t)
        monkeypatch.setattr(trading.alpaca_broker,'get_order',lambda *a,**k:{'id':'ts2','status':'filled','filled_avg_price':'110.00','filled_qty':'2','hwm':'112','stop_price':'109.76'})
        assert trading._sync_stock_trailing(db,u,t) is True
        db.refresh(t)
        assert t.status=='CLOSED' and t.exit==110.0 and t.pnl==20.0
    finally: db.close()


def test_trailing_protection_failure_emergency_closes(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'v20-fail@test.local'); prepare(db,u)
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:10000)
        monkeypatch.setattr(trading.alpaca_broker,'submit_market_buy',lambda **kw:{'id':'e2','status':'new','client_order_id':kw['client_order_id']})
        def terminal(oid,timeout_seconds=8):
            if oid=='e2': return {'id':'e2','status':'filled','filled_qty':'2','filled_avg_price':'100.00'}
            return {'id':oid,'status':'filled','filled_qty':'2','filled_avg_price':'99.80'}
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',terminal)
        monkeypatch.setattr(trading.alpaca_broker,'submit_trailing_stop_sell',lambda **kw: (_ for _ in ()).throw(RuntimeError('reject')))
        closed={}
        monkeypatch.setattr(trading.alpaca_broker,'close_position',lambda symbol,qty=None: closed.update(symbol=symbol,qty=qty) or {'id':'cx'})
        sig=SimpleNamespace(symbol='NVDA',price=100.0,stop=99.0,target=102.0,score=90.0,to_dict=lambda:{})
        t=trading._open_from_signal(db,u,sig)
        assert t is None
        assert closed=={'symbol':'NVDA','qty':2.0}
    finally: db.close()

def test_canceled_native_trailing_blocks_new_entries(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'v20-cancel@test.local'); prepare(db,u)
        t=Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=2,entry=100,status='OPEN',pnl=0,
                stop_loss=98,take_profit=None,reason='ALPACA_STOCK_TRAILING_PROTECTED',data_source='ALPACA_PAPER_ORDER',
                indicators=json.dumps({'stock_trailing_order_id':'tsx','broker_protection_confirmed':True}))
        db.add(t); db.commit()
        monkeypatch.setattr(trading.alpaca_broker,'get_order',lambda *a,**k:{'id':'tsx','status':'canceled','hwm':'105','stop_price':'102.90'})
        assert trading._sync_stock_trailing(db,u,t) is False
        db.refresh(u)
        assert u.settings.broker_reconciliation_required is True
        payload=json.loads(t.indicators)
        assert payload['broker_protection_confirmed'] is False
    finally: db.close()


def test_dashboard_contains_independent_stock_trailing_controls():
    from pathlib import Path
    html=Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    assert 'name="stocks_exit_mode"' in html
    assert 'name="stocks_trailing_distance_pct"' in html
    assert 'Alpaca Broker Trailing Stop' in html
