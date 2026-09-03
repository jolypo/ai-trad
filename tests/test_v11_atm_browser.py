from datetime import date

import app.options_engine as oe


def test_contract_browser_keeps_nearest_strikes_and_marks_atm(monkeypatch):
    contracts=[]
    snaps={}
    expiry=date.today().isoformat()
    for strike in [280, 290, 300, 310, 315, 317.5, 320, 322.5, 325, 330, 340, 350]:
        sym=f"AAPL{str(strike).replace('.', '')}C"
        contracts.append({"symbol":sym,"tradable":True,"strike_price":str(strike),"expiration_date":expiry,"type":"call"})
        snaps[sym]={"latestQuote":{"bp":1.0,"ap":1.2,"t":"2099-01-01T00:00:00Z"},"greeks":{"delta":0.5}}

    monkeypatch.setattr(oe.alpaca_broker, "option_contracts", lambda **kwargs: contracts)
    monkeypatch.setattr(oe.market_data, "option_chain", lambda *args, **kwargs: snaps)

    rows=oe.contract_browser("AAPL", "call", 0, 14, limit=5, underlying_price=319.70)
    strikes=[float(x["strike_price"]) for x in rows]
    assert len(rows)==5
    assert 320.0 in strikes
    assert min(strikes) >= 315.0
    atm=[x for x in rows if x.get("atm")]
    assert len(atm)==1
    assert float(atm[0]["strike_price"])==320.0
    assert all(float(x.get("underlying_price") or 0)==319.70 for x in rows)


def test_contract_browser_without_spot_preserves_backward_behavior(monkeypatch):
    expiry=date.today().isoformat()
    contracts=[
        {"symbol":"X1","tradable":True,"strike_price":"100","expiration_date":expiry,"type":"call"},
        {"symbol":"X2","tradable":True,"strike_price":"110","expiration_date":expiry,"type":"call"},
    ]
    snaps={x["symbol"]:{"latestQuote":{"bp":1.0,"ap":1.1,"t":"2099-01-01T00:00:00Z"}} for x in contracts}
    monkeypatch.setattr(oe.alpaca_broker, "option_contracts", lambda **kwargs: contracts)
    monkeypatch.setattr(oe.market_data, "option_chain", lambda *args, **kwargs: snaps)
    rows=oe.contract_browser("XYZ", "call", 0, 14, limit=1)
    assert len(rows)==1
    assert rows[0]["symbol"]=="X1"
    assert not rows[0].get("atm")
