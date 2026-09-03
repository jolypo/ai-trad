import json
from types import SimpleNamespace
from app import trading
from app.models import Trade


def test_fractional_broker_stop_payload(monkeypatch):
    captured={}
    monkeypatch.setattr(trading.alpaca_broker,'is_fractionable',lambda s: True)
    class R:
        def raise_for_status(self): pass
        def json(self): return {'id':'stop1','status':'new'}
    class C:
        def __init__(self,*a,**k): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def post(self,url,headers=None,json=None): captured.update(json); return R()
    import app.broker as broker
    monkeypatch.setattr(broker.httpx,'Client',C)
    o=trading.alpaca_broker.submit_fractional_stop_sell(symbol='AAPL',qty=.25,stop_price=95,client_order_id='x')
    assert o['id']=='stop1'
    assert captured['qty']=='0.250000000' and captured['side']=='sell' and captured['type']=='stop'
    assert captured['time_in_force']=='day' and captured['stop_price']=='95.00'


def test_fractional_stop_fill_closes_trade(monkeypatch):
    t=SimpleNamespace(indicators=json.dumps({'fractional_stop_order_id':'s1'}), status='OPEN')
    u=SimpleNamespace(id=1)
    class DB:
        def commit(self): pass
        def add(self,x): pass
    monkeypatch.setattr(trading.alpaca_broker,'get_order',lambda *a,**k:{'id':'s1','status':'filled','filled_avg_price':'94.5'})
    seen={}
    monkeypatch.setattr(trading,'_close_trade',lambda db,user,trade,px,reason: seen.update(px=px,reason=reason))
    assert trading._sync_fractional_stop(DB(),u,t) is True
    assert seen=={'px':94.5,'reason':'ALPACA_FRACTIONAL_STOP_LOSS'}


def test_luqman_exit_cancels_fractional_stop_first(monkeypatch):
    t=SimpleNamespace(symbol='AAPL',indicators=json.dumps({'fractional_stop_order_id':'s1'}))
    u=SimpleNamespace(id=1)
    class DB:
        def add(self,x): pass
        def commit(self): pass
    seq=[]
    monkeypatch.setattr(trading,'_cancel_fractional_stop',lambda trade,wait=True: seq.append('cancel_stop'))
    monkeypatch.setattr(trading,'_broker_order_id',lambda trade: None)
    monkeypatch.setattr(trading.alpaca_broker,'close_position',lambda symbol: seq.append('close') or None)
    monkeypatch.setattr(trading,'_sync_broker_trade',lambda *a,**k: None)
    trading._broker_close_trade(DB(),u,t,'TARGET')
    assert seq[:2]==['cancel_stop','close']
