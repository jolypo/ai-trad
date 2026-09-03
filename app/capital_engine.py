from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .broker import alpaca_broker
from .models import BotSettings, CapitalReservation, Trade

ACTIVE_RESERVATION = "PENDING"

_PRESETS = {
    "stocks_only": (100.0, 0.0),
    "balanced": (50.0, 50.0),
    "stocks_focus": (70.0, 30.0),
    "options_focus": (30.0, 70.0),
    "options_only": (0.0, 100.0),
}


def _normalized_plan(settings: BotSettings) -> tuple[str, float, float]:
    mode = str(getattr(settings, "allocation_mode", "opportunity") or "opportunity")
    if mode == "dynamic":
        mode = "opportunity"
    if mode == "fixed":
        mode = "manual"
    stock_pct = max(0.0, min(100.0, float(getattr(settings, "stocks_allocation_pct", 60) or 0)))
    option_pct = max(0.0, min(100.0, float(getattr(settings, "options_allocation_pct", 40) or 0)))
    if mode in _PRESETS:
        stock_pct, option_pct = _PRESETS[mode]
    if mode not in {*_PRESETS.keys(), "manual", "opportunity"}:
        mode = "manual"
    if mode != "manual" and mode != "opportunity":
        total = stock_pct + option_pct
        if total > 0 and abs(total - 100.0) > 1e-8:
            stock_pct = stock_pct / total * 100.0
            option_pct = 100.0 - stock_pct
    return mode, stock_pct, option_pct




def _rebase_fixed_buckets(settings: BotSettings, new_target: float, stock_pct: float, option_pct: float) -> None:
    """Re-target fixed buckets while preserving the existing combined drawdown.

    Existing engine drawdowns stay with their engine whenever the new target has
    room.  If a user explicitly removes that engine (for example 100/0 -> 0/100),
    the orphan drawdown follows the reallocated capital instead of disappearing.
    """
    old_total_target = max(0.0, float(getattr(settings, "capital", 0) or 0))
    old_total_current = max(0.0, min(old_total_target, float(getattr(settings, "current_bot_capital", old_total_target) or 0)))
    total_dd = max(0.0, old_total_target - old_total_current)
    old_stock_dd = max(0.0, float(getattr(settings, "stocks_target_capital", 0) or 0) - float(getattr(settings, "stocks_current_capital", 0) or 0))
    old_option_dd = max(0.0, float(getattr(settings, "options_target_capital", 0) or 0) - float(getattr(settings, "options_current_capital", 0) or 0))

    st = max(0.0, new_target * stock_pct / 100.0)
    ot = max(0.0, new_target * option_pct / 100.0)
    sd = min(st, old_stock_dd)
    od = min(ot, old_option_dd)
    remaining = max(0.0, min(st + ot, total_dd) - sd - od)
    stock_room = max(0.0, st - sd)
    option_room = max(0.0, ot - od)
    room = stock_room + option_room
    if remaining > 0 and room > 1e-9:
        extra_stock = remaining * stock_room / room
        sd += min(stock_room, extra_stock)
        od += min(option_room, remaining - min(stock_room, extra_stock))
    settings.stocks_target_capital = st
    settings.options_target_capital = ot
    settings.stocks_current_capital = max(0.0, st - sd)
    settings.options_current_capital = max(0.0, ot - od)

def _sync_combined_totals(settings: BotSettings) -> None:
    """Keep legacy total fields as a compatibility summary of engine buckets."""
    mode, _, _ = _normalized_plan(settings)
    if mode == "opportunity":
        return
    st = max(0.0, float(getattr(settings, "stocks_target_capital", 0) or 0))
    ot = max(0.0, float(getattr(settings, "options_target_capital", 0) or 0))
    sc = max(0.0, min(st, float(getattr(settings, "stocks_current_capital", 0) or 0)))
    oc = max(0.0, min(ot, float(getattr(settings, "options_current_capital", 0) or 0)))
    unallocated_target = max(0.0, float(settings.capital or 0) - st - ot)
    # Unallocated target has no trading engine and therefore carries no drawdown.
    settings.current_bot_capital = min(float(settings.capital or 0), sc + oc + unallocated_target)
    settings.excess_realized_profit = max(0.0, float(getattr(settings, "stocks_excess_realized_profit", 0) or 0)) + max(
        0.0, float(getattr(settings, "options_excess_realized_profit", 0) or 0)
    )


def ensure_capital_account(settings: BotSettings) -> None:
    """Initialize persistent total + per-engine capital accounts.

    v23 keeps independent stock/options current balances for every fixed allocation
    plan.  A stock loss therefore cannot shrink the options bucket (and vice versa).
    """
    target = max(0.0, float(settings.capital or 0))
    if getattr(settings, "current_bot_capital", None) is None:
        settings.current_bot_capital = target
    settings.current_bot_capital = max(0.0, min(target, float(getattr(settings, "current_bot_capital", 0) or 0)))
    settings.excess_realized_profit = max(0.0, float(getattr(settings, "excess_realized_profit", 0) or 0))
    settings.stocks_excess_realized_profit = max(0.0, float(getattr(settings, "stocks_excess_realized_profit", 0) or 0))
    settings.options_excess_realized_profit = max(0.0, float(getattr(settings, "options_excess_realized_profit", 0) or 0))

    mode, sp, op = _normalized_plan(settings)
    if mode == "opportunity":
        # Deliberately shared pool.  Seed independent values only so switching plans
        # later has deterministic state; execution still uses the shared total pool.
        if getattr(settings, "stocks_target_capital", None) is None:
            settings.stocks_target_capital = target
        if getattr(settings, "options_target_capital", None) is None:
            settings.options_target_capital = target
        if getattr(settings, "stocks_current_capital", None) is None:
            settings.stocks_current_capital = settings.current_bot_capital
        if getattr(settings, "options_current_capital", None) is None:
            settings.options_current_capital = settings.current_bot_capital
        return

    stock_target = target * sp / 100.0
    option_target = target * op / 100.0
    missing = any(
        getattr(settings, name, None) is None
        for name in ("stocks_target_capital", "options_target_capital", "stocks_current_capital", "options_current_capital")
    )
    if missing:
        allocated_target = stock_target + option_target
        allocated_current = min(float(settings.current_bot_capital or 0), allocated_target)
        drawdown = max(0.0, allocated_target - allocated_current)
        # Best-effort in-memory fallback.  The DB additive migration performs a
        # stronger historical P&L bootstrap before normal requests begin.
        sl = max(0.0, -float(getattr(settings, "stocks_realized_pnl", 0) or 0))
        ol = max(0.0, -float(getattr(settings, "options_realized_pnl", 0) or 0))
        if drawdown > 0 and sl + ol > 1e-9:
            stock_dd = drawdown * sl / (sl + ol)
        elif allocated_target > 1e-9:
            stock_dd = drawdown * stock_target / allocated_target
        else:
            stock_dd = 0.0
        option_dd = drawdown - stock_dd
        settings.stocks_target_capital = stock_target
        settings.options_target_capital = option_target
        settings.stocks_current_capital = max(0.0, stock_target - stock_dd)
        settings.options_current_capital = max(0.0, option_target - option_dd)
    else:
        stored_st = max(0.0, float(settings.stocks_target_capital or 0))
        stored_ot = max(0.0, float(settings.options_target_capital or 0))
        # Some internal/admin paths and older tests update plan metadata directly.
        # Treat a target mismatch as an explicit plan change and rebase once.
        if abs(stored_st - stock_target) > 1e-8 or abs(stored_ot - option_target) > 1e-8:
            _rebase_fixed_buckets(settings, target, sp, op)
        else:
            settings.stocks_target_capital = stored_st
            settings.options_target_capital = stored_ot
            settings.stocks_current_capital = max(
                0.0, min(stored_st, float(settings.stocks_current_capital or 0))
            )
            settings.options_current_capital = max(
                0.0, min(stored_ot, float(settings.options_current_capital or 0))
            )
    _sync_combined_totals(settings)


def set_target_capital(settings: BotSettings, new_target: float) -> None:
    """Legacy-compatible target edit preserving each engine's absolute drawdown."""
    ensure_capital_account(settings)
    mode, sp, op = _normalized_plan(settings)
    old_target = max(0.0, float(settings.capital or 0))
    old_current = max(0.0, float(settings.current_bot_capital or 0))
    new_target = max(0.0, float(new_target))
    if mode == "opportunity":
        if new_target > old_target:
            old_current += new_target - old_target
        settings.capital = new_target
        settings.current_bot_capital = min(new_target, old_current)
        settings.stocks_target_capital = new_target
        settings.options_target_capital = new_target
        settings.stocks_current_capital = settings.current_bot_capital
        settings.options_current_capital = settings.current_bot_capital
        return

    stock_dd = max(0.0, float(settings.stocks_target_capital or 0) - float(settings.stocks_current_capital or 0))
    option_dd = max(0.0, float(settings.options_target_capital or 0) - float(settings.options_current_capital or 0))
    settings.capital = new_target
    # _rebase_fixed_buckets reads the old total capital/current as the authoritative
    # drawdown, so assign the new target only after rebasing.
    settings.capital = old_capital
    settings.current_bot_capital = old_current
    _rebase_fixed_buckets(settings, new_target, sp, op)
    settings.capital = new_target
    _sync_combined_totals(settings)


def set_capital_plan(settings: BotSettings, new_target: float, mode: str, stock_pct: float, option_pct: float) -> None:
    """Apply an explicit user allocation edit without transferring engine P&L.

    Changing 50/50 to 70/30 changes the *targets* but preserves each engine's
    absolute drawdown.  It never uses options capital to heal a stock loss or vice
    versa.
    """
    ensure_capital_account(settings)
    old_mode, _, _ = _normalized_plan(settings)
    new_target = max(0.0, float(new_target))
    stock_dd = 0.0
    option_dd = 0.0
    if old_mode != "opportunity":
        stock_dd = max(0.0, float(settings.stocks_target_capital or 0) - float(settings.stocks_current_capital or 0))
        option_dd = max(0.0, float(settings.options_target_capital or 0) - float(settings.options_current_capital or 0))
    else:
        total_dd = max(0.0, float(settings.capital or 0) - float(settings.current_bot_capital or 0))
        denom = max(1e-9, stock_pct + option_pct)
        stock_dd = total_dd * stock_pct / denom
        option_dd = total_dd - stock_dd

    old_capital = float(settings.capital or 0)
    old_current = float(settings.current_bot_capital or 0)
    settings.allocation_mode = mode
    settings.stocks_allocation_pct = max(0.0, min(100.0, float(stock_pct)))
    settings.options_allocation_pct = max(0.0, min(100.0, float(option_pct)))
    new_mode, sp, op = _normalized_plan(settings)
    if new_mode == "opportunity":
        combined_dd = max(0.0, old_capital - old_current)
        settings.capital = new_target
        settings.current_bot_capital = max(0.0, new_target - combined_dd)
        settings.stocks_target_capital = new_target
        settings.options_target_capital = new_target
        settings.stocks_current_capital = settings.current_bot_capital
        settings.options_current_capital = settings.current_bot_capital
        return

    # _rebase_fixed_buckets reads the old total capital/current as the authoritative
    # drawdown, so assign the new target only after rebasing.
    settings.capital = old_capital
    settings.current_bot_capital = old_current
    _rebase_fixed_buckets(settings, new_target, sp, op)
    settings.capital = new_target
    _sync_combined_totals(settings)


def apply_realized_pnl(settings: BotSettings, pnl: float, engine: str | None = None) -> tuple[float, float]:
    """Apply realized P&L to only the engine that generated it.

    In fixed allocation plans, a stock loss only reduces Stock Current Capital and
    an options loss only reduces Options Current Capital.  Profits restore that same
    engine only up to its own target; the remainder becomes that engine's excess.
    """
    ensure_capital_account(settings)
    pnl = float(pnl or 0)
    eng = str(engine or "").lower()
    if eng == "options":
        settings.options_realized_pnl = round(float(getattr(settings, "options_realized_pnl", 0) or 0) + pnl, 8)
    elif eng == "stocks":
        settings.stocks_realized_pnl = round(float(getattr(settings, "stocks_realized_pnl", 0) or 0) + pnl, 8)

    mode, _, _ = _normalized_plan(settings)
    if mode != "opportunity" and eng in {"stocks", "options"}:
        target_attr = f"{eng}_target_capital"
        current_attr = f"{eng}_current_capital"
        excess_attr = f"{eng}_excess_realized_profit"
        target = max(0.0, float(getattr(settings, target_attr, 0) or 0))
        current = max(0.0, min(target, float(getattr(settings, current_attr, 0) or 0)))
        if pnl < 0:
            new_current = max(0.0, current + pnl)
            applied = new_current - current
            setattr(settings, current_attr, new_current)
            _sync_combined_totals(settings)
            return applied, 0.0
        room = max(0.0, target - current)
        restored = min(room, pnl)
        excess = max(0.0, pnl - restored)
        setattr(settings, current_attr, current + restored)
        setattr(settings, excess_attr, max(0.0, float(getattr(settings, excess_attr, 0) or 0)) + excess)
        _sync_combined_totals(settings)
        return restored, excess

    # Opportunity Pool / legacy unscoped behavior remains one shared capital pool.
    target = max(0.0, float(settings.capital or 0))
    current = max(0.0, float(settings.current_bot_capital or 0))
    if pnl < 0:
        new_current = max(0.0, current + pnl)
        applied = new_current - current
        settings.current_bot_capital = new_current
        if mode == "opportunity":
            settings.stocks_current_capital = new_current
            settings.options_current_capital = new_current
        return applied, 0.0
    room = max(0.0, target - current)
    restored = min(room, pnl)
    excess = max(0.0, pnl - restored)
    settings.current_bot_capital = current + restored
    settings.excess_realized_profit = max(0.0, float(settings.excess_realized_profit or 0)) + excess
    if mode == "opportunity":
        settings.stocks_current_capital = settings.current_bot_capital
        settings.options_current_capital = settings.current_bot_capital
    return restored, excess


def _trade_exposure(row, prices: dict[str, float] | None = None) -> float:
    prices = prices or {}
    px = float(prices.get(row.symbol, row.entry) or row.entry or 0)
    multiplier = 100.0 if str(getattr(row, "engine", "")) == "options" else 1.0
    return max(0.0, px * float(row.qty or 0) * multiplier)


def _open_exposure(db: Session, user_id: int, prices: dict[str, float] | None = None, engine: str | None = None) -> float:
    rows = db.query(Trade).filter_by(user_id=user_id, status="OPEN").all()
    if engine:
        rows = [x for x in rows if str(getattr(x, "engine", "stocks") or "stocks") == engine]
    return sum(_trade_exposure(row, prices) for row in rows)


def pending_reserved(db: Session, user_id: int, engine: str | None = None) -> float:
    rows = db.query(CapitalReservation).filter_by(user_id=user_id, status=ACTIVE_RESERVATION).all()
    if engine:
        rows = [x for x in rows if str(x.engine or "stocks") == engine]
    return round(sum(max(0.0, float(x.amount or 0)) for x in rows), 8)


def broker_cash_available() -> float | None:
    if not alpaca_broker.configured:
        return None
    try:
        account = alpaca_broker.account()
        return max(0.0, float(account.get("cash") or 0))
    except Exception:
        return None


def _allocation_caps(s, usable: float, reserve_pct: float) -> dict[str, float | str]:
    mode, stock_pct, option_pct = _normalized_plan(s)
    if mode == "opportunity":
        return {
            "mode": "opportunity", "stocks_pct": 100.0, "options_pct": 100.0,
            "stocks_target": float(s.capital or 0), "options_target": float(s.capital or 0),
            "stocks_current": float(s.current_bot_capital or 0), "options_current": float(s.current_bot_capital or 0),
            "stocks_cap": usable, "options_cap": usable,
        }

    stock_target = max(0.0, float(getattr(s, "stocks_target_capital", 0) or 0))
    option_target = max(0.0, float(getattr(s, "options_target_capital", 0) or 0))
    stock_current = max(0.0, min(stock_target, float(getattr(s, "stocks_current_capital", 0) or 0)))
    option_current = max(0.0, min(option_target, float(getattr(s, "options_current_capital", 0) or 0)))
    reserve_factor = max(0.0, 1.0 - reserve_pct / 100.0)
    return {
        "mode": mode,
        "stocks_pct": stock_pct,
        "options_pct": option_pct,
        "stocks_target": stock_target,
        "options_target": option_target,
        "stocks_current": stock_current,
        "options_current": option_current,
        # cap remains the post-reserve risk/execution base used by engines.
        "stocks_cap": stock_current * reserve_factor,
        "options_cap": option_current * reserve_factor,
    }


def budget_state(db: Session, user, prices: dict[str, float] | None = None, broker_cash: float | None = None) -> dict[str, Any]:
    s = user.settings
    ensure_capital_account(s)
    target = max(0.0, float(s.capital or 0))
    current = max(0.0, min(target, float(s.current_bot_capital or 0)))
    reserve_pct = min(95.0, max(0.0, float(getattr(s, "cash_reserve_pct", 20) or 0)))
    reserve_floor = current * reserve_pct / 100.0
    usable = max(0.0, current - reserve_floor)
    invested = _open_exposure(db, user.id, prices)
    reserved = pending_reserved(db, user.id)
    internal_available = max(0.0, usable - invested - reserved)
    if broker_cash is None:
        broker_cash = broker_cash_available()
    available = internal_available if broker_cash is None else max(0.0, min(internal_available, float(broker_cash)))
    max_position_pct = min(100.0, max(1.0, float(getattr(s, "max_position_allocation_pct", 30) or 30)))
    per_position_cap = current * max_position_pct / 100.0
    alloc = _allocation_caps(s, usable, reserve_pct)
    stock_invested = _open_exposure(db, user.id, prices, "stocks")
    stock_reserved = pending_reserved(db, user.id, "stocks")
    option_invested = _open_exposure(db, user.id, prices, "options")
    option_reserved = pending_reserved(db, user.id, "options")
    stock_used = stock_invested + stock_reserved
    options_used = option_invested + option_reserved
    alloc.update({
        "stocks_invested": round(stock_invested, 8), "stocks_reserved": round(stock_reserved, 8),
        "options_invested": round(option_invested, 8), "options_reserved": round(option_reserved, 8),
        "stocks_used": round(stock_used, 8), "options_used": round(options_used, 8),
        "stocks_available": round(max(0.0, min(available, float(alloc["stocks_cap"]) - stock_used)) if alloc["mode"] != "opportunity" else available, 8),
        "options_available": round(max(0.0, min(available, float(alloc["options_cap"]) - options_used)) if alloc["mode"] != "opportunity" else available, 8),
        "stocks_excess": round(float(getattr(s, "stocks_excess_realized_profit", 0) or 0), 8),
        "options_excess": round(float(getattr(s, "options_excess_realized_profit", 0) or 0), 8),
    })
    return {
        "target": round(target, 8), "current": round(current, 8), "allocated": round(target, 8),
        "excess_realized_profit": round(float(getattr(s, "excess_realized_profit", 0) or 0), 8),
        "stocks_realized_pnl": round(float(getattr(s, "stocks_realized_pnl", 0) or 0), 8),
        "options_realized_pnl": round(float(getattr(s, "options_realized_pnl", 0) or 0), 8),
        "stocks_options_realized_pnl": round(float(getattr(s, "stocks_realized_pnl", 0) or 0) + float(getattr(s, "options_realized_pnl", 0) or 0), 8),
        "reserve_pct": reserve_pct, "reserve": round(reserve_floor, 8), "usable": round(usable, 8),
        "invested": round(invested, 8), "reserved": round(reserved, 8), "available_internal": round(internal_available, 8),
        "broker_cash": None if broker_cash is None else round(float(broker_cash), 8), "available": round(available, 8),
        "max_position_pct": max_position_pct, "per_position_cap": round(per_position_cap, 8), "allocation": alloc,
    }


def reserve_capital(db: Session, user, amount: float, engine: str, symbol: str) -> CapitalReservation:
    amount = round(max(0.0, float(amount)), 8)
    if amount <= 0:
        raise ValueError("Reservation amount must be positive")
    db.query(BotSettings).filter(BotSettings.user_id == user.id).with_for_update().one()
    state = budget_state(db, user)
    eng = "options" if (engine or "").lower() == "options" else "stocks"
    allowed = min(state["available"], state["allocation"][f"{eng}_available"])
    if amount > allowed + 1e-8:
        raise ValueError(f"Allocated capital unavailable for {eng}: requested ${amount:.2f}, available ${allowed:.2f}")
    row = CapitalReservation(user_id=user.id, engine=eng, symbol=(symbol or "UNKNOWN").upper()[:32], amount=amount, status=ACTIVE_RESERVATION)
    db.add(row)
    db.flush()
    return row


def release_reservation(db: Session, reservation: CapitalReservation | None, note: str = "") -> None:
    if not reservation or reservation.status != ACTIVE_RESERVATION:
        return
    reservation.status = "RELEASED"
    reservation.note = (note or "")[:500]
    reservation.released_at = datetime.now(timezone.utc)
    db.add(reservation)


def commit_reservation(db: Session, reservation: CapitalReservation | None, trade_id: int | None = None) -> None:
    if not reservation or reservation.status != ACTIVE_RESERVATION:
        return
    reservation.status = "COMMITTED"
    reservation.trade_id = trade_id
    reservation.released_at = datetime.now(timezone.utc)
    db.add(reservation)
