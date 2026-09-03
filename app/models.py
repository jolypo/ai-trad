from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .db import Base


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    language = Column(String(2), default="ar", nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)

    settings = relationship("BotSettings", uselist=False, back_populates="user", cascade="all,delete-orphan")
    allowed_symbols = relationship("AllowedSymbol", back_populates="user", cascade="all,delete-orphan")


class BotSettings(Base):
    __tablename__ = "bot_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    capital = Column(Float, default=10000, nullable=False)  # Target Bot Capital
    current_bot_capital = Column(Float, nullable=True)
    # v23: persistent engine buckets. Fixed allocation plans no longer re-split
    # the combined current balance after one engine wins or loses.
    stocks_target_capital = Column(Float, nullable=True)
    options_target_capital = Column(Float, nullable=True)
    stocks_current_capital = Column(Float, nullable=True)
    options_current_capital = Column(Float, nullable=True)
    stocks_excess_realized_profit = Column(Float, default=0, nullable=False)
    options_excess_realized_profit = Column(Float, default=0, nullable=False)
    excess_realized_profit = Column(Float, default=0, nullable=False)
    broker_reconciliation_required = Column(Boolean, default=False, nullable=False)
    daily_loss_pct = Column(Float, default=2, nullable=False)
    risk_per_trade_pct = Column(Float, default=0.5, nullable=False)  # stocks only
    stocks_risk_locked = Column(Boolean, default=False, nullable=False)
    max_trades = Column(Integer, default=5, nullable=False)
    profit_target_pct = Column(Float, default=3, nullable=False)
    max_position_allocation_pct = Column(Float, default=30, nullable=False)
    max_open_positions_user = Column(Integer, default=3, nullable=False)
    cash_reserve_pct = Column(Float, default=20, nullable=False)
    allow_fractional = Column(Boolean, default=False, nullable=False)
    start_mode = Column(String(16), default="manual", nullable=False)
    scheduled_days = Column(String(32), default="0,1,2,3,4", nullable=False)
    start_delay_minutes = Column(Integer, default=0, nullable=False)
    auto_stop_before_close_minutes = Column(Integer, default=5, nullable=False)
    trade_cooldown_seconds = Column(Integer, default=600, nullable=False)
    risk_profile = Column(String(24), default="custom", nullable=False)
    stop_target_mode = Column(String(24), default="atr", nullable=False)
    stop_loss_value = Column(Float, default=1.0, nullable=False)
    take_profit_value = Column(Float, default=2.0, nullable=False)
    stocks_exit_mode = Column(String(16), default="trailing", nullable=False)
    stocks_trailing_distance_pct = Column(Float, default=1.0, nullable=False)
    risk_reward_ratio = Column(Float, default=2.0, nullable=False)
    index_bot_active = Column(Boolean, default=False, nullable=False)
    index_symbols = Column(String(64), default="QQQ,SPX", nullable=False)
    index_execution_mode = Column(String(24), default="safe", nullable=False)
    options_bot_active = Column(Boolean, default=False, nullable=False)
    options_start_mode = Column(String(16), default="manual", nullable=False)
    options_scheduled_days = Column(String(32), default="0,1,2,3,4", nullable=False)
    options_start_delay_minutes = Column(Integer, default=0, nullable=False)
    options_auto_stop_before_close_minutes = Column(Integer, default=5, nullable=False)
    options_max_trades = Column(Integer, default=5, nullable=False)
    options_trades_today = Column(Integer, default=0, nullable=False)
    options_last_trade_at = Column(DateTime, nullable=True)
    options_session_started_at = Column(DateTime, nullable=True)
    options_session_stopped_at = Column(DateTime, nullable=True)
    options_symbols = Column(Text, default="QQQ,AAPL,MSFT,NVDA,AMD,AMZN,META,TSLA", nullable=False)
    options_contract_type = Column(String(12), default="auto", nullable=False)
    options_min_dte = Column(Integer, default=0, nullable=False)
    options_max_dte = Column(Integer, default=7, nullable=False)
    options_target_delta = Column(Float, default=0.50, nullable=False)
    options_max_contracts = Column(Integer, default=1, nullable=False)
    options_max_allocation_pct = Column(Float, default=20, nullable=False)
    options_risk_per_trade_pct = Column(Float, default=2.0, nullable=False)
    options_daily_loss_pct = Column(Float, default=5.0, nullable=False)
    options_max_open_positions = Column(Integer, default=2, nullable=False)
    options_trade_cooldown_seconds = Column(Integer, default=300, nullable=False)
    options_risk_locked = Column(Boolean, default=False, nullable=False)
    options_stop_loss_pct = Column(Float, default=30, nullable=False)
    options_take_profit_pct = Column(Float, default=50, nullable=False)
    options_exit_mode = Column(String(16), default="trailing", nullable=False)
    options_trailing_activation_pct = Column(Float, default=40, nullable=False)
    options_trailing_distance_pct = Column(Float, default=20, nullable=False)
    allocation_mode = Column(String(16), default="dynamic", nullable=False)
    stocks_allocation_pct = Column(Float, default=60, nullable=False)
    options_allocation_pct = Column(Float, default=40, nullable=False)
    active = Column(Boolean, default=False, nullable=False)
    locked = Column(Boolean, default=False, nullable=False)
    realized_pnl = Column(Float, default=0, nullable=False)
    stocks_realized_pnl = Column(Float, default=0, nullable=False)
    options_realized_pnl = Column(Float, default=0, nullable=False)
    trades_today = Column(Integer, default=0, nullable=False)
    session_day = Column(String(10), default="", nullable=False)
    session_started_at = Column(DateTime, nullable=True)
    session_stopped_at = Column(DateTime, nullable=True)
    last_trade_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="settings")


class AllowedSymbol(Base):
    __tablename__ = "allowed_symbols"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(16), nullable=False, index=True)

    user = relationship("User", back_populates="allowed_symbols")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(16), nullable=False)
    side = Column(String(8), nullable=False)
    engine = Column(String(24), default="stocks", nullable=False)
    qty = Column(Float, nullable=False)
    entry = Column(Float, nullable=False)
    exit = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    trailing_enabled = Column(Boolean, default=False, nullable=False)
    trailing_active = Column(Boolean, default=False, nullable=False)
    trailing_activation_price = Column(Float, nullable=True)
    trailing_high_watermark = Column(Float, nullable=True)
    trailing_stop_price = Column(Float, nullable=True)
    trailing_distance_pct = Column(Float, nullable=True)
    trailing_activated_at = Column(DateTime, nullable=True)
    signal_score = Column(Float, nullable=True)
    indicators = Column(Text, default="")
    pnl = Column(Float, default=0, nullable=False)
    status = Column(String(20), default="OPEN", nullable=False)
    reason = Column(Text, default="")
    data_source = Column(String(40), default="SIMULATOR", nullable=False)
    opened_at = Column(DateTime, default=now, nullable=False)
    closed_at = Column(DateTime, nullable=True)


class CapitalReservation(Base):
    __tablename__ = "capital_reservations"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    engine = Column(String(24), default="stocks", nullable=False)
    symbol = Column(String(32), nullable=False)
    amount = Column(Float, default=0, nullable=False)
    status = Column(String(16), default="PENDING", nullable=False, index=True)
    trade_id = Column(Integer, nullable=True)
    note = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=now, nullable=False, index=True)
    released_at = Column(DateTime, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    title = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class PortfolioSnapshot(Base):
    """Broker/account snapshot used by the dashboard and performance reports.

    New table only, so existing Render databases can adopt it via create_all without
    a destructive migration.
    """
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    equity = Column(Float, default=0, nullable=False)
    cash = Column(Float, default=0, nullable=False)
    buying_power = Column(Float, default=0, nullable=False)
    portfolio_value = Column(Float, default=0, nullable=False)
    long_market_value = Column(Float, default=0, nullable=False)
    day_pnl = Column(Float, default=0, nullable=False)
    source = Column(String(32), default="ALPACA_PAPER", nullable=False)
    created_at = Column(DateTime, default=now, nullable=False, index=True)


class StockIndicatorSnapshot(Base):
    __tablename__ = "stock_indicator_snapshots"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    price = Column(Float, default=0, nullable=False)
    score = Column(Float, default=0, nullable=False)
    qualified = Column(Boolean, default=False, nullable=False)
    ema9 = Column(Float, default=0, nullable=False)
    ema20 = Column(Float, default=0, nullable=False)
    ema50 = Column(Float, default=0, nullable=False)
    rsi14 = Column(Float, default=0, nullable=False)
    macd = Column(Float, default=0, nullable=False)
    macd_signal = Column(Float, default=0, nullable=False)
    atr14 = Column(Float, default=0, nullable=False)
    vwap = Column(Float, default=0, nullable=False)
    adx14 = Column(Float, default=0, nullable=False)
    rel_volume = Column(Float, default=0, nullable=False)
    momentum_5 = Column(Float, default=0, nullable=False)
    verdict = Column(String(32), default="WAIT", nullable=False)
    reasons = Column(Text, default="")
    created_at = Column(DateTime, default=now, nullable=False, index=True)


class CustomIndicator(Base):
    __tablename__ = "custom_indicators"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    name = Column(String(120), nullable=False)
    source_type = Column(String(24), default="pine_reference", nullable=False)
    source_code = Column(Text, default="", nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    role = Column(String(24), default="display", nullable=False)  # display | confirm | filter
    weight = Column(Float, default=0, nullable=False)
    timeframe = Column(String(16), default="5m", nullable=False)
    symbols = Column(Text, default="*", nullable=False)
    parameters = Column(Text, default="{}", nullable=False)
    compile_status = Column(String(24), default="PENDING", nullable=False)
    compile_progress = Column(Integer, default=0, nullable=False)
    compile_error = Column(Text, default="", nullable=False)
    compiled_python = Column(Text, default="", nullable=False)
    supported_pct = Column(Float, default=0, nullable=False)
    validation_status = Column(String(24), default="NOT_TESTED", nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)

