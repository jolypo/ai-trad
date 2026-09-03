from __future__ import annotations

import time
from typing import Any

import httpx

from .config import settings


class AlpacaPaperBroker:
    """Paper-only Alpaca Trading API client.

    The live hostname is explicitly rejected so a configuration mistake cannot
    silently route this project to real-money trading.
    """

    @property
    def configured(self) -> bool:
        return bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key)

    @property
    def base_url(self) -> str:
        configured = settings.alpaca_trading_base_url.rstrip("/")
        if "paper-api.alpaca.markets" not in configured:
            raise RuntimeError("Refusing non-paper Alpaca trading URL")
        return configured

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        with httpx.Client(timeout=12) as client:
            r = client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    def account(self) -> dict[str, Any]:
        return self._get("/v2/account")

    def option_contracts(self, *, underlying_symbols: list[str], contract_type: str | None = None, expiration_date_gte: str | None = None, expiration_date_lte: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"underlying_symbols": ",".join([x.upper() for x in underlying_symbols if x]), "status": "active", "limit": min(max(int(limit), 1), 10000)}
        if contract_type in {"call", "put"}: params["type"] = contract_type
        if expiration_date_gte: params["expiration_date_gte"] = expiration_date_gte
        if expiration_date_lte: params["expiration_date_lte"] = expiration_date_lte
        data = self._get("/v2/options/contracts", params)
        return list(data.get("option_contracts", [])) if isinstance(data, dict) else []

    def option_contract(self, symbol: str) -> dict[str, Any]:
        return self._get(f"/v2/options/contracts/{symbol}")

    def options_enabled(self, symbol: str) -> bool:
        if symbol.upper() == "SPX":
            try:
                return any(bool(x.get("tradable")) for x in self.option_contracts(underlying_symbols=["SPX"], limit=5))
            except Exception:
                return False
        try:
            attrs = self.asset(symbol).get("attributes") or []
            return "options_enabled" in attrs
        except Exception:
            return False

    def submit_option_limit_buy(self, *, contract_symbol: str, qty: int, limit_price: float, client_order_id: str) -> dict[str, Any]:
        if int(qty) < 1: raise ValueError("Option quantity must be at least 1 contract")
        if float(limit_price) <= 0: raise ValueError("Option limit price must be positive")
        payload = {"symbol": contract_symbol.upper(), "qty": str(int(qty)), "side": "buy", "type": "limit", "limit_price": f"{float(limit_price):.2f}", "time_in_force": "day", "client_order_id": client_order_id[:128]}
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status(); return r.json()

    def asset(self, symbol: str) -> dict[str, Any]:
        return self._get(f"/v2/assets/{symbol.upper()}")

    def is_fractionable(self, symbol: str) -> bool:
        try:
            return bool(self.asset(symbol).get("fractionable"))
        except Exception:
            return False

    def positions(self) -> list[dict[str, Any]]:
        return self._get("/v2/positions")

    def orders(self, *, status: str = "all", limit: int = 100, nested: bool = True) -> list[dict[str, Any]]:
        return self._get(
            "/v2/orders",
            {"status": status, "limit": min(max(limit, 1), 500), "nested": str(nested).lower(), "direction": "desc"},
        )

    def portfolio_history(self, *, period: str = "1M", timeframe: str = "1D") -> dict[str, Any]:
        return self._get("/v2/account/portfolio/history", {"period": period, "timeframe": timeframe, "extended_hours": "false"})

    def submit_market_buy(self, *, symbol: str, qty: float, client_order_id: str) -> dict[str, Any]:
        if qty <= 0:
            raise ValueError("Buy quantity must be positive")
        payload = {
            "symbol": symbol.upper(), "qty": f"{float(qty):.9f}", "side": "buy",
            "type": "market", "time_in_force": "day", "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    def submit_trailing_stop_sell(self, *, symbol: str, qty: float, trail_percent: float, client_order_id: str, time_in_force: str = "gtc") -> dict[str, Any]:
        """Submit an Alpaca-native trailing stop for a whole-share equity position.

        Alpaca currently supports trailing_stop for equities as a single order.
        Luqman deliberately does not claim support for fractional trailing stops or
        use it as a bracket/OCO leg.
        """
        if qty < 1 or abs(float(qty) - round(float(qty))) > 1e-9:
            raise ValueError("Broker-native trailing stop requires a whole-share quantity")
        if trail_percent <= 0 or trail_percent > 50:
            raise ValueError("Trailing percent must be between 0 and 50")
        tif = time_in_force if time_in_force in {"day", "gtc"} else "gtc"
        payload = {
            "symbol": symbol.upper(), "qty": str(int(round(float(qty)))), "side": "sell",
            "type": "trailing_stop", "trail_percent": f"{float(trail_percent):.4f}",
            "time_in_force": tif, "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    def submit_bracket_buy(
        self,
        *,
        symbol: str,
        qty: float,
        take_profit: float,
        stop_loss: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        if qty < 1:
            raise ValueError("Protected bracket order requires at least 1 whole share")
        payload = {
            "symbol": symbol,
            "qty": str(int(qty)),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
            "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()


    def confirm_bracket_protection(self, order_id: str, timeout_seconds: float = 4.0) -> dict[str, Any]:
        """Return the nested parent only after both TP and SL child legs exist.

        Alpaca accepting the parent request is not enough for Luqman to claim
        broker-side protection. We verify the nested order representation first.
        """
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get_order(order_id, nested=True)
            legs = list(last.get("legs") or [])
            has_tp = any(str(x.get("type") or "").lower() == "limit" and str(x.get("side") or "").lower() == "sell" for x in legs)
            has_sl = any(str(x.get("type") or "").lower() in {"stop", "stop_limit"} and str(x.get("side") or "").lower() == "sell" for x in legs)
            if has_tp and has_sl:
                return last
            time.sleep(0.25)
        raise RuntimeError("Alpaca bracket protection legs were not confirmed")

    def submit_fractional_market_buy(self, *, symbol: str, qty: float, client_order_id: str) -> dict[str, Any]:
        if qty <= 0:
            raise ValueError("Fractional quantity must be positive")
        if not self.is_fractionable(symbol):
            raise ValueError(f"{symbol} is not fractionable")
        payload = {
            "symbol": symbol.upper(), "qty": f"{qty:.6f}", "side": "buy",
            "type": "market", "time_in_force": "day", "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()


    def submit_fractional_stop_sell(self, *, symbol: str, qty: float, stop_price: float, client_order_id: str) -> dict[str, Any]:
        """Place a DAY broker-side stop for a fractional equity position.

        Fractional advanced OCO/bracket semantics are deliberately not assumed.
        This single protective stop lives at Alpaca after acceptance, so it can
        execute without the Luqman process remaining online during that session.
        """
        if qty <= 0 or abs(float(qty) - int(float(qty))) < 1e-9:
            raise ValueError("Fractional stop requires a positive fractional quantity")
        if stop_price <= 0:
            raise ValueError("Stop price must be positive")
        if not self.is_fractionable(symbol):
            raise ValueError(f"{symbol} is not fractionable")
        payload = {
            "symbol": symbol.upper(), "qty": f"{float(qty):.9f}", "side": "sell",
            "type": "stop", "stop_price": f"{float(stop_price):.2f}",
            "time_in_force": "day", "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    def confirm_open_order(self, order_id: str, timeout_seconds: float = 4.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get_order(order_id, nested=True)
            status = str(last.get("status") or "").lower()
            if status in {"new", "accepted", "pending_new", "partially_filled", "held"}:
                return last
            if status in {"filled", "canceled", "expired", "rejected", "done_for_day"}:
                break
            time.sleep(0.25)
        raise RuntimeError(f"Alpaca protective order was not confirmed open ({last.get('status') or 'unknown'})")

    def submit_extended_hours_sell(self, *, symbol: str, qty: float, limit_price: float, client_order_id: str, time_in_force: str = "day") -> dict[str, Any]:
        """Submit a marketable limit sell that is eligible for Alpaca extended hours.

        Alpaca requires extended-hours equity orders to be limit orders and DAY/GTC.
        This is used only for explicit user exits, never for automatic strategy entries.
        """
        if qty <= 0:
            raise ValueError("Sell quantity must be positive")
        if limit_price <= 0:
            raise ValueError("Extended-hours limit price must be positive")
        tif = time_in_force if time_in_force in {"day", "gtc"} else "day"
        payload = {
            "symbol": symbol.upper(), "qty": f"{qty:.9f}", "side": "sell",
            "type": "limit", "limit_price": f"{float(limit_price):.2f}",
            "time_in_force": tif, "extended_hours": True,
            "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    def get_order(self, order_id: str, nested: bool = True) -> dict[str, Any]:
        return self._get(f"/v2/orders/{order_id}", {"nested": "true" if nested else "false"})

    def cancel_order(self, order_id: str) -> None:
        with httpx.Client(timeout=10) as client:
            r = client.delete(f"{self.base_url}/v2/orders/{order_id}", headers=self._headers())
            if r.status_code not in (204, 404, 422):
                r.raise_for_status()

    def close_position(self, symbol: str, *, qty: float | None = None, percentage: float | None = None) -> dict[str, Any] | None:
        if qty is not None and percentage is not None:
            raise ValueError("Use qty or percentage, not both")
        params = {}
        if qty is not None:
            if qty <= 0:
                raise ValueError("Close quantity must be positive")
            params["qty"] = f"{qty:.9f}"
        if percentage is not None:
            if percentage <= 0 or percentage > 100:
                raise ValueError("Close percentage must be in (0, 100]")
            params["percentage"] = f"{percentage:.9f}"
        with httpx.Client(timeout=12) as client:
            r = client.delete(f"{self.base_url}/v2/positions/{symbol}", headers=self._headers(), params=params)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()


    def open_orders_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        symbol = symbol.upper().strip()
        return [o for o in self.orders(status="open", limit=100, nested=True) if str(o.get("symbol") or "").upper() == symbol]

    def cancel_open_orders_for_symbol(self, symbol: str) -> list[str]:
        """Cancel every open order for a symbol and return the attempted order ids.

        Cancellation errors are not hidden: a manual close must never proceed while an
        older protective sell could still liquidate the same shares.
        """
        canceled: list[str] = []
        for order in self.open_orders_for_symbol(symbol):
            if order.get("id"):
                oid = str(order["id"])
                self.cancel_order(oid)
                canceled.append(oid)
        return canceled

    def wait_for_no_open_orders(self, symbol: str, timeout_seconds: float = 4.0) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            if not self.open_orders_for_symbol(symbol):
                return True
            time.sleep(0.25)
        return not self.open_orders_for_symbol(symbol)

    def submit_oco_exit(self, *, symbol: str, qty: float, take_profit: float, stop_loss: float, client_order_id: str) -> dict[str, Any]:
        if qty < 1 or abs(qty - int(qty)) > 1e-9:
            raise ValueError("OCO protection is enabled for whole-share remainder only")
        payload = {
            "symbol": symbol.upper(),
            "qty": str(int(qty)),
            "side": "sell",
            "type": "limit",
            "time_in_force": "gtc",
            "order_class": "oco",
            "take_profit": {"limit_price": f"{take_profit:.2f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.2f}"},
            "client_order_id": client_order_id[:128],
        }
        with httpx.Client(timeout=12) as client:
            r = client.post(f"{self.base_url}/v2/orders", headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    def wait_for_terminal_order(self, order_id: str, timeout_seconds: float = 8.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.get_order(order_id, nested=True)
            if last.get("status") in {"filled", "canceled", "expired", "rejected", "done_for_day"}:
                return last
            time.sleep(0.4)
        return last


alpaca_broker = AlpacaPaperBroker()
