from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite


@dataclass
class Signal:
    symbol: str
    action: str
    score: float
    price: float
    stop: float
    target: float
    ema9: float
    ema20: float
    ema50: float
    rsi14: float
    macd: float
    macd_signal: float
    atr14: float
    vwap: float
    adx14: float
    rel_volume: float
    momentum_5: float
    reasons: list[str]

    def to_dict(self):
        return asdict(self)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for a, b in zip(values[-(period + 1):-1], values[-period:]):
        change = b - a
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0
    trs = []
    window = bars[-(period + 1):]
    for prev, cur in zip(window[:-1], window[1:]):
        h, l, pc = float(cur['h']), float(cur['l']), float(prev['c'])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def _adx(bars: list[dict], period: int = 14) -> float:
    if len(bars) < period + 2:
        return 0.0
    plus_dm, minus_dm, tr = [], [], []
    window = bars[-(period + 2):]
    for prev, cur in zip(window[:-1], window[1:]):
        up = float(cur['h']) - float(prev['h'])
        down = float(prev['l']) - float(cur['l'])
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(
            float(cur['h']) - float(cur['l']),
            abs(float(cur['h']) - float(prev['c'])),
            abs(float(cur['l']) - float(prev['c'])),
        ))
    tr_sum = sum(tr[-period:])
    if tr_sum <= 0:
        return 0.0
    pdi = 100 * sum(plus_dm[-period:]) / tr_sum
    mdi = 100 * sum(minus_dm[-period:]) / tr_sum
    denom = pdi + mdi
    return 100 * abs(pdi - mdi) / denom if denom else 0.0


def _vwap(bars: list[dict], lookback: int = 78) -> float:
    use = bars[-lookback:]
    if not use:
        return 0.0
    num = den = 0.0
    for b in use:
        vol = float(b.get('v') or 0)
        typical = (float(b['h']) + float(b['l']) + float(b['c'])) / 3
        num += typical * vol
        den += vol
    return num / den if den else float(use[-1]['c'])


def _rel_volume(bars: list[dict], period: int = 20) -> float:
    if len(bars) < period + 1:
        return 1.0
    vols = [float(b.get('v') or 0) for b in bars]
    baseline = sum(vols[-(period + 1):-1]) / period
    return vols[-1] / baseline if baseline > 0 else 1.0


def evaluate_symbol(symbol: str, bars: list[dict], min_score: float = 70.0) -> dict | None:
    """Return transparent indicator state even when no trade is qualified."""
    if len(bars) < 55:
        return None
    closes = [float(b['c']) for b in bars if b.get('c') is not None]
    if len(closes) < 55:
        return None

    ema9s, ema20s, ema50s = _ema(closes, 9), _ema(closes, 20), _ema(closes, 50)
    ema9, ema20, ema50 = ema9s[-1], ema20s[-1], ema50s[-1]
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    macd_series = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    macd = macd_series[-1]
    macd_sig = _ema(macd_series, 9)[-1]
    price = closes[-1]
    rsi = _rsi(closes, 14)
    atr = _atr(bars, 14)
    vwap = _vwap(bars)
    adx = _adx(bars, 14)
    relv = _rel_volume(bars, 20)
    base = closes[-6]
    mom5 = (price / base - 1) if base else 0.0

    values = [ema9, ema20, ema50, macd, macd_sig, price, rsi, atr, vwap, adx, relv, mom5]
    if not all(isfinite(x) for x in values) or price <= 0 or atr <= 0:
        return None

    score = 0.0
    reasons: list[str] = []
    blockers: list[str] = []
    if price > vwap:
        score += 15; reasons.append('Price above VWAP')
    else:
        blockers.append('Below VWAP')
    if ema9 > ema20 > ema50:
        score += 20; reasons.append('EMA 9 > 20 > 50 bullish alignment')
    elif ema20 > ema50:
        score += 10; reasons.append('EMA 20 > EMA 50 trend support')
    else:
        blockers.append('EMA trend not aligned')
    if macd > macd_sig and macd > 0:
        score += 15; reasons.append('MACD bullish and above zero')
    else:
        blockers.append('MACD not confirmed')
    if 52 <= rsi <= 72:
        score += 12; reasons.append('RSI in constructive momentum zone')
    elif 45 <= rsi < 52:
        score += 5; reasons.append('RSI recovering')
    elif rsi > 78:
        blockers.append('RSI overextended')
    if adx >= 22:
        score += 12; reasons.append('ADX confirms trend strength')
    elif adx < 18:
        blockers.append('ADX weak trend')
    if relv >= 1.25:
        score += 14; reasons.append('Relative volume >= 1.25x')
    elif relv >= 1.0:
        score += 6; reasons.append('Volume at/above recent average')
    else:
        blockers.append('Relative volume below average')
    if 0.001 <= mom5 <= 0.025:
        score += 12; reasons.append('Positive controlled 5-bar momentum')
    elif mom5 > 0.035:
        blockers.append('Momentum chase risk')

    chase_reject = rsi > 78 or mom5 > 0.035 or price < ema20 or price < vwap
    qualified = bool(score >= min_score and not chase_reject)
    stop_distance = max(atr * 1.25, price * 0.004)
    stop = max(0.01, price - stop_distance)
    target = price + stop_distance * 1.8

    return {
        'symbol': symbol,
        'action': 'BUY' if qualified else 'WAIT',
        'qualified': qualified,
        'score': round(score, 1),
        'price': round(price, 4),
        'stop': round(stop, 4),
        'target': round(target, 4),
        'ema9': round(ema9, 4),
        'ema20': round(ema20, 4),
        'ema50': round(ema50, 4),
        'rsi14': round(rsi, 2),
        'macd': round(macd, 4),
        'macd_signal': round(macd_sig, 4),
        'atr14': round(atr, 4),
        'vwap': round(vwap, 4),
        'adx14': round(adx, 2),
        'rel_volume': round(relv, 2),
        'momentum_5': round(mom5 * 100, 3),
        'reasons': reasons,
        'blockers': blockers,
        'verdict': 'QUALIFIED' if qualified else ('WATCH' if score >= max(50, min_score - 15) else 'WAIT'),
    }


def analyze_symbol(symbol: str, bars: list[dict], min_score: float = 70.0) -> Signal | None:
    snapshot = evaluate_symbol(symbol, bars, min_score)
    if not snapshot or not snapshot['qualified']:
        return None
    return Signal(
        symbol=snapshot['symbol'], action='BUY', score=snapshot['score'], price=snapshot['price'],
        stop=snapshot['stop'], target=snapshot['target'], ema9=snapshot['ema9'], ema20=snapshot['ema20'],
        ema50=snapshot['ema50'], rsi14=snapshot['rsi14'], macd=snapshot['macd'],
        macd_signal=snapshot['macd_signal'], atr14=snapshot['atr14'], vwap=snapshot['vwap'],
        adx14=snapshot['adx14'], rel_volume=snapshot['rel_volume'], momentum_5=snapshot['momentum_5'],
        reasons=snapshot['reasons'],
    )
