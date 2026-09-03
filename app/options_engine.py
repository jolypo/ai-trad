from __future__ import annotations

import json, time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from sqlalchemy.orm import Session

from .broker import alpaca_broker
from .capital_engine import budget_state, ensure_capital_account, reserve_capital, release_reservation, commit_reservation
from .config import settings
from .market_data import market_data
from .models import Alert, BotSettings, Trade
from .strategy import evaluate_symbol
from .trading import DEFAULT_WATCH, market_is_open, market_now, capital_budget_state, reset_for_new_day

OPTION_UNIVERSE = ["SPX", "QQQ", "SPY"] + DEFAULT_WATCH
OPTION_UNIVERSE = list(dict.fromkeys(OPTION_UNIVERSE))


def selected_option_symbols(user) -> list[str]:
    raw = str(getattr(user.settings, "options_symbols", "") or "")
    valid = set(OPTION_UNIVERSE)
    return [x for x in (z.strip().upper() for z in raw.split(",")) if x in valid]


def option_support_map(symbols: list[str]) -> dict[str, bool]:
    out = {}
    for sym in symbols:
        try: out[sym] = bool(alpaca_broker.options_enabled(sym))
        except Exception: out[sym] = False
    return out


def _bearish_score(state: dict) -> float:
    score = 0.0
    if state["price"] < state["vwap"]: score += 15
    if state["ema9"] < state["ema20"] < state["ema50"]: score += 20
    elif state["ema20"] < state["ema50"]: score += 10
    if state["macd"] < state["macd_signal"] and state["macd"] < 0: score += 15
    if 28 <= state["rsi14"] <= 48: score += 12
    elif 48 < state["rsi14"] <= 55: score += 5
    if state["adx14"] >= 22: score += 12
    if state["rel_volume"] >= 1.25: score += 14
    elif state["rel_volume"] >= 1.0: score += 6
    if -2.5 <= state["momentum_5"] <= -0.1: score += 12
    return round(score, 1)


def underlying_state(symbol: str, bars: list[dict]) -> dict | None:
    """Technical state used by the options page and auto-trader.

    SPX is analyzed from the cash-index reference bars; all stock/ETF symbols use
    Alpaca bars. The state is informational until the broker contract checks pass.
    """
    state = evaluate_symbol(symbol, bars, settings.min_signal_score)
    if not state:
        return None
    bull = float(state.get("score") or 0)
    bear = _bearish_score(state)
    best = max(bull, bear)
    direction = "call" if bull >= bear else "put"
    watch_threshold = max(0.0, float(settings.min_signal_score) - 10.0)
    verdict = "QUALIFIED" if best >= float(settings.min_signal_score) else ("WATCH" if best >= watch_threshold else "WAIT")
    return {**state, "bull_score": round(bull, 1), "bear_score": round(bear, 1), "options_score": round(best, 1), "options_direction": direction, "options_verdict": verdict}


def option_underlying_states(symbols: list[str]) -> dict[str, dict]:
    """Return current WAIT/WATCH/QUALIFIED states for option underlyings."""
    clean=list(dict.fromkeys(x.upper() for x in symbols if x))
    stock_symbols=[x for x in clean if x != "SPX"]
    bars_by_symbol = market_data.recent_bars(stock_symbols) if stock_symbols else {}
    if "SPX" in clean:
        try:
            bars_by_symbol["SPX"] = market_data.spx_recent_bars(days=7)
        except Exception:
            bars_by_symbol["SPX"] = []
    out={}
    for symbol in clean:
        st=underlying_state(symbol,bars_by_symbol.get(symbol,[]))
        if st:
            out[symbol]=st
    return out


def _underlying_signal(symbol: str, bars: list[dict], direction_mode: str) -> tuple[str, float, dict] | None:
    state = evaluate_symbol(symbol, bars, settings.min_signal_score)
    if not state: return None
    bull = float(state["score"])
    bear = _bearish_score(state)
    if direction_mode == "call": return ("call", bull, state) if bull >= settings.min_signal_score else None
    if direction_mode == "put": return ("put", bear, state) if bear >= settings.min_signal_score else None
    if max(bull, bear) < settings.min_signal_score: return None
    return ("call", bull, state) if bull >= bear else ("put", bear, state)


def _contract_price(snapshot: dict) -> tuple[float, float, float]:
    q = snapshot.get("latestQuote") or {}
    bid = float(q.get("bp") or 0); ask = float(q.get("ap") or 0)
    trade = float((snapshot.get("latestTrade") or {}).get("p") or 0)
    if ask > 0 and bid > 0 and ask >= bid: mid = (ask + bid) / 2
    elif ask > 0: mid = ask
    elif trade > 0: mid = trade
    else: mid = 0
    return bid, ask, mid


def contract_browser(underlying: str, contract_type: str = "call", min_dte: int = 0, max_dte: int = 14, limit: int = 80, underlying_price: float | None = None) -> list[dict]:
    underlying = underlying.upper(); today = date.today()
    gte = (today + timedelta(days=max(0,min_dte))).isoformat(); lte = (today + timedelta(days=max(min_dte,max_dte))).isoformat()
    contracts = alpaca_broker.option_contracts(underlying_symbols=[underlying], contract_type=contract_type, expiration_date_gte=gte, expiration_date_lte=lte, limit=1000)
    feed = str(getattr(settings, "alpaca_options_data_feed", "indicative") or "indicative").lower()
    feed = feed if feed in {"indicative", "opra"} else "indicative"
    snaps = market_data.option_chain(underlying, feed=feed, limit=1000)
    rows=[]
    for c in contracts:
        if not c.get("tradable"): continue
        snap = snaps.get(c.get("symbol"), {})
        bid, ask, mid = _contract_price(snap)
        greeks = snap.get("greeks") or {}
        q = snap.get("latestQuote") or {}; tr = snap.get("latestTrade") or {}
        quote_time = q.get("t")
        quote_age = None
        if quote_time:
            try:
                qt = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
                quote_age = max(0.0, (datetime.now(timezone.utc) - qt).total_seconds())
            except (TypeError, ValueError):
                quote_age = None
        stale = bool(quote_age is None or quote_age > 300)
        rows.append({**c, "bid":bid, "ask":ask, "mid":mid, "display_price":mid, "price_source":"quote_mid" if bid > 0 and ask > 0 and ask >= bid else ("ask" if ask > 0 else ("latest_trade" if float(tr.get("p") or 0) > 0 else "unavailable")), "delta":float(greeks.get("delta") or 0), "gamma":float(greeks.get("gamma") or 0), "theta":float(greeks.get("theta") or 0), "iv":float(snap.get("impliedVolatility") or 0), "quote_time": quote_time, "trade_time": tr.get("t"), "quote_age_seconds": quote_age, "stale": stale, "data_feed": feed})
    # Browser UX: when a current underlying price is already available from the
    # live-card request, keep only the nearest strikes around ATM, then restore
    # normal expiration/strike ordering. This does not add an extra market-data
    # request and prevents the browser from opening at very low strikes.
    spot = float(underlying_price or 0)
    if spot > 0 and rows:
        nearest = sorted(
            rows,
            key=lambda x: (
                abs(float(x.get("strike_price") or 0) - spot),
                x.get("expiration_date") or "",
                float(x.get("strike_price") or 0),
            ),
        )[: max(1, int(limit))]
        # Mark the nearest strike for the earliest available expiration as ATM
        # so the UI can scroll it into view while still allowing up/down scroll.
        expiries = sorted({str(x.get("expiration_date") or "") for x in nearest})
        first_expiry = expiries[0] if expiries else ""
        first_rows = [x for x in nearest if str(x.get("expiration_date") or "") == first_expiry]
        if first_rows:
            atm = min(first_rows, key=lambda x: abs(float(x.get("strike_price") or 0) - spot))
            atm_symbol = atm.get("symbol")
            for x in nearest:
                x["atm"] = bool(x.get("symbol") == atm_symbol)
                x["underlying_price"] = spot
        rows = sorted(nearest, key=lambda x:(x.get("expiration_date") or "", float(x.get("strike_price") or 0)))
        return rows
    rows.sort(key=lambda x:(x.get("expiration_date") or "", float(x.get("strike_price") or 0)))
    return rows[:limit]


def _choose_contract(underlying: str, ctype: str, s: BotSettings) -> dict | None:
    rows = contract_browser(underlying, ctype, int(s.options_min_dte), int(s.options_max_dte), 300)
    # Options require a stricter quality gate than stocks: stale or one-sided quotes
    # are not eligible, and very wide spreads are rejected. Theta is already
    # available in the same snapshot, so it is used as a ranking penalty without
    # adding any extra market-data request.
    eligible=[]
    for x in rows:
        mid=float(x.get("mid") or 0); bid=float(x.get("bid") or 0); ask=float(x.get("ask") or 0)
        if mid <= 0 or bid <= 0 or ask <= 0 or ask < bid or x.get("stale"):
            continue
        spread=(ask-bid)/mid if mid else 99
        if spread > 0.35:
            continue
        x["spread_ratio"] = spread
        x["theta_decay_ratio"] = abs(float(x.get("theta") or 0))/mid if mid else 99
        eligible.append(x)
    if not eligible: return None
    target = min(.90,max(.10,float(s.options_target_delta or .50)))
    def key(x):
        d=abs(float(x.get("delta") or 0))
        return (abs(d-target), float(x.get("spread_ratio") or 99), float(x.get("theta_decay_ratio") or 99), x.get("expiration_date") or "")
    return min(eligible,key=key)


def _options_entry_gate(db: Session, user, contract_symbol: str | None = None) -> tuple[bool, str]:
    s=user.settings
    if not bool(getattr(s,"options_bot_active",False)): return False,"options bot inactive"
    if s.locked or bool(getattr(s,"options_risk_locked",False)): return False,"options risk locked"
    if settings.enforce_market_hours and not market_is_open(): return False,"market closed"
    if int(getattr(s,"options_trades_today",0) or 0) >= int(getattr(s,"options_max_trades",5) or 5): return False,"maximum daily options trades reached"
    opens=db.query(Trade).filter_by(user_id=user.id,status="OPEN").all()
    if contract_symbol and any(t.symbol==contract_symbol for t in opens): return False,"contract already open"
    option_opens=[t for t in opens if str(getattr(t,"engine","") or "") == "options"]
    max_open=max(1,min(20,int(getattr(s,"options_max_open_positions",2) or 2)))
    if len(option_opens)>=max_open: return False,f"maximum open option positions reached ({len(option_opens)}/{max_open})"
    if getattr(s,"options_last_trade_at",None):
        raw_last=s.options_last_trade_at
        last=raw_last.replace(tzinfo=timezone.utc) if raw_last.tzinfo is None else raw_last
        elapsed=(datetime.now(timezone.utc)-last).total_seconds(); cooldown=max(0,int(getattr(s,"options_trade_cooldown_seconds",300) or 0))
        if elapsed<cooldown: return False,f"trade cooldown: {int(cooldown-elapsed)}s remaining"
    budget = capital_budget_state(db,user)
    if budget["available"] <= 0: return False,"no allocated capital available"
    if float((budget.get("allocation") or {}).get("options_available") or 0) <= 0: return False,"no options allocation available"
    return True,"ready"

def _option_order_capacity(s, budget: dict, per_contract: float, stop_pct: float) -> dict:
    """Calculate option sizing from the options bucket only.

    Stock risk/position settings are deliberately not consulted here.
    """
    alloc=budget.get("allocation") or {}
    option_available=float(alloc.get("options_available") or 0)
    option_bucket=float(alloc.get("options_cap") or 0)
    max_alloc_pct=min(100.0,max(1.0,float(getattr(s,"options_max_allocation_pct",20) or 20)))
    option_cap=min(float(budget.get("available") or 0),option_available,option_bucket*(max_alloc_pct/100))
    risk_pct=max(0.0,min(100.0,float(getattr(s,"options_risk_per_trade_pct",2) or 0)))
    risk_budget=option_bucket*(risk_pct/100)
    risk_per_contract=max(0.0,float(per_contract or 0))*(max(0.0,float(stop_pct or 0))/100)
    by_cap=int(option_cap//per_contract) if per_contract>0 else 0
    by_risk=int(risk_budget//risk_per_contract) if risk_per_contract>0 and risk_budget>0 else by_cap
    return {"option_bucket":option_bucket,"option_cap":option_cap,"risk_budget":risk_budget,"by_cap":by_cap,"by_risk":by_risk}


def _open_option_trade(db: Session, user, underlying: str, ctype: str, score: float, state: dict) -> Trade | None:
    s=user.settings; contract=_choose_contract(underlying,ctype,s)
    if not contract:
        db.add(Alert(user_id=user.id,title="OPTION BLOCKED",body=f"{underlying}: no active tradable {ctype} contract matched the DTE/delta filters")); db.commit(); return None
    # Do not stack multiple option positions on the same underlying unintentionally.
    for t in db.query(Trade).filter_by(user_id=user.id,status="OPEN",engine="options").all():
        try:
            if json.loads(t.indicators or "{}").get("underlying") == underlying:
                db.add(Alert(user_id=user.id,title="OPTION BLOCKED",body=f"{underlying}: an option position on this underlying is already open")); db.commit(); return None
        except Exception:
            pass
    ok,why=_options_entry_gate(db,user,contract.get("symbol"))
    if not ok:
        db.add(Alert(user_id=user.id,title="OPTION BLOCKED",body=f"{underlying}: {why}")); db.commit(); return None
    premium=float(contract["ask"] or contract["mid"]); per_contract=premium*100
    budget=budget_state(db,user)
    ensure_capital_account(s)
    stop_pct=min(95,max(5,float(s.options_stop_loss_pct or 30)))
    capacity=_option_order_capacity(s,budget,per_contract,stop_pct)
    qty=max(0,min(int(s.options_max_contracts or 1),capacity["by_cap"],capacity["by_risk"]))
    if qty<1:
        db.add(Alert(user_id=user.id,title="OPTION BLOCKED",body=f"{underlying}: allocated/risk capital cannot fund one contract (~${per_contract:.2f})")); db.commit(); return None
    est=round(per_contract*qty,8)
    try:
        reservation=reserve_capital(db,user,est,"options",contract["symbol"]); db.commit()
    except ValueError as exc:
        db.rollback(); db.add(Alert(user_id=user.id,title="OPTION BLOCKED",body=str(exc))); db.commit(); return None
    try:
        oid=f"luq-opt-u{user.id}-{underlying}-{int(time.time())}"
        order=alpaca_broker.submit_option_limit_buy(contract_symbol=contract["symbol"],qty=qty,limit_price=premium,client_order_id=oid)
        terminal=alpaca_broker.wait_for_terminal_order(order["id"],timeout_seconds=8)
        if terminal.get("status") != "filled" or not terminal.get("filled_avg_price"):
            try: alpaca_broker.cancel_order(order["id"])
            except Exception: pass
            raise ValueError(f"option order not filled ({terminal.get('status')})")
        fill=float(terminal["filled_avg_price"]); sl=round(fill*(1-stop_pct/100),2)
        exit_mode=str(getattr(s,"options_exit_mode","trailing") or "trailing").lower()
        trailing_enabled=exit_mode == "trailing"
        trail_activation_pct=max(5,min(500,float(getattr(s,"options_trailing_activation_pct",40) or 40)))
        trail_distance_pct=max(2,min(80,float(getattr(s,"options_trailing_distance_pct",20) or 20)))
        tp_pct=max(5,float(s.options_take_profit_pct or 50))
        tp=None if trailing_enabled else round(fill*(1+tp_pct/100),2)
        activation_price=round(fill*(1+trail_activation_pct/100),2) if trailing_enabled else None
        payload={"broker_order_id":order["id"],"underlying":underlying,"option_type":ctype,"expiration":contract.get("expiration_date"),"strike":contract.get("strike_price"),"multiplier":100,"delta":contract.get("delta"),"theta":contract.get("theta"),"spread_ratio":contract.get("spread_ratio"),"theta_decay_ratio":contract.get("theta_decay_ratio"),"entry_quote":{"bid":contract["bid"],"ask":contract["ask"]},"underlying_signal":state,"exit_policy":{"mode":exit_mode,"initial_stop_loss":sl,"fixed_take_profit_pct":tp_pct,"trailing_activation_pct":trail_activation_pct,"trailing_distance_pct":trail_distance_pct}}
        trade=Trade(user_id=user.id,symbol=contract["symbol"],side="BUY",engine="options",qty=qty,entry=fill,stop_loss=sl,take_profit=tp,signal_score=score,indicators=json.dumps(payload,ensure_ascii=False),pnl=0,status="OPEN",reason="ALPACA_OPTION_LIMIT_FILLED",data_source="ALPACA_PAPER_OPTION",trailing_enabled=trailing_enabled,trailing_active=False,trailing_activation_price=activation_price,trailing_high_watermark=fill if trailing_enabled else None,trailing_stop_price=None,trailing_distance_pct=trail_distance_pct if trailing_enabled else None)
        db.add(trade); db.flush(); commit_reservation(db,reservation,trade.id); s.options_trades_today = int(getattr(s,"options_trades_today",0) or 0) + 1; s.options_last_trade_at=datetime.now(timezone.utc)
        exit_text=(f"TRAIL ON +{trail_activation_pct:.0f}% / {trail_distance_pct:.0f}%" if trailing_enabled else f"TP ${tp:.2f}")
        db.add(Alert(user_id=user.id,title="OPTION OPEN",body=f"{underlying} {ctype.upper()} | {contract['symbol']} x{qty} @ ${fill:.2f} | SL ${sl:.2f} | {exit_text}")); db.commit(); return trade
    except Exception as exc:
        db.rollback(); r=db.get(__import__('app.models',fromlist=['CapitalReservation']).CapitalReservation,reservation.id) if reservation and reservation.id else None; release_reservation(db,r,f"option_failed:{type(exc).__name__}")
        db.add(Alert(user_id=user.id,title="OPTION ORDER ERROR",body=f"{underlying}: {type(exc).__name__}: {str(exc)[:180]}")); db.commit(); return None


def _advance_option_trailing(db: Session, user, trade: Trade, price: float) -> tuple[str | None, bool]:
    """Advance an option-only Luqman-managed trailing stop.

    Returns (exit_reason, changed). The high-water mark and effective stop are persisted
    so a Render restart resumes from the last confirmed state. The stop can only move
    upward for a long option position; it never loosens after activation.
    """
    if not bool(getattr(trade, "trailing_enabled", False)):
        return None, False
    px=float(price or 0)
    if px <= 0:
        return None, False
    changed=False
    hwm=max(float(getattr(trade,"trailing_high_watermark",0) or 0), px)
    if abs(hwm-float(getattr(trade,"trailing_high_watermark",0) or 0)) > 1e-9:
        trade.trailing_high_watermark=hwm; changed=True
    activation=float(getattr(trade,"trailing_activation_price",0) or 0)
    distance=max(0.02,min(0.80,float(getattr(trade,"trailing_distance_pct",20) or 20)/100.0))
    if not bool(getattr(trade,"trailing_active",False)):
        if activation > 0 and hwm >= activation:
            trade.trailing_active=True
            trade.trailing_activated_at=datetime.now(timezone.utc)
            new_stop=round(max(float(trade.stop_loss or 0), float(trade.entry or 0), hwm*(1-distance)),2)
            trade.trailing_stop_price=new_stop
            trade.stop_loss=new_stop
            changed=True
            db.add(Alert(user_id=user.id,title="OPTION TRAILING ACTIVE",body=f"{trade.symbol}: high ${hwm:.2f} | trailing stop ${new_stop:.2f}"))
        return None, changed
    candidate=round(max(float(trade.entry or 0), hwm*(1-distance)),2)
    current=max(float(getattr(trade,"trailing_stop_price",0) or 0), float(trade.stop_loss or 0))
    if candidate > current + 1e-9:
        trade.trailing_stop_price=candidate
        trade.stop_loss=candidate
        current=candidate
        changed=True
    if current > 0 and px <= current:
        return "ALPACA_OPTION_TRAILING_STOP", changed
    return None, changed


def manage_option_positions(db: Session, user):
    """Reconcile and protect open option positions without duplicate exit orders.

    Option exits can remain accepted/pending at Alpaca. The pending broker order id is
    persisted inside the trade payload and is reconciled on every realtime cycle before
    any new exit can be submitted.
    """
    opens = db.query(Trade).filter_by(user_id=user.id, status="OPEN", engine="options").all()
    if not opens:
        return
    try:
        positions = {str(p.get("symbol") or "").upper(): p for p in alpaca_broker.positions()}
    except Exception:
        return
    from .trading import _close_trade
    terminal_statuses = {"filled", "canceled", "expired", "rejected", "done_for_day"}
    for t in opens:
        try:
            payload = json.loads(t.indicators or "{}")
        except Exception:
            payload = {}
        pending = payload.get("pending_option_close") or {}
        pending_id = pending.get("order_id")
        if pending_id:
            try:
                order = alpaca_broker.get_order(str(pending_id), nested=True)
            except Exception:
                continue
            status = str(order.get("status") or "").lower()
            filled = float(order.get("filled_qty") or 0)
            exitp = float(order.get("filled_avg_price") or 0)
            if status == "filled" and filled > 0 and exitp > 0:
                payload.pop("pending_option_close", None)
                t.indicators = json.dumps(payload, ensure_ascii=False)
                _close_trade(db, user, t, exitp, str(pending.get("reason") or "ALPACA_OPTION_MANUAL_EXIT"))
                _enforce_options_daily_risk(db,user)
            elif status in terminal_statuses:
                payload.pop("pending_option_close", None)
                t.indicators = json.dumps(payload, ensure_ascii=False)
                db.add(Alert(user_id=user.id, title="OPTION EXIT NOT FILLED", body=f"{t.symbol}: exit order {status}"))
                db.commit()
            continue

        p = positions.get(t.symbol.upper())
        px = float((p or {}).get("current_price") or 0)
        if not px:
            continue
        reason = None
        trail_reason, trail_changed = _advance_option_trailing(db, user, t, px)
        if trail_reason:
            reason = trail_reason
        elif not bool(getattr(t,"trailing_active",False)) and t.stop_loss and px <= t.stop_loss:
            reason = "ALPACA_OPTION_STOP_LOSS"
        elif not bool(getattr(t,"trailing_enabled",False)) and t.take_profit and px >= t.take_profit:
            reason = "ALPACA_OPTION_TAKE_PROFIT"
        if not reason:
            if trail_changed:
                db.commit()
            continue
        try:
            order = alpaca_broker.close_position(t.symbol, qty=float(t.qty or 0))
            if not order or not order.get("id"):
                raise ValueError("broker did not accept option close")
            accepted_qty = float(order.get("qty") or t.qty or 0)
            if abs(accepted_qty - float(t.qty or 0)) > 1e-6:
                try:
                    alpaca_broker.cancel_order(str(order["id"]))
                finally:
                    raise ValueError("option close quantity mismatch")
            terminal = alpaca_broker.wait_for_terminal_order(str(order["id"]), 8)
            status = str(terminal.get("status") or order.get("status") or "").lower()
            filled = float(terminal.get("filled_qty") or 0)
            exitp = float(terminal.get("filled_avg_price") or 0)
            if status == "filled" and filled > 0 and exitp > 0:
                _close_trade(db, user, t, exitp, reason)
                _enforce_options_daily_risk(db,user)
            elif status not in {"canceled", "expired", "rejected", "done_for_day"}:
                payload["pending_option_close"] = {"order_id": str(order["id"]), "reason": reason, "qty": float(t.qty or 0), "submitted_at": datetime.now(timezone.utc).isoformat()}
                t.indicators = json.dumps(payload, ensure_ascii=False)
                db.add(Alert(user_id=user.id, title="OPTION EXIT PENDING", body=f"{t.symbol}: close accepted and awaiting fill"))
                db.commit()
            else:
                db.add(Alert(user_id=user.id, title="OPTION EXIT NOT FILLED", body=f"{t.symbol}: close order {status}"))
                db.commit()
        except Exception as exc:
            db.rollback()
            db.add(Alert(user_id=user.id, title="OPTION CLOSE ERROR", body=f"{t.symbol}: {type(exc).__name__}: {str(exc)[:160]}"))
            db.commit()


def _enforce_options_daily_risk(db: Session, user) -> bool:
    """Independent realized-P&L daily guard for the options engine.

    Open contracts remain protected by their own stop/trailing logic; this guard avoids
    an extra broker-data request solely for risk accounting.
    """
    s=user.settings
    if bool(getattr(s,"options_risk_locked",False)):
        return False
    state=capital_budget_state(db,user)
    base=max(0.0,float((state.get("allocation") or {}).get("options_cap") or 0))
    pct=max(0.0,min(100.0,float(getattr(s,"options_daily_loss_pct",5) or 0)))
    if pct <= 0 or base <= 0:
        return True
    realized=float(getattr(s,"options_realized_pnl",0) or 0)
    limit=-(base*pct/100)
    if realized <= limit:
        s.options_risk_locked=True
        s.options_bot_active=False
        db.add(Alert(user_id=user.id,title="OPTIONS RISK LOCK",body=f"Options realized daily loss limit reached at ${realized:.2f}. Options bot locked for today."))
        db.commit()
        return False
    return True


def run_options_cycle(db: Session,user):
    s=user.settings
    if not bool(s.options_bot_active) or s.locked or bool(getattr(s,"options_risk_locked",False)): return None
    if not _enforce_options_daily_risk(db,user): return None
    if settings.enforce_market_hours and not market_is_open(): return None
    manage_option_positions(db,user)
    symbols=selected_option_symbols(user)
    stock_symbols=[x for x in symbols if x != "SPX"]
    bars=market_data.recent_bars(stock_symbols) if stock_symbols else {}
    if "SPX" in symbols:
        try:
            bars["SPX"] = market_data.spx_recent_bars(days=7)
        except Exception:
            bars["SPX"] = []
    candidates=[]
    for underlying in symbols:
        sig=_underlying_signal(underlying,bars.get(underlying,[]),str(s.options_contract_type or "auto"))
        if sig: candidates.append((sig[1],underlying,*sig))
    if not candidates: return None
    candidates.sort(reverse=True,key=lambda x:x[0])
    _,underlying,ctype,score,state=candidates[0]
    return _open_option_trade(db,user,underlying,ctype,score,state)


def options_schedule_window_open(s: BotSettings) -> bool:
    """True only inside the independently configured options schedule window."""
    if str(getattr(s, "options_start_mode", "manual")) not in {"scheduled", "both"}:
        return False
    days = {int(x) for x in str(getattr(s, "options_scheduled_days", "0,1,2,3,4")).split(",") if x.strip().isdigit()}
    n = market_now()
    if n.weekday() not in days or not market_is_open():
        return False
    mins = n.hour * 60 + n.minute
    start_at = 9 * 60 + 30 + max(0, int(getattr(s, "options_start_delay_minutes", 0) or 0))
    before = max(5, min(120, int(getattr(s, "options_auto_stop_before_close_minutes", 5) or 5)))
    stop_at = 16 * 60 - before
    return start_at <= mins < stop_at


def options_schedule_should_start(s: BotSettings) -> bool:
    if not options_schedule_window_open(s):
        return False
    if bool(getattr(s, "options_bot_active", False)) or s.locked or bool(getattr(s,"options_risk_locked",False)) or getattr(s, "options_session_started_at", None) is not None:
        return False
    return True


def options_schedule_should_resume_after_restart(s: BotSettings) -> bool:
    """Restart-only resume check for the options engine.

    Scheduled/both mode resumes after successful reconciliation when the current time is
    inside the options window. Manual mode remains off after a process restart.
    """
    if bool(getattr(s, "options_bot_active", False)) or s.locked or bool(getattr(s, "options_risk_locked", False)):
        return False
    return options_schedule_window_open(s)


def options_schedule_should_stop(s: BotSettings) -> bool:
    if not bool(getattr(s, "options_bot_active", False)):
        return False
    if str(getattr(s, "options_start_mode", "manual")) not in {"scheduled", "both"}:
        return False
    n = market_now()
    before = max(5, min(120, int(getattr(s, "options_auto_stop_before_close_minutes", 5) or 5)))
    return n.weekday() < 5 and (n.hour * 60 + n.minute) >= (16 * 60 - before)


def start_options_bot(db:Session,user):
    reset_for_new_day(user.settings)
    db.commit()
    if user.settings.locked or bool(getattr(user.settings,"options_risk_locked",False)): return False,"options risk locked"
    if bool(getattr(user.settings,"broker_reconciliation_required",False)): return False,"broker reconciliation required"
    if settings.enforce_market_hours and not market_is_open(): return False,"market closed"
    # One shared Alpaca credential set cannot safely serve two Luqman users concurrently.
    others=db.query(BotSettings).filter(BotSettings.user_id != user.id).all()
    if any(bool(x.active) or bool(getattr(x,"index_bot_active",False)) or bool(getattr(x,"options_bot_active",False)) for x in others):
        return False,"Alpaca Paper account is already in use by another active user"
    if settings.broker_mode == "alpaca_market_paper":
        from .trading import reconcile_broker_state
        ok, issues = reconcile_broker_state(db,user)
        if not ok: return False,"broker reconciliation required: " + "; ".join(issues[:3])
    user.settings.options_bot_active=True
    user.settings.options_session_started_at=datetime.now(timezone.utc)
    user.settings.options_session_stopped_at=None
    db.add(Alert(user_id=user.id,title="OPTIONS BOT ACTIVE",body=f"Options bot started: {', '.join(selected_option_symbols(user))}"))
    db.commit(); return True,"started"


def stop_options_bot(db:Session,user,close_positions:bool=False):
    user.settings.options_bot_active=False
    user.settings.options_session_stopped_at=datetime.now(timezone.utc)
    if close_positions:
        rows=db.query(Trade).filter_by(user_id=user.id,status="OPEN",engine="options").all()
        from .trading import close_user_position
        for t in rows:
            try: close_user_position(db,user,t.symbol)
            except Exception: db.rollback()
    db.add(Alert(user_id=user.id,title="OPTIONS BOT OFF",body="Options bot stopped")); db.commit()
