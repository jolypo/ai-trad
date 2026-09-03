from app.db import SessionLocal
from app.models import BotSettings, User
from app.security import hash_password
from app.capital_engine import budget_state
from app import trading, options_engine


def make_user(db,email='v18@test.local'):
    u=User(name='V18',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u);db.flush()
    db.add(BotSettings(user_id=u.id,capital=500,current_bot_capital=500,cash_reserve_pct=0,
        allocation_mode='manual',stocks_allocation_pct=10,options_allocation_pct=90,
        max_position_allocation_pct=100,risk_per_trade_pct=100,
        options_max_allocation_pct=100,options_risk_per_trade_pct=100,
        daily_loss_pct=10,options_daily_loss_pct=10,max_trades=20))
    db.commit();db.refresh(u);return u


def test_stock_sizing_cannot_use_options_bucket():
    db=SessionLocal()
    try:
        u=make_user(db);u.settings.allow_fractional=True;db.commit()
        qty=trading._size_position(db,u,entry=100,stop=90)
        assert qty == 0.5  # stock bucket is $50, despite $450 being assigned to options
    finally: db.close()


def test_option_capacity_uses_only_option_risk_and_option_bucket():
    db=SessionLocal()
    try:
        u=make_user(db,'cap@test.local');b=budget_state(db,u,broker_cash=500)
        u.settings.risk_per_trade_pct=0.1  # stock risk must not affect options
        u.settings.options_risk_per_trade_pct=50
        u.settings.options_max_allocation_pct=50
        c=options_engine._option_order_capacity(u.settings,b,per_contract=200,stop_pct=30)
        assert c['option_bucket']==450
        assert c['option_cap']==225
        assert c['risk_budget']==225
        assert c['by_cap']==1
        assert c['by_risk']>=1
    finally: db.close()


def test_stock_daily_loss_lock_does_not_stop_options_bot():
    db=SessionLocal()
    try:
        u=make_user(db,'stock-lock@test.local');s=u.settings;s.active=True;s.options_bot_active=True
        # Stock bucket=$50; 10% daily limit=$5.
        s.stocks_realized_pnl=-6;db.commit()
        assert trading._enforce_daily_limits(db,u) is False
        assert s.stocks_risk_locked is True
        assert s.active is False
        assert s.options_bot_active is True
        assert s.options_risk_locked is False
        assert s.locked is False
    finally: db.close()


def test_options_daily_loss_lock_does_not_stop_stock_bot():
    db=SessionLocal()
    try:
        u=make_user(db,'option-lock@test.local');s=u.settings;s.active=True;s.options_bot_active=True
        # Options bucket=$450; 10% daily limit=$45.
        s.options_realized_pnl=-46;db.commit()
        assert options_engine._enforce_options_daily_risk(db,u) is False
        assert s.options_risk_locked is True
        assert s.options_bot_active is False
        assert s.active is True
        assert s.stocks_risk_locked is False
        assert s.locked is False
    finally: db.close()


def test_pages_show_both_engine_statuses_and_separate_option_risk():
    dash=open('app/templates/dashboard.html',encoding='utf-8').read()
    opt=open('app/templates/options.html',encoding='utf-8').read()
    for text in ('بوت الأسهم:','بوت العقود:','Alpaca Paper'):
        assert text in dash
        assert text in opt
    assert 'options_risk_per_trade_pct' in opt
    assert 'options_daily_loss_pct' in opt
    assert 'options_max_open_positions' in opt
    assert 'options_trade_cooldown_seconds' in opt
    assert 'مستقلة تماماً عن مخاطرة الأسهم' in opt


def test_same_user_can_run_stock_and_options_bots_together(monkeypatch):
    from app.models import AllowedSymbol
    db=SessionLocal()
    try:
        u=make_user(db,'both@test.local')
        db.add(AllowedSymbol(user_id=u.id,symbol='AAPL'));db.commit()
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr(options_engine,'market_is_open',lambda:True)
        ok1,_=trading.start_session(db,u,hard_cap=100)
        ok2,_=options_engine.start_options_bot(db,u)
        assert ok1 is True and ok2 is True
        assert u.settings.active is True
        assert u.settings.options_bot_active is True
    finally: db.close()


def test_shared_capital_cannot_be_double_reserved_between_engines():
    from app.capital_engine import reserve_capital
    db=SessionLocal()
    try:
        u=make_user(db,'reserve@test.local')
        reserve_capital(db,u,50,'stocks','AAPL')
        db.commit()
        reserve_capital(db,u,450,'options','AAPL270115C00300000')
        db.commit()
        try:
            reserve_capital(db,u,1,'stocks','MSFT')
            assert False, 'expected shared capital overspend rejection'
        except ValueError:
            db.rollback()
    finally: db.close()
