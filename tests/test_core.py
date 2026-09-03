from app.trading import DEFAULT_WATCH, preflight


class S:
    capital = 1000
    daily_loss_pct = 2
    risk_per_trade_pct = 0.5
    max_trades = 5


def test_preflight_ok():
    assert preflight(S(), ["AAPL"]) == []


def test_preflight_bad():
    s = S()
    s.daily_loss_pct = 101
    assert "daily_loss_pct" in preflight(s, ["AAPL"])


def test_requires_allowed_stock():
    assert "allowed_symbols" in preflight(S(), [])


def test_default_universe_has_liquid_names():
    assert {"AAPL", "NVDA", "AMD"}.issubset(set(DEFAULT_WATCH))

from app.strategy import analyze_symbol


def _trend_bars(n=90):
    bars=[]
    px=100.0
    for i in range(n):
        px *= (0.9985 if i % 3 == 0 else 1.0015)
        bars.append({
            'o': px*0.999,
            'h': px*1.002,
            'l': px*0.998,
            'c': px,
            'v': 100000 + (i % 10)*5000,
        })
    bars[-1]['v'] = 220000
    return bars


def test_multi_indicator_strategy_can_form_signal():
    sig = analyze_symbol('AAPL', _trend_bars(), min_score=60)
    assert sig is not None
    assert sig.action == 'BUY'
    assert sig.stop < sig.price < sig.target
    assert sig.ema9 > sig.ema20 > sig.ema50
    assert sig.atr14 > 0


def test_strategy_rejects_insufficient_history():
    assert analyze_symbol('AAPL', _trend_bars(20), min_score=0) is None


def test_alpaca_broker_refuses_live_url(monkeypatch):
    from app.broker import alpaca_broker
    from app.config import settings
    monkeypatch.setattr(settings, "alpaca_trading_base_url", "https://api.alpaca.markets")
    try:
        _ = alpaca_broker.base_url
        assert False, "live URL should be refused"
    except RuntimeError:
        assert True


def test_indicator_snapshot_available_below_trade_threshold():
    from app.strategy import evaluate_symbol
    state = evaluate_symbol('AAPL', _trend_bars(), min_score=101)
    assert state is not None
    assert state['symbol'] == 'AAPL'
    assert 'score' in state and 'rsi14' in state and 'adx14' in state
    assert state['qualified'] is False


def test_trade_metrics():
    from types import SimpleNamespace
    from app.analytics import trade_metrics
    trades = [
        SimpleNamespace(status='CLOSED', pnl=10),
        SimpleNamespace(status='CLOSED', pnl=-5),
        SimpleNamespace(status='OPEN', pnl=0),
    ]
    m = trade_metrics(trades)
    assert m['net'] == 5
    assert m['win_rate'] == 50
    assert m['profit_factor'] == 2


def test_fractional_position_sizing(monkeypatch):
    from types import SimpleNamespace
    from app import trading
    s = SimpleNamespace(capital=1000, risk_per_trade_pct=1, cash_reserve_pct=20,
                        max_position_allocation_pct=30, allow_fractional=True)
    u = SimpleNamespace(id=1, settings=s)
    class Q:
        def filter_by(self, **kw): return self
        def order_by(self, *a, **k): return self
        def all(self): return []
    class DB:
        def query(self, *a): return Q()
    monkeypatch.setattr(trading, "_real_broker_mode", lambda: True)
    qty = trading._size_position(DB(), u, entry=500, stop=495)
    assert 0 < qty < 1


def test_schedule_does_not_restart_same_session(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app import trading
    s = SimpleNamespace(start_mode='scheduled', active=False, locked=False,
                        session_started_at=datetime.now(timezone.utc), scheduled_days='0,1,2,3,4',
                        start_delay_minutes=0)
    assert trading.schedule_should_start(s) is False


def test_custom_indicator_model_has_unbounded_user_library():
    from app.models import CustomIndicator
    row = CustomIndicator(user_id=1, name='My indicator', symbols='AAPL,MSFT')
    assert row.name == 'My indicator'
    assert row.symbols == 'AAPL,MSFT'
