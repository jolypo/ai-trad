from __future__ import annotations

import json
import math
import time as _time
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import settings
from .broker import alpaca_broker
from .market_data import market_data
from .models import Alert, AllowedSymbol, BotSettings, PortfolioSnapshot, StockIndicatorSnapshot, Trade
from .capital_engine import budget_state as unified_budget_state, reserve_capital, release_reservation, commit_reservation, apply_realized_pnl, ensure_capital_account
from .strategy import analyze_symbol, evaluate_symbol
from .custom_indicator_runtime import apply_custom_indicator_rules

DEFAULT_WATCH = ["AAPL", "MSFT", "NVDA", "AMD", "AMZN", "META", "AVGO", "MU", "UBER", "INTC", "ORCL", "TSLA", "IBM", "RKLB"]
NY = ZoneInfo("America/New_York")


def market_now():
    return datetime.now(timezone.utc).astimezone(NY)


def market_is_open() -> bool:
    if market_data.configured:
        try:
            clock = market_data.broker_clock()
            if clock is not None:
                return bool(clock.get("is_open"))
        except Exception:
            pass
    n = market_now()
    if n.weekday() >= 5:
        return False
    return time(9, 30) <= n.time().replace(tzinfo=None) < time(16, 0)


def market_phase() -> str:
    """Return regular / premarket / afterhours / overnight / closed.

    This is intentionally based on New York wall-clock time for user-initiated equity
    exits. Alpaca still makes the final accept/reject decision for the order.
    """
    n = market_now()
    if n.weekday() >= 5:
        return "closed"
    t = n.time().replace(tzinfo=None)
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(4, 0) <= t < time(9, 30):
        return "premarket"
    if time(16, 0) <= t < time(20, 0):
        return "afterhours"
    # Alpaca documents overnight equity trading Sunday-Friday. Weekday handling here
    # is a best-effort UI gate; the broker remains authoritative.
    return "overnight"


def _manual_close_payload(trade: Trade) -> dict:
    try:
        return json.loads(trade.indicators or "{}")
    except Exception:
        return {}


def _apply_manual_close_fill(db: Session, user, trade: Trade, filled: float, exitp: float, order_id: str | None = None):
    filled = min(float(filled or 0), float(trade.qty or 0))
    if filled <= 0 or exitp <= 0:
        return False
    multiplier = 100.0 if str(getattr(trade, "engine", "")) == "options" else 1.0
    realized = round((exitp - trade.entry) * filled * multiplier, 2)
    remainder = max(0.0, float(trade.qty) - filled)
    user.settings.realized_pnl += realized
    apply_realized_pnl(user.settings, realized, getattr(trade, "engine", "stocks"))
    payload = _manual_close_payload(trade)
    original_qty = float(payload.get("original_qty") or trade.qty or 0)
    payload["original_qty"] = original_qty
    payload.pop("pending_manual_close", None)
    if order_id:
        payload["last_manual_close_order_id"] = order_id
    history = list(payload.get("partial_close_history") or [])
    history.append({"qty": filled, "price": exitp, "time": datetime.now(timezone.utc).isoformat()})
    payload["partial_close_history"] = history[-20:]
    payload["partial_close_last"] = {"qty": filled, "price": exitp}
    trade.indicators = json.dumps(payload, ensure_ascii=False)
    trade.pnl = round(float(trade.pnl or 0) + realized, 2)
    if remainder <= 1e-8:
        trade.exit = exitp
        trade.qty = original_qty
        trade.status = "CLOSED"
        trade.reason = "ALPACA_USER_MANUAL_CLOSE"
        trade.closed_at = datetime.now(timezone.utc)
    else:
        trade.qty = round(remainder, 8)
        trade.reason = "ALPACA_PARTIAL_USER_CLOSE"
        is_stock = str(getattr(trade, "engine", "stocks") or "stocks") == "stocks"
        is_whole = abs(remainder - int(remainder)) < 1e-9 and remainder >= 1
        if is_stock and is_whole:
            if str(getattr(user.settings, "stocks_exit_mode", "trailing") or "trailing") == "trailing":
                try:
                    trail_pct = max(0.1, min(20.0, float(getattr(user.settings, "stocks_trailing_distance_pct", 1.0) or 1.0)))
                    protected = alpaca_broker.submit_trailing_stop_sell(
                        symbol=trade.symbol, qty=remainder, trail_percent=trail_pct,
                        client_order_id=f"luqman-trail-reprotect-u{user.id}-{trade.symbol}-{int(_time.time())}",
                        time_in_force="gtc",
                    )
                    protected = alpaca_broker.confirm_open_order(str(protected["id"]), timeout_seconds=4)
                    payload = _manual_close_payload(trade)
                    payload["stock_trailing_order_id"] = protected.get("id")
                    payload["stock_trailing_status"] = protected.get("status")
                    payload["stock_trailing_percent"] = trail_pct
                    payload["protection_mode"] = "alpaca-native-trailing"
                    payload["broker_protection_confirmed"] = True
                    trade.indicators = json.dumps(payload, ensure_ascii=False)
                except Exception as exc:
                    db.add(Alert(user_id=user.id, title="PROTECTION WARNING", body=f"{trade.symbol}: remaining whole-share position needs trailing protection: {type(exc).__name__}"))
            elif trade.stop_loss and trade.take_profit:
                try:
                    oco = alpaca_broker.submit_oco_exit(
                        symbol=trade.symbol, qty=remainder, take_profit=float(trade.take_profit), stop_loss=float(trade.stop_loss),
                        client_order_id=f"luqman-protect-u{user.id}-{trade.symbol}-{int(_time.time())}",
                    )
                    payload = _manual_close_payload(trade)
                    payload["broker_order_id"] = oco.get("id")
                    payload["partial_close_last"] = {"qty": filled, "price": exitp}
                    trade.indicators = json.dumps(payload, ensure_ascii=False)
                except Exception as exc:
                    db.add(Alert(user_id=user.id, title="PROTECTION WARNING", body=f"{trade.symbol}: remaining position needs protection: {type(exc).__name__}"))
        else:
            trade.reason = "ALPACA_FRACTIONAL_MARKET_SUBMITTED"
            if is_stock and trade.stop_loss and abs(remainder - int(remainder)) > 1e-9:
                try:
                    stop_order = alpaca_broker.submit_fractional_stop_sell(
                        symbol=trade.symbol, qty=remainder, stop_price=float(trade.stop_loss),
                        client_order_id=f"luqman-frac-reprotect-u{user.id}-{trade.symbol}-{int(_time.time())}",
                    )
                    stop_order = alpaca_broker.confirm_open_order(str(stop_order["id"]), timeout_seconds=4)
                    payload = _manual_close_payload(trade)
                    payload["fractional_stop_order_id"] = stop_order.get("id")
                    payload["fractional_stop_status"] = stop_order.get("status")
                    payload["protection_mode"] = "broker-stop+luqman-target"
                    payload["broker_protection_confirmed"] = True
                    trade.indicators = json.dumps(payload, ensure_ascii=False)
                except Exception as exc:
                    payload = _manual_close_payload(trade)
                    payload["broker_protection_confirmed"] = False
                    trade.indicators = json.dumps(payload, ensure_ascii=False)
                    db.add(Alert(user_id=user.id, title="PROTECTION WARNING", body=f"{trade.symbol}: fractional remainder stop was not confirmed ({type(exc).__name__})"))
    db.add(Alert(user_id=user.id, title="PARTIAL CLOSE", body=f"{trade.symbol} sold x{filled:g} @ ${exitp:.2f} | Realized ${realized:.2f} | Remaining x{remainder:g}"))
    db.commit()
    return True


def _sync_pending_manual_close(db: Session, user, trade: Trade) -> bool:
    payload = _manual_close_payload(trade)
    pending = payload.get("pending_manual_close") or {}
    oid = pending.get("order_id")
    if not oid:
        return False
    try:
        order = alpaca_broker.get_order(str(oid), nested=True)
    except Exception:
        return False
    status = str(order.get("status") or "").lower()
    filled = float(order.get("filled_qty") or 0)
    exitp = float(order.get("filled_avg_price") or 0)
    if filled > 0 and exitp > 0:
        return bool(_apply_manual_close_fill(db, user, trade, filled, exitp, str(oid)))
    if status in {"canceled", "expired", "rejected"}:
        payload.pop("pending_manual_close", None)
        trade.indicators = json.dumps(payload, ensure_ascii=False)
        db.add(Alert(user_id=user.id, title="MANUAL CLOSE NOT FILLED", body=f"{trade.symbol}: close order {status}"))
        db.commit()
    return False


def market_closing_soon() -> bool:
    n = market_now()
    if n.weekday() >= 5:
        return False
    # Flatten intraday exposure before the regular 16:00 ET close.
    return time(15, 55) <= n.time().replace(tzinfo=None) < time(16, 0)


def today_key() -> str:
    return market_now().date().isoformat()


def seed_default_symbols(db: Session, user_id: int):
    if db.query(AllowedSymbol).filter_by(user_id=user_id).count() == 0:
        db.add_all([AllowedSymbol(user_id=user_id, symbol=s) for s in DEFAULT_WATCH])
        db.commit()


def allowed_symbols(db: Session, user_id: int) -> list[str]:
    return [x.symbol for x in db.query(AllowedSymbol).filter_by(user_id=user_id).order_by(AllowedSymbol.symbol).all()]


def set_allowed_symbols(db: Session, user_id: int, symbols: list[str]):
    cleaned = []
    for s in symbols:
        s = s.strip().upper()
        if s in DEFAULT_WATCH and s not in cleaned:
            cleaned.append(s)
    if not cleaned:
        raise ValueError("Select at least one allowed stock")
    db.query(AllowedSymbol).filter_by(user_id=user_id).delete(synchronize_session=False)
    db.add_all([AllowedSymbol(user_id=user_id, symbol=s) for s in cleaned])
    db.commit()
    return cleaned


def reset_for_new_day(s: BotSettings):
    key = today_key()
    if s.session_day != key:
        s.session_day = key
        s.realized_pnl = 0
        s.stocks_realized_pnl = 0
        s.options_realized_pnl = 0
        s.trades_today = 0
        s.locked = False
        s.stocks_risk_locked = False
        s.options_risk_locked = False
        s.active = False
        s.session_started_at = None
        s.session_stopped_at = None
        s.options_bot_active = False
        s.options_session_started_at = None
        s.options_session_stopped_at = None
        s.options_trades_today = 0
        s.options_last_trade_at = None
        s.last_trade_at = None


def preflight(s: BotSettings, symbols: list[str] | None = None):
    issues = []
    if s.capital <= 0:
        issues.append("capital")
    if not (0 <= s.daily_loss_pct <= 100):
        issues.append("daily_loss_pct")
    if not (0 <= s.risk_per_trade_pct <= 100):
        issues.append("risk_per_trade_pct")
    if not (0 <= float(getattr(s, "options_risk_per_trade_pct", 2) or 0) <= 100):
        issues.append("options_risk_per_trade_pct")
    if s.max_trades < 1:
        issues.append("max_trades")
    if not (1 <= getattr(s, "max_position_allocation_pct", 30) <= 100):
        issues.append("max_position_allocation_pct")
    if not (1 <= getattr(s, "max_open_positions_user", 3) <= 20):
        issues.append("max_open_positions_user")
    if not (0 <= getattr(s, "cash_reserve_pct", 20) < 100):
        issues.append("cash_reserve_pct")
    if symbols is not None and not symbols:
        issues.append("allowed_symbols")
    if settings.broker_mode == "alpaca_market_paper" and not market_data.configured:
        issues.append("alpaca_credentials")
    return issues


def _real_broker_mode() -> bool:
    return settings.broker_mode == "alpaca_market_paper"


def _broker_account_preflight() -> tuple[bool, str]:
    if not _real_broker_mode():
        return True, ""
    if not alpaca_broker.configured:
        return False, "alpaca credentials missing"
    try:
        account = alpaca_broker.account()
    except Exception as exc:
        return False, f"alpaca account unavailable: {type(exc).__name__}"
    if account.get("trading_blocked"):
        return False, "alpaca account trading blocked"
    if str(account.get("status", "")).upper() not in {"ACTIVE", ""}:
        return False, f"alpaca account status {account.get('status')}"
    try:
        if float(account.get("buying_power") or 0) <= 0:
            return False, "no Alpaca buying power"
    except (TypeError, ValueError):
        return False, "invalid Alpaca buying power"
    return True, ""


def start_session(db: Session, user, hard_cap: float):
    s = user.settings
    reset_for_new_day(s)
    symbols = allowed_symbols(db, user.id)
    issues = preflight(s, symbols)
    if issues:
        return False, ",".join(issues)
    if s.daily_loss_pct > hard_cap and not user.is_admin:
        return False, f"daily loss exceeds platform hard cap {hard_cap}%"
    if s.locked or bool(getattr(s, "stocks_risk_locked", False)):
        return False, "stock risk locked"
    if settings.enforce_market_hours and not market_is_open():
        return False, "market closed"
    if _real_broker_mode():
        # One API-key pair controls one Alpaca Paper account. Prevent two website
        # users from sending orders into that same account simultaneously.
        others = db.query(BotSettings).filter(BotSettings.user_id != user.id).all()
        if any(bool(x.active) or bool(getattr(x, "options_bot_active", False)) or bool(getattr(x, "index_bot_active", False)) for x in others):
            return False, "Alpaca Paper account is already in use by another active user"
        ok, broker_issue = _broker_account_preflight()
        if not ok:
            return False, broker_issue
        reconciled, issues = reconcile_broker_state(db, user)
        if not reconciled:
            return False, "broker reconciliation required: " + "; ".join(issues[:3])
    s.active = True
    s.session_started_at = datetime.now(timezone.utc)
    db.add(Alert(user_id=user.id, title="BOT ACTIVE", body=f"Paper session started. Allowed stocks: {', '.join(symbols)}"))
    db.commit()
    return True, "started"




def record_portfolio_snapshot(db: Session, user):
    """Persist the latest broker account state for dashboard charts/reports."""
    if not _real_broker_mode() or not alpaca_broker.configured:
        return None
    try:
        a = alpaca_broker.account()
        equity = float(a.get("equity") or a.get("portfolio_value") or 0)
        last_equity = float(a.get("last_equity") or equity)
        snap = PortfolioSnapshot(
            user_id=user.id,
            equity=equity,
            cash=float(a.get("cash") or 0),
            buying_power=float(a.get("buying_power") or 0),
            portfolio_value=float(a.get("portfolio_value") or equity),
            long_market_value=float(a.get("long_market_value") or 0),
            day_pnl=equity - last_equity,
            source="ALPACA_PAPER",
        )
        db.add(snap)
        db.commit()
        return snap
    except Exception:
        db.rollback()
        return None


def _save_indicator_snapshot(db: Session, user_id: int, state: dict):
    snap = StockIndicatorSnapshot(
        user_id=user_id, symbol=state["symbol"], price=state["price"], score=state["score"],
        qualified=state["qualified"], ema9=state["ema9"], ema20=state["ema20"], ema50=state["ema50"],
        rsi14=state["rsi14"], macd=state["macd"], macd_signal=state["macd_signal"], atr14=state["atr14"],
        vwap=state["vwap"], adx14=state["adx14"], rel_volume=state["rel_volume"],
        momentum_5=state["momentum_5"], verdict=state["verdict"],
        reasons=json.dumps({"reasons": state.get("reasons", []), "blockers": state.get("blockers", [])}, ensure_ascii=False),
    )
    db.add(snap)


def refresh_indicator_snapshots(db: Session, user, symbols: list[str], bars_by_symbol: dict[str, list[dict]]):
    states = []
    for symbol in symbols:
        state = evaluate_symbol(symbol, bars_by_symbol.get(symbol, []), settings.min_signal_score)
        if state:
            _save_indicator_snapshot(db, user.id, state)
            states.append(state)
    db.commit()
    return states


def _open_trades(db: Session, user_id: int, engine: str | None = None):
    q = db.query(Trade).filter_by(user_id=user_id, status="OPEN")
    if engine:
        q = q.filter_by(engine=engine)
    return q.order_by(Trade.id).all()


def _current_equity_pnl(db: Session, user, prices: dict[str, float] | None = None) -> float:
    """Stock-engine daily P&L only. Options risk is enforced independently."""
    pnl = float(getattr(user.settings, "stocks_realized_pnl", 0) or 0)
    prices = prices or {}
    for t in _open_trades(db, user.id):
        if str(getattr(t, "engine", "stocks") or "stocks") != "stocks":
            continue
        px = prices.get(t.symbol, t.entry)
        pnl += (px - t.entry) * t.qty
    return pnl


def _close_trade(db: Session, user, trade: Trade, exitp: float, reason: str):
    if trade.status != "OPEN":
        return trade
    multiplier = 100.0 if str(getattr(trade, "engine", "")) == "options" else 1.0
    leg_pnl = round((exitp - trade.entry) * trade.qty * multiplier, 2)
    pnl = round(float(trade.pnl or 0) + leg_pnl, 2)
    trade.exit = round(exitp, 4)
    trade.pnl = pnl
    trade.status = "CLOSED"
    trade.reason = reason
    trade.closed_at = datetime.now(timezone.utc)
    user.settings.realized_pnl += leg_pnl
    apply_realized_pnl(user.settings, leg_pnl, getattr(trade, "engine", "stocks"))
    db.add(Alert(user_id=user.id, title="TRADE CLOSED", body=f"{trade.symbol} {reason} | P&L ${pnl:.2f}"))
    db.commit()
    return trade


def _enforce_daily_limits(db: Session, user, prices: dict[str, float] | None = None) -> bool:
    s = user.settings
    total_pnl = _current_equity_pnl(db, user, prices)
    risk_state = capital_budget_state(db, user, prices)
    stock_base = max(0.0, float((risk_state.get("allocation") or {}).get("stocks_cap") or 0))
    loss_limit = -(stock_base * s.daily_loss_pct / 100)
    profit_target = stock_base * s.profit_target_pct / 100
    if total_pnl <= loss_limit:
        s.stocks_risk_locked = True
        s.active = False
        db.add(Alert(user_id=user.id, title="STOCK RISK LOCK", body=f"Stock daily loss limit reached at ${total_pnl:.2f}. Stock bot locked for today."))
        db.commit()
        return False
    if s.profit_target_pct > 0 and total_pnl >= profit_target:
        s.active = False
        db.add(Alert(user_id=user.id, title="PROFIT TARGET", body=f"Daily profit target reached at ${total_pnl:.2f}. Bot stopped."))
        db.commit()
        return False
    if s.trades_today >= s.max_trades:
        s.active = False
        db.add(Alert(user_id=user.id, title="MAX TRADES", body="Daily trade limit reached. Bot stopped."))
        db.commit()
        return False
    return True


def _close_all_open(db: Session, user, reason: str, engine: str | None = None):
    opens = list(_open_trades(db, user.id, engine=engine))
    if not opens:
        return
    if _real_broker_mode():
        for t in opens:
            _broker_close_trade(db, user, t, reason)
        return
    prices = {}
    if market_data.configured:
        try:
            prices = market_data.latest_prices([t.symbol for t in opens])
        except Exception:
            prices = {}
    for t in opens:
        _close_trade(db, user, t, prices.get(t.symbol, t.entry), reason)


def stop_session(db: Session, user, reason: str = "manual"):
    """Stop only the stock engine and close only Luqman stock positions.

    Options are an independent engine and must never be closed merely because the
    stock bot is stopped or reaches its scheduled stock close time.
    """
    s = user.settings
    stock_opens = _open_trades(db, user.id, engine="stocks")
    if s.active or stock_opens:
        _close_all_open(db, user, f"SESSION_{reason.upper()}", engine="stocks")
        s.active = False
        s.session_stopped_at = datetime.now(timezone.utc)
        db.add(Alert(user_id=user.id, title="STOCK BOT OFF", body=f"Stock session stopped: {reason}. Stock P&L ${float(getattr(s,'stocks_realized_pnl',0) or 0):.2f}"))
        db.commit()


def _open_exposure(db: Session, user_id: int, prices: dict[str, float] | None = None) -> float:
    prices = prices or {}
    total = 0.0
    for t in _open_trades(db, user_id):
        px = float(prices.get(t.symbol, t.entry) or t.entry or 0)
        total += max(0.0, px * float(t.qty or 0))
    return total


def capital_budget_state(db: Session, user, prices: dict[str, float] | None = None, broker_cash: float | None = None) -> dict:
    return unified_budget_state(db, user, prices, broker_cash=broker_cash)


def entry_gate_status(db: Session, user, symbol: str | None = None, entry: float | None = None) -> dict:
    """Explain whether a new entry is currently allowed, without sending an order."""
    s = user.settings
    if not s.active:
        return {"ok": False, "code": "BOT_INACTIVE", "message_ar": "البوت غير نشط", "message_en": "Bot is inactive"}
    if s.locked or bool(getattr(s, "stocks_risk_locked", False)):
        return {"ok": False, "code": "RISK_LOCKED", "message_ar": "بوت الأسهم مقفل بسبب حدود مخاطر الأسهم", "message_en": "Stock risk lock is active"}
    if bool(getattr(s, "broker_reconciliation_required", False)):
        return {"ok": False, "code": "BROKER_RECONCILIATION_REQUIRED", "message_ar": "يوجد اختلاف غير محلول بين Luqman وAlpaca", "message_en": "Unresolved Luqman/Alpaca position mismatch"}
    if settings.enforce_market_hours and not market_is_open():
        return {"ok": False, "code": "MARKET_CLOSED", "message_ar": "السوق مغلق", "message_en": "Market is closed"}
    if s.trades_today >= s.max_trades:
        return {"ok": False, "code": "MAX_DAILY_TRADES", "message_ar": "تم الوصول للحد الأقصى للصفقات اليومية", "message_en": "Maximum daily trades reached"}
    opens = _open_trades(db, user.id)
    if symbol and any(t.symbol == symbol for t in opens):
        return {"ok": False, "code": "SYMBOL_ALREADY_OPEN", "message_ar": "يوجد مركز مفتوح على نفس السهم", "message_en": "This symbol already has an open position"}
    max_open = max(1, min(20, int(getattr(s, "max_open_positions_user", 3) or 3)))
    if len(opens) >= max_open:
        return {"ok": False, "code": "MAX_OPEN_POSITIONS", "message_ar": f"تم الوصول للحد الأقصى للصفقات المفتوحة ({len(opens)}/{max_open})", "message_en": f"Maximum open positions reached ({len(opens)}/{max_open})"}
    if s.last_trade_at:
        last = s.last_trade_at.replace(tzinfo=timezone.utc) if s.last_trade_at.tzinfo is None else s.last_trade_at
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        cooldown = max(0, int(getattr(s, "trade_cooldown_seconds", settings.min_seconds_between_trades) or 0))
        remaining = max(0, int(cooldown - elapsed))
        if remaining > 0:
            mm, ss = divmod(remaining, 60)
            return {"ok": False, "code": "COOLDOWN", "remaining_seconds": remaining, "message_ar": f"فترة انتظار قبل الصفقة التالية: {mm}:{ss:02d}", "message_en": f"Trade cooldown: {mm}:{ss:02d} remaining"}
    budget = capital_budget_state(db, user)
    stock_available = float((budget.get("allocation") or {}).get("stocks_available") or 0)
    if budget["available"] <= 0 or stock_available <= 0:
        return {"ok": False, "code": "NO_STOCK_ALLOCATION", "message_ar": "لا توجد ميزانية متاحة للأسهم بعد الاحتياطي وخطة التوزيع", "message_en": "No stock allocation is available after reserve and the selected capital plan", "budget": budget}
    if entry and entry > min(float(budget["available"]), stock_available) and not bool(getattr(s, "allow_fractional", False)):
        return {"ok": False, "code": "INSUFFICIENT_AVAILABLE_CAPITAL", "message_ar": "رأس المال المتاح للأسهم لا يكفي لسهم كامل بهذه الخطة", "message_en": "Available stock allocation is insufficient for one whole share", "budget": budget}
    return {"ok": True, "code": "READY", "message_ar": "جاهز للتداول عند اكتمال شروط الإشارة", "message_en": "Ready to trade when signal conditions qualify", "budget": budget}


def _can_enter(db: Session, user, symbol: str | None = None, entry: float | None = None) -> bool:
    return bool(entry_gate_status(db, user, symbol=symbol, entry=entry).get("ok"))


def _size_position(db: Session, user, entry: float, stop: float) -> float:
    s = user.settings
    ensure_capital_account(s)
    budget = capital_budget_state(db, user)
    alloc = budget.get("allocation") or {}
    stock_available = float(alloc.get("stocks_available") or 0)
    stock_bucket = float(alloc.get("stocks_cap") or 0)
    risk_budget = stock_bucket * (s.risk_per_trade_pct / 100)
    per_share_risk = max(entry - stop, entry * 0.001, 0.01)
    risk_qty = risk_budget / per_share_risk if risk_budget > 0 else 0
    stock_position_cap = stock_bucket * (min(100.0,max(1.0,float(getattr(s,"max_position_allocation_pct",30) or 30)))/100)
    dollar_cap = min(stock_position_cap, budget["available"], stock_available)
    capital_qty = dollar_cap / max(entry, 0.01)
    raw_qty = min(risk_qty, capital_qty)
    if _real_broker_mode():
        if bool(getattr(s, "allow_fractional", False)):
            return round(max(raw_qty, 0.0), 6)
        return float(max(0, math.floor(raw_qty)))
    return round(max(raw_qty, 0.0001), 4) if raw_qty > 0 else 0.0


def _apply_exit_plan(user, signal):
    s = user.settings
    entry = float(signal.price)
    # In broker-native stock trailing mode, the real broker protection distance is
    # also the risk-sizing distance. This prevents Luqman from sizing on a tighter
    # synthetic stop than the order actually resting at Alpaca.
    if str(getattr(s, "stocks_exit_mode", "trailing") or "trailing") == "trailing":
        trail = max(0.1, min(20.0, float(getattr(s, "stocks_trailing_distance_pct", 1.0) or 1.0)))
        signal.stop = round(entry * (1 - trail / 100), 2)
        return signal
    mode = str(getattr(s, "stop_target_mode", "atr") or "atr")
    if mode == "fixed_pct":
        sl = max(0.1, float(getattr(s, "stop_loss_value", 1.0) or 1.0))
        tp = max(0.1, float(getattr(s, "take_profit_value", 2.0) or 2.0))
        signal.stop = round(entry * (1 - sl / 100), 2)
        signal.target = round(entry * (1 + tp / 100), 2)
    elif mode == "risk_reward":
        risk = max(entry - float(signal.stop), entry * 0.001)
        rr = max(0.5, float(getattr(s, "risk_reward_ratio", 2.0) or 2.0))
        signal.target = round(entry + risk * rr, 2)
    return signal

def _open_from_signal(db: Session, user, signal, engine: str = "stocks"):
    signal = _apply_exit_plan(user, signal)
    gate = entry_gate_status(db, user, symbol=signal.symbol, entry=signal.price)
    if not gate.get("ok"):
        db.add(Alert(user_id=user.id, title="ENTRY BLOCKED", body=f"{signal.symbol}: {gate.get('message_en') or gate.get('code')}"))
        db.commit()
        return None
    qty = _size_position(db, user, signal.price, signal.stop)
    fractional = _real_broker_mode() and qty > 0 and abs(qty - int(qty)) > 1e-9
    if fractional and qty * float(signal.price) < 1.0:
        db.add(Alert(user_id=user.id, title="NO ORDER", body=f"{signal.symbol}: fractional order value is below the $1 minimum guard."))
        db.commit(); return None
    if fractional and not bool(getattr(user.settings, "allow_fractional", False)):
        db.add(Alert(user_id=user.id, title="NO ORDER", body=f"{signal.symbol}: fractional shares are disabled."))
        db.commit(); return None
    if qty <= 0:
        db.add(Alert(user_id=user.id, title="NO ORDER", body=f"{signal.symbol}: no risk-adjusted buying capacity."))
        db.commit(); return None

    estimated_cost = round(float(qty) * float(signal.price), 8)
    reservation = None
    try:
        reservation = reserve_capital(db, user, estimated_cost, engine, signal.symbol)
        db.commit()  # make the reservation visible before the broker request
    except ValueError as exc:
        db.rollback()
        db.add(Alert(user_id=user.id, title="ENTRY BLOCKED", body=f"{signal.symbol}: {exc}"))
        db.commit()
        return None

    indicator_payload = signal.to_dict()
    if hasattr(signal, "custom_indicators"):
        indicator_payload["custom_indicators"] = getattr(signal, "custom_indicators")
    indicator_payload["capital_engine"] = engine
    indicator_payload["reserved_amount"] = estimated_cost
    reason = "MULTI_INDICATOR_ENTRY"
    data_source = "SIMULATOR"
    entry_price = signal.price

    try:
        order = None
        if _real_broker_mode():
            client_order_id = f"luqman-u{user.id}-{signal.symbol}-{int(_time.time())}"
            if fractional:
                if not alpaca_broker.is_fractionable(signal.symbol):
                    raise ValueError(f"{signal.symbol}: Alpaca marks this asset as non-fractionable")
                order = alpaca_broker.submit_fractional_market_buy(symbol=signal.symbol, qty=qty, client_order_id=client_order_id)
                terminal = alpaca_broker.wait_for_terminal_order(order["id"], timeout_seconds=8)
                if terminal.get("status") != "filled" or not terminal.get("filled_avg_price"):
                    try:
                        alpaca_broker.cancel_order(order["id"])
                    except Exception:
                        pass
                    raise ValueError(f"fractional order not filled ({terminal.get('status')})")
                filled_qty = float(terminal.get("filled_qty") or qty)
                if filled_qty <= 0:
                    raise ValueError("fractional order returned zero filled quantity")
                qty = round(filled_qty, 6)
                entry_price = float(terminal["filled_avg_price"])
                order = terminal
                # Fractional OCO/bracket support is not assumed. Arm a single
                # broker-side DAY stop after the actual fractional fill instead.
                try:
                    stop_order = alpaca_broker.submit_fractional_stop_sell(
                        symbol=signal.symbol, qty=qty, stop_price=float(signal.stop),
                        client_order_id=f"luqman-frac-stop-u{user.id}-{signal.symbol}-{int(_time.time())}",
                    )
                    stop_order = alpaca_broker.confirm_open_order(str(stop_order["id"]), timeout_seconds=4)
                except Exception:
                    # Entry filled but protection failed: do not leave an intentionally
                    # unprotected fractional position. Request an immediate broker close.
                    emergency = alpaca_broker.close_position(signal.symbol, qty=qty)
                    if emergency and emergency.get("id"):
                        alpaca_broker.wait_for_terminal_order(str(emergency["id"]), timeout_seconds=8)
                    raise
                indicator_payload["protection_mode"] = "broker-stop+luqman-target"
                indicator_payload["broker_protection_confirmed"] = True
                indicator_payload["fractional_stop_order_id"] = stop_order.get("id")
                indicator_payload["fractional_stop_status"] = stop_order.get("status")
                reason = "ALPACA_FRACTIONAL_MARKET_SUBMITTED"
            else:
                stock_exit_mode = str(getattr(user.settings, "stocks_exit_mode", "trailing") or "trailing")
                if stock_exit_mode == "trailing":
                    trail_pct = max(0.1, min(20.0, float(getattr(user.settings, "stocks_trailing_distance_pct", 1.0) or 1.0)))
                    order = alpaca_broker.submit_market_buy(symbol=signal.symbol, qty=int(qty), client_order_id=client_order_id)
                    terminal = alpaca_broker.wait_for_terminal_order(str(order["id"]), timeout_seconds=8)
                    status = str(terminal.get("status") or order.get("status") or "").lower()
                    filled_qty = float(terminal.get("filled_qty") or 0)
                    fill_price = float(terminal.get("filled_avg_price") or 0)
                    if status != "filled" or filled_qty <= 0 or fill_price <= 0:
                        try: alpaca_broker.cancel_order(str(order.get("id") or ""))
                        finally: raise ValueError(f"stock entry not fully filled ({status or 'unknown'})")
                    if abs(filled_qty - round(filled_qty)) > 1e-9:
                        raise ValueError("whole-share trailing entry returned a fractional fill quantity")
                    qty = round(filled_qty, 8)
                    entry_price = fill_price
                    # Record an initial estimated stop for UI/risk reporting. Alpaca is
                    # authoritative for the live trailing stop_price/high-water mark.
                    signal.stop = round(entry_price * (1 - trail_pct / 100), 2)
                    try:
                        trailing = alpaca_broker.submit_trailing_stop_sell(
                            symbol=signal.symbol, qty=qty, trail_percent=trail_pct,
                            client_order_id=f"luqman-stock-trail-u{user.id}-{signal.symbol}-{int(_time.time())}",
                            time_in_force="gtc",
                        )
                        trailing = alpaca_broker.confirm_open_order(str(trailing["id"]), timeout_seconds=4)
                    except Exception:
                        # The entry already filled. Fail safe by liquidating rather than
                        # intentionally leaving a whole-share position unprotected.
                        emergency = alpaca_broker.close_position(signal.symbol, qty=qty)
                        if emergency and emergency.get("id"):
                            alpaca_broker.wait_for_terminal_order(str(emergency["id"]), timeout_seconds=8)
                        raise
                    order = terminal
                    indicator_payload["stock_trailing_order_id"] = trailing.get("id")
                    indicator_payload["stock_trailing_status"] = trailing.get("status")
                    indicator_payload["stock_trailing_percent"] = trail_pct
                    indicator_payload["stock_trailing_hwm"] = trailing.get("hwm")
                    indicator_payload["stock_trailing_stop_price"] = trailing.get("stop_price")
                    indicator_payload["protection_mode"] = "alpaca-native-trailing"
                    indicator_payload["broker_protection_confirmed"] = True
                    reason = "ALPACA_STOCK_TRAILING_PROTECTED"
                else:
                    order = alpaca_broker.submit_bracket_buy(
                        symbol=signal.symbol, qty=int(qty), take_profit=signal.target, stop_loss=signal.stop, client_order_id=client_order_id,
                    )
                    try:
                        order = alpaca_broker.confirm_bracket_protection(str(order["id"]), timeout_seconds=4)
                        terminal = alpaca_broker.wait_for_terminal_order(str(order["id"]), timeout_seconds=8)
                    except Exception:
                        try: alpaca_broker.cancel_order(str(order.get("id") or ""))
                        finally: raise
                    status = str(terminal.get("status") or order.get("status") or "").lower()
                    filled_qty = float(terminal.get("filled_qty") or 0)
                    fill_price = float(terminal.get("filled_avg_price") or 0)
                    if status != "filled" or filled_qty <= 0 or fill_price <= 0:
                        try: alpaca_broker.cancel_order(str(order.get("id") or ""))
                        finally: raise ValueError(f"bracket entry not fully filled ({status or 'unknown'})")
                    qty = round(filled_qty, 8)
                    entry_price = fill_price
                    order = terminal
                    indicator_payload["protection_mode"] = "broker-side"
                    indicator_payload["broker_protection_confirmed"] = True
                    reason = "ALPACA_BRACKET_PROTECTED"
            indicator_payload["broker_order_id"] = order.get("id")
            indicator_payload["client_order_id"] = order.get("client_order_id") or client_order_id
            indicator_payload["broker_status"] = order.get("status")
            if order.get("filled_avg_price"):
                entry_price = float(order["filled_avg_price"])
            data_source = "ALPACA_PAPER_ORDER"

        trade = Trade(
            user_id=user.id, symbol=signal.symbol, side="BUY", engine=engine, qty=qty, entry=entry_price,
            stop_loss=signal.stop, take_profit=(None if (engine == "stocks" and not fractional and str(getattr(user.settings, "stocks_exit_mode", "trailing") or "trailing") == "trailing") else signal.target), signal_score=signal.score,
            indicators=json.dumps(indicator_payload, ensure_ascii=False), pnl=0, status="OPEN",
            reason=reason, data_source=data_source,
        )
        db.add(trade)
        db.flush()
        commit_reservation(db, reservation, trade.id)
        user.settings.trades_today += 1
        user.settings.last_trade_at = datetime.now(timezone.utc)
        title = "ALPACA PAPER ORDER" if _real_broker_mode() else "PAPER ENTRY"
        if engine == "stocks" and not fractional and str(getattr(user.settings, "stocks_exit_mode", "trailing") or "trailing") == "trailing":
            trail_pct = float(getattr(user.settings, "stocks_trailing_distance_pct", 1.0) or 1.0)
            body = f"{signal.symbol} BUY x{qty:g} @ ~${entry_price:.2f} | Alpaca Trailing {trail_pct:.2f}% | Score {signal.score:.0f}"
        else:
            body = f"{signal.symbol} BUY x{qty:g} @ ~${entry_price:.2f} | Stop ${signal.stop:.2f} | Target ${signal.target:.2f} | Score {signal.score:.0f}"
        db.add(Alert(user_id=user.id, title=title, body=body))
        db.commit()
        return trade
    except Exception as exc:
        db.rollback()
        # Re-fetch after rollback because the original reservation instance may be expired.
        from .models import CapitalReservation
        r = db.get(CapitalReservation, reservation.id) if reservation and reservation.id else None
        release_reservation(db, r, f"order_failed:{type(exc).__name__}")
        db.add(Alert(user_id=user.id, title="ORDER ERROR", body=f"{signal.symbol}: {type(exc).__name__}: {str(exc)[:180]}"))
        db.commit()
        return None


def _broker_order_id(trade: Trade) -> str | None:
    try:
        payload = json.loads(trade.indicators or "{}")
        return payload.get("broker_order_id")
    except Exception:
        return None


def _void_trade(db: Session, user, trade: Trade, reason: str):
    trade.status = "CLOSED"
    trade.reason = reason
    trade.pnl = 0
    trade.closed_at = datetime.now(timezone.utc)
    db.add(Alert(user_id=user.id, title="ORDER NOT FILLED", body=f"{trade.symbol}: {reason}"))
    db.commit()


def _stock_trailing_order_id(trade: Trade) -> str | None:
    try:
        return (json.loads(trade.indicators or "{}") or {}).get("stock_trailing_order_id")
    except Exception:
        return None

def _cancel_stock_trailing(trade: Trade, *, wait: bool = True):
    oid = _stock_trailing_order_id(trade)
    if not oid:
        return
    alpaca_broker.cancel_order(str(oid))
    if wait:
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline:
            o = alpaca_broker.get_order(str(oid), nested=True)
            if str(o.get("status") or "").lower() in {"canceled", "expired", "rejected", "filled", "done_for_day"}:
                return
            _time.sleep(0.25)
        raise RuntimeError("stock trailing stop cancellation was not confirmed")

def _sync_stock_trailing(db: Session, user, trade: Trade) -> bool:
    oid = _stock_trailing_order_id(trade)
    if not oid:
        return False
    order = alpaca_broker.get_order(str(oid), nested=True)
    status = str(order.get("status") or "").lower()
    payload = _manual_close_payload(trade)
    payload["stock_trailing_status"] = status
    if order.get("hwm") is not None:
        payload["stock_trailing_hwm"] = order.get("hwm")
    if order.get("stop_price") is not None:
        payload["stock_trailing_stop_price"] = order.get("stop_price")
        try:
            trade.stop_loss = float(order.get("stop_price"))
        except Exception:
            pass
    trade.indicators = json.dumps(payload, ensure_ascii=False)
    if status == "filled" and order.get("filled_avg_price"):
        _close_trade(db, user, trade, float(order["filled_avg_price"]), "ALPACA_STOCK_TRAILING_STOP")
        return True
    if status in {"canceled", "expired", "rejected", "done_for_day"}:
        payload["broker_protection_confirmed"] = False
        trade.indicators = json.dumps(payload, ensure_ascii=False)
        user.settings.broker_reconciliation_required = True
        db.add(Alert(user_id=user.id, title="PROTECTION WARNING", body=f"{trade.symbol}: Alpaca trailing protection is {status}; new entries are blocked until reconciled."))
    db.commit()
    return False

def _fractional_stop_order_id(trade: Trade) -> str | None:
    try:
        return (json.loads(trade.indicators or "{}") or {}).get("fractional_stop_order_id")
    except Exception:
        return None

def _cancel_fractional_stop(trade: Trade, *, wait: bool = True):
    oid = _fractional_stop_order_id(trade)
    if not oid:
        return
    alpaca_broker.cancel_order(str(oid))
    if wait:
        deadline = _time.monotonic() + 4.0
        while _time.monotonic() < deadline:
            o = alpaca_broker.get_order(str(oid), nested=True)
            if str(o.get("status") or "").lower() in {"canceled", "expired", "rejected", "filled", "done_for_day"}:
                return
            _time.sleep(0.25)
        raise RuntimeError("fractional protective stop cancellation was not confirmed")

def _sync_fractional_stop(db: Session, user, trade: Trade) -> bool:
    oid = _fractional_stop_order_id(trade)
    if not oid:
        return False
    order = alpaca_broker.get_order(str(oid), nested=True)
    status = str(order.get("status") or "").lower()
    if status == "filled" and order.get("filled_avg_price"):
        _close_trade(db, user, trade, float(order["filled_avg_price"]), "ALPACA_FRACTIONAL_STOP_LOSS")
        return True
    if status in {"canceled", "expired", "rejected", "done_for_day"}:
        payload = _manual_close_payload(trade)
        payload["broker_protection_confirmed"] = False
        payload["fractional_stop_status"] = status
        trade.indicators = json.dumps(payload, ensure_ascii=False)
        db.add(Alert(user_id=user.id, title="PROTECTION WARNING", body=f"{trade.symbol}: fractional broker stop is {status}; protection must be re-armed before relying on it."))
        db.commit()
    return False

def _sync_broker_trade(db: Session, user, trade: Trade):
    order_id = _broker_order_id(trade)
    if not order_id:
        return
    order = alpaca_broker.get_order(order_id, nested=True)
    status = str(order.get("status") or "")
    if order.get("filled_avg_price"):
        trade.entry = float(order["filled_avg_price"])

    # Bracket child that fills represents the actual exit executed by Alpaca.
    for leg in order.get("legs") or []:
        if leg.get("status") == "filled" and leg.get("filled_avg_price"):
            exitp = float(leg["filled_avg_price"])
            leg_type = str(leg.get("type") or "").lower()
            reason = "TAKE_PROFIT" if leg_type == "limit" else "STOP_LOSS"
            _close_trade(db, user, trade, exitp, f"ALPACA_{reason}")
            return

    try:
        filled_qty = float(order.get("filled_qty") or 0)
    except (TypeError, ValueError):
        filled_qty = 0
    if status in {"canceled", "expired", "rejected"} and filled_qty <= 0:
        _void_trade(db, user, trade, f"ALPACA_{status.upper()}")
        return
    db.commit()


def _manage_fractional_broker_exits(db: Session, user, prices: dict[str, float]):
    for t in list(_open_trades(db, user.id)):
        if t.reason != "ALPACA_FRACTIONAL_MARKET_SUBMITTED":
            continue
        px = float(prices.get(t.symbol, 0) or 0)
        if not px: continue
        if t.stop_loss and px <= t.stop_loss:
            _broker_close_trade(db, user, t, "FRACTIONAL_STOP_LOSS")
        elif t.take_profit and px >= t.take_profit:
            _broker_close_trade(db, user, t, "FRACTIONAL_TAKE_PROFIT")


def _sync_all_broker_trades(db: Session, user):
    for trade in list(_open_trades(db, user.id)):
        try:
            if _sync_pending_manual_close(db, user, trade):
                continue
            if _sync_stock_trailing(db, user, trade):
                continue
            if _sync_fractional_stop(db, user, trade):
                continue
            _sync_broker_trade(db, user, trade)
        except Exception as exc:
            db.rollback()
            db.add(Alert(user_id=user.id, title="BROKER SYNC ERROR", body=f"{trade.symbol}: {type(exc).__name__}"))
            db.commit()



def _parse_broker_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _flatten_orders(rows):
    out=[]
    for o in rows or []:
        out.append(o)
        out.extend(list(o.get("legs") or []))
    return out


def reconcile_broker_state(db: Session, user) -> tuple[bool, list[str]]:
    """Reconcile local open trades against Alpaca before new trading is allowed.

    Alpaca is authoritative for actual fills/positions. Luqman only auto-applies a
    missing/reduced local position when a matching filled SELL after the local entry
    is available. Otherwise it raises a persistent mismatch block instead of guessing.
    """
    if not _real_broker_mode() or not alpaca_broker.configured:
        user.settings.broker_reconciliation_required = False
        return True, []
    try:
        # Release any checked-out connection before external HTTP waits.
        db.commit()
        positions = alpaca_broker.positions()
        orders = alpaca_broker.orders(status="all", limit=200, nested=True)
    except Exception as exc:
        user.settings.broker_reconciliation_required = True
        db.add(Alert(user_id=user.id,title="BROKER RECONCILIATION ERROR",body=f"Alpaca reconciliation unavailable: {type(exc).__name__}"))
        db.commit()
        return False, [f"broker unavailable: {type(exc).__name__}"]

    try:
        _sync_all_broker_trades(db, user)
    except Exception:
        db.rollback()
    local = db.query(Trade).filter_by(user_id=user.id,status="OPEN").all()
    pmap = {str(p.get("symbol") or "").upper(): p for p in positions}
    flat = _flatten_orders(orders)
    issues=[]

    def matching_sells(t):
        opened=t.opened_at
        if opened and opened.tzinfo is None: opened=opened.replace(tzinfo=timezone.utc)
        found=[]
        for o in flat:
            if str(o.get("symbol") or "").upper()!=t.symbol.upper(): continue
            if str(o.get("side") or "").lower()!="sell" or str(o.get("status") or "").lower()!="filled": continue
            when=_parse_broker_time(o.get("filled_at") or o.get("updated_at"))
            if opened and when and when < opened: continue
            if opened and when is None: continue
            try:
                fq=float(o.get("filled_qty") or 0); fp=float(o.get("filled_avg_price") or 0)
            except Exception:
                continue
            if fq>0 and fp>0: found.append((when or datetime.now(timezone.utc),fq,fp,str(o.get("id") or "")))
        found.sort(key=lambda x:x[0], reverse=True)
        return found

    for t in list(local):
        bp=pmap.get(t.symbol.upper())
        bqty=abs(float((bp or {}).get("qty") or 0))
        lqty=float(t.qty or 0)
        if abs(bqty-lqty) <= 1e-6:
            continue
        sells=matching_sells(t)
        diff=max(0.0,lqty-bqty)
        evidence=next((x for x in sells if abs(x[1]-diff)<=1e-6),None) if diff>0 else None
        if evidence:
            _,fq,fp,oid=evidence
            _apply_manual_close_fill(db,user,t,fq,fp,oid or None)
            continue
        issues.append(f"{t.symbol}: local={lqty:g}, broker={bqty:g}")
        db.add(Alert(user_id=user.id,title="POSITION MISMATCH",body=f"{t.symbol}: Luqman qty {lqty:g} vs Alpaca qty {bqty:g}; no unambiguous matching fill was found."))

    local_symbols={t.symbol.upper() for t in db.query(Trade).filter_by(user_id=user.id,status="OPEN").all()}
    for sym,p in pmap.items():
        if sym not in local_symbols and abs(float(p.get("qty") or 0))>1e-9:
            # Never adopt outside exposure silently. It may be a manual Alpaca position.
            issues.append(f"{sym}: broker-only position")
            db.add(Alert(user_id=user.id,title="POSITION MISMATCH",body=f"{sym}: Alpaca has a position not owned by an open Luqman trade."))
    user.settings.broker_reconciliation_required = bool(issues)
    db.commit()
    return not issues, issues

def sync_broker_trades(db: Session, user):
    """Reconcile Luqman open trades with Alpaca order state.

    This is intentionally lightweight and safe to run frequently. It updates fills,
    bracket exits, pending manual closes, quantities and ledger P&L without running
    the strategy or creating new entries.
    """
    if not _real_broker_mode() or not alpaca_broker.configured:
        return
    _sync_all_broker_trades(db, user)


def _broker_close_trade(db: Session, user, trade: Trade, reason: str):
    # A broker-native stock trailing order must be canceled before another Luqman
    # liquidation is submitted, otherwise two independent SELL paths can race.
    if _stock_trailing_order_id(trade):
        try:
            _cancel_stock_trailing(trade, wait=True)
        except Exception as exc:
            db.add(Alert(user_id=user.id, title="BROKER CLOSE ERROR", body=f"{trade.symbol}: trailing stop cancellation not confirmed ({type(exc).__name__})"))
            db.commit()
            return
    # A Luqman-managed target/manual risk exit must never race the independent
    # fractional broker stop. Cancel and confirm that stop first.
    if _fractional_stop_order_id(trade):
        try:
            _cancel_fractional_stop(trade, wait=True)
        except Exception as exc:
            db.add(Alert(user_id=user.id, title="BROKER CLOSE ERROR", body=f"{trade.symbol}: protective stop cancellation not confirmed ({type(exc).__name__})"))
            db.commit()
            return
    order_id = _broker_order_id(trade)
    if order_id:
        try:
            alpaca_broker.cancel_order(order_id)
        except Exception:
            pass
    try:
        close_order = alpaca_broker.close_position(trade.symbol)
    except Exception as exc:
        db.add(Alert(user_id=user.id, title="BROKER CLOSE ERROR", body=f"{trade.symbol}: {type(exc).__name__}"))
        db.commit()
        return
    if close_order and close_order.get("id"):
        try:
            terminal = alpaca_broker.wait_for_terminal_order(close_order["id"], timeout_seconds=8)
            exitp = float(terminal.get("filled_avg_price") or 0)
        except Exception:
            exitp = 0
        if exitp > 0:
            _close_trade(db, user, trade, exitp, f"ALPACA_{reason}")
            return
    # If broker reports no position (e.g. an exit leg already filled), sync once.
    try:
        _sync_broker_trade(db, user, trade)
    except Exception:
        pass


def _manage_open_positions(db: Session, user, prices: dict[str, float]):
    closed = []
    for t in _open_trades(db, user.id):
        px = prices.get(t.symbol)
        if px is None:
            continue
        if t.stop_loss is not None and px <= t.stop_loss:
            closed.append(_close_trade(db, user, t, px, "STOP_LOSS"))
        elif t.take_profit is not None and px >= t.take_profit:
            closed.append(_close_trade(db, user, t, px, "TAKE_PROFIT"))
    return closed


def simulate_trade(db: Session, user, symbol: str | None = None):
    if not user.settings.active or user.settings.locked or bool(getattr(user.settings, "stocks_risk_locked", False)):
        return None
    symbols = allowed_symbols(db, user.id)
    if not symbols:
        return None
    if symbol not in symbols:
        symbol = symbols[(user.settings.trades_today + len(_open_trades(db, user.id))) % len(symbols)]
    seed = (user.id * 17 + user.settings.trades_today * 13 + sum(map(ord, symbol))) % 100
    entry = 50 + seed / 2
    stop = entry * 0.995
    target = entry * 1.009
    signal_like = type("Sig", (), {"symbol": symbol, "price": entry, "stop": stop, "target": target, "score": 75.0, "to_dict": lambda self: {"demo": True}})()
    if not _open_trades(db, user.id):
        return _open_from_signal(db, user, signal_like)
    t = _open_trades(db, user.id)[0]
    move = ((seed % 9) - 4) / 1000
    closed = _close_trade(db, user, t, t.entry * (1 + move), "SIMULATOR_EXIT")
    _enforce_daily_limits(db, user)
    return closed


def market_paper_cycle(db: Session, user):
    if not user.settings.active:
        return None
    symbols = allowed_symbols(db, user.id)
    if not symbols:
        stop_session(db, user, "no allowed stocks")
        return None

    if settings.enforce_market_hours and not market_is_open():
        stop_session(db, user, "market closed")
        return None
    if settings.enforce_market_hours and market_closing_soon():
        stop_session(db, user, "end of day")
        return None

    if _real_broker_mode():
        _sync_all_broker_trades(db, user)
        record_portfolio_snapshot(db, user)
    prices = {}
    try:
        prices = market_data.latest_prices(symbols)
    except Exception:
        pass
    if _real_broker_mode():
        _manage_fractional_broker_exits(db, user, prices)
    if not _real_broker_mode():
        _manage_open_positions(db, user, prices)
    if not _enforce_daily_limits(db, user, prices):
        if user.settings.locked or bool(getattr(user.settings, "stocks_risk_locked", False)):
            _close_all_open(db, user, "RISK_LIMIT")
        return None

    bars_by_symbol = market_data.recent_bars(symbols)
    refresh_indicator_snapshots(db, user, symbols, bars_by_symbol)
    if not _can_enter(db, user):
        return None

    signals = []
    for symbol, bars in bars_by_symbol.items():
        signal = analyze_symbol(symbol, bars, settings.min_signal_score)
        if signal:
            signal, custom_results = apply_custom_indicator_rules(db, user, symbol, bars, signal)
            if signal:
                setattr(signal, "custom_indicators", custom_results)
                signals.append(signal)
    if not signals:
        return None
    # Prefer the best eligible symbol, skipping symbols already open.
    opens = {t.symbol for t in _open_trades(db, user.id)}
    signals = [sig for sig in signals if sig.symbol not in opens]
    if not signals:
        return None
    best = max(signals, key=lambda s: (s.score, s.rel_volume, s.momentum_5))
    return _open_from_signal(db, user, best)


def run_user_cycle(db: Session, user):
    reset_for_new_day(user.settings)
    db.commit()
    if settings.broker_mode == "alpaca_market_paper" and market_data.configured:
        return market_paper_cycle(db, user)
    return simulate_trade(db, user)


def close_user_position(db: Session, user, symbol: str):
    symbol = symbol.upper().strip()
    trade = db.query(Trade).filter_by(user_id=user.id, symbol=symbol, status="OPEN").order_by(Trade.id.desc()).first()
    if not trade:
        raise ValueError("No open Luqman Trade position for this symbol")
    if _real_broker_mode():
        _broker_close_trade(db, user, trade, "USER_MANUAL_CLOSE")
    else:
        px = market_data.latest_prices([symbol]).get(symbol, trade.entry) if market_data.configured else trade.entry
        _close_trade(db, user, trade, px, "USER_MANUAL_CLOSE")
    return trade



def close_user_position_partial(db: Session, user, symbol: str, percentage: float | None = None, qty: float | None = None):
    """Close all or part of a Luqman-managed position.

    In Alpaca Paper mode this distinguishes between a fill and an accepted/pending
    broker order. A pending order is never reported to the user as a failure.
    Explicit equity exits can use Alpaca extended-hours limit orders outside the
    regular session.
    """
    symbol = symbol.upper().strip()
    trade = db.query(Trade).filter_by(user_id=user.id, symbol=symbol, status="OPEN").order_by(Trade.id.desc()).first()
    if not trade:
        raise ValueError("No open Luqman Trade position for this symbol")
    if qty is not None:
        close_qty = float(qty or 0)
    elif percentage is not None:
        # Backward-compatible server path for old clients only. The current UI always
        # submits an explicit quantity so there is no ambiguity between qty and %.
        percentage = max(0.01, min(100.0, float(percentage)))
        close_qty = float(trade.qty) * percentage / 100.0
    else:
        close_qty = float(trade.qty)
    if close_qty <= 0:
        raise ValueError("Close quantity must be positive")
    if close_qty > float(trade.qty) + 1e-9:
        raise ValueError(f"Close quantity exceeds Luqman open quantity ({trade.qty:g})")
    if str(getattr(trade, "engine", "")) == "options":
        close_qty = float(max(1, min(int(float(trade.qty)), int(math.floor(close_qty + 1e-9)))))
    if not _real_broker_mode():
        px = market_data.latest_prices([symbol]).get(symbol, trade.entry) if market_data.configured else trade.entry
        if close_qty >= float(trade.qty) - 1e-9:
            result = _close_trade(db, user, trade, px, "USER_MANUAL_CLOSE")
        else:
            multiplier = 100.0 if str(getattr(trade, "engine", "")) == "options" else 1.0
            realized = round((px - trade.entry) * close_qty * multiplier, 2)
            trade.qty = round(float(trade.qty) - close_qty, 8)
            trade.pnl += realized
            user.settings.realized_pnl += realized
            apply_realized_pnl(user.settings, realized, getattr(trade, "engine", "stocks"))
            db.add(Alert(user_id=user.id, title="PARTIAL CLOSE", body=f"{symbol} x{close_qty:g} | P&L ${realized:.2f}"))
            db.commit(); result = trade
        setattr(result, "_close_submission_status", "filled")
        return result

    # If an earlier manual close is still pending, avoid duplicate sell orders.
    payload = _manual_close_payload(trade)
    pending = payload.get("pending_manual_close") or {}
    if pending.get("order_id"):
        setattr(trade, "_close_submission_status", "pending")
        setattr(trade, "_close_submission_message", "A manual close order is already pending at Alpaca")
        return trade

    # Broker position is authoritative for the quantity we are allowed to close.
    broker_positions = {str(p.get("symbol") or "").upper(): p for p in alpaca_broker.positions()}
    broker_pos = broker_positions.get(symbol)
    if not broker_pos:
        raise ValueError("Alpaca reports no open position for this symbol")
    broker_qty = abs(float(broker_pos.get("qty") or 0))
    if close_qty > broker_qty + 1e-9:
        raise ValueError(f"Close quantity exceeds Alpaca position ({broker_qty:g})")

    alpaca_broker.cancel_open_orders_for_symbol(symbol)
    if hasattr(alpaca_broker, "wait_for_no_open_orders") and not alpaca_broker.wait_for_no_open_orders(symbol, timeout_seconds=4):
        raise ValueError("Protective orders are still open at Alpaca; close request was not sent")
    phase = market_phase()
    order = None
    mode = "regular_market"
    if str(getattr(trade, "engine", "stocks") or "stocks") == "stocks" and phase in {"premarket","afterhours","overnight"}:
        # Outside regular hours, use a marketable limit order so Alpaca can execute
        # it in supported extended sessions. If a reliable quote is unavailable,
        # fall back to Alpaca's normal close endpoint, which may queue the order.
        q = {}
        for feed in [settings.alpaca_data_feed, "overnight"]:
            try:
                q = market_data.latest_quotes([symbol], feed=feed).get(symbol) or {}
                if q:
                    break
            except Exception:
                q = {}
        bid = float(q.get("bp") or 0); ask = float(q.get("ap") or 0)
        reference = bid or ask
        if reference > 0:
            limit_price = max(0.01, round(reference * 0.998, 2))
            order = alpaca_broker.submit_extended_hours_sell(
                symbol=symbol, qty=close_qty, limit_price=limit_price,
                client_order_id=f"luqman-manual-exit-u{user.id}-{symbol}-{int(_time.time())}",
                time_in_force="day",
            )
            mode = f"extended_{phase}"
        else:
            order = alpaca_broker.close_position(symbol, qty=close_qty)
            mode = "broker_queued_close"
    else:
        # Regular session uses Alpaca position liquidation. Fully closed periods
        # (e.g. weekend) are still accepted as a user request and may queue at the broker.
        order = alpaca_broker.close_position(symbol, qty=close_qty)
        if phase == "closed":
            mode = "broker_queued_close"

    if not order or not order.get("id"):
        raise ValueError("Broker did not accept the close order")
    try:
        accepted_qty = float(order.get("qty") or close_qty)
    except (TypeError, ValueError):
        accepted_qty = close_qty
    if abs(accepted_qty - close_qty) > 1e-6:
        try:
            alpaca_broker.cancel_order(str(order["id"]))
        finally:
            raise ValueError(f"Broker order quantity mismatch: requested {close_qty:g}, accepted {accepted_qty:g}")
    oid = str(order["id"])
    terminal = alpaca_broker.wait_for_terminal_order(oid, timeout_seconds=12)
    filled = float(terminal.get("filled_qty") or 0)
    exitp = float(terminal.get("filled_avg_price") or 0)
    if filled > close_qty + 1e-6:
        db.add(Alert(user_id=user.id, title="BROKER QUANTITY MISMATCH", body=f"{symbol}: requested x{close_qty:g}, broker reports filled x{filled:g}"))
        db.commit()
        raise ValueError("Broker reported a fill larger than the requested close quantity")
    if filled > 0 and exitp > 0:
        _apply_manual_close_fill(db, user, trade, filled, exitp, oid)
        setattr(trade, "_close_submission_status", "filled")
        setattr(trade, "_close_submission_message", f"Filled x{filled:g} @ ${exitp:.2f}")
        return trade

    # Accepted but not filled yet is a pending broker state, not an application error.
    status = str(terminal.get("status") or order.get("status") or "accepted").lower()
    if status in {"rejected", "canceled", "expired"}:
        raise ValueError(f"Close order {status} by broker")
    payload = _manual_close_payload(trade)
    payload["pending_manual_close"] = {
        "order_id": oid, "qty": close_qty, "mode": mode, "submitted_at": datetime.now(timezone.utc).isoformat(), "status": status
    }
    trade.indicators = json.dumps(payload, ensure_ascii=False)
    db.add(Alert(user_id=user.id, title="MANUAL CLOSE PENDING", body=f"{symbol} x{close_qty:g} accepted by Alpaca ({mode}); waiting for fill"))
    db.commit()
    setattr(trade, "_close_submission_status", "pending")
    setattr(trade, "_close_submission_message", f"Order accepted by Alpaca and waiting for fill ({status})")
    return trade


def stock_schedule_window_open(s: BotSettings) -> bool:
    """Return True only while the configured stock schedule is actively tradable.

    This intentionally includes both the delayed start and the configured pre-close
    cutoff so a Render restart near the close cannot accidentally restart the bot
    after its scheduled stop time.
    """
    if str(getattr(s, "start_mode", "manual")) not in {"scheduled", "both"}:
        return False
    days = {int(x) for x in str(getattr(s, "scheduled_days", "0,1,2,3,4")).split(",") if x.strip().isdigit()}
    n = market_now()
    if n.weekday() not in days or not market_is_open():
        return False
    mins = n.hour * 60 + n.minute
    start_at = 9 * 60 + 30 + max(0, int(getattr(s, "start_delay_minutes", 0) or 0))
    before = max(0, min(120, int(getattr(s, "auto_stop_before_close_minutes", 5) or 0)))
    stop_at = 16 * 60 - before
    return start_at <= mins < stop_at


def schedule_should_start(s: BotSettings) -> bool:
    if not stock_schedule_window_open(s):
        return False
    if s.active or s.locked or bool(getattr(s, "stocks_risk_locked", False)) or s.session_started_at is not None:
        return False
    return True


def stock_schedule_should_resume_after_restart(s: BotSettings) -> bool:
    """Restart-only scheduler check.

    Unlike schedule_should_start(), this deliberately ignores session_started_at because
    that timestamp survives process restarts. A scheduled/both bot is expected to be ON
    whenever the service comes back inside its configured window, after reconciliation.
    Manual-only bots never auto-resume.
    """
    if s.active or s.locked or bool(getattr(s, "stocks_risk_locked", False)):
        return False
    return stock_schedule_window_open(s)


def schedule_should_stop(s: BotSettings) -> bool:
    if not s.active: return False
    if str(getattr(s, "start_mode", "manual")) not in {"scheduled", "both"}: return False
    n=market_now(); before=max(0,min(120,int(getattr(s,"auto_stop_before_close_minutes",5) or 0)))
    return n.weekday()<5 and (n.hour*60+n.minute) >= (16*60-before)


def manage_realtime_risk(db: Session, user):
    """Lightweight real-time protection pass for broker synchronization and fractional exits.

    Whole-share bracket exits live at Alpaca. Fractional entries use a broker-side
    DAY stop plus a Luqman-managed take-profit because fractional OCO/bracket support
    is not assumed. This function reconciles the broker stop and manages the target
    from the heavier strategy scan interval.
    """
    if not _real_broker_mode():
        return
    # Reconcile broker fills even when the stock bot has been stopped manually. This is
    # essential for pending manual exits and broker-side bracket fills to reach the ledger.
    sync_broker_trades(db, user)
    if not user.settings.active:
        return
    fractional = [t for t in _open_trades(db, user.id) if t.reason == "ALPACA_FRACTIONAL_MARKET_SUBMITTED"]
    if not fractional or not market_data.configured:
        return
    try:
        prices = market_data.latest_prices([t.symbol for t in fractional])
        _manage_fractional_broker_exits(db, user, prices)
    except Exception:
        db.rollback()
