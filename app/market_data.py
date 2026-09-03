from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import httpx

from .config import settings


class AlpacaMarketData:
    DATA_BASE = "https://data.alpaca.markets"
    SPX_REFERENCE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"

    def __init__(self):
        self._spx_cache: dict[str, object] = {}


    @property
    def configured(self) -> bool:
        return bool(settings.alpaca_api_key_id and settings.alpaca_api_secret_key)

    def _headers(self):
        return {
            "APCA-API-KEY-ID": settings.alpaca_api_key_id,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret_key,
        }

    def recent_bars(self, symbols: list[str], days: int = 7) -> dict[str, list[dict]]:
        """Fetch enough 5-minute bars for indicators, one symbol at a time.

        Per-symbol calls avoid multi-symbol pagination starving later symbols.
        """
        if not self.configured or not symbols:
            return {}
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(days, 3))
        result: dict[str, list[dict]] = {}
        params = {
            "timeframe": "5Min",
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": 500,
            "feed": settings.alpaca_data_feed,
            "sort": "asc",
        }
        with httpx.Client(timeout=12) as client:
            for symbol in symbols[:30]:
                r = client.get(
                    f"{self.DATA_BASE}/v2/stocks/{symbol}/bars",
                    params=params,
                    headers=self._headers(),
                )
                r.raise_for_status()
                result[symbol] = r.json().get("bars", [])
        return result

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not self.configured or not symbols:
            return {}
        params = {"symbols": ",".join(symbols[:30]), "feed": settings.alpaca_data_feed}
        with httpx.Client(timeout=10) as client:
            r = client.get(
                f"{self.DATA_BASE}/v2/stocks/bars/latest",
                params=params,
                headers=self._headers(),
            )
            r.raise_for_status()
            bars = r.json().get("bars", {})
            return {s: float(b["c"]) for s, b in bars.items() if b and b.get("c") is not None}



    def latest_quotes(self, symbols: list[str], feed: str | None = None) -> dict[str, dict]:
        """Latest bid/ask quotes with timestamps for UI and extended-hours exits."""
        if not self.configured or not symbols:
            return {}
        params = {"symbols": ",".join([s.upper() for s in symbols[:30]]), "feed": feed or settings.alpaca_data_feed}
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{self.DATA_BASE}/v2/stocks/quotes/latest", params=params, headers=self._headers())
            r.raise_for_status()
            return dict(r.json().get("quotes") or {})

    def latest_price_details(self, symbols: list[str], feed: str | None = None) -> dict[str, dict]:
        """Return best-effort live price metadata for stock/ETF cards.

        Uses the latest quote midpoint when available, falling back to the latest bar.
        Values include the source feed and timestamp so the UI can disclose freshness.
        """
        out: dict[str, dict] = {}
        syms=[s.upper() for s in symbols[:30] if s]
        if not syms or not self.configured:
            return out
        try:
            quotes=self.latest_quotes(syms, feed=feed)
            for sym,q in quotes.items():
                bid=float(q.get("bp") or 0); ask=float(q.get("ap") or 0)
                px=(bid+ask)/2 if bid>0 and ask>0 else (ask or bid or 0)
                if px>0:
                    out[sym]={"price":px,"bid":bid,"ask":ask,"timestamp":q.get("t"),"feed":feed or settings.alpaca_data_feed,"source":"quote"}
        except Exception:
            pass
        missing=[s for s in syms if s not in out]
        if missing:
            try:
                prices=self.latest_prices(missing)
                for sym,px in prices.items():
                    out[sym]={"price":float(px),"bid":0.0,"ask":0.0,"timestamp":None,"feed":feed or settings.alpaca_data_feed,"source":"bar"}
            except Exception:
                pass
        return out

    def chart_bars(self, symbol: str, period: str = "1D") -> list[dict]:
        """Return chart-ready Alpaca bars for a single stock.

        Periods are intentionally conservative for the free IEX feed.
        """
        if not self.configured:
            return []
        period = (period or "1D").upper()
        now = datetime.now(timezone.utc)
        cfg = {
            "1D": (timedelta(days=2), "5Min", 500),
            "5D": (timedelta(days=8), "15Min", 1000),
            "1M": (timedelta(days=35), "1Hour", 1000),
            "3M": (timedelta(days=100), "1Day", 500),
        }.get(period, (timedelta(days=2), "5Min", 500))
        delta, timeframe, limit = cfg
        params = {
            "timeframe": timeframe,
            "start": (now - delta).isoformat().replace("+00:00", "Z"),
            "end": now.isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "feed": settings.alpaca_data_feed,
            "sort": "asc",
        }
        with httpx.Client(timeout=12) as client:
            r = client.get(
                f"{self.DATA_BASE}/v2/stocks/{symbol.upper()}/bars",
                params=params, headers=self._headers(),
            )
            r.raise_for_status()
            bars = r.json().get("bars", [])
        # For 1D, show only the latest US trading date returned.
        if period == "1D" and bars:
            latest_day = str(bars[-1].get("t", ""))[:10]
            bars = [b for b in bars if str(b.get("t", ""))[:10] == latest_day]
        return bars


    def spx_reference(self, *, max_age_seconds: int = 5) -> dict:
        """Best-effort S&P 500 cash-index reference for display/analysis only.

        Alpaca provides SPX option contracts/data but its stock quote endpoint does not
        expose the SPX cash index as an equity quote. We therefore keep execution and
        option-chain data on Alpaca, while this reference is fetched separately and
        explicitly labeled so it is never confused with a broker quote.
        """
        now = time.time()
        cached = self._spx_cache.get("quote")
        if isinstance(cached, dict) and now - float(cached.get("_cached_at", 0) or 0) <= max(1, max_age_seconds):
            return {k: v for k, v in cached.items() if not k.startswith("_")}
        params = {"interval": "1m", "range": "1d", "includePrePost": "true"}
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LuqmanTrade/1.0)"}
        with httpx.Client(timeout=8, headers=headers, follow_redirects=True) as client:
            r = client.get(self.SPX_REFERENCE_URL, params=params)
            r.raise_for_status()
            result = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        closes = quote.get("close") or []
        price = float(meta.get("regularMarketPrice") or 0)
        ts = int(meta.get("regularMarketTime") or 0)
        if not price:
            for idx in range(min(len(timestamps), len(closes)) - 1, -1, -1):
                if closes[idx] is not None:
                    price = float(closes[idx]); ts = int(timestamps[idx]); break
        previous = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
        change = price - previous if price and previous else 0.0
        pct = (change / previous * 100.0) if previous else 0.0
        out = {
            "symbol": "SPX", "price": price or None, "previous_close": previous or None,
            "change": change, "change_pct": pct, "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            "source": "S&P 500 CASH INDEX REFERENCE", "provider": "Yahoo Finance reference", "broker_source": False,
        }
        self._spx_cache["quote"] = {**out, "_cached_at": now}
        return out

    def spx_recent_bars(self, *, days: int = 7) -> list[dict]:
        """Return SPX 5-minute reference bars in the same shape as Alpaca bars."""
        key = f"bars:{max(1, int(days))}"
        now = time.time()
        cached = self._spx_cache.get(key)
        if isinstance(cached, dict) and now - float(cached.get("_cached_at", 0) or 0) <= 20:
            return list(cached.get("bars") or [])
        rng = "5d" if days <= 7 else "1mo"
        params = {"interval": "5m", "range": rng, "includePrePost": "false"}
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LuqmanTrade/1.0)"}
        with httpx.Client(timeout=10, headers=headers, follow_redirects=True) as client:
            r = client.get(self.SPX_REFERENCE_URL, params=params)
            r.raise_for_status()
            result = ((r.json().get("chart") or {}).get("result") or [None])[0] or {}
        ts = result.get("timestamp") or []
        q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        rows = []
        for i, stamp in enumerate(ts):
            try:
                o, h, l, c = q.get("open", [])[i], q.get("high", [])[i], q.get("low", [])[i], q.get("close", [])[i]
                if None in (o, h, l, c):
                    continue
                vlist = q.get("volume") or []
                v = float(vlist[i] or 0) if i < len(vlist) else 0.0
                rows.append({"t": datetime.fromtimestamp(int(stamp), timezone.utc).isoformat().replace("+00:00", "Z"), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": v})
            except (IndexError, TypeError, ValueError):
                continue
        self._spx_cache[key] = {"bars": rows, "_cached_at": now}
        return rows

    def option_chain(self, underlying_symbol: str, feed: str = "indicative", limit: int = 1000) -> dict[str, dict]:
        if not self.configured: return {}
        params = {"feed": feed, "limit": min(max(int(limit),1),1000)}
        with httpx.Client(timeout=12) as client:
            r = client.get(f"{self.DATA_BASE}/v1beta1/options/snapshots/{underlying_symbol.upper()}", params=params, headers=self._headers())
            r.raise_for_status(); data = r.json()
        return dict(data.get("snapshots") or {})

    def broker_clock(self) -> dict | None:
        if not self.configured:
            return None
        base = settings.alpaca_trading_base_url.rstrip("/")
        with httpx.Client(timeout=8) as client:
            r = client.get(f"{base}/v2/clock", headers=self._headers())
            r.raise_for_status()
            return r.json()


market_data = AlpacaMarketData()
