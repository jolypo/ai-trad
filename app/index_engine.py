from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .market_data import market_data
from .models import Alert, BotSettings, StockIndicatorSnapshot, Trade
from .strategy import analyze_symbol, evaluate_symbol
from .trading import _open_from_signal, entry_gate_status, market_is_open

INDEX_PRODUCTS = {
    "QQQ": {
        "label_ar": "ناسداك 100 - QQQ",
        "label_en": "Nasdaq-100 ETF - QQQ",
        "data_symbol": "QQQ",
        "execution": "equity",
        "execution_ar": "تداول ETF فعلي عبر Alpaca Paper",
        "execution_en": "Tradable ETF through Alpaca Paper",
    },
    "SPX": {
        "label_ar": "مؤشر S&P 500 - SPX",
        "label_en": "S&P 500 Index - SPX",
        # Alpaca's public stock-bar API does not expose SPX spot index bars. SPY is
        # used only as an explicit proxy for the technical dashboard.
        "data_symbol": "SPY",
        "execution": "analysis_only",
        "execution_ar": "تحليل عبر SPY كوكيل؛ تنفيذ SPX Options معطل حتى تحقق دعم الوسيط للعقد",
        "execution_en": "SPY proxy analytics; SPX options execution is safety-gated pending broker contract support",
    },
}


def selected_index_symbols(user) -> list[str]:
    raw = str(getattr(user.settings, "index_symbols", "QQQ,SPX") or "QQQ,SPX")
    return [x for x in (s.strip().upper() for s in raw.split(",")) if x in INDEX_PRODUCTS]


def index_states(db: Session, user) -> list[dict]:
    selected = selected_index_symbols(user)
    proxies = [INDEX_PRODUCTS[s]["data_symbol"] for s in selected]
    if not market_data.configured or not proxies:
        return []
    bars_by_proxy = market_data.recent_bars(proxies)
    out = []
    for display_symbol in selected:
        cfg = INDEX_PRODUCTS[display_symbol]
        proxy = cfg["data_symbol"]
        state = evaluate_symbol(proxy, bars_by_proxy.get(proxy, []), settings.min_signal_score)
        if not state:
            continue
        state = dict(state)
        state["data_symbol"] = proxy
        state["symbol"] = display_symbol
        state["execution_mode"] = cfg["execution"]
        state["execution_note_ar"] = cfg["execution_ar"]
        state["execution_note_en"] = cfg["execution_en"]
        out.append(state)
    return out


def run_index_cycle(db: Session, user):
    s = user.settings
    if not bool(getattr(s, "index_bot_active", False)) or s.locked:
        return None
    if settings.enforce_market_hours and not market_is_open():
        return None
    selected = selected_index_symbols(user)
    proxies = [INDEX_PRODUCTS[x]["data_symbol"] for x in selected]
    if not proxies or not market_data.configured:
        return None
    bars_by_proxy = market_data.recent_bars(proxies)
    candidates = []
    for symbol in selected:
        cfg = INDEX_PRODUCTS[symbol]
        proxy = cfg["data_symbol"]
        state = evaluate_symbol(proxy, bars_by_proxy.get(proxy, []), settings.min_signal_score)
        if state:
            snap = StockIndicatorSnapshot(
                user_id=user.id, symbol=f"INDEX:{symbol}", price=state["price"], score=state["score"],
                qualified=state["qualified"], ema9=state["ema9"], ema20=state["ema20"], ema50=state["ema50"],
                rsi14=state["rsi14"], macd=state["macd"], macd_signal=state["macd_signal"], atr14=state["atr14"],
                vwap=state["vwap"], adx14=state["adx14"], rel_volume=state["rel_volume"], momentum_5=state["momentum_5"],
                verdict=state["verdict"], reasons="index_dashboard",
            )
            db.add(snap)
        sig = analyze_symbol(proxy, bars_by_proxy.get(proxy, []), settings.min_signal_score)
        if not sig:
            continue
        if cfg["execution"] == "equity":
            sig.symbol = symbol
            candidates.append(sig)
        else:
            # Analysis bot is still useful, but no false claim of SPX order support.
            if state and state.get("qualified"):
                db.add(Alert(
                    user_id=user.id,
                    title="INDEX SIGNAL",
                    body=f"{symbol} qualified ({state['score']:.0f}/100). Analysis-only: broker contract execution is safety-gated.",
                ))
    db.commit()
    if not candidates:
        return None
    open_index = {t.symbol for t in db.query(Trade).filter_by(user_id=user.id, status="OPEN", engine="index").all()}
    candidates = [c for c in candidates if c.symbol not in open_index]
    if not candidates:
        return None
    best = max(candidates, key=lambda x: (x.score, x.rel_volume, x.momentum_5))
    if not entry_gate_status(db, user, symbol=best.symbol, entry=best.price).get("ok"):
        return None
    return _open_from_signal(db, user, best, engine="index")


def start_index_bot(db: Session, user):
    if user.settings.locked:
        return False, "risk locked"
    if settings.enforce_market_hours and not market_is_open():
        return False, "market closed"
    # The current deployment uses one global Alpaca credential pair. Keep the
    # same cross-user safety lock used by the stock bot until per-user OAuth exists.
    other = (db.query(BotSettings)
             .filter(BotSettings.user_id != user.id)
             .filter((BotSettings.active.is_(True)) | (BotSettings.index_bot_active.is_(True)))
             .count())
    if other:
        return False, "Alpaca Paper account is already in use by another active user"
    user.settings.index_bot_active = True
    db.add(Alert(user_id=user.id, title="INDEX BOT ACTIVE", body=f"Index dashboard bot started: {', '.join(selected_index_symbols(user))}"))
    db.commit()
    return True, "started"


def stop_index_bot(db: Session, user, close_positions: bool = False):
    user.settings.index_bot_active = False
    if close_positions:
        # Index-engine positions are ordinary broker positions tagged in Trade.engine.
        from .trading import close_user_position
        rows = db.query(Trade).filter_by(user_id=user.id, status="OPEN", engine="index").all()
        for row in rows:
            try:
                close_user_position(db, user, row.symbol)
            except Exception:
                db.rollback()
    db.add(Alert(user_id=user.id, title="INDEX BOT OFF", body="Index dashboard bot stopped."))
    db.commit()
