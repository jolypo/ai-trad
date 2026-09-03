import json
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.db import Base, SessionLocal, engine
from app.models import BotSettings, Trade, User, CapitalReservation
from app.security import hash_password
from app import trading
from app.capital_engine import budget_state, reserve_capital
from app.options_engine import _choose_contract
from app.config import settings


def _user(db, email='final@test.local', capital=500):
    u=db.query(User).filter_by(email=email).first()
    if u: return u
    u=User(name='Final',email=email,password_hash=hash_password('StrongPass123!'))
    db.add(u);db.flush();db.add(BotSettings(user_id=u.id,capital=capital,cash_reserve_pct=0,max_position_allocation_pct=100,risk_per_trade_pct=1,max_trades=10));db.commit();db.refresh(u);return u


def _manual_close_mocks(monkeypatch, symbol, broker_qty, order_qty=None, status='accepted', filled_qty='0', fill_price=None):
    monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
    monkeypatch.setattr(trading,'market_phase',lambda:'regular')
    monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':symbol,'qty':str(broker_qty)}])
    monkeypatch.setattr(trading.alpaca_broker,'cancel_open_orders_for_symbol',lambda symbol:[])
    monkeypatch.setattr(trading.alpaca_broker,'wait_for_no_open_orders',lambda symbol,timeout_seconds=4:True)
    def close(symbol,qty=None,percentage=None):
        return {'id':'close-1','status':'accepted','qty':str(order_qty if order_qty is not None else qty)}
    monkeypatch.setattr(trading.alpaca_broker,'close_position',close)
    terminal={'id':'close-1','status':status,'filled_qty':str(filled_qty)}
    if fill_price is not None: terminal['filled_avg_price']=str(fill_price)
    monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=12:terminal)


def test_exact_manual_qty_one_never_becomes_full_position(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'exact-one@test.local');t=Trade(user_id=u.id,symbol='AVGO',side='BUY',engine='stocks',qty=8,entry=370,status='OPEN',pnl=0);db.add(t);db.commit()
        captured={}
        _manual_close_mocks(monkeypatch,'AVGO',8)
        def close(symbol,qty=None,percentage=None):captured.update(symbol=symbol,qty=qty,percentage=percentage);return {'id':'close-1','status':'accepted','qty':str(qty)}
        monkeypatch.setattr(trading.alpaca_broker,'close_position',close)
        trading.close_user_position_partial(db,u,'AVGO',qty=1)
        assert captured=={'symbol':'AVGO','qty':1.0,'percentage':None}
        db.refresh(t);assert t.qty==8 and t.status=='OPEN'
    finally:db.close()


def test_manual_qty_above_local_position_is_rejected(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'local-cap@test.local');db.add(Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=2,entry=100,status='OPEN'));db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        with pytest.raises(ValueError,match='Luqman open quantity'):trading.close_user_position_partial(db,u,'AAPL',qty=3)
    finally:db.close()


def test_manual_qty_above_broker_position_is_rejected(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'broker-cap@test.local');db.add(Trade(user_id=u.id,symbol='AAPL',side='BUY',engine='stocks',qty=8,entry=100,status='OPEN'));db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True);monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':'AAPL','qty':'1'}])
        with pytest.raises(ValueError,match='Alpaca position'):trading.close_user_position_partial(db,u,'AAPL',qty=2)
    finally:db.close()


def test_broker_qty_mismatch_cancels_order(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal();canceled=[]
    try:
        u=_user(db,'mismatch@test.local');db.add(Trade(user_id=u.id,symbol='AVGO',side='BUY',engine='stocks',qty=8,entry=370,status='OPEN'));db.commit()
        _manual_close_mocks(monkeypatch,'AVGO',8,order_qty=8)
        monkeypatch.setattr(trading.alpaca_broker,'cancel_order',lambda oid:canceled.append(oid))
        with pytest.raises(ValueError,match='quantity mismatch'):trading.close_user_position_partial(db,u,'AVGO',qty=1)
        assert canceled==['close-1']
    finally:db.close()


def test_protective_orders_must_be_gone_before_manual_sell(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal();called=[]
    try:
        u=_user(db,'protection@test.local');db.add(Trade(user_id=u.id,symbol='MSFT',side='BUY',engine='stocks',qty=2,entry=500,status='OPEN'));db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True);monkeypatch.setattr(trading.alpaca_broker,'positions',lambda:[{'symbol':'MSFT','qty':'2'}]);monkeypatch.setattr(trading.alpaca_broker,'cancel_open_orders_for_symbol',lambda s:[]);monkeypatch.setattr(trading.alpaca_broker,'wait_for_no_open_orders',lambda s,timeout_seconds=4:False);monkeypatch.setattr(trading.alpaca_broker,'close_position',lambda *a,**k:called.append(1))
        with pytest.raises(ValueError,match='Protective orders'):trading.close_user_position_partial(db,u,'MSFT',qty=1)
        assert not called
    finally:db.close()


def test_partial_then_final_close_preserves_original_qty_and_cumulative_pnl(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'partial-history@test.local');t=Trade(user_id=u.id,symbol='AVGO',side='BUY',engine='stocks',qty=8,entry=100,status='OPEN',pnl=0,indicators='{}');db.add(t);db.commit()
        monkeypatch.setattr(trading.alpaca_broker,'submit_oco_exit',lambda **k:{'id':'protect'})
        assert trading._apply_manual_close_fill(db,u,t,1,110,'one') is True;db.refresh(t);assert t.qty==7 and t.pnl==10
        assert trading._apply_manual_close_fill(db,u,t,7,105,'two') is True;db.refresh(t);assert t.status=='CLOSED' and t.qty==8 and t.pnl==45
        payload=json.loads(t.indicators);assert payload['original_qty']==8 and len(payload['partial_close_history'])==2
    finally:db.close()


def test_exit_does_not_increment_daily_trade_count(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'exit-count@test.local');u.settings.trades_today=4;t=Trade(user_id=u.id,symbol='IBM',side='BUY',engine='stocks',qty=1,entry=100,status='OPEN',pnl=0);db.add(t);db.commit();trading._apply_manual_close_fill(db,u,t,1,101,'x');db.refresh(u.settings);assert u.settings.trades_today==4
    finally:db.close()


def test_options_exposure_uses_contract_multiplier_100():
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'opt-mult@test.local',1000);db.add(Trade(user_id=u.id,symbol='QQQ260101C00100000',side='BUY',engine='options',qty=2,entry=3,status='OPEN'));db.commit();b=budget_state(db,u,prices={'QQQ260101C00100000':4});assert b['invested']==800 and b['available']==200
    finally:db.close()


def test_shared_capital_reservation_blocks_double_spending(monkeypatch):
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'double-spend@test.local',500);monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:500)
        r=reserve_capital(db,u,400,'stocks','AAPL');db.commit()
        with pytest.raises(ValueError):reserve_capital(db,u,101,'options','QQQOPT')
        assert r.status=='PENDING'
    finally:db.close()


def test_manual_allocation_caps_are_respected():
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'manual-alloc@test.local',500);u.settings.allocation_mode='manual';u.settings.stocks_allocation_pct=40;u.settings.options_allocation_pct=30;db.commit();b=budget_state(db,u,broker_cash=500);assert b['allocation']['stocks_cap']==200 and b['allocation']['options_cap']==150
    finally:db.close()


def test_stale_option_contract_never_auto_selected(monkeypatch):
    s=SimpleNamespace(options_min_dte=0,options_max_dte=7,options_target_delta=.5)
    rows=[{'symbol':'OLD','mid':2,'ask':2.1,'bid':1.9,'delta':.5,'expiration_date':'2026-09-01','stale':True},{'symbol':'FRESH','mid':3,'ask':3.1,'bid':2.9,'delta':.55,'expiration_date':'2026-09-01','stale':False}]
    monkeypatch.setattr('app.options_engine.contract_browser',lambda *a,**k:rows)
    assert _choose_contract('SPX','call',s)['symbol']=='FRESH'


def test_options_template_has_real_spx_reference_and_no_proxy_copy():
    text=open('app/templates/options.html',encoding='utf-8').read().lower()
    assert 'spy proxy' not in text and 'مرجع spy' not in text
    assert 'cash-index reference' in text or 'النقدي' in text
    assert 'data-live-verdict' in text and 'data-live-score' in text


def test_dashboard_manual_sell_ui_has_qty_only():
    text=open('app/templates/dashboard.html',encoding='utf-8').read()
    assert 'name="qty"' in text
    assert 'name="percentage"' not in text


def test_portfolio_chart_has_axis_dates_values_and_dynamic_tooltip():
    text=open('app/static/charts.js',encoding='utf-8').read()
    assert 'chart-axis' in text and 'chart-time-axis' in text and 'Change' in text and 'التغير' in text
    assert "type==='stock'?10000:10000" in text


def test_capital_page_has_live_budget_bindings():
    text=open('app/templates/capital.html',encoding='utf-8').read()
    for key in ['data-cap-allocated','data-cap-invested','data-cap-reserved','data-cap-available','/api/live/capital']:
        assert key in text


def test_option_exit_pending_is_persisted_and_not_duplicated(monkeypatch):
    from app import options_engine
    Base.metadata.create_all(engine);db=SessionLocal();calls=[]
    try:
        u=_user(db,'opt-pending@test.local',1000)
        t=Trade(user_id=u.id,symbol='QQQ260101C00100000',side='BUY',engine='options',qty=1,entry=3,stop_loss=2,take_profit=5,status='OPEN',pnl=0,indicators='{}');db.add(t);db.commit()
        monkeypatch.setattr(options_engine.alpaca_broker,'positions',lambda:[{'symbol':t.symbol,'qty':'1','current_price':'1.5'}])
        def close(symbol,qty=None,percentage=None):calls.append((symbol,qty));return {'id':'exit-1','status':'accepted','qty':'1'}
        monkeypatch.setattr(options_engine.alpaca_broker,'close_position',close)
        monkeypatch.setattr(options_engine.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=8:{'id':oid,'status':'accepted','filled_qty':'0'})
        monkeypatch.setattr(options_engine.alpaca_broker,'get_order',lambda oid,nested=True:{'id':oid,'status':'accepted','filled_qty':'0'})
        options_engine.manage_option_positions(db,u);db.refresh(t)
        assert json.loads(t.indicators)['pending_option_close']['order_id']=='exit-1'
        options_engine.manage_option_positions(db,u)
        assert len(calls)==1
    finally:db.close()


def test_option_pending_exit_fill_closes_once(monkeypatch):
    from app import options_engine
    Base.metadata.create_all(engine);db=SessionLocal()
    try:
        u=_user(db,'opt-pending-fill@test.local',1000)
        payload={'pending_option_close':{'order_id':'exit-2','reason':'ALPACA_OPTION_STOP_LOSS','qty':1}}
        t=Trade(user_id=u.id,symbol='QQQ260101C00100000',side='BUY',engine='options',qty=1,entry=3,stop_loss=2,status='OPEN',pnl=0,indicators=json.dumps(payload));db.add(t);db.commit()
        monkeypatch.setattr(options_engine.alpaca_broker,'positions',lambda:[{'symbol':t.symbol,'qty':'1','current_price':'1.5'}])
        monkeypatch.setattr(options_engine.alpaca_broker,'get_order',lambda oid,nested=True:{'id':oid,'status':'filled','filled_qty':'1','filled_avg_price':'1.8'})
        options_engine.manage_option_positions(db,u);db.refresh(t)
        assert t.status=='CLOSED' and t.pnl==-120
    finally:db.close()


def test_stocks_only_and_options_only_are_hard_caps():
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'exclusive-plans@test.local',100)
        u.settings.cash_reserve_pct=35
        u.settings.allocation_mode='stocks_only'; db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['usable']==65
        assert b['allocation']['stocks_cap']==65
        assert b['allocation']['stocks_available']==65
        assert b['allocation']['options_cap']==0
        assert b['allocation']['options_available']==0
        u.settings.allocation_mode='options_only'; db.commit()
        b=budget_state(db,u,broker_cash=1000)
        assert b['allocation']['stocks_cap']==0
        assert b['allocation']['stocks_available']==0
        assert b['allocation']['options_cap']==65
        assert b['allocation']['options_available']==65
    finally: db.close()


def test_fractional_entry_waits_for_fill_and_records_fractional_qty(monkeypatch):
    from types import SimpleNamespace
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'fractional-fill@test.local',100)
        u.settings.cash_reserve_pct=0; u.settings.max_position_allocation_pct=100; u.settings.risk_per_trade_pct=100
        u.settings.allow_fractional=True; u.settings.active=True; u.settings.max_trades=10; u.settings.trades_today=0
        u.settings.allocation_mode='stocks_only'; db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:1000)
        monkeypatch.setattr(trading.alpaca_broker,'is_fractionable',lambda symbol:True)
        monkeypatch.setattr(trading.alpaca_broker,'submit_fractional_market_buy',lambda **kw:{'id':'frac-1','status':'accepted','client_order_id':kw['client_order_id']})
        monkeypatch.setattr(trading.alpaca_broker,'wait_for_terminal_order',lambda oid,timeout_seconds=8:{'id':oid,'status':'filled','filled_qty':'0.25','filled_avg_price':'200.00','client_order_id':'cid'})
        monkeypatch.setattr(trading.alpaca_broker,'submit_fractional_stop_sell',lambda **kw:{'id':'frac-stop-1','status':'new','client_order_id':kw['client_order_id']})
        monkeypatch.setattr(trading.alpaca_broker,'confirm_open_order',lambda oid,timeout_seconds=4:{'id':oid,'status':'new'})
        sig=SimpleNamespace(symbol='AAPL',price=200.0,stop=190.0,target=220.0,score=90.0,to_dict=lambda:{})
        t=trading._open_from_signal(db,u,sig)
        assert t is not None
        assert t.qty==0.25
        assert t.entry==200.0
        assert t.reason=='ALPACA_FRACTIONAL_MARKET_SUBMITTED'
    finally: db.close()


def test_fractional_entry_rejects_non_fractionable_asset(monkeypatch):
    from types import SimpleNamespace
    Base.metadata.create_all(engine); db=SessionLocal()
    try:
        u=_user(db,'fractional-reject@test.local',100)
        u.settings.cash_reserve_pct=0; u.settings.max_position_allocation_pct=100; u.settings.risk_per_trade_pct=100
        u.settings.allow_fractional=True; u.settings.active=True; u.settings.max_trades=10; u.settings.trades_today=0
        u.settings.allocation_mode='stocks_only'; db.commit()
        monkeypatch.setattr(trading,'_real_broker_mode',lambda:True)
        monkeypatch.setattr(trading,'market_is_open',lambda:True)
        monkeypatch.setattr('app.capital_engine.broker_cash_available',lambda:1000)
        monkeypatch.setattr(trading.alpaca_broker,'is_fractionable',lambda symbol:False)
        sig=SimpleNamespace(symbol='AAPL',price=200.0,stop=190.0,target=220.0,score=90.0,to_dict=lambda:{})
        assert trading._open_from_signal(db,u,sig) is None
        a=db.query(__import__('app.models',fromlist=['Alert']).Alert).filter_by(user_id=u.id).order_by(__import__('app.models',fromlist=['Alert']).Alert.id.desc()).first()
        assert a and 'non-fractionable' in a.body
    finally: db.close()


def test_arabic_live_broker_order_status_uses_labels():
    text=open('app/templates/dashboard.html',encoding='utf-8').read()
    assert 'o.status_label||status(o.status)' in text
    assert "filled:'منفذ'" in text
    assert "canceled:'ملغي'" in text


def test_dashboard_removes_expectancy_and_uses_capital_first_kpis():
    text=open('app/templates/dashboard.html',encoding='utf-8').read()
    assert 'العائد المتوقع' not in text
    for key in ['capital-invested','capital-reserved','capital-available','capital-reserve-note']:
        assert key in text


def test_reports_use_dynamic_axis_chart():
    text=open('app/templates/reports.html',encoding='utf-8').read()
    assert 'reports-portfolio-chart' in text
    assert '/static/charts.js' in text
    assert 'data-chart-high' in text and 'data-chart-low' in text and 'data-chart-change' in text


def test_fractional_broker_payload_preserves_decimal_qty_and_day_tif(monkeypatch):
    from app import broker as broker_mod
    sent={}
    monkeypatch.setattr(broker_mod.alpaca_broker,'is_fractionable',lambda symbol:True)
    class Resp:
        def raise_for_status(self): pass
        def json(self): return {'id':'fractional-order','status':'accepted'}
    class Client:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def post(self,url,headers=None,json=None): sent.update(url=url,headers=headers,payload=json); return Resp()
    monkeypatch.setattr(broker_mod.httpx,'Client',Client)
    out=broker_mod.alpaca_broker.submit_fractional_market_buy(symbol='AAPL',qty=0.25,client_order_id='frac-test')
    assert out['id']=='fractional-order'
    assert sent['payload']['qty']=='0.250000'
    assert sent['payload']['type']=='market'
    assert sent['payload']['time_in_force']=='day'
    assert sent['payload']['side']=='buy'


def test_arabic_alert_translation_covers_options_bot_and_manual():
    from app.i18n import code_label, alert_body_label
    assert code_label('OPTIONS BOT ACTIVE','ar')=='بوت العقود نشط'
    assert code_label('FILLED','ar')=='منفذ'
    body=alert_body_label('Options bot stopped manual','ar')
    assert 'تم إيقاف بوت العقود' in body and 'يدوي' in body


def test_capital_post_stocks_only_persists_and_recomputes_budget():
    from fastapi.testclient import TestClient
    from app.main import app
    Base.metadata.create_all(engine)
    db=SessionLocal()
    try:
        u=_user(db,'capital-route@test.local',100)
        u.password_hash=hash_password('StrongPass123!')
        u.settings.cash_reserve_pct=35
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        r=client.post('/login',data={'email':'capital-route@test.local','password':'StrongPass123!'},follow_redirects=False)
        assert r.status_code in (302,303)
        r=client.post('/capital',data={
            'allocated_capital':'100','cash_reserve_pct':'35','allocation_mode':'stocks_only',
            'stocks_allocation_pct':'60','options_allocation_pct':'40'
        },follow_redirects=False)
        assert r.status_code==303
        r=client.get('/api/live/capital')
        assert r.status_code==200
        data=r.json(); b=data['budget']
        assert b['allocated']==100 and b['usable']==65
        assert b['allocation']['mode']=='stocks_only'
        assert b['allocation']['stocks_cap']==65 and b['allocation']['stocks_available']==65
        assert b['allocation']['options_cap']==0 and b['allocation']['options_available']==0


def test_capital_post_options_only_persists_and_recomputes_budget():
    from fastapi.testclient import TestClient
    from app.main import app
    Base.metadata.create_all(engine)
    db=SessionLocal()
    try:
        u=_user(db,'capital-opt-route@test.local',100)
        u.password_hash=hash_password('StrongPass123!')
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        assert client.post('/login',data={'email':'capital-opt-route@test.local','password':'StrongPass123!'},follow_redirects=False).status_code in (302,303)
        assert client.post('/capital',data={'allocated_capital':'100','cash_reserve_pct':'0','allocation_mode':'options_only','stocks_allocation_pct':'100','options_allocation_pct':'0'},follow_redirects=False).status_code==303
        b=client.get('/api/live/capital').json()['budget']
        assert b['allocation']['stocks_available']==0
        assert b['allocation']['options_available']==100


def test_fractional_setting_is_persisted_by_risk_settings_route():
    from fastapi.testclient import TestClient
    from app.main import app
    Base.metadata.create_all(engine)
    db=SessionLocal()
    try:
        u=_user(db,'fractional-setting@test.local',100)
        u.password_hash=hash_password('StrongPass123!')
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        assert client.post('/login',data={'email':'fractional-setting@test.local','password':'StrongPass123!'},follow_redirects=False).status_code in (302,303)
        # Send the complete required settings payload while enabling fractional shares.
        payload={
            'capital':'100','daily_loss_pct':'2','risk_per_trade_pct':'1','max_trades':'5','profit_target_pct':'3',
            'max_position_allocation_pct':'30','max_open_positions_user':'3','cash_reserve_pct':'20',
            'risk_profile':'custom','stop_target_mode':'atr','stop_loss_value':'1','take_profit_value':'2','risk_reward_ratio':'2',
            'allow_fractional':'on','start_mode':'manual','scheduled_days':['0','1','2','3','4'],
            'start_delay_minutes':'0','auto_stop_before_close_minutes':'5','trade_cooldown_seconds':'600','language':'ar'
        }
        r=client.post('/settings',data=payload,follow_redirects=False)
        assert r.status_code==303
    db=SessionLocal()
    try:
        u=db.query(User).filter_by(email='fractional-setting@test.local').first()
        assert u and u.settings.allow_fractional is True
    finally:
        db.close()


def test_i18n_covers_all_common_live_order_states():
    from app.i18n import code_label
    expected={
        'FILLED':'منفذ','CANCELED':'ملغي','CANCELLED':'ملغي','ACCEPTED':'مقبول',
        'PENDING_NEW':'قيد الإرسال','PARTIALLY_FILLED':'منفذ جزئياً','REJECTED':'مرفوض',
        'EXPIRED':'منتهي','DONE_FOR_DAY':'منتهي لليوم'
    }
    for raw,label in expected.items():
        assert code_label(raw,'ar')==label
