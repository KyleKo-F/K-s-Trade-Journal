import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import requests
from requests.adapters import HTTPAdapter

import database as db
from theme import logger, get_pip_multiplier

_thread_local = threading.local()
_sessions_lock = threading.Lock()
_all_sessions = []
_executor_lock = threading.Lock()
_tv_executor = None


def _get_tv_session():
    """Returns a thread-local pooled HTTP session for concurrent TradingView requests."""
    session = getattr(_thread_local, "session", None)
    if session is None or getattr(session, "_tv_closed", False):
        session = requests.Session()
        session._tv_closed = False
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        _thread_local.session = session
        with _sessions_lock:
            _all_sessions.append(session)
    return session


def _get_tv_executor():
    global _tv_executor
    with _executor_lock:
        if _tv_executor is None:
            _tv_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tv_poll")
        return _tv_executor


def close_tv_sessions():
    """Closes all tracked TradingView HTTP sessions and shuts down the worker pool cleanly."""
    global _tv_executor
    with _executor_lock:
        pool = _tv_executor
        _tv_executor = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
    with _sessions_lock:
        sessions = list(_all_sessions)
        _all_sessions.clear()
    for sess in sessions:
        try:
            sess._tv_closed = True
            sess.close()
        except Exception:
            pass


# Mapping of pair symbols to TradingView specifications
TV_SYMBOL_MAP = {
    "EUR/USD": ("EURUSD", "FX", "forex"),
    "GBP/USD": ("GBPUSD", "FX", "forex"),
    "USD/JPY": ("USDJPY", "FX", "forex"),
    "USD/CHF": ("USDCHF", "FX", "forex"),
    "AUD/USD": ("AUDUSD", "FX", "forex"),
    "USD/CAD": ("USDCAD", "FX", "forex"),
    "NZD/USD": ("NZDUSD", "FX", "forex"),
    "EUR/GBP": ("EURGBP", "FX", "forex"),
    "EUR/JPY": ("EURJPY", "FX", "forex"),
    "EUR/CHF": ("EURCHF", "FX", "forex"),
    "EUR/CAD": ("EURCAD", "FX", "forex"),
    "EUR/AUD": ("EURAUD", "FX", "forex"),
    "EUR/NZD": ("EURNZD", "FX", "forex"),
    "GBP/JPY": ("GBPJPY", "FX", "forex"),
    "GBP/CHF": ("GBPCHF", "FX", "forex"),
    "GBP/CAD": ("GBPCAD", "FX", "forex"),
    "GBP/AUD": ("GBPAUD", "FX", "forex"),
    "GBP/NZD": ("GBPNZD", "FX", "forex"),
    "AUD/JPY": ("AUDJPY", "FX", "forex"),
    "AUD/NZD": ("AUDNZD", "FX", "forex"),
    "AUD/CAD": ("AUDCAD", "FX", "forex"),
    "AUD/CHF": ("AUDCHF", "FX", "forex"),
    "NZD/JPY": ("NZDJPY", "FX", "forex"),
    "NZD/CAD": ("NZDCAD", "FX", "forex"),
    "NZD/CHF": ("NZDCHF", "FX", "forex"),
    "CAD/JPY": ("CADJPY", "FX", "forex"),
    "CAD/CHF": ("CADCHF", "FX", "forex"),
    "CHF/JPY": ("CHFJPY", "FX", "forex"),
    "XAU/USD": ("GOLD", "TVC", "cfd"),
    "XAU/USD (Gold)": ("GOLD", "TVC", "cfd"),
    "GOLD": ("GOLD", "TVC", "cfd"),
    "BTC/USD": ("BTCUSD", "BITSTAMP", "crypto"),
    "BTC/USD (Crypto)": ("BTCUSD", "BITSTAMP", "crypto"),
    "ETH/USD": ("ETHUSD", "BITSTAMP", "crypto"),
    "ETH/USD (Crypto)": ("ETHUSD", "BITSTAMP", "crypto")
}


@lru_cache(maxsize=128)
def resolve_tv_symbol(pair_name):
    if not pair_name:
        return None
    raw = str(pair_name).strip()
    if raw in TV_SYMBOL_MAP:
        return TV_SYMBOL_MAP[raw]

    upper = raw.upper()
    if upper in TV_SYMBOL_MAP:
        return TV_SYMBOL_MAP[upper]

    base_token = upper.split()[0].replace(" ", "")
    if base_token in TV_SYMBOL_MAP:
        return TV_SYMBOL_MAP[base_token]

    if len(base_token) == 6 and base_token.isalpha():
        slashed = f"{base_token[:3]}/{base_token[3:]}"
        if slashed in TV_SYMBOL_MAP:
            return TV_SYMBOL_MAP[slashed]

    return None


def get_tradingview_prices(pair_name):
    """Fetches high, low, and close prices for a given pair from TradingView."""
    map_info = resolve_tv_symbol(pair_name)
    if not map_info:
        return None, None, None

    tv_symbol, tv_exchange, tv_screener = map_info

    # Method 1: TradingView direct API endpoint (fast pooled HTTP)
    try:
        url = f"https://scanner.tradingview.com/{tv_screener}/scan"
        payload = {
            "symbols": {"tickers": [f"{tv_exchange}:{tv_symbol}"]},
            "columns": ["high|1", "low|1", "close"]
        }
        resp = _get_tv_session().post(url, json=payload, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data") and len(data["data"]) > 0:
                vals = data["data"][0]["d"]
                if vals and vals[2] is not None:
                    return vals[0] or vals[2], vals[1] or vals[2], vals[2]
    except Exception:
        pass

    # Method 2: Lazy-imported fallback using tradingview_ta library
    try:
        from tradingview_ta import TA_Handler, Interval
        handler = TA_Handler(
            symbol=tv_symbol,
            exchange=tv_exchange,
            screener=tv_screener,
            interval=Interval.INTERVAL_1_MINUTE
        )
        analysis = handler.get_analysis()
        high = analysis.indicators.get("high")
        low = analysis.indicators.get("low")
        close = analysis.indicators.get("close")
        if close is not None:
            return high or close, low or close, close
    except Exception as e:
        logger.warning(f"TradingView fetch error for {pair_name}: {e}")

    return None, None, None


def _fetch_screener_batch(screener, ticker_to_pairs):
    """Fetches high, low, and close for all tickers in a single TradingView screener call."""
    results = {}
    ok = False
    try:
        url = f"https://scanner.tradingview.com/{screener}/scan"
        payload = {
            "symbols": {"tickers": list(ticker_to_pairs.keys())},
            "columns": ["high|1", "low|1", "close"]
        }
        resp = _get_tv_session().post(url, json=payload, timeout=4)
        if resp.status_code == 200:
            ok = True
            data = resp.json()
            for row in data.get("data") or []:
                ticker = row.get("s")
                vals = row.get("d")
                if ticker in ticker_to_pairs and vals and len(vals) >= 3 and vals[2] is not None:
                    price_tuple = (vals[0] or vals[2], vals[1] or vals[2], vals[2])
                    for p in ticker_to_pairs[ticker]:
                        results[p] = price_tuple
    except Exception as e:
        logger.warning(f"TradingView batch scan error for screener '{screener}': {e}")
    return screener, ok, results


def check_open_trades(username=None):
    """Scans OPEN trades (scoped by username when provided) in batched TradingView requests."""
    try:
        open_trades = db.fetch_open_trades(username=username)
    except Exception as e:
        logger.warning(f"Tracker DB fetch error: {e}")
        return False

    if not open_trades:
        return False

    unique_pairs = {t[1] for t in open_trades if t[1]}
    price_cache = {}
    succeeded_screeners = set()

    screener_groups = defaultdict(lambda: defaultdict(list))
    for p in unique_pairs:
        map_info = resolve_tv_symbol(p)
        if map_info:
            tv_symbol, tv_exchange, tv_screener = map_info
            screener_groups[tv_screener][f"{tv_exchange}:{tv_symbol}"].append(p)

    if len(screener_groups) == 1:
        screener, ticker_map = next(iter(screener_groups.items()))
        scr, ok, batch_res = _fetch_screener_batch(screener, ticker_map)
        if ok:
            succeeded_screeners.add(scr)
        price_cache.update(batch_res)
    elif len(screener_groups) > 1:
        pool = _get_tv_executor()
        futures = [
            pool.submit(_fetch_screener_batch, screener, ticker_map)
            for screener, ticker_map in screener_groups.items()
        ]
        for fut in futures:
            try:
                scr, ok, batch_res = fut.result(timeout=6)
                if ok:
                    succeeded_screeners.add(scr)
                price_cache.update(batch_res)
            except Exception:
                pass

    missing_pairs = [
        p for p in unique_pairs
        if p not in price_cache
        and (info := resolve_tv_symbol(p)) is not None
        and info[2] not in succeeded_screeners
    ]
    if len(missing_pairs) == 1:
        p = missing_pairs[0]
        price_cache[p] = get_tradingview_prices(p)
    elif len(missing_pairs) > 1:
        pool = _get_tv_executor()
        futures = {p: pool.submit(get_tradingview_prices, p) for p in missing_pairs}
        for p, fut in futures.items():
            try:
                price_cache[p] = fut.result(timeout=6)
            except Exception:
                price_cache[p] = (None, None, None)

    any_updated = False
    for trade in open_trades:
        trade_id, pair, direction, entry, sl, tp = trade

        try:
            entry = float(str(entry).replace(",", "").strip())
            if not math.isfinite(entry) or entry <= 0:
                continue
        except (ValueError, TypeError):
            continue

        try:
            sl = float(str(sl).replace(",", "").strip()) if str(sl or "").strip() else 0.0
            if not math.isfinite(sl):
                sl = 0.0
        except (ValueError, TypeError):
            sl = 0.0

        try:
            tp = float(str(tp).replace(",", "").strip()) if str(tp or "").strip() else 0.0
            if not math.isfinite(tp):
                tp = 0.0
        except (ValueError, TypeError):
            tp = 0.0

        if sl <= 0 and tp <= 0:
            continue

        candle_high, candle_low, current_price = price_cache.get(pair, (None, None, None))
        if current_price is None:
            continue

        try:
            close_p = float(current_price)
            high_p = float(candle_high) if candle_high is not None else close_p
            low_p = float(candle_low) if candle_low is not None else close_p
            if not (math.isfinite(close_p) and math.isfinite(high_p) and math.isfinite(low_p)):
                continue
            if high_p < low_p:
                high_p, low_p = max(high_p, close_p), min(low_p, close_p)
        except (ValueError, TypeError):
            continue

        hit_status = None
        dir_u = str(direction).strip().upper()

        if dir_u in ("BUY", "LONG"):
            if sl > 0 and tp > 0 and tp > sl:
                if low_p <= sl:
                    hit_status = "SL"
                elif high_p >= tp:
                    hit_status = "TP"
            elif sl > 0 and tp <= 0 and entry > sl:
                if low_p <= sl:
                    hit_status = "SL"
            elif tp > 0 and sl <= 0 and tp > entry:
                if high_p >= tp:
                    hit_status = "TP"
        elif dir_u in ("SELL", "SHORT"):
            if sl > 0 and tp > 0 and tp < sl:
                if high_p >= sl:
                    hit_status = "SL"
                elif low_p <= tp:
                    hit_status = "TP"
            elif sl > 0 and tp <= 0 and sl > entry:
                if high_p >= sl:
                    hit_status = "SL"
            elif tp > 0 and sl <= 0 and tp < entry:
                if low_p <= tp:
                    hit_status = "TP"

        if hit_status:
            multiplier = get_pip_multiplier(pair)
            pnl_pips = (tp - entry if hit_status == "TP" else sl - entry) * multiplier
            if dir_u in ("SELL", "SHORT"):
                pnl_pips = -pnl_pips

            if db.close_trade_status(trade_id, hit_status, round(pnl_pips, 1), username=username):
                any_updated = True
                logger.info(f"Trade #{trade_id} ({pair}) closed as {hit_status} via TradingView price update.")

    return any_updated