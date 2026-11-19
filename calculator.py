import os
import re
import json

from theme import logger, parse_fx_currencies


def get_settings_file_path(username):
    raw_user = username if username else "default"
    usr = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(raw_user))
    return os.path.join(os.path.expanduser("~"), f".trade_journal_calc_settings_{usr}.json")


def load_calc_settings(username):
    path = get_settings_file_path(username)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("balance", "10000"), data.get("risk", "1.0"), data.get("mode", "Risk %")
        except Exception as e:
            logger.warning(f"Error loading calc settings: {e}")
    return "10000", "1.0", "Risk %"


def save_calc_settings(username, balance, risk, mode):
    try:
        data = {
            "balance": balance,
            "risk": risk,
            "mode": mode,
        }
        with open(get_settings_file_path(username), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"Error saving calc settings: {e}")


def match_calculator_pair_option(raw_pair, options, clean_pair_fn):
    """Matches an extracted ticker against calculator dropdown options; returns (matched, new_options_or_None)."""
    if not raw_pair:
        return None, None
    cleaned = clean_pair_fn(raw_pair)
    upper = cleaned.upper()

    if "GOLD" in upper or "XAU" in upper:
        return "XAU/USD (Gold)", None
    if "BTC" in upper:
        return "BTC/USD (Crypto)", None
    if "ETH" in upper:
        return "ETH/USD (Crypto)", None

    for opt in options:
        opt_clean = opt.split()[0].strip().upper()
        if opt == cleaned or opt_clean == upper:
            return opt, None

    return cleaned, [cleaned] + list(options)


def calculate_lot_size(balance_str, risk_str, risk_mode, pair_str, entry_str, sl_str, tp_str="", quote_to_usd_rate=None):
    """Computes position lot size, cash risk, and projected reward/R:R strings."""
    try:
        balance = float(str(balance_str).replace(",", "").strip())
        risk_input = float(str(risk_str).replace(",", "").strip())
        entry_p = float(str(entry_str).replace(",", "").strip())
        sl_p = float(str(sl_str).replace(",", "").strip())
    except ValueError:
        return "0.00 Lots", "$0.00", "$0.00 (1:0.00)"

    if balance <= 0 or risk_input <= 0 or entry_p <= 0 or sl_p <= 0:
        return "0.00 Lots", "$0.00", "$0.00 (1:0.00)"

    if risk_mode == "Risk %":
        cash_risk = balance * (risk_input / 100.0)
    else:
        cash_risk = risk_input

    price_diff = abs(entry_p - sl_p)
    if cash_risk <= 0 or price_diff <= 0:
        return "0.00 Lots", "$0.00", "$0.00 (1:0.00)"

    pair_u = str(pair_str or "").upper()
    base_ccy, quote_ccy = parse_fx_currencies(pair_u)
    has_live_conv = quote_to_usd_rate is not None and quote_to_usd_rate > 0

    if "XAU" in pair_u or "GOLD" in pair_u or base_ccy == "XAU":
        lots = cash_risk / (price_diff * 100.0)
    elif "BTC" in pair_u or "CRYPTO" in pair_u or "ETH" in pair_u or base_ccy in ("BTC", "ETH"):
        lots = cash_risk / price_diff
    elif quote_ccy == "JPY" or "JPY" in pair_u:
        pip_diff = price_diff * 100.0
        if base_ccy == "USD":
            pip_value_per_lot = 1000.0 / entry_p
        elif has_live_conv:
            pip_value_per_lot = 1000.0 * float(quote_to_usd_rate)
        else:
            pip_value_per_lot = 1000.0 / 150.0
        lots = cash_risk / (pip_diff * pip_value_per_lot)
    else:
        pip_diff = price_diff * 10000.0
        if quote_ccy == "USD" or not quote_ccy:
            pip_value_per_lot = 10.0
        elif base_ccy == "USD":
            pip_value_per_lot = 10.0 / entry_p
        elif has_live_conv:
            pip_value_per_lot = 10.0 * float(quote_to_usd_rate)
        else:
            pip_value_per_lot = 10.0
        lots = cash_risk / (pip_diff * pip_value_per_lot)

    lots_text = f"{round(lots, 2):.2f} Lots"
    cash_text = f"${cash_risk:,.2f}"

    reward_text = "$0.00 (1:0.00)"
    tp_raw = str(tp_str or "").replace(",", "").strip()
    if tp_raw:
        try:
            tp_p = float(tp_raw)
            is_buy = entry_p > sl_p
            valid_tp_side = (tp_p > entry_p) if is_buy else (tp_p < entry_p)
            if valid_tp_side and tp_p > 0:
                reward_diff = abs(tp_p - entry_p)
                rr_mult = reward_diff / price_diff
                projected_profit = cash_risk * rr_mult
                reward_text = f"${projected_profit:,.2f} (1:{rr_mult:.2f})"
        except ValueError:
            pass

    return lots_text, cash_text, reward_text