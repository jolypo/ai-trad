from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Luqman Trade"
    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./luqman_trade.db"
    admin_email: str = "admin@luqman.local"
    admin_password: str = "ChangeMe123!"
    broker_mode: str = "simulator"  # simulator | alpaca_market_paper
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    alpaca_trading_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"
    alpaca_options_data_feed: str = "indicative"  # indicative (free) | opra (subscription)
    max_daily_loss_hard_cap: float = 10.0
    bot_tick_seconds: int = 300
    realtime_sync_seconds: int = 5
    min_seconds_between_trades: int = 600
    min_signal_score: float = 70.0
    max_open_positions: int = 1
    enforce_market_hours: bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
