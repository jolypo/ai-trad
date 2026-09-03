from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


database_url = settings.database_url.strip()

# Render commonly provides postgresql:// while this project installs psycopg v3.
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine_kwargs = {"connect_args": connect_args, "pool_pre_ping": True}
if not database_url.startswith("sqlite"):
    # Render + frequent live polling can create short bursts of concurrent requests.
    # Keep the pool deliberately bounded and recycle stale connections instead of
    # allowing a large local queue to hang for 30 seconds and surface as a 502.
    engine_kwargs.update({
        "pool_size": 5,
        "max_overflow": 5,
        "pool_timeout": 8,
        "pool_recycle": 300,
    })
engine = create_engine(database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_additive_schema():
    """Add new optional columns without touching or recreating existing data.

    SQLAlchemy create_all() does not ALTER an existing table, so this tiny
    additive migration keeps older Render PostgreSQL databases compatible.
    """
    inspector = inspect(engine)
    if "bot_settings" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("bot_settings")}
    additions = {
        "current_bot_capital": "FLOAT NULL",
        "stocks_target_capital": "FLOAT NULL",
        "options_target_capital": "FLOAT NULL",
        "stocks_current_capital": "FLOAT NULL",
        "options_current_capital": "FLOAT NULL",
        "stocks_excess_realized_profit": "FLOAT NOT NULL DEFAULT 0",
        "options_excess_realized_profit": "FLOAT NOT NULL DEFAULT 0",
        "excess_realized_profit": "FLOAT NOT NULL DEFAULT 0",
        "stocks_risk_locked": "BOOLEAN NOT NULL DEFAULT FALSE",
        "stocks_realized_pnl": "FLOAT NOT NULL DEFAULT 0",
        "options_realized_pnl": "FLOAT NOT NULL DEFAULT 0",
        "broker_reconciliation_required": "BOOLEAN NOT NULL DEFAULT FALSE",
        "max_position_allocation_pct": "FLOAT NOT NULL DEFAULT 30",
        "max_open_positions_user": "INTEGER NOT NULL DEFAULT 3",
        "cash_reserve_pct": "FLOAT NOT NULL DEFAULT 20",
        "allow_fractional": "BOOLEAN NOT NULL DEFAULT FALSE",
        "start_mode": "VARCHAR(16) NOT NULL DEFAULT 'manual'",
        "scheduled_days": "VARCHAR(32) NOT NULL DEFAULT '0,1,2,3,4'",
        "start_delay_minutes": "INTEGER NOT NULL DEFAULT 0",
        "auto_stop_before_close_minutes": "INTEGER NOT NULL DEFAULT 5",
        "trade_cooldown_seconds": "INTEGER NOT NULL DEFAULT 600",
        "risk_profile": "VARCHAR(24) NOT NULL DEFAULT 'custom'",
        "stop_target_mode": "VARCHAR(24) NOT NULL DEFAULT 'atr'",
        "stop_loss_value": "FLOAT NOT NULL DEFAULT 1",
        "take_profit_value": "FLOAT NOT NULL DEFAULT 2",
        "stocks_exit_mode": "VARCHAR(16) NOT NULL DEFAULT 'trailing'",
        "stocks_trailing_distance_pct": "FLOAT NOT NULL DEFAULT 1",
        "risk_reward_ratio": "FLOAT NOT NULL DEFAULT 2",
        "index_bot_active": "BOOLEAN NOT NULL DEFAULT FALSE",
        "index_symbols": "VARCHAR(64) NOT NULL DEFAULT 'QQQ,SPX'",
        "index_execution_mode": "VARCHAR(24) NOT NULL DEFAULT 'safe'",
        "options_bot_active": "BOOLEAN NOT NULL DEFAULT FALSE",
        "options_start_mode": "VARCHAR(16) NOT NULL DEFAULT 'manual'",
        "options_scheduled_days": "VARCHAR(32) NOT NULL DEFAULT '0,1,2,3,4'",
        "options_start_delay_minutes": "INTEGER NOT NULL DEFAULT 0",
        "options_auto_stop_before_close_minutes": "INTEGER NOT NULL DEFAULT 5",
        "options_max_trades": "INTEGER NOT NULL DEFAULT 5",
        "options_trades_today": "INTEGER NOT NULL DEFAULT 0",
        "options_last_trade_at": "TIMESTAMP NULL",
        "options_session_started_at": "TIMESTAMP NULL",
        "options_session_stopped_at": "TIMESTAMP NULL",
        "options_symbols": "TEXT NOT NULL DEFAULT 'QQQ,AAPL,MSFT,NVDA,AMD,AMZN,META,TSLA'",
        "options_contract_type": "VARCHAR(12) NOT NULL DEFAULT 'auto'",
        "options_min_dte": "INTEGER NOT NULL DEFAULT 0",
        "options_max_dte": "INTEGER NOT NULL DEFAULT 7",
        "options_target_delta": "FLOAT NOT NULL DEFAULT 0.50",
        "options_max_contracts": "INTEGER NOT NULL DEFAULT 1",
        "options_max_allocation_pct": "FLOAT NOT NULL DEFAULT 20",
        "options_risk_per_trade_pct": "FLOAT NOT NULL DEFAULT 2",
        "options_daily_loss_pct": "FLOAT NOT NULL DEFAULT 5",
        "options_max_open_positions": "INTEGER NOT NULL DEFAULT 2",
        "options_trade_cooldown_seconds": "INTEGER NOT NULL DEFAULT 300",
        "options_risk_locked": "BOOLEAN NOT NULL DEFAULT FALSE",
        "options_stop_loss_pct": "FLOAT NOT NULL DEFAULT 30",
        "options_take_profit_pct": "FLOAT NOT NULL DEFAULT 50",
        "options_exit_mode": "VARCHAR(16) NOT NULL DEFAULT 'trailing'",
        "options_trailing_activation_pct": "FLOAT NOT NULL DEFAULT 40",
        "options_trailing_distance_pct": "FLOAT NOT NULL DEFAULT 20",
        "allocation_mode": "VARCHAR(16) NOT NULL DEFAULT 'dynamic'",
        "stocks_allocation_pct": "FLOAT NOT NULL DEFAULT 60",
        "options_allocation_pct": "FLOAT NOT NULL DEFAULT 40",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE bot_settings ADD COLUMN {name} {ddl}"))

    # v23 one-time bootstrap for the independent stock/options current buckets.
    # The old schema only knew one Current Bot Capital.  When possible, use the
    # user's historical realized P&L by engine to assign an existing drawdown to
    # the engine that actually produced it instead of blindly splitting the loss.
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, user_id, capital, current_bot_capital, allocation_mode,
                   stocks_allocation_pct, options_allocation_pct,
                   stocks_target_capital, options_target_capital,
                   stocks_current_capital, options_current_capital
            FROM bot_settings
        """)).mappings().all()
        for row in rows:
            if row["stocks_target_capital"] is not None and row["options_target_capital"] is not None \
               and row["stocks_current_capital"] is not None and row["options_current_capital"] is not None:
                continue
            target = max(0.0, float(row["capital"] or 0))
            current = max(0.0, min(target, float(row["current_bot_capital"] if row["current_bot_capital"] is not None else target)))
            mode = str(row["allocation_mode"] or "opportunity")
            presets = {
                "stocks_only": (100.0, 0.0), "balanced": (50.0, 50.0),
                "stocks_focus": (70.0, 30.0), "options_focus": (30.0, 70.0),
                "options_only": (0.0, 100.0),
            }
            if mode == "dynamic":
                mode = "opportunity"
            if mode == "fixed":
                mode = "manual"
            sp = max(0.0, min(100.0, float(row["stocks_allocation_pct"] or 0)))
            op = max(0.0, min(100.0, float(row["options_allocation_pct"] or 0)))
            if mode in presets:
                sp, op = presets[mode]
            # Opportunity Pool remains a deliberately shared pool; bucket values
            # are initialized to the total only for safe future plan switching.
            if mode == "opportunity":
                st, ot, sc, oc = target, target, current, current
            else:
                st, ot = target * sp / 100.0, target * op / 100.0
                alloc_target = st + ot
                alloc_current = min(current, alloc_target)
                drawdown = max(0.0, alloc_target - alloc_current)
                pnl_rows = conn.execute(text("""
                    SELECT engine, COALESCE(SUM(pnl), 0) AS pnl
                    FROM trades WHERE user_id=:uid GROUP BY engine
                """), {"uid": row["user_id"]}).mappings().all()
                pnl = {str(x["engine"] or "stocks"): float(x["pnl"] or 0) for x in pnl_rows}
                sl = max(0.0, -pnl.get("stocks", 0.0))
                ol = max(0.0, -pnl.get("options", 0.0))
                if drawdown > 0 and sl + ol > 1e-9:
                    stock_dd = drawdown * sl / (sl + ol)
                elif alloc_target > 1e-9:
                    stock_dd = drawdown * st / alloc_target
                else:
                    stock_dd = 0.0
                option_dd = drawdown - stock_dd
                sc = max(0.0, min(st, st - stock_dd))
                oc = max(0.0, min(ot, ot - option_dd))
            conn.execute(text("""
                UPDATE bot_settings SET
                    stocks_target_capital=:st, options_target_capital=:ot,
                    stocks_current_capital=:sc, options_current_capital=:oc
                WHERE id=:id
            """), {"st": st, "ot": ot, "sc": sc, "oc": oc, "id": row["id"]})

    inspector = inspect(engine)
    if "custom_indicators" in inspector.get_table_names():
        existing_ci = {c["name"] for c in inspector.get_columns("custom_indicators")}
        ci_additions = {
            "compile_status": "VARCHAR(24) NOT NULL DEFAULT 'PENDING'",
            "compile_progress": "INTEGER NOT NULL DEFAULT 0",
            "compile_error": "TEXT NOT NULL DEFAULT ''",
            "compiled_python": "TEXT NOT NULL DEFAULT ''",
            "supported_pct": "FLOAT NOT NULL DEFAULT 0",
            "validation_status": "VARCHAR(24) NOT NULL DEFAULT 'NOT_TESTED'",
        }
        with engine.begin() as conn:
            for name, ddl in ci_additions.items():
                if name not in existing_ci:
                    conn.execute(text(f"ALTER TABLE custom_indicators ADD COLUMN {name} {ddl}"))


def ensure_trade_schema():
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("trades")}
    additions = {
        "engine": "VARCHAR(24) NOT NULL DEFAULT 'stocks'",
        "trailing_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
        "trailing_active": "BOOLEAN NOT NULL DEFAULT FALSE",
        "trailing_activation_price": "FLOAT NULL",
        "trailing_high_watermark": "FLOAT NULL",
        "trailing_stop_price": "FLOAT NULL",
        "trailing_distance_pct": "FLOAT NULL",
        "trailing_activated_at": "TIMESTAMP NULL",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {name} {ddl}"))
