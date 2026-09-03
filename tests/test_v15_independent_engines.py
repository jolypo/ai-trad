from datetime import datetime
from zoneinfo import ZoneInfo

from app.db import SessionLocal
from app.models import BotSettings, Trade, User
from app.security import hash_password
from app.capital_engine import budget_state
from app import trading, options_engine


def make_user(db, email='v15@test.local'):
    u=User(name='V15',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u);db.flush();db.add(BotSettings(user_id=u.id,capital=500,current_bot_capital=500,cash_reserve_pct=0,allocation_mode='manual',stocks_allocation_pct=10,options_allocation_pct=90,max_position_allocation_pct=100,risk_per_trade_pct=100,max_trades=20));db.commit();db.refresh(u);return u


def test_500_capital_is_split_50_stocks_450_options_not_950():
    db=SessionLocal()
    try:
        u=make_user(db)
        b=budget_state(db,u,broker_cash=500)
        assert b['current']==500
        assert b['allocation']['stocks_cap']==50
        assert b['allocation']['options_cap']==450
        assert b['allocation']['stocks_available']==50
        assert b['allocation']['options_available']==450
        assert b['available']==500
    finally: db.close()


def test_stopping_stock_session_does_not_close_option_trade(monkeypatch):
    db=SessionLocal()
    try:
        u=make_user(db,'stop-stock@test.local');u.settings.active=True
        st=Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=1,entry=100,status='OPEN')
        op=Trade(user_id=u.id,symbol='AAPL270115C00300000',side='BUY',engine='options',qty=1,entry=2,status='OPEN')
        db.add_all([st,op]);db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:False)
        trading.stop_session(db,u,'test')
        db.refresh(st);db.refresh(op)
        assert st.status=='CLOSED'
        assert op.status=='OPEN'
        assert u.settings.active is False
    finally: db.close()


def test_options_scheduler_is_independent_from_stock_scheduler(monkeypatch):
    s=BotSettings(user_id=123,active=False,options_bot_active=False,locked=False,start_mode='manual',options_start_mode='scheduled',options_scheduled_days='0,1,2,3,4',options_start_delay_minutes=10,options_auto_stop_before_close_minutes=5)
    fake=datetime(2026,8,31,9,45,tzinfo=ZoneInfo('America/New_York')) # Monday
    monkeypatch.setattr(options_engine,'market_now',lambda:fake)
    monkeypatch.setattr(options_engine,'market_is_open',lambda:True)
    monkeypatch.setattr(trading,'market_now',lambda:fake)
    monkeypatch.setattr(trading,'market_is_open',lambda:True)
    assert options_engine.options_schedule_should_start(s) is True
    assert trading.schedule_should_start(s) is False


def test_options_scheduled_stop_does_not_depend_on_stock_mode(monkeypatch):
    s=BotSettings(user_id=123,active=False,options_bot_active=True,locked=False,start_mode='manual',options_start_mode='scheduled',options_scheduled_days='0,1,2,3,4',options_auto_stop_before_close_minutes=5)
    fake=datetime(2026,8,31,15,56,tzinfo=ZoneInfo('America/New_York'))
    monkeypatch.setattr(options_engine,'market_now',lambda:fake)
    assert options_engine.options_schedule_should_stop(s) is True
    assert trading.schedule_should_stop(s) is False


def test_pages_show_engine_specific_budgets_and_options_schedule():
    dash=open('app/templates/dashboard.html',encoding='utf-8').read()
    opt=open('app/templates/options.html',encoding='utf-8').read()
    assert 'ميزانية الأسهم' in dash and 'allocation.stocks_cap' in dash
    assert 'متاح لصفقات أسهم جديدة' in dash and 'allocation.stocks_available' in dash
    assert 'ميزانية العقود' in opt and 'allocation.options_cap' in opt
    assert 'options_start_mode' in opt and 'options_scheduled_days' in opt
    assert 'options_start_delay_minutes' in opt and 'options_auto_stop_before_close_minutes' in opt
