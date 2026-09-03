from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite

from sqlalchemy.orm import Session

from .broker import alpaca_broker
from .models import PortfolioSnapshot, StockIndicatorSnapshot, Trade


def f(value, default=0.0):
    try:
        x = float(value)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


def broker_state() -> dict:
    out = {
        "connected": False,
        "account": {},
        "positions": [],
        "orders": [],
        "error": "",
    }
    try:
        a = alpaca_broker.account()
        positions = alpaca_broker.positions()
        orders = alpaca_broker.orders(status="all", limit=80, nested=True)
        equity = f(a.get("equity") or a.get("portfolio_value"))
        last_equity = f(a.get("last_equity"), equity)
        out.update({
            "connected": True,
            "account": {
                "equity": equity,
                "cash": f(a.get("cash")),
                "buying_power": f(a.get("buying_power")),
                "portfolio_value": f(a.get("portfolio_value"), equity),
                "long_market_value": f(a.get("long_market_value")),
                "day_pnl": equity - last_equity,
                "day_pct": ((equity / last_equity - 1) * 100) if last_equity else 0.0,
                "status": str(a.get("status") or ""),
                "currency": str(a.get("currency") or "USD"),
            },
            "positions": [_position_view(x) for x in positions],
            "orders": [_order_view(x) for x in orders],
        })
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out


def _position_view(p: dict) -> dict:
    return {
        "symbol": p.get("symbol"),
        "qty": f(p.get("qty")),
        "avg_entry_price": f(p.get("avg_entry_price")),
        "current_price": f(p.get("current_price")),
        "market_value": f(p.get("market_value")),
        "cost_basis": f(p.get("cost_basis")),
        "unrealized_pl": f(p.get("unrealized_pl")),
        "unrealized_plpc": f(p.get("unrealized_plpc")) * 100,
        "change_today": f(p.get("change_today")) * 100,
        "side": p.get("side") or "long",
    }


def _order_view(o: dict) -> dict:
    return {
        "id": o.get("id"),
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "type": o.get("type"),
        "status": o.get("status"),
        "qty": f(o.get("qty")),
        "filled_qty": f(o.get("filled_qty")),
        "filled_avg_price": f(o.get("filled_avg_price")),
        "limit_price": f(o.get("limit_price")),
        "stop_price": f(o.get("stop_price")),
        "submitted_at": o.get("submitted_at"),
        "filled_at": o.get("filled_at"),
        "client_order_id": o.get("client_order_id"),
        "order_class": o.get("order_class"),
    }


def trade_metrics(trades: list[Trade]) -> dict:
    closed = [t for t in trades if t.status == "CLOSED"]
    open_ = [t for t in trades if t.status == "OPEN"]
    wins = [t for t in closed if f(t.pnl) > 0]
    losses = [t for t in closed if f(t.pnl) < 0]
    gross_profit = sum(f(t.pnl) for t in wins)
    gross_loss = abs(sum(f(t.pnl) for t in losses))
    net = sum(f(t.pnl) for t in closed)
    win_rate = 100 * len(wins) / len(closed) if closed else 0
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    expectancy = net / len(closed) if closed else 0
    return {
        "closed": len(closed), "open": len(open_), "wins": len(wins), "losses": len(losses),
        "net": net, "win_rate": win_rate, "gross_profit": gross_profit, "gross_loss": gross_loss,
        "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": profit_factor, "expectancy": expectancy,
    }


def daily_trade_rows(trades: list[Trade], days: int = 31) -> list[dict]:
    buckets = defaultdict(list)
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    for t in trades:
        dt = t.closed_at or t.opened_at
        if not dt:
            continue
        d = dt.date()
        if d >= cutoff:
            buckets[d].append(t)
    rows = []
    for d in sorted(buckets, reverse=True):
        m = trade_metrics(buckets[d])
        rows.append({"date": d.isoformat(), **m})
    return rows


def monthly_trade_rows(trades: list[Trade], months: int = 12) -> list[dict]:
    buckets = defaultdict(list)
    for t in trades:
        dt = t.closed_at or t.opened_at
        if dt:
            buckets[dt.strftime("%Y-%m")].append(t)
    rows = []
    for key in sorted(buckets, reverse=True)[:months]:
        m = trade_metrics(buckets[key])
        rows.append({"month": key, **m})
    return rows


def equity_points(db: Session, user_id: int, limit: int = 80) -> list[float]:
    rows = (
        db.query(PortfolioSnapshot)
        .filter_by(user_id=user_id)
        .order_by(PortfolioSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    return [f(x.equity) for x in reversed(rows)]


def sparkline_points(values: list[float], width: int = 720, height: int = 180, pad: int = 8) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    pts = []
    for i, v in enumerate(values):
        x = pad + i * (width - 2 * pad) / max(len(values) - 1, 1)
        y = pad + (hi - v) * (height - 2 * pad) / span
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def latest_indicators(db: Session, user_id: int, symbols: list[str]) -> list[dict]:
    rows = []
    for symbol in symbols:
        x = (
            db.query(StockIndicatorSnapshot)
            .filter_by(user_id=user_id, symbol=symbol)
            .order_by(StockIndicatorSnapshot.id.desc())
            .first()
        )
        if not x:
            rows.append({"symbol": symbol, "available": False, "verdict": "WAIT"})
            continue
        try:
            reason_payload = json.loads(x.reasons or "{}")
        except Exception:
            reason_payload = {}
        rows.append({
            "available": True, "symbol": x.symbol, "price": x.price, "score": x.score,
            "qualified": x.qualified, "ema9": x.ema9, "ema20": x.ema20, "ema50": x.ema50,
            "rsi14": x.rsi14, "macd": x.macd, "macd_signal": x.macd_signal, "atr14": x.atr14,
            "vwap": x.vwap, "adx14": x.adx14, "rel_volume": x.rel_volume,
            "momentum_5": x.momentum_5, "verdict": x.verdict,
            "reasons": reason_payload.get("reasons", []), "blockers": reason_payload.get("blockers", []),
            "updated_at": x.created_at,
        })
    return rows


def signal_lifecycle(db: Session, user_id: int, symbol: str, depth: int = 6) -> dict:
    rows = (db.query(StockIndicatorSnapshot).filter_by(user_id=user_id, symbol=symbol)
            .order_by(StockIndicatorSnapshot.id.desc()).limit(max(2, depth)).all())
    if not rows:
        return {"state":"LOST","trend":"flat","duration_minutes":0,"qualified_scans":0,"delta":0}
    rows = list(reversed(rows))
    last = rows[-1]
    scores = [f(x.score) for x in rows]
    qrun = 0
    for x in reversed(rows):
        if x.qualified: qrun += 1
        else: break
    delta = scores[-1] - scores[-2] if len(scores) > 1 else 0
    trend = "up" if delta >= 4 else ("down" if delta <= -4 else "flat")
    if last.qualified and qrun >= 2 and trend == "up": state = "STRONG"
    elif last.qualified and qrun >= 2: state = "STABLE"
    elif last.qualified: state = "RE_ENTRY_WATCH"
    elif scores[-1] >= 55 and trend == "down": state = "WEAKENING"
    else: state = "LOST"
    start = rows[-qrun].created_at if qrun and len(rows) >= qrun else last.created_at
    try:
        mins = max(0, int(((last.created_at - start).total_seconds())/60))
    except Exception:
        mins = 0
    return {"state":state,"trend":trend,"duration_minutes":mins,"qualified_scans":qrun,"delta":round(delta,1)}
