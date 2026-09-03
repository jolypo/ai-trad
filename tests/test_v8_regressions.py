import json
from types import SimpleNamespace

from app.db import SessionLocal
from app.models import BotSettings, Trade, User
from app.security import hash_password
from app.capital_engine import apply_realized_pnl, budget_state, set_target_capital
from app import trading


def make_user(db, email='v8@test.local', capital=100):
    u=User(name='V8',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u);db.flush();db.add(BotSettings(user_id=u.id,capital=capital,current_bot_capital=capital,cash_reserve_pct=0,max_position_allocation_pct=100,risk_per_trade_pct=100,max_trades=10));db.commit();db.refresh(u);return u


def test_persistent_current_capital_loss_recovery_and_excess():
    db=SessionLocal()
    try:
        u=make_user(db)
        apply_realized_pnl(u.settings,-10); db.commit()
        assert u.settings.current_bot_capital==90
        apply_realized_pnl(u.settings,5); db.commit()
        assert u.settings.current_bot_capital==95 and u.settings.excess_realized_profit==0
        apply_realized_pnl(u.settings,10); db.commit()
        assert u.settings.current_bot_capital==100 and u.settings.excess_realized_profit==5
        db.expire_all(); s=db.query(BotSettings).filter_by(user_id=u.id).one()
        assert s.current_bot_capital==100 and s.excess_realized_profit==5
    finally: db.close()


def test_reserve_uses_current_not_target():
    db=SessionLocal()
    try:
        u=make_user(db);u.settings.current_bot_capital=90;u.settings.cash_reserve_pct=35;db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['target']==100 and b['current']==90
        assert b['reserve']==31.5 and b['usable']==58.5 and b['available']==58.5
    finally: db.close()


def test_resaving_same_target_does_not_refill_drawdown():
    s=SimpleNamespace(capital=100,current_bot_capital=90,excess_realized_profit=0)
    set_target_capital(s,100)
    assert s.current_bot_capital==90
    set_target_capital(s,120)
    assert s.current_bot_capital==110
    set_target_capital(s,80)
    assert s.current_bot_capital==80


def test_manual_100_0_and_0_100_are_hard_execution_caps():
    db=SessionLocal()
    try:
        u=make_user(db);u.settings.allocation_mode='manual';u.settings.stocks_allocation_pct=100;u.settings.options_allocation_pct=0;db.commit()
        b=budget_state(db,u,broker_cash=1000);assert b['allocation']['stocks_available']==100 and b['allocation']['options_available']==0
        u.settings.stocks_allocation_pct=0;u.settings.options_allocation_pct=100;db.commit()
        b=budget_state(db,u,broker_cash=1000);assert b['allocation']['stocks_available']==0 and b['allocation']['options_available']==100
    finally: db.close()


def test_partial_option_close_never_submits_equity_oco(monkeypatch):
    db=SessionLocal();called=[]
    try:
        u=make_user(db);t=Trade(user_id=u.id,symbol='QQQ260101C00100000',side='BUY',engine='options',qty=2,entry=2,status='OPEN',stop_loss=1,take_profit=3,indicators='{}');db.add(t);db.commit()
        monkeypatch.setattr(trading.alpaca_broker,'submit_oco_exit',lambda **kw:called.append(kw))
        trading._apply_manual_close_fill(db,u,t,1,2.5,'x')
        assert called==[]
    finally: db.close()


def test_whole_share_entry_uses_actual_broker_fill_qty_and_price(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'whole-fill@test.local',1000);s=u.settings;s.active=True;s.allocation_mode='stocks_only';s.cash_reserve_pct=0;s.max_position_allocation_pct=100;s.risk_per_trade_pct=100;s.stocks_exit_mode='bracket';db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:10000)
        monkeypatch.setattr(trading.alpaca_broker,'submit_bracket_buy',lambda **kw:{'id':'b1','status':'new','client_order_id':kw['client_order_id']})
        monkeypatch.setattr(trading.alpaca_broker,'confirm_bracket_protection',lambda oid,timeout_seconds=4:{'id':oid,'status':'new','legs':[{'side':'sell','type':'limit'},{'side':'sell','type':'stop'}]})
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=8:{'id':oid,'status':'filled','filled_qty':'3','filled_avg_price':'99.50','legs':[{'side':'sell','type':'limit'},{'side':'sell','type':'stop'}]})
        sig=SimpleNamespace(symbol='AAPL',price=100.0,stop=99.0,target=102.0,score=90.0,to_dict=lambda:{})
        t=trading._open_from_signal(db,u,sig)
        assert t and t.qty==3 and t.entry==99.5
        payload=json.loads(t.indicators);assert payload['protection_mode']=='broker-side' and payload['broker_protection_confirmed'] is True
    finally: db.close()


def test_reconcile_blocks_unexplained_position_mismatch(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'reconcile@test.local',1000);t=Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=2,entry=100,status='OPEN',indicators='{}');db.add(t);db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading.settings,'alpaca_api_key_id','x')
        monkeypatch.setattr(trading.settings,'alpaca_api_secret_key','y')
        monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':'AAPL','qty':'1'}])
        monkeypatch.setattr(trading.alpaca_broker,'orders',lambda **kw:[])
        monkeypatch.setattr(trading,'_sync_all_broker_trades',lambda *a,**k:None)
        ok,issues=trading.reconcile_broker_state(db,u)
        assert not ok and issues and u.settings.broker_reconciliation_required is True
    finally: db.close()
