import asyncio
import os
import resource
import threading
import time as _process_time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .analytics import (
    broker_state,
    daily_trade_rows,
    equity_points,
    latest_indicators,
    monthly_trade_rows,
    sparkline_points,
    trade_metrics,
    signal_lifecycle,
)
from .broker import alpaca_broker
from .config import settings

PROCESS_STARTED_AT = _process_time.time()
from .db import Base, SessionLocal, engine, ensure_additive_schema, ensure_trade_schema, get_db
from .market_data import market_data
from .models import Alert, BotSettings, PortfolioSnapshot, Trade, User, CustomIndicator
from .pine_converter import convert_pine_to_python
from .index_engine import INDEX_PRODUCTS, index_states, run_index_cycle, start_index_bot, stop_index_bot, selected_index_symbols
from .options_engine import OPTION_UNIVERSE, selected_option_symbols, option_support_map, contract_browser, option_underlying_states, run_options_cycle, manage_option_positions, start_options_bot, stop_options_bot, options_schedule_should_start, options_schedule_should_stop, options_schedule_should_resume_after_restart
from .security import hash_password, verify_password
from .i18n import code_label, alert_body_label
from .capital_engine import set_target_capital, set_capital_plan
from .trading import (
    capital_budget_state,
    entry_gate_status,
    DEFAULT_WATCH,
    allowed_symbols,
    market_is_open,
    record_portfolio_snapshot,
    reset_for_new_day,
    run_user_cycle,
    seed_default_symbols,
    set_allowed_symbols,
    start_session,
    stop_session,
    close_user_position,
    close_user_position_partial,
    schedule_should_start,
    schedule_should_stop,
    stock_schedule_should_resume_after_restart,
    manage_realtime_risk,
    sync_broker_trades,
    reconcile_broker_state,
)


def seed_admin():
    db = SessionLocal()
    try:
        u = db.query(User).filter_by(email=settings.admin_email.lower()).first()
        if not u:
            u = User(
                name="Admin",
                email=settings.admin_email.lower(),
                password_hash=hash_password(settings.admin_password),
                is_admin=True,
            )
            db.add(u)
            db.flush()
            db.add(BotSettings(user_id=u.id))
            db.commit()
        seed_default_symbols(db, u.id)
    finally:
        db.close()


async def bot_worker():
    # Scheduler checks frequently, while expensive strategy cycles respect BOT_TICK_SECONDS.
    last_cycle: dict[int, float] = {}
    loop = asyncio.get_running_loop()
    while True:
        try:
            db = SessionLocal()
            try:
                rows = db.query(BotSettings).all()
                now_mono = loop.time()
                for bs in rows:
                    user = db.get(User, bs.user_id)
                    if not user:
                        continue
                    try:
                        # Keep the stock and options schedulers independent. Shared capital/risk
                        # remains authoritative, but stopping one engine must not stop the other.
                        reset_for_new_day(bs)
                        db.commit()
                        if schedule_should_stop(bs):
                            stop_session(db, user, "scheduled_close")
                        if options_schedule_should_stop(bs):
                            stop_options_bot(db, user, close_positions=True)
                        if schedule_should_start(bs):
                            start_session(db, user, settings.max_daily_loss_hard_cap)
                        if options_schedule_should_start(bs):
                            start_options_bot(db, user)
                        # Fast broker reconciliation is separate from the heavy strategy scan.
                        # It keeps fills, P&L and the ledger current within a few seconds.
                        manage_realtime_risk(db, user)
                        manage_option_positions(db, user)
                        due = now_mono - last_cycle.get(user.id, 0) >= max(15, settings.bot_tick_seconds)
                        if user.settings.active and due:
                            run_user_cycle(db, user)
                        if bool(getattr(user.settings, "options_bot_active", False)) and due:
                            run_options_cycle(db, user)
                        if due and (user.settings.active or bool(getattr(user.settings, "options_bot_active", False))):
                            last_cycle[user.id] = now_mono
                    except Exception as exc:
                        db.rollback()
                        db.add(Alert(user_id=user.id, title="BOT ERROR", body=f"Trading cycle failed safely: {type(exc).__name__}: {str(exc)[:180]}"))
                        db.commit()
            finally:
                db.close()
        except Exception:
            pass
        await asyncio.sleep(max(2, int(settings.realtime_sync_seconds)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.broker_mode == "alpaca_market_paper":
        if settings.secret_key == "dev-secret-change-me":
            raise RuntimeError("Unsafe default SECRET_KEY is forbidden in Alpaca Paper mode")
        if settings.admin_password == "ChangeMe123!":
            raise RuntimeError("Unsafe default ADMIN_PASSWORD is forbidden in Alpaca Paper mode")
    Base.metadata.create_all(engine)
    ensure_additive_schema()
    ensure_trade_schema()
    seed_admin()
    # Restart safety + schedule recovery:
    # 1) never trust in-memory active flags after a process restart;
    # 2) reconcile broker/DB state first;
    # 3) only scheduled/both engines may auto-resume, and only inside their own
    #    configured trading window. Manual-only engines stay OFF.
    startup_db = SessionLocal()
    try:
        for initial_bs in startup_db.query(BotSettings).all():
            reset_for_new_day(initial_bs)
            initial_bs.active = False
            initial_bs.options_bot_active = False
            initial_bs.index_bot_active = False
            startup_db.commit()

            u = startup_db.get(User, initial_bs.user_id)
            if not u:
                continue

            reconciliation_ok = True
            if settings.broker_mode == "alpaca_market_paper":
                if not alpaca_broker.configured:
                    reconciliation_ok = False
                else:
                    try:
                        reconciliation_ok, issues = reconcile_broker_state(startup_db, u)
                        if not reconciliation_ok:
                            bs = startup_db.query(BotSettings).filter_by(user_id=u.id).first()
                            if bs:
                                bs.broker_reconciliation_required = True
                            startup_db.add(Alert(
                                user_id=u.id,
                                title="BROKER RECONCILIATION REQUIRED",
                                body="Startup auto-resume blocked until broker reconciliation is clean: " + "; ".join(issues[:3]),
                            ))
                            startup_db.commit()
                    except Exception as exc:
                        startup_db.rollback()
                        bs = startup_db.query(BotSettings).filter_by(user_id=u.id).first()
                        if bs:
                            bs.broker_reconciliation_required = True
                        startup_db.add(Alert(user_id=u.id,title="BROKER RECONCILIATION ERROR",body=f"Startup reconciliation failed safely: {type(exc).__name__}"))
                        startup_db.commit()
                        reconciliation_ok = False

            # A schedule is persistent intent. If Render wakes/restarts while that
            # schedule says the engine should be running, resume it after reconciliation.
            if reconciliation_ok:
                bs = startup_db.query(BotSettings).filter_by(user_id=u.id).first()
                if bs and stock_schedule_should_resume_after_restart(bs):
                    ok, msg = start_session(startup_db, u, settings.max_daily_loss_hard_cap)
                    if ok:
                        startup_db.add(Alert(user_id=u.id, title="STOCK BOT AUTO-RESUMED", body="Scheduled stock bot resumed automatically after server restart."))
                        startup_db.commit()
                    else:
                        startup_db.add(Alert(user_id=u.id, title="STOCK AUTO-RESUME BLOCKED", body=f"Scheduled restart resume was blocked safely: {msg}"))
                        startup_db.commit()

                bs = startup_db.query(BotSettings).filter_by(user_id=u.id).first()
                if bs and options_schedule_should_resume_after_restart(bs):
                    ok, msg = start_options_bot(startup_db, u)
                    if ok:
                        startup_db.add(Alert(user_id=u.id, title="OPTIONS BOT AUTO-RESUMED", body="Scheduled options bot resumed automatically after server restart."))
                        startup_db.commit()
                    else:
                        startup_db.add(Alert(user_id=u.id, title="OPTIONS AUTO-RESUME BLOCKED", body=f"Scheduled restart resume was blocked safely: {msg}"))
                        startup_db.commit()
    finally:
        startup_db.close()
    task = asyncio.create_task(bot_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Luqman Trade", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=(settings.broker_mode == "alpaca_market_paper"),
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["code_label"] = code_label
templates.env.globals["alert_body_label"] = alert_body_label


def current_user(req: Request, db: Session):
    uid = req.session.get("uid")
    return db.get(User, uid) if uid else None


def require_user(req: Request, db: Session):
    u = current_user(req, db)
    if not u:
        raise HTTPException(401)
    return u


def set_flash(req: Request, message: str, kind: str = "info"):
    req.session["flash"] = {"message": message, "kind": kind}


def pop_flash(req: Request):
    return req.session.pop("flash", None)


@app.get("/", response_class=HTMLResponse)
def home(req: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("home.html", {"request": req, "u": current_user(req, db)})


@app.get("/about", response_class=HTMLResponse)
def about(req: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("about.html", {"request": req, "u": current_user(req, db)})


@app.get("/pricing", response_class=HTMLResponse)
def pricing(req: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("pricing.html", {"request": req, "u": current_user(req, db)})


@app.get("/login", response_class=HTMLResponse)
def login_page(req: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": req, "u": current_user(req, db), "mode": "login"})


@app.post("/login")
def login(req: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.query(User).filter_by(email=email.lower().strip()).first()
    if not u or not verify_password(password, u.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": req, "u": None, "mode": "login", "error": "بيانات الدخول غير صحيحة / Invalid credentials"},
            status_code=400,
        )
    req.session["uid"] = u.id
    return RedirectResponse("/dashboard", 303)


@app.get("/register", response_class=HTMLResponse)
def reg_page(req: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("login.html", {"request": req, "u": current_user(req, db), "mode": "register"})


@app.post("/register")
def register(req: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.lower().strip()
    name = name.strip()
    if len(name) < 2 or len(password) < 8:
        return templates.TemplateResponse(
            "login.html", {"request": req, "u": None, "mode": "register", "error": "الاسم مطلوب وكلمة المرور 8 أحرف على الأقل"}, status_code=400
        )
    if db.query(User).filter_by(email=email).first():
        return templates.TemplateResponse(
            "login.html", {"request": req, "u": None, "mode": "register", "error": "البريد مستخدم"}, status_code=400
        )
    u = User(name=name, email=email, password_hash=hash_password(password))
    db.add(u)
    db.flush()
    db.add(BotSettings(user_id=u.id))
    db.commit()
    seed_default_symbols(db, u.id)
    req.session["uid"] = u.id
    return RedirectResponse("/dashboard", 303)


@app.get("/logout")
def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/", 303)


def _dashboard_context(req: Request, db: Session, u: User):
    reset_for_new_day(u.settings)
    db.commit()
    selected = set(allowed_symbols(db, u.id))
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try:
            sync_broker_trades(db, u)
        except Exception:
            db.rollback()
    trades = db.query(Trade).filter_by(user_id=u.id).order_by(Trade.id.desc()).limit(100).all()
    alerts = db.query(Alert).filter_by(user_id=u.id).order_by(Alert.id.desc()).limit(18).all()
    db.commit()  # do not hold a PostgreSQL connection while waiting on Alpaca HTTP
    broker = broker_state() if settings.broker_mode == "alpaca_market_paper" else {"connected": False, "account": {}, "positions": [], "orders": [], "error": "simulator"}
    if broker.get("connected"):
        # Dashboard visits create a sparse snapshot even when the bot is off.
        record_portfolio_snapshot(db, u)
    eq = equity_points(db, u.id, 90)
    metrics = trade_metrics(trades)
    stock_trades = [t for t in trades if str(getattr(t,"engine","stocks") or "stocks") == "stocks"]
    stock_metrics = trade_metrics(stock_trades)
    stock_broker_positions = [p for p in broker.get("positions", []) if str(p.get("symbol") or "").upper() in set(DEFAULT_WATCH)]
    return {
        "request": req, "u": u, "s": u.settings, "trades": trades, "stock_trades": stock_trades,
        "open_trades": [t for t in trades if t.status == "OPEN"], "open_stock_trades": [t for t in stock_trades if t.status == "OPEN"], "alerts": alerts,
        "watch": DEFAULT_WATCH, "selected": selected, "hard_cap": settings.max_daily_loss_hard_cap,
        "market_open": market_is_open(), "broker_mode": settings.broker_mode, "flash": pop_flash(req),
        "broker": broker, "stock_broker_positions": stock_broker_positions, "position_map": {p.get("symbol"): p for p in stock_broker_positions}, "metrics": metrics, "stock_metrics": stock_metrics,
        "indicator_rows": [{**r, "lifecycle": signal_lifecycle(db, u.id, r.get("symbol"))} for r in latest_indicators(db, u.id, sorted(selected))],
        "capital_budget": capital_budget_state(db, u),
        "equity_values": eq, "equity_points": sparkline_points(eq),
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    return templates.TemplateResponse("dashboard.html", _dashboard_context(req, db, u))


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    ctx = _dashboard_context(req, db, u)
    return templates.TemplateResponse("portfolio.html", ctx)


@app.get("/stock/{symbol}", response_class=HTMLResponse)
def stock_page(symbol: str, req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    symbol = symbol.upper().strip()
    selected = set(allowed_symbols(db, u.id))
    db.commit()  # release DB connection before external stock-data HTTP
    if symbol not in selected and symbol not in DEFAULT_WATCH:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    broker = broker_state() if settings.broker_mode == "alpaca_market_paper" else {"connected": False, "positions": [], "orders": []}
    rows = latest_indicators(db, u.id, [symbol])
    position = next((p for p in broker.get("positions", []) if p.get("symbol") == symbol), None)
    trades = db.query(Trade).filter_by(user_id=u.id, symbol=symbol).order_by(Trade.id.desc()).limit(30).all()
    return templates.TemplateResponse("stock.html", {
        "request": req, "u": u, "symbol": symbol, "indicator": rows[0] if rows else {"symbol":symbol,"available":False},
        "position": position, "trades": trades, "broker_mode": settings.broker_mode,
    })


@app.get("/api/chart/portfolio")
def api_portfolio_chart(req: Request, period: str = "1D", db: Session = Depends(get_db)):
    u = require_user(req, db)
    db.commit()  # release DB connection before external portfolio-history HTTP
    if settings.broker_mode != "alpaca_market_paper" or not alpaca_broker.configured:
        return JSONResponse({"ok": False, "points": [], "error": "broker_unavailable"})
    period = (period or "1D").upper()
    timeframe = {"1D":"5Min", "5D":"15Min", "1M":"1H", "3M":"1D", "1Y":"1D", "ALL":"1D"}.get(period, "5Min")
    alpaca_period = {"1D":"1D", "5D":"1W", "1M":"1M", "3M":"3M", "1Y":"1A", "ALL":"all"}.get(period, "1D")
    try:
        h = alpaca_broker.portfolio_history(period=alpaca_period, timeframe=timeframe)
        times = h.get("timestamp") or []
        equity = h.get("equity") or []
        points = [{"t": int(t), "v": float(v)} for t, v in zip(times, equity) if v is not None]
        try:
            account = alpaca_broker.account()
            current_equity = float(account.get("equity") or account.get("portfolio_value") or 0)
            now_ts = int(datetime.now(timezone.utc).timestamp())
            if current_equity > 0 and (not points or abs(points[-1]["v"] - current_equity) > 1e-8 or now_ts - int(points[-1]["t"]) > 30):
                points.append({"t": now_ts, "v": current_equity})
        except Exception:
            pass
        return JSONResponse({"ok": True, "period": period, "points": points})
    except Exception as exc:
        return JSONResponse({"ok": False, "points": [], "error": type(exc).__name__})


@app.get("/api/chart/stock/{symbol}")
def api_stock_chart(symbol: str, req: Request, period: str = "1D", db: Session = Depends(get_db)):
    u = require_user(req, db)
    symbol = symbol.upper().strip()
    selected = set(allowed_symbols(db, u.id))
    if symbol not in selected and symbol not in DEFAULT_WATCH:
        raise HTTPException(status_code=404, detail="Unknown symbol")
    try:
        bars = market_data.chart_bars(symbol, period)
        rows = [{"t": b.get("t"), "o": float(b.get("o",0)), "h": float(b.get("h",0)), "l": float(b.get("l",0)), "c": float(b.get("c",0)), "v": float(b.get("v",0))} for b in bars]
        return JSONResponse({"ok": True, "symbol": symbol, "period": period.upper(), "bars": rows})
    except Exception as exc:
        return JSONResponse({"ok": False, "symbol": symbol, "bars": [], "error": type(exc).__name__})


@app.get("/capital", response_class=HTMLResponse)
def capital_page(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    db.commit()  # release DB connection before broker_state network calls
    broker = broker_state() if settings.broker_mode == "alpaca_market_paper" else {"connected": False, "account": {}, "positions": []}
    budget = capital_budget_state(db, u)
    return templates.TemplateResponse("capital.html", {"request": req, "u": u, "s": u.settings, "broker": broker, "budget": budget, "flash": pop_flash(req)})


@app.post("/capital")
def save_capital_control(req: Request, allocated_capital: float = Form(...), cash_reserve_pct: float = Form(20), allocation_mode: str = Form("opportunity"), stocks_allocation_pct: float = Form(60), options_allocation_pct: float = Form(40), db: Session = Depends(get_db)):
    u = require_user(req, db); s = u.settings
    if s.active or bool(getattr(s, "options_bot_active", False)):
        set_flash(req, "أوقف بوتات التداول قبل تعديل توزيع رأس المال" if u.language == "ar" else "Stop trading bots before changing capital allocation", "error")
        return RedirectResponse("/capital", 303)
    db.commit()  # release DB connection before validating broker cash
    broker_cash = None
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try: broker_cash = max(0.0, float(alpaca_broker.account().get("cash") or 0))
        except Exception: broker_cash = None
    requested = max(1.0, float(allocated_capital))
    if broker_cash is not None and requested > broker_cash + 1e-8:
        set_flash(req, (f"رأس المال المخصص لا يمكن أن يتجاوز نقد الوسيط المتاح ($ {broker_cash:.2f})" if u.language == "ar" else f"Allocated capital cannot exceed available broker cash ($ {broker_cash:.2f})"), "error")
        return RedirectResponse("/capital", 303)
    plans = {
        "opportunity": (100.0, 100.0),
        "stocks_only": (100.0, 0.0),
        "balanced": (50.0, 50.0),
        "stocks_focus": (70.0, 30.0),
        "options_focus": (30.0, 70.0),
        "options_only": (0.0, 100.0),
    }
    mode = allocation_mode if allocation_mode in {*plans.keys(), "manual"} else "opportunity"
    if mode in plans:
        sp, op = plans[mode]
    else:
        sp = max(0.0, min(100.0, float(stocks_allocation_pct))); op = max(0.0, min(100.0, float(options_allocation_pct)))
        if abs((sp + op) - 100.0) > 0.000001:
            set_flash(req, "في التوزيع اليدوي يجب أن يكون مجموع الأسهم والعقود 100% بالضبط" if u.language == "ar" else "In manual allocation, stocks + options must equal exactly 100%", "error")
            return RedirectResponse("/capital", 303)
    set_capital_plan(s, requested, mode, sp, op); s.cash_reserve_pct=max(0.0,min(95.0,float(cash_reserve_pct)))
    db.commit()
    labels_ar={"opportunity":"مرن حسب الفرصة","stocks_only":"أسهم فقط 100/0","balanced":"متوازن 50/50","stocks_focus":"تركيز أسهم 70/30","options_focus":"تركيز عقود 30/70","options_only":"عقود فقط 0/100","manual":"يدوي"}
    labels_en={"opportunity":"Opportunity Pool","stocks_only":"Stocks Only 100/0","balanced":"Balanced 50/50","stocks_focus":"Stocks Focus 70/30","options_focus":"Options Focus 30/70","options_only":"Options Only 0/100","manual":"Manual"}
    set_flash(req, (f"تم حفظ رأس المال وخطة التوزيع: {labels_ar[mode]}" if u.language=="ar" else f"Capital plan saved: {labels_en[mode]}"), "success")
    return RedirectResponse("/capital", 303)


@app.get("/api/live/capital")
def api_live_capital(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    # Release the DB connection before external broker I/O; expire_on_commit=False keeps loaded settings usable.
    db.commit()
    broker_cash = None
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try:
            broker_cash = max(0.0, float(alpaca_broker.account().get("cash") or 0))
        except Exception:
            broker_cash = None
    budget = capital_budget_state(db, u, broker_cash=broker_cash)
    return JSONResponse({"ok": True, "budget": budget, "broker_cash": broker_cash, "server_time": datetime.now(timezone.utc).isoformat()})


@app.get("/reports", response_class=HTMLResponse)
def reports(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    trades = db.query(Trade).filter_by(user_id=u.id).order_by(Trade.id.desc()).all()
    all_metrics = trade_metrics(trades)
    daily = daily_trade_rows(trades, 31)
    monthly = monthly_trade_rows(trades, 12)
    eq = equity_points(db, u.id, 180)
    return templates.TemplateResponse(
        "reports.html",
        {"request": req, "u": u, "metrics": all_metrics, "daily": daily, "monthly": monthly,
         "equity_values": eq, "equity_points": sparkline_points(eq), "broker_mode": settings.broker_mode},
    )


@app.get("/api/status")
def api_status(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    s = u.settings
    payload = {"bot": {"active": s.active, "locked": s.locked, "pnl": s.realized_pnl, "trades": s.trades_today}, "market_open": market_is_open()}
    db.commit()  # release DB connection before broker HTTP
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try:
            a = alpaca_broker.account()
            positions = alpaca_broker.positions()
            equity = float(a.get("equity") or a.get("portfolio_value") or 0)
            last_equity = float(a.get("last_equity") or equity)
            position_rows = []
            stock_symbols = set(DEFAULT_WATCH)
            for p in positions:
                if str(p.get("symbol") or "").upper() not in stock_symbols:
                    continue
                position_rows.append({
                    "symbol": p.get("symbol"), "qty": float(p.get("qty") or 0),
                    "avg_entry_price": float(p.get("avg_entry_price") or 0),
                    "current_price": float(p.get("current_price") or 0),
                    "market_value": float(p.get("market_value") or 0),
                    "unrealized_pl": float(p.get("unrealized_pl") or 0),
                    "unrealized_plpc": float(p.get("unrealized_plpc") or 0) * 100,
                    "change_today": float(p.get("change_today") or 0) * 100,
                })
            payload["broker"] = {
                "connected": True, "equity": equity, "cash": float(a.get("cash") or 0),
                "buying_power": float(a.get("buying_power") or 0), "day_pnl": equity - last_equity,
                "positions": len(position_rows), "position_rows": position_rows,
                "long_market_value": float(a.get("long_market_value") or 0),
            }
        except Exception as exc:
            payload["broker"] = {"connected": False, "error": type(exc).__name__}
    return JSONResponse(payload)


@app.get("/api/live/dashboard")
def api_live_dashboard(req: Request, db: Session = Depends(get_db)):
    """Live dashboard payload. It does not place trades or mutate strategy state."""
    u = require_user(req, db)
    s = u.settings
    selected = sorted(allowed_symbols(db, u.id))
    rows = latest_indicators(db, u.id, selected)
    # Avoid holding a PostgreSQL connection while waiting on Alpaca market-data HTTP.
    db.commit()
    prices = {}
    if market_data.configured and selected:
        try:
            prices = market_data.latest_prices(selected)
        except Exception:
            prices = {}

    indicator_rows = []
    for row in rows:
        item = dict(row)
        symbol = item.get("symbol")
        if symbol in prices:
            item["price"] = prices[symbol]
        gate = entry_gate_status(db, u, symbol=symbol, entry=item.get("price")) if item.get("qualified") else {
            "ok": False, "code": "SIGNAL_NOT_QUALIFIED",
            "message_ar": "الإشارة غير مؤهلة حاليًا", "message_en": "Signal is not currently qualified",
        }
        item["execution"] = gate
        item["lifecycle"] = signal_lifecycle(db, u.id, symbol)
        if item.get("updated_at"):
            item["updated_at"] = item["updated_at"].isoformat()
        indicator_rows.append(item)

    budget = capital_budget_state(db, u, prices)
    # Budget/gate reads are complete; release the connection before broker HTTP calls.
    db.commit()
    payload = {
        "ok": True,
        "language": u.language,
        "market_open": market_is_open(),
        "bot": {"active": s.active, "locked": bool(s.locked or getattr(s,"stocks_risk_locked",False)), "system_locked": s.locked, "stock_risk_locked": bool(getattr(s,"stocks_risk_locked",False)), "options_active": bool(getattr(s,"options_bot_active",False)), "options_risk_locked": bool(getattr(s,"options_risk_locked",False)), "pnl": s.stocks_realized_pnl, "trades": s.trades_today, "max_trades": s.max_trades},
        "budget": budget,
        "indicators": indicator_rows,
        "entry_gate": entry_gate_status(db, u),
    }
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try:
            a = alpaca_broker.account()
            positions = alpaca_broker.positions()
            equity = float(a.get("equity") or a.get("portfolio_value") or 0)
            last_equity = float(a.get("last_equity") or equity)
            position_rows = []
            stock_symbols = set(DEFAULT_WATCH)
            for p in positions:
                if str(p.get("symbol") or "").upper() not in stock_symbols:
                    continue
                position_rows.append({
                    "symbol": p.get("symbol"), "qty": float(p.get("qty") or 0),
                    "avg_entry_price": float(p.get("avg_entry_price") or 0),
                    "current_price": float(p.get("current_price") or 0),
                    "market_value": float(p.get("market_value") or 0),
                    "unrealized_pl": float(p.get("unrealized_pl") or 0),
                    "unrealized_plpc": float(p.get("unrealized_plpc") or 0) * 100,
                    "change_today": float(p.get("change_today") or 0) * 100,
                })
            payload["broker"] = {
                "connected": True, "equity": equity, "cash": float(a.get("cash") or 0),
                "buying_power": float(a.get("buying_power") or 0), "day_pnl": equity - last_equity,
                "positions": len(position_rows), "position_rows": position_rows,
                "long_market_value": float(a.get("long_market_value") or 0),
            }
        except Exception as exc:
            payload["broker"] = {"connected": False, "error": type(exc).__name__}

    position_map = {x.get("symbol"): x for x in (payload.get("broker") or {}).get("position_rows", [])}
    trade_rows = db.query(Trade).filter_by(user_id=u.id, engine="stocks").order_by(Trade.id.desc()).limit(100).all()
    payload["trades"] = []
    for t in trade_rows:
        live = position_map.get(t.symbol) if t.status == "OPEN" else None
        payload["trades"].append({
            "id": t.id, "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None, "symbol": t.symbol,
            "status": t.status, "status_label": code_label(t.status, u.language), "qty": float(t.qty or 0), "entry": float(t.entry or 0),
            "stop_loss": float(t.stop_loss or 0), "take_profit": float(t.take_profit or 0),
            "score": float(t.signal_score or 0), "exit": None if t.exit is None else float(t.exit),
            "pnl": float(live.get("unrealized_pl") if live else (t.pnl or 0)), "is_live": bool(live),
            "reason": t.reason or "", "reason_label": code_label(t.reason, u.language), "engine": getattr(t, "engine", "stocks") or "stocks",
        })
    payload["alerts"] = [{"id": a.id, "title": code_label(a.title, u.language), "body": alert_body_label(a.body, u.language), "created_at": a.created_at.isoformat() if a.created_at else None} for a in db.query(Alert).filter_by(user_id=u.id).order_by(Alert.id.desc()).limit(18).all()]
    db.commit()
    if settings.broker_mode == "alpaca_market_paper" and alpaca_broker.configured:
        try:
            payload["broker_orders"] = [{
                "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
                "qty": o.get("qty"), "filled_avg_price": o.get("filled_avg_price"), "status": o.get("status"),
                "side_label": code_label(str(o.get("side") or "").upper(), u.language),
                "type_label": code_label(str(o.get("type") or "").upper(), u.language),
                "status_label": code_label(str(o.get("status") or "").upper(), u.language),
            } for o in alpaca_broker.orders(status="all", limit=20, nested=True)]
        except Exception:
            payload["broker_orders"] = []
    return JSONResponse(payload)


@app.get("/api/live/indicators")
def api_live_indicators(req: Request, db: Session = Depends(get_db)):
    """Recalculate indicators from fresh 5-minute Alpaca bars without triggering an order."""
    u = require_user(req, db)
    selected = sorted(allowed_symbols(db, u.id))
    db.commit()  # release DB connection before recent-bars HTTP
    if not market_data.configured or not selected:
        return JSONResponse({"ok": False, "indicators": [], "error": "market_data_unavailable"})
    try:
        from .strategy import evaluate_symbol
        bars_by_symbol = market_data.recent_bars(selected)
        out = []
        for symbol in selected:
            state = evaluate_symbol(symbol, bars_by_symbol.get(symbol, []), settings.min_signal_score)
            if not state:
                continue
            state["execution"] = entry_gate_status(db, u, symbol=symbol, entry=state.get("price")) if state.get("qualified") else {
                "ok": False, "code": "SIGNAL_NOT_QUALIFIED",
                "message_ar": "الإشارة غير مؤهلة حاليًا", "message_en": "Signal is not currently qualified",
            }
            state["lifecycle"] = signal_lifecycle(db, u.id, symbol)
            out.append(state)
        return JSONResponse({"ok": True, "indicators": out})
    except Exception as exc:
        return JSONResponse({"ok": False, "indicators": [], "error": type(exc).__name__})


@app.get("/api/live/alert/latest")
def api_latest_alert(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    row = db.query(Alert).filter_by(user_id=u.id).order_by(Alert.id.desc()).first()
    if not row:
        return JSONResponse({"ok": True, "alert": None})
    return JSONResponse({"ok": True, "alert": {"id": row.id, "title": code_label(row.title, u.language), "body": alert_body_label(row.body, u.language), "created_at": row.created_at.isoformat() if row.created_at else None}})


@app.get("/language/{code}")
def change_language(code: str, req: Request, db: Session = Depends(get_db)):
    lang = "en" if code == "en" else "ar"
    req.session["lang"] = lang
    u = current_user(req, db)
    if u:
        u.language = lang
        db.commit()
    referer = req.headers.get("referer") or ("/dashboard" if u else "/")
    if not referer.startswith(str(req.base_url)):
        referer = "/dashboard" if u else "/"
    return RedirectResponse(referer, 303)


@app.post("/settings")
def save_settings(
    req: Request, daily_loss_pct: float = Form(...), risk_per_trade_pct: float = Form(...),
    max_trades: int = Form(...), profit_target_pct: float = Form(...),
    max_position_allocation_pct: float = Form(30), max_open_positions_user: int = Form(3),
    cash_reserve_pct: float = Form(20), language: str = Form("ar"),
    allow_fractional: str = Form("off"), start_mode: str = Form("manual"), scheduled_days: list[str] = Form(default=[]),
    start_delay_minutes: int = Form(0), auto_stop_before_close_minutes: int = Form(5), trade_cooldown_seconds: int = Form(600),
    risk_profile: str = Form("custom"), stop_target_mode: str = Form("atr"), stop_loss_value: float = Form(1),
    take_profit_value: float = Form(2), risk_reward_ratio: float = Form(2),
    stocks_exit_mode: str = Form("trailing"), stocks_trailing_distance_pct: float = Form(1.0), db: Session = Depends(get_db),
):
    u = require_user(req, db)
    s = u.settings
    if s.active or bool(getattr(s, "options_bot_active", False)):
        set_flash(req, "أوقف بوت الأسهم وبوت العقود قبل تعديل إعدادات المخاطر المشتركة" if u.language=="ar" else "Stop both stock and options bots before changing shared risk settings", "error")
        return RedirectResponse("/dashboard", 303)
    s.daily_loss_pct = min(100, max(0, daily_loss_pct))
    s.risk_per_trade_pct = min(100, max(0, risk_per_trade_pct))
    s.max_trades = max(1, min(1000, max_trades))
    s.profit_target_pct = min(100, max(0, profit_target_pct))
    s.max_position_allocation_pct = min(100, max(1, max_position_allocation_pct))
    s.max_open_positions_user = max(1, min(20, max_open_positions_user))
    s.cash_reserve_pct = min(95, max(0, cash_reserve_pct))
    s.allow_fractional = allow_fractional in {"on","true","1","yes"}
    s.start_mode = start_mode if start_mode in {"manual","scheduled","both"} else "manual"
    clean_days = sorted({int(x) for x in scheduled_days if str(x).isdigit() and 0 <= int(x) <= 4})
    s.scheduled_days = ",".join(map(str, clean_days or [0,1,2,3,4]))
    s.start_delay_minutes = max(0, min(120, start_delay_minutes))
    s.auto_stop_before_close_minutes = max(5, min(120, auto_stop_before_close_minutes))
    s.trade_cooldown_seconds = max(0, min(86400, trade_cooldown_seconds))
    # Ready profiles are authoritative for core risk controls. Bot capital is managed only from Capital Control.
    profiles = {
        "very_low": dict(risk=.25, daily=1, target=1.5, max_pos=15, open_pos=2, reserve=35, max_trades=3, stop_mode="risk_reward", stop=0.75, take=1.5, rr=2, cooldown=900),
        "low": dict(risk=.5, daily=2, target=2, max_pos=20, open_pos=3, reserve=30, max_trades=5, stop_mode="risk_reward", stop=1, take=2, rr=2, cooldown=600),
        "medium": dict(risk=1, daily=3, target=3, max_pos=30, open_pos=3, reserve=20, max_trades=8, stop_mode="atr", stop=1, take=2, rr=2, cooldown=300),
        "high": dict(risk=2, daily=5, target=5, max_pos=40, open_pos=4, reserve=15, max_trades=12, stop_mode="fixed_pct", stop=1.5, take=3, rr=2, cooldown=180),
        "very_high": dict(risk=3, daily=7, target=7, max_pos=50, open_pos=5, reserve=10, max_trades=20, stop_mode="fixed_pct", stop=2, take=4, rr=2, cooldown=60),
    }
    s.risk_profile = risk_profile if risk_profile in {*profiles.keys(), "custom"} else "custom"
    s.stop_target_mode = stop_target_mode if stop_target_mode in {"atr","fixed_pct","risk_reward"} else "atr"
    s.stop_loss_value = max(0.1, min(20, stop_loss_value))
    s.take_profit_value = max(0.1, min(50, take_profit_value))
    s.risk_reward_ratio = max(0.5, min(10, risk_reward_ratio))
    s.stocks_exit_mode = stocks_exit_mode if stocks_exit_mode in {"trailing","bracket"} else "trailing"
    s.stocks_trailing_distance_pct = max(0.1, min(20, stocks_trailing_distance_pct))
    if s.risk_profile in profiles:
        p = profiles[s.risk_profile]
        s.risk_per_trade_pct = p["risk"]
        s.daily_loss_pct = min(p["daily"], settings.max_daily_loss_hard_cap)
        s.profit_target_pct = p["target"]
        s.max_position_allocation_pct = p["max_pos"]
        s.max_open_positions_user = p["open_pos"]
        s.cash_reserve_pct = p["reserve"]
        s.max_trades = p["max_trades"]
        s.stop_target_mode = p["stop_mode"]
        s.stop_loss_value = p["stop"]
        s.take_profit_value = p["take"]
        s.risk_reward_ratio = p["rr"]
        s.trade_cooldown_seconds = p["cooldown"]
    u.language = "en" if language == "en" else "ar"
    db.commit()
    set_flash(req, "تم حفظ الإعدادات" if u.language=="ar" else "Settings saved", "success")
    return RedirectResponse("/dashboard", 303)


@app.post("/stocks")
def save_stocks(req: Request, symbols: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    u = require_user(req, db)
    if u.settings.active:
        set_flash(req, "أوقف البوت قبل تغيير الأسهم" if u.language=="ar" else "Stop the bot before changing stocks", "error")
        return RedirectResponse("/dashboard", 303)
    try:
        saved = set_allowed_symbols(db, u.id, symbols)
        set_flash(req, (f"الأسهم المسموحة: {', '.join(saved)}" if u.language=="ar" else f"Allowed stocks: {', '.join(saved)}"), "success")
    except ValueError:
        set_flash(req, "اختر سهمًا واحدًا على الأقل" if u.language=="ar" else "Select at least one stock", "error")
    return RedirectResponse("/dashboard", 303)


@app.post("/bot/start")
def bot_start(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    ok, msg = start_session(db, u, settings.max_daily_loss_hard_cap)
    set_flash(req, ("تم تشغيل البوت" if u.language=="ar" else "Bot started") if ok else ((f"لم يبدأ البوت: {msg}") if u.language=="ar" else f"Bot did not start: {msg}"), "success" if ok else "error")
    return RedirectResponse("/dashboard", 303)


@app.post("/bot/stop")
def bot_stop(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    stop_session(db, u)
    set_flash(req, "تم إيقاف البوت وإغلاق مراكز الجلسة" if u.language=="ar" else "Bot stopped and session positions closed", "info")
    return RedirectResponse("/dashboard", 303)


@app.post("/bot/simulate")
def bot_sim(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    t = run_user_cycle(db, u)
    set_flash(req, ((f"تم تنفيذ/تحديث صفقة: {t.symbol}" if u.language=="ar" else f"Trade: {t.symbol}") if t else ("تم الفحص؛ لا توجد إشارة مؤهلة الآن" if u.language=="ar" else "Scan complete; no qualified trade")), "info")
    return RedirectResponse("/dashboard", 303)


@app.post("/positions/{symbol}/close")
def close_position_route(symbol: str, req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    try:
        result = close_user_position_partial(db, u, symbol, percentage=100)
        state = getattr(result, "_close_submission_status", "filled")
        set_flash(req, ("تم قبول أمر البيع وهو بانتظار التنفيذ" if state=="pending" else "تم تنفيذ البيع وإغلاق المركز") if u.language=="ar" else ("Sell accepted and pending fill" if state=="pending" else "Sell filled and position closed"), "info" if state=="pending" else "success")
    except ValueError as exc:
        set_flash(req, (("تعذر تنفيذ البيع: " + str(exc)) if u.language=="ar" else ("Sell failed: " + str(exc))), "error")
    return RedirectResponse(req.headers.get("referer") or "/dashboard", 303)


@app.post("/positions/{symbol}/partial-close")
def partial_close_position_route(symbol: str, req: Request, qty: float = Form(...), db: Session = Depends(get_db)):
    u = require_user(req, db)
    try:
        if qty <= 0:
            raise ValueError("أدخل كمية بيع أكبر من صفر" if u.language=="ar" else "Enter a sell quantity greater than zero")
        result = close_user_position_partial(db, u, symbol, qty=qty)
        state = getattr(result, "_close_submission_status", "filled")
        if state == "pending":
            set_flash(req, "تم قبول أمر البيع لدى Alpaca وهو بانتظار التنفيذ، وسيتم تحديث السجل تلقائياً عند التنفيذ." if u.language=="ar" else "Sell order accepted by Alpaca and is pending fill; the ledger will reconcile automatically.", "info")
        else:
            set_flash(req, "تم تنفيذ البيع وتحديث الصفقة" if u.language=="ar" else "Sell filled and position updated", "success")
    except ValueError as exc:
        set_flash(req, (("تعذر تنفيذ البيع: " + str(exc)) if u.language=="ar" else ("Sell failed: " + str(exc))), "error")
    return RedirectResponse(req.headers.get("referer") or "/dashboard", 303)


@app.get("/indicators", response_class=HTMLResponse)
def indicator_studio(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    rows = db.query(CustomIndicator).filter_by(user_id=u.id).order_by(CustomIndicator.id.desc()).all()
    indicator_universe = list(dict.fromkeys(["SPX", "QQQ", "SPY"] + list(DEFAULT_WATCH)))
    return templates.TemplateResponse("indicators.html", {"request":req,"u":u,"rows":rows,"watch":indicator_universe,"selected":set(allowed_symbols(db,u.id)),"flash":pop_flash(req)})


@app.post("/indicators")
async def save_custom_indicator(req: Request, name: str = Form(...), source_code: str = Form(""), source_type: str = Form("pine_reference"),
                          role: str = Form("display"), weight: float = Form(0), timeframe: str = Form("5m"),
                          symbols: list[str] = Form(default=[]), enabled: str = Form("on"), pine_file: UploadFile | None = File(default=None), db: Session = Depends(get_db)):
    u = require_user(req, db)
    name = name.strip()[:120]
    if not name:
        set_flash(req, "اسم المؤشر مطلوب" if u.language=="ar" else "Indicator name is required", "error"); return RedirectResponse("/indicators",303)
    if pine_file and pine_file.filename:
        raw = await pine_file.read()
        if len(raw) > 300_000:
            set_flash(req, "ملف Pine أكبر من الحد المسموح" if u.language=="ar" else "Pine file is too large", "error"); return RedirectResponse("/indicators",303)
        try:
            source_code = raw.decode("utf-8")
        except UnicodeDecodeError:
            set_flash(req, "ملف Pine يجب أن يكون UTF-8" if u.language=="ar" else "Pine file must be UTF-8", "error"); return RedirectResponse("/indicators",303)
    result = convert_pine_to_python(source_code) if source_type.startswith("pine") else None
    row = CustomIndicator(
        user_id=u.id, name=name, source_type=source_type[:24], source_code=source_code[:300000],
        enabled=enabled in {"on","1","true"}, role=role if role in {"display","confirm","filter"} else "display",
        weight=max(0,min(100,weight)), timeframe=timeframe[:16], symbols=",".join(symbols) if symbols else "*",
        compile_status=(result.status if result else "COMPLETE"), compile_progress=(result.progress if result else 100),
        compile_error=("\n".join(result.errors + result.warnings)[:10000] if result else ""),
        compiled_python=(result.python_code[:300000] if result else ""), supported_pct=(result.supported_pct if result else 100),
        validation_status=("READY_FOR_TEST" if result and result.status=="COMPLETE" else ("NATIVE" if not result else "NEEDS_REVIEW")),
    )
    # Safety rule: incomplete imports cannot affect entries until reviewed.
    if result and result.status != "COMPLETE":
        row.enabled = False
        row.role = "display"
    db.add(row); db.commit()
    if result and result.status == "COMPLETE":
        msg = "✅ اكتمل تحويل المؤشر إلى Python واجتاز فحص الصياغة" if u.language=="ar" else "✅ Pine converted to Python and syntax validation passed"
        kind = "success"
    elif result:
        msg = "⚠️ تم الحفظ لكن التحويل يحتاج مراجعة؛ لن يؤثر على التداول" if u.language=="ar" else "⚠️ Saved, but conversion needs review and cannot affect trading yet"
        kind = "info"
    else:
        msg = "تم حفظ المؤشر" if u.language=="ar" else "Indicator saved"; kind="success"
    set_flash(req, msg, kind)
    return RedirectResponse("/indicators",303)


@app.get("/indicators/{indicator_id}/edit", response_class=HTMLResponse)
def edit_indicator_page(indicator_id: int, req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    row = db.query(CustomIndicator).filter_by(id=indicator_id, user_id=u.id).first()
    if not row: raise HTTPException(404)
    universe = list(dict.fromkeys(["SPX", "QQQ", "SPY"] + list(DEFAULT_WATCH)))
    selected = set() if row.symbols == "*" else {x for x in row.symbols.split(",") if x}
    return templates.TemplateResponse("indicator_edit.html", {"request": req, "u": u, "row": row, "watch": universe, "selected_symbols": selected, "flash": pop_flash(req)})


@app.post("/indicators/{indicator_id}/edit")
async def edit_indicator(indicator_id: int, req: Request, name: str = Form(...), source_code: str = Form(""), source_type: str = Form("pine_reference"), role: str = Form("display"), weight: float = Form(0), timeframe: str = Form("5m"), symbols: list[str] = Form(default=[]), enabled: str = Form("off"), pine_file: UploadFile | None = File(default=None), db: Session = Depends(get_db)):
    u = require_user(req, db)
    row = db.query(CustomIndicator).filter_by(id=indicator_id, user_id=u.id).first()
    if not row: raise HTTPException(404)
    if pine_file and pine_file.filename:
        raw = await pine_file.read()
        if len(raw) > 300_000:
            set_flash(req, "ملف Pine أكبر من الحد المسموح" if u.language=="ar" else "Pine file is too large", "error"); return RedirectResponse(f"/indicators/{indicator_id}/edit",303)
        source_code = raw.decode("utf-8")
    result = convert_pine_to_python(source_code) if source_type.startswith("pine") else None
    row.name = name.strip()[:120] or row.name; row.source_type = source_type[:24]; row.source_code = source_code[:300000]
    row.role = role if role in {"display","confirm","filter"} else "display"; row.weight = max(0,min(100,weight)); row.timeframe = timeframe[:16]; row.symbols = ",".join(symbols) if symbols else "*"
    row.compile_status = result.status if result else "COMPLETE"; row.compile_progress = result.progress if result else 100; row.compile_error = "\n".join(result.errors + result.warnings)[:10000] if result else ""; row.compiled_python = result.python_code[:300000] if result else ""; row.supported_pct = result.supported_pct if result else 100
    row.validation_status = "READY_FOR_TEST" if result and result.status == "COMPLETE" else ("NATIVE" if not result else "NEEDS_REVIEW")
    row.enabled = enabled in {"on","1","true"}
    if result and result.status != "COMPLETE": row.enabled = False; row.role = "display"
    db.commit(); set_flash(req, "تم تحديث المؤشر وإعادة فحصه" if u.language=="ar" else "Indicator updated and revalidated", "success" if (not result or result.status=="COMPLETE") else "info")
    return RedirectResponse("/indicators",303)


@app.post("/indicators/{indicator_id}/toggle")
def toggle_custom_indicator(indicator_id: int, req: Request, db: Session = Depends(get_db)):
    u=require_user(req,db); row=db.query(CustomIndicator).filter_by(id=indicator_id,user_id=u.id).first()
    if not row: raise HTTPException(404)
    row.enabled=not row.enabled; db.commit(); set_flash(req, ("تم تفعيل المؤشر" if row.enabled else "تم إيقاف المؤشر") if u.language=="ar" else ("Indicator enabled" if row.enabled else "Indicator disabled"), "success" if row.enabled else "info"); return RedirectResponse("/indicators",303)


@app.post("/indicators/{indicator_id}/delete")
def delete_custom_indicator(indicator_id: int, req: Request, db: Session = Depends(get_db)):
    u=require_user(req,db); row=db.query(CustomIndicator).filter_by(id=indicator_id,user_id=u.id).first()
    if not row: raise HTTPException(404)
    db.delete(row); db.commit(); set_flash(req, "تم حذف المؤشر" if u.language=="ar" else "Indicator deleted", "success"); return RedirectResponse("/indicators",303)


@app.get("/indexes", response_class=HTMLResponse)
def indexes_dashboard(req: Request, db: Session = Depends(get_db)):
    require_user(req, db)
    return RedirectResponse("/options", 303)


@app.post("/indexes/settings")
def indexes_settings(req: Request, symbols: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    u = require_user(req, db)
    clean = [x for x in symbols if x in INDEX_PRODUCTS]
    u.settings.index_symbols = ",".join(clean or ["QQQ","SPX"])
    db.commit()
    set_flash(req, "تم حفظ أصول صفحة المؤشرات" if u.language=="ar" else "Index instruments saved", "success")
    return RedirectResponse("/indexes",303)


@app.post("/indexes/start")
def indexes_start(req: Request, db: Session = Depends(get_db)):
    u=require_user(req,db); ok,msg=start_index_bot(db,u)
    set_flash(req, ("تم تشغيل بوت المؤشرات" if u.language=="ar" else "Index bot started") if ok else msg, "success" if ok else "error")
    return RedirectResponse("/indexes",303)


@app.post("/indexes/stop")
def indexes_stop(req: Request, close_positions: str = Form("off"), db: Session = Depends(get_db)):
    u=require_user(req,db); stop_index_bot(db,u,close_positions in {"on","1","true"})
    set_flash(req, "تم إيقاف بوت المؤشرات" if u.language=="ar" else "Index bot stopped", "info")
    return RedirectResponse("/indexes",303)



@app.get("/options", response_class=HTMLResponse)
def options_dashboard(req: Request, db: Session = Depends(get_db)):
    u=require_user(req,db); s=u.settings
    selected=selected_option_symbols(u)
    option_trades=db.query(Trade).filter_by(user_id=u.id,engine="options").order_by(Trade.id.desc()).limit(60).all()
    db.commit()  # release PostgreSQL while the options page performs remote market-data calls
    support=option_support_map(selected)
    underlying=(req.query_params.get("underlying") or (selected[0] if selected else "QQQ")).upper()
    ctype=(req.query_params.get("type") or "call").lower(); ctype=ctype if ctype in {"call","put"} else "call"
    # The browser chain is loaded by /api/live/options after the page renders.
    # This avoids fetching the same heavy option snapshot twice on initial load.
    rows=[]; chain_error=""
    try: underlying_states=option_underlying_states(OPTION_UNIVERSE)
    except Exception: underlying_states={}
    try: spx_reference=market_data.spx_reference()
    except Exception: spx_reference={"symbol":"SPX","price":None,"source":"S&P 500 CASH INDEX REFERENCE","provider":"reference unavailable","timestamp":None}
    return templates.TemplateResponse("options.html", {"request":req,"u":u,"s":s,"universe":OPTION_UNIVERSE,"selected_options":set(selected),"support":support,"underlying":underlying,"ctype":ctype,"contracts":rows,"chain_error":chain_error,"capital_budget":capital_budget_state(db,u),"market_open":market_is_open(),"option_trades":option_trades,"underlying_states":underlying_states,"spx_reference":spx_reference,"options_feed":str(getattr(settings,"alpaca_options_data_feed","indicative") or "indicative"),"flash":pop_flash(req)})

@app.get("/api/live/options")
def api_live_options(req: Request, underlying: str = "", contract_type: str = "call", include_chain: int = 0, db: Session = Depends(get_db)):
    u = require_user(req, db)
    selected = selected_option_symbols(u)
    db.commit()  # release DB connection before external market-data requests
    stock_like = [x for x in OPTION_UNIVERSE if x != "SPX"]
    # Use the exact same stock-price source as the stock dashboard/engine:
    # Alpaca latest stock bar close. Quote metadata is optional display context only.
    cards = {}
    if stock_like:
        try:
            stock_prices = market_data.latest_prices(stock_like)
        except Exception:
            stock_prices = {}
        try:
            stock_meta = market_data.latest_price_details(stock_like)
        except Exception:
            stock_meta = {}
        for sym in stock_like:
            meta = dict(stock_meta.get(sym) or {})
            px = stock_prices.get(sym)
            if px is not None:
                meta["price"] = float(px)
                meta["source"] = "latest_bar_close"
            if meta:
                cards[sym] = meta
    if "SPX" in OPTION_UNIVERSE:
        try:
            ref = market_data.spx_reference()
            cards["SPX"] = {**ref, "feed": "cash-index-reference"}
        except Exception as exc:
            cards["SPX"] = {"price": None, "timestamp": None, "feed": "cash-index-reference", "source": "S&P 500 CASH INDEX REFERENCE", "error": type(exc).__name__}
    try: states = option_underlying_states(OPTION_UNIVERSE)
    except Exception: states = {}
    chain=[]; chain_error=""
    target=(underlying or (selected[0] if selected else "QQQ")).upper()
    if bool(include_chain) and target in selected:
        try:
            spot = float((cards.get(target) or {}).get("price") or 0)
            chain=contract_browser(
                target,
                contract_type if contract_type in {"call","put"} else "call",
                int(u.settings.options_min_dte),
                int(u.settings.options_max_dte),
                60,
                underlying_price=spot,
            )
        except Exception as exc:
            chain_error=type(exc).__name__
    option_rows = db.query(Trade).filter_by(user_id=u.id, engine="options").order_by(Trade.id.desc()).limit(60).all()
    option_trades = [{
        "id": t.id, "symbol": t.symbol, "status": t.status, "status_label": code_label(t.status, u.language),
        "qty": float(t.qty or 0), "entry": float(t.entry or 0), "stop_loss": float(t.stop_loss or 0),
        "take_profit": float(t.take_profit or 0), "pnl": float(t.pnl or 0),
        "trailing_enabled": bool(getattr(t,"trailing_enabled",False)), "trailing_active": bool(getattr(t,"trailing_active",False)),
        "trailing_activation_price": float(getattr(t,"trailing_activation_price",0) or 0), "trailing_high_watermark": float(getattr(t,"trailing_high_watermark",0) or 0),
        "trailing_stop_price": float(getattr(t,"trailing_stop_price",0) or 0), "trailing_distance_pct": float(getattr(t,"trailing_distance_pct",0) or 0),
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    } for t in option_rows]
    budget = capital_budget_state(db, u)
    return JSONResponse({
        "ok":True,"cards":cards,"states":states,"underlying":target,"contracts":chain,"chain_error":chain_error,
        "option_trades": option_trades, "capital_budget": budget,
        "engine_status": {"stocks_active": bool(s.active), "stocks_risk_locked": bool(getattr(s,"stocks_risk_locked",False)), "options_active": bool(getattr(s,"options_bot_active",False)), "options_risk_locked": bool(getattr(s,"options_risk_locked",False)), "system_locked": bool(s.locked), "market_open": market_is_open()},
        "server_time":datetime.now(timezone.utc).isoformat()
    })


@app.post("/options/settings")
def options_settings(req:Request, symbols:list[str]=Form(default=[]), contract_type:str=Form("auto"), min_dte:int=Form(0), max_dte:int=Form(7), target_delta:float=Form(.50), max_contracts:int=Form(1), max_allocation_pct:float=Form(20), options_risk_per_trade_pct:float=Form(2), options_daily_loss_pct:float=Form(5), options_max_open_positions:int=Form(2), options_trade_cooldown_seconds:int=Form(300), stop_loss_pct:float=Form(30), take_profit_pct:float=Form(50), options_exit_mode:str=Form("trailing"), trailing_activation_pct:float=Form(40), trailing_distance_pct:float=Form(20), options_max_trades:int=Form(5), options_start_mode:str=Form("manual"), options_scheduled_days:list[str]=Form(default=[]), options_start_delay_minutes:int=Form(0), options_auto_stop_before_close_minutes:int=Form(5), db:Session=Depends(get_db)):
    u=require_user(req,db); clean=[x.upper() for x in symbols if x.upper() in OPTION_UNIVERSE]
    if bool(getattr(u.settings, "options_bot_active", False)):
        set_flash(req,"أوقف بوت العقود قبل تعديل إعداداته" if u.language=="ar" else "Stop the options bot before changing its settings","error"); return RedirectResponse("/options",303)
    u.settings.options_symbols=",".join(clean or ["QQQ"]); u.settings.options_contract_type=contract_type if contract_type in {"auto","call","put"} else "auto"
    u.settings.options_min_dte=max(0,min(365,int(min_dte))); u.settings.options_max_dte=max(u.settings.options_min_dte,min(730,int(max_dte))); u.settings.options_target_delta=max(.10,min(.90,float(target_delta)))
    u.settings.options_max_contracts=max(1,min(100,int(max_contracts))); u.settings.options_max_allocation_pct=max(1,min(100,float(max_allocation_pct))); u.settings.options_risk_per_trade_pct=max(0,min(100,float(options_risk_per_trade_pct))); u.settings.options_daily_loss_pct=max(0,min(100,float(options_daily_loss_pct))); u.settings.options_max_open_positions=max(1,min(20,int(options_max_open_positions))); u.settings.options_trade_cooldown_seconds=max(0,min(86400,int(options_trade_cooldown_seconds))); u.settings.options_stop_loss_pct=max(5,min(95,float(stop_loss_pct))); u.settings.options_take_profit_pct=max(5,min(500,float(take_profit_pct))); u.settings.options_max_trades=max(1,min(100,int(options_max_trades)))
    u.settings.options_exit_mode=options_exit_mode if options_exit_mode in {"trailing","fixed"} else "trailing"
    u.settings.options_trailing_activation_pct=max(5,min(500,float(trailing_activation_pct)))
    u.settings.options_trailing_distance_pct=max(2,min(80,float(trailing_distance_pct)))
    u.settings.options_start_mode=options_start_mode if options_start_mode in {"manual","scheduled","both"} else "manual"
    clean_days=sorted({int(x) for x in options_scheduled_days if str(x).isdigit() and 0 <= int(x) <= 4})
    u.settings.options_scheduled_days=",".join(map(str,clean_days or [0,1,2,3,4]))
    u.settings.options_start_delay_minutes=max(0,min(120,int(options_start_delay_minutes)))
    u.settings.options_auto_stop_before_close_minutes=max(5,min(120,int(options_auto_stop_before_close_minutes)))
    db.commit(); set_flash(req,"تم حفظ إعدادات وجدولة العقود" if u.language=="ar" else "Options settings and schedule saved","success"); return RedirectResponse("/options",303)

@app.post("/options/start")
def options_start(req:Request,db:Session=Depends(get_db)):
    u=require_user(req,db); ok,msg=start_options_bot(db,u); set_flash(req,("تم تشغيل بوت العقود" if u.language=="ar" else "Options bot started") if ok else msg,"success" if ok else "error"); return RedirectResponse("/options",303)

@app.post("/options/stop")
def options_stop(req:Request,close_positions:str=Form("off"),db:Session=Depends(get_db)):
    u=require_user(req,db); stop_options_bot(db,u,close_positions in {"1","on","true"}); set_flash(req,"تم إيقاف بوت العقود" if u.language=="ar" else "Options bot stopped","info"); return RedirectResponse("/options",303)

@app.get("/admin", response_class=HTMLResponse)
def admin(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    if not u.is_admin:
        raise HTTPException(403)
    users = db.query(User).order_by(User.id).all()
    active_count = db.query(BotSettings).filter_by(active=True).count()
    trade_count = db.query(Trade).count()
    return templates.TemplateResponse(
        "admin.html", {"request": req, "u": u, "users": users, "active_count": active_count, "trade_count": trade_count, "user_count": len(users)}
    )


@app.get("/admin/diagnostics")
def admin_diagnostics(req: Request, db: Session = Depends(get_db)):
    u = require_user(req, db)
    if not u.is_admin:
        raise HTTPException(403)
    pool = engine.pool
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KB; diagnostics are intentionally coarse and contain no secrets.
    return {
        "ok": True,
        "uptime_seconds": int(max(0, _process_time.time() - PROCESS_STARTED_AT)),
        "process_rss_mb": round(float(rss_kb) / 1024.0, 2),
        "threads": threading.active_count(),
        "db_pool": {
            "size": getattr(pool, "size", lambda: None)(),
            "checked_out": getattr(pool, "checkedout", lambda: None)(),
            "overflow": getattr(pool, "overflow", lambda: None)(),
        },
    }


@app.get("/health")
def health():
    # Deliberately dependency-free so Render health checks are never blocked by Alpaca/Postgres.
    return {"ok": True, "app": "Luqman Trade", "broker_mode": settings.broker_mode}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(__import__("sqlalchemy").text("SELECT 1"))
    return {"ok": True, "database": "ready"}
