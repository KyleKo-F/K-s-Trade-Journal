import os
import re
import sys
import json
import time
import tempfile
import threading
import logging
from logging.handlers import RotatingFileHandler

# Rotating file logger to replace silent exception swallowing
LOG_FILE_PATH = os.path.join(os.path.expanduser("~"), ".trade_journal.log")
logger = logging.getLogger("KsTradeJournal")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    try:
        _handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(_handler)
    except Exception:
        pass


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
        candidate = os.path.join(base_path, relative_path)
        if os.path.exists(candidate):
            return candidate
    except Exception:
        pass

    cwd_candidate = os.path.join(os.path.abspath("."), relative_path)
    if os.path.exists(cwd_candidate):
        return cwd_candidate

    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, relative_path)


def get_env_file_path():
    """Persistent user-level .env file path (works across dev and frozen .exe builds)."""
    return os.path.join(os.path.expanduser("~"), ".trade_journal.env")


def _unescape_env_value(raw):
    val = str(raw or "").strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        quote = val[0]
        inner = val[1:-1]
        out = []
        i = 0
        n = len(inner)
        while i < n:
            ch = inner[i]
            if ch == "\\" and i + 1 < n and inner[i + 1] in ("\\", quote):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)
    return val


def _iter_env_candidate_paths():
    paths = [get_env_file_path()]
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(os.path.dirname(sys.executable), ".env"))
    paths.append(os.path.join(os.path.abspath("."), ".env"))
    paths.append(resource_path(".env"))
    seen = set()
    unique = []
    for path in paths:
        norm_path = os.path.abspath(path)
        if norm_path not in seen:
            seen.add(norm_path)
            unique.append(norm_path)
    return unique


def read_env_value(key):
    """Reads a key from process environment or candidate .env files with quote/escape handling."""
    env_val = os.getenv(key, "").strip()
    if env_val:
        return env_val
    for norm_path in _iter_env_candidate_paths():
        if not os.path.exists(norm_path):
            continue
        try:
            with open(norm_path, "r", encoding="utf-8-sig") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        cleaned = _unescape_env_value(v)
                        if cleaned:
                            return cleaned
        except Exception as e:
            logger.warning(f"Failed reading env file {norm_path}: {e}")
    return ""


def load_env_file():
    """Loads environment variables from local .env or ~/.trade_journal.env without overriding OS env vars."""
    for norm_path in _iter_env_candidate_paths():
        if not os.path.exists(norm_path):
            continue
        try:
            with open(norm_path, "r", encoding="utf-8-sig") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = _unescape_env_value(val)
                    if key and val and key not in os.environ:
                        os.environ[key] = val
        except Exception as e:
            logger.warning(f"Failed reading env file {norm_path}: {e}")


def save_env_values(updates):
    """Updates key-value pairs in ~/.trade_journal.env atomically and applies them to os.environ immediately."""
    env_path = get_env_file_path()
    existing = {}
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8-sig") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    existing[k.strip()] = _unescape_env_value(v)
        except Exception as e:
            logger.warning(f"Failed reading existing env file: {e}")

    for k, v in updates.items():
        clean_k = str(k).replace("\r", "").replace("\n", "").strip()
        clean_v = str(v).replace("\r", "").replace("\n", "").strip()
        if not clean_k or "=" in clean_k:
            continue
        if clean_v:
            existing[clean_k] = clean_v
            os.environ[clean_k] = clean_v
        else:
            existing.pop(clean_k, None)
            os.environ.pop(clean_k, None)

    env_dir = os.path.dirname(env_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".trade_journal_env_", dir=env_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for k, v in existing.items():
                escaped_v = v.replace("\\", "\\\\").replace('"', '\\"')
                f.write(f'{k}="{escaped_v}"\n')
        try:
            os.chmod(tmp_path, 0o600)
        except Exception:
            pass
        os.replace(tmp_path, env_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


load_env_file()


DARK_THEME = {
    "bg_root": "#090d13",
    "bg_sidebar": "#0d121a",
    "bg_card": "#111722",
    "bg_card_alt": "#151d2b",
    "bg_input": "#0a0e16",
    "bg_hover": "#1b2434",
    "bg_active_nav": "#16242b",
    "border": "#1e2736",
    "border_strong": "#2d3a4f",
    "accent": "#00e6a8",
    "accent_hover": "#00c892",
    "accent_muted": "#0d3832",
    "cyan": "#38bdf8",
    "purple": "#a855f7",
    "amber": "#f59e0b",
    "danger": "#da3633",
    "danger_hover": "#f85149",
    "danger_text": "#ff6b6b",
    "text_primary": "#f0f6fc",
    "text_secondary": "#8b949e",
    "text_muted": "#4d5766",
    "row_even": "#111722",
    "row_odd": "#0d121b",
    "row_selected": "#183642",
    "row_selected_fg": "#ffffff",
    "passed_even": "#0c1018",
    "passed_odd": "#090c12",
    "cal_win_bg": "#133629",
    "cal_win_border": "#1f7a58",
    "cal_loss_bg": "#3b1319",
    "cal_loss_border": "#8c2633",
}

LIGHT_THEME = {
    "bg_root": "#eef2f6",
    "bg_sidebar": "#ffffff",
    "bg_card": "#ffffff",
    "bg_card_alt": "#f6f8fa",
    "bg_input": "#f3f6fa",
    "bg_hover": "#e4ebf3",
    "bg_active_nav": "#e1f7f0",
    "border": "#d0d7de",
    "border_strong": "#afb8c1",
    "accent": "#00a878",
    "accent_hover": "#008f66",
    "accent_muted": "#d4f5ea",
    "cyan": "#0284c7",
    "purple": "#7e22ce",
    "amber": "#d97706",
    "danger": "#cf222e",
    "danger_hover": "#a40e26",
    "danger_text": "#cf222e",
    "text_primary": "#1f2328",
    "text_secondary": "#57606a",
    "text_muted": "#8c959f",
    "row_even": "#ffffff",
    "row_odd": "#f6f8fa",
    "row_selected": "#d4f5ea",
    "row_selected_fg": "#1f2328",
    "passed_even": "#f1f4f8",
    "passed_odd": "#e8ecf2",
    "cal_win_bg": "#dcfce7",
    "cal_win_border": "#22c55e",
    "cal_loss_bg": "#fee2e2",
    "cal_loss_border": "#ef4444",
}

THEME = DARK_THEME.copy()

# Native CustomTkinter (light, dark) color pairs
THEME_PAIRS = {
    k: (LIGHT_THEME[k], DARK_THEME[k]) for k in DARK_THEME
}


def pair_color(key):
    """Return a CustomTkinter (light_hex, dark_hex) tuple for a theme key."""
    return THEME_PAIRS[key]


def get_theme_file_path():
    return os.path.join(os.path.expanduser("~"), ".trade_journal_theme.json")


def load_theme_preference():
    path = get_theme_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            mode = data.get("theme", "Dark")
            if mode in ("Dark", "Light"):
                return mode
        except Exception as e:
            logger.warning(f"Failed to load theme preference: {e}")
    return "Dark"


def save_theme_preference(mode):
    try:
        with open(get_theme_file_path(), "w", encoding="utf-8") as f:
            json.dump({"theme": mode}, f)
    except Exception as e:
        logger.warning(f"Error saving theme preference: {e}")


# ---------------- TIMEZONE DETECTION & PREFERENCES ---------------- #

from datetime import datetime, timedelta, timezone as dt_timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

WINDOWS_TO_IANA = {
    "UTC": "UTC",
    "Coordinated Universal Time": "UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Amsterdam",
    "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "GTB Standard Time": "Europe/Athens",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Turkey Standard Time": "Europe/Istanbul",
    "Russian Standard Time": "Europe/Moscow",
    "Arabian Standard Time": "Asia/Dubai",
    "India Standard Time": "Asia/Kolkata",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Argentina Standard Time": "America/Buenos_Aires",
    "SA Pacific Standard Time": "America/Bogota",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Egypt Standard Time": "Africa/Cairo",
    "Israel Standard Time": "Asia/Jerusalem",
    "Arab Standard Time": "Asia/Riyadh",
    "Pakistan Standard Time": "Asia/Karachi",
    "Bangladesh Standard Time": "Asia/Dhaka",
}

FALLBACK_TZ_OFFSETS = {
    "UTC": (0, "UTC"),
    "Europe/London": (0, "GMT"),
    "Europe/Amsterdam": (60, "CET"),
    "Europe/Berlin": (60, "CET"),
    "Europe/Paris": (60, "CET"),
    "Europe/Zurich": (60, "CET"),
    "Europe/Warsaw": (60, "CET"),
    "Europe/Budapest": (60, "CET"),
    "Europe/Athens": (120, "EET"),
    "Europe/Istanbul": (180, "TRT"),
    "Europe/Moscow": (180, "MSK"),
    "Africa/Johannesburg": (120, "SAST"),
    "Africa/Cairo": (120, "EET"),
    "Asia/Jerusalem": (120, "IST"),
    "Asia/Riyadh": (180, "AST"),
    "Asia/Dubai": (240, "GST"),
    "Asia/Karachi": (300, "PKT"),
    "Asia/Kolkata": (330, "IST"),
    "Asia/Dhaka": (360, "BST"),
    "Asia/Bangkok": (420, "ICT"),
    "Asia/Singapore": (480, "SGT"),
    "Asia/Hong_Kong": (480, "HKT"),
    "Asia/Shanghai": (480, "CST"),
    "Asia/Taipei": (480, "CST"),
    "Asia/Tokyo": (540, "JST"),
    "Asia/Seoul": (540, "KST"),
    "Australia/Perth": (480, "AWST"),
    "Australia/Adelaide": (570, "ACST"),
    "Australia/Sydney": (600, "AEST"),
    "Pacific/Auckland": (720, "NZST"),
    "America/New_York": (-300, "EST"),
    "America/Toronto": (-300, "EST"),
    "America/Chicago": (-360, "CST"),
    "America/Mexico_City": (-360, "CST"),
    "America/Denver": (-420, "MST"),
    "America/Phoenix": (-420, "MST"),
    "America/Los_Angeles": (-480, "PST"),
    "America/Anchorage": (-540, "AKST"),
    "Pacific/Honolulu": (-600, "HST"),
    "America/Halifax": (-240, "AST"),
    "America/Sao_Paulo": (-180, "BRT"),
    "America/Buenos_Aires": (-180, "ART"),
    "America/Bogota": (-300, "COT"),
}

TIMEZONE_PRESETS = [
    "UTC",
    "Europe/London",
    "Europe/Amsterdam",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Zurich",
    "Europe/Athens",
    "America/New_York",
    "America/Toronto",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Pacific/Auckland",
]


def _format_utc_offset(offset_minutes):
    sign = "+" if offset_minutes >= 0 else "-"
    abs_m = abs(int(offset_minutes))
    hrs, mins = divmod(abs_m, 60)
    if mins == 0:
        return f"UTC{sign}{hrs}"
    return f"UTC{sign}{hrs}:{mins:02d}"


def _clean_tz_abbr(raw_abbr, offset_minutes):
    abbr = str(raw_abbr or "").strip()
    if abbr and len(abbr) <= 6 and abbr.isascii() and not abbr.startswith(("+", "-")):
        return abbr
    return _format_utc_offset(offset_minutes)


_TZ_LOCK = threading.Lock()
_TZ_DETECT_CACHE = {"value": None, "ts": 0.0}
_TZ_RESOLVE_CACHE = {}
_TZ_CACHE_TTL = 60.0
_TZ_CACHE_EPOCH = 0


def _detect_system_timezone_uncached():
    """Detects the user's Windows or POSIX IANA timezone name, falling back gracefully."""
    if os.name == "nt":
        try:
            import winreg
            reg_path = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                for val_name in ("TimeZoneKeyName", "StandardName"):
                    try:
                        win_tz, _ = winreg.QueryValueEx(key, val_name)
                        win_tz = str(win_tz or "").strip()
                        if win_tz in WINDOWS_TO_IANA:
                            return WINDOWS_TO_IANA[win_tz]
                    except FileNotFoundError:
                        continue
        except Exception as e:
            logger.warning(f"Windows timezone detection error: {e}")

    try:
        local_tz = datetime.now().astimezone().tzinfo
        tz_key = getattr(local_tz, "key", None)
        if tz_key:
            return str(tz_key)
    except Exception:
        pass

    try:
        if os.path.islink("/etc/localtime"):
            target = os.path.realpath("/etc/localtime")
            if "/zoneinfo/" in target:
                cand = target.split("/zoneinfo/", 1)[1]
                if cand:
                    return cand
    except Exception:
        pass

    try:
        local_now = datetime.now().astimezone()
        offset_min = int(round((local_now.utcoffset() or timedelta(0)).total_seconds() / 60))
        for iana_name, (std_min, _) in FALLBACK_TZ_OFFSETS.items():
            if std_min == offset_min:
                return iana_name
        return _format_utc_offset(offset_min)
    except Exception:
        return "UTC"


def detect_system_timezone():
    """Returns cached system timezone name (refreshed periodically every 60s)."""
    now_m = time.monotonic()
    with _TZ_LOCK:
        cached = _TZ_DETECT_CACHE["value"]
        if cached is not None and (now_m - _TZ_DETECT_CACHE["ts"]) < _TZ_CACHE_TTL:
            return cached
    detected = _detect_system_timezone_uncached()
    with _TZ_LOCK:
        _TZ_DETECT_CACHE["value"] = detected
        _TZ_DETECT_CACHE["ts"] = time.monotonic()
    return detected


def get_timezone_file_path():
    return os.path.join(os.path.expanduser("~"), ".trade_journal_timezone.json")


def load_timezone_preference():
    path = get_timezone_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tz_val = str(data.get("timezone", "Auto")).strip()
            if tz_val:
                return tz_val
        except Exception as e:
            logger.warning(f"Failed to load timezone preference: {e}")
    return "Auto"


def save_timezone_preference(tz_key):
    clean_val = str(tz_key or "Auto").strip() or "Auto"
    try:
        with open(get_timezone_file_path(), "w", encoding="utf-8") as f:
            json.dump({"timezone": clean_val}, f)
    except Exception as e:
        logger.warning(f"Error saving timezone preference: {e}")
    return clean_val


_APP_TIMEZONE_PREF = load_timezone_preference()


def get_app_timezone_pref():
    return _APP_TIMEZONE_PREF


def get_tz_cache_epoch():
    return _TZ_CACHE_EPOCH


def set_app_timezone(tz_key):
    global _APP_TIMEZONE_PREF, _TZ_CACHE_EPOCH
    saved = save_timezone_preference(tz_key)
    with _TZ_LOCK:
        _APP_TIMEZONE_PREF = saved
        _TZ_DETECT_CACHE["value"] = None
        _TZ_DETECT_CACHE["ts"] = 0.0
        _TZ_RESOLVE_CACHE.clear()
        _TZ_CACHE_EPOCH += 1
    return saved


def _resolve_tz_info_uncached(pref):
    is_auto = (pref == "Auto")
    target_name = detect_system_timezone() if is_auto else pref

    if target_name == "UTC":
        return dt_timezone.utc, "UTC", "UTC", "UTC+0"

    if ZoneInfo is not None and "/" in target_name:
        try:
            zi = ZoneInfo(target_name)
            now_z = datetime.now(zi)
            offset_min = int(round((now_z.utcoffset() or timedelta(0)).total_seconds() / 60))
            offset_lbl = _format_utc_offset(offset_min)
            abbr = _clean_tz_abbr(now_z.strftime("%Z"), offset_min)
            return zi, target_name, abbr, offset_lbl
        except Exception:
            pass

    if is_auto:
        try:
            local_now = datetime.now().astimezone()
            offset_min = int(round((local_now.utcoffset() or timedelta(0)).total_seconds() / 60))
            offset_lbl = _format_utc_offset(offset_min)
            abbr = _clean_tz_abbr(local_now.strftime("%Z"), offset_min)
            return local_now.tzinfo or dt_timezone(timedelta(minutes=offset_min)), target_name, abbr, offset_lbl
        except Exception:
            pass

    if target_name in FALLBACK_TZ_OFFSETS:
        std_min, std_abbr = FALLBACK_TZ_OFFSETS[target_name]
        offset_lbl = _format_utc_offset(std_min)
        tz_obj = dt_timezone(timedelta(minutes=std_min), name=std_abbr)
        return tz_obj, target_name, std_abbr, offset_lbl

    if target_name.startswith("UTC"):
        try:
            rest = target_name[3:]
            if not rest:
                return dt_timezone.utc, "UTC", "UTC", "UTC+0"
            sign = 1 if rest[0] == "+" else -1
            parts = rest[1:].split(":")
            hrs = int(parts[0])
            mins = int(parts[1]) if len(parts) > 1 else 0
            total_m = sign * (hrs * 60 + mins)
            lbl = _format_utc_offset(total_m)
            return dt_timezone(timedelta(minutes=total_m), name=lbl), lbl, lbl, lbl
        except Exception:
            pass

    return dt_timezone.utc, "UTC", "UTC", "UTC+0"


def resolve_tz_info(tz_pref=None):
    """Returns (tzinfo_obj, resolved_name, short_abbr, offset_label) safely with TTL caching."""
    pref = str(tz_pref if tz_pref is not None else _APP_TIMEZONE_PREF).strip() or "Auto"
    now_m = time.monotonic()
    with _TZ_LOCK:
        entry = _TZ_RESOLVE_CACHE.get(pref)
        if entry is not None and (now_m - entry[1]) < _TZ_CACHE_TTL:
            return entry[0]
    resolved = _resolve_tz_info_uncached(pref)
    with _TZ_LOCK:
        if len(_TZ_RESOLVE_CACHE) > 64:
            _TZ_RESOLVE_CACHE.clear()
        _TZ_RESOLVE_CACHE[pref] = (resolved, time.monotonic())
    return resolved


def get_app_tz_info():
    return resolve_tz_info(_APP_TIMEZONE_PREF)


def get_app_tzinfo():
    return resolve_tz_info(_APP_TIMEZONE_PREF)[0]


def utc_timestamp_to_local_short_date(raw_date):
    """Converts DB UTC timestamps (or datetime objects) to local YYYY-MM-DD while preserving date-only strings."""
    if not raw_date:
        return ""
    if isinstance(raw_date, datetime):
        dt = raw_date if raw_date.tzinfo is not None else raw_date.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(get_app_tzinfo()).strftime("%Y-%m-%d")
    s = str(raw_date).strip()
    if len(s) <= 10:
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(get_app_tzinfo()).strftime("%Y-%m-%d")
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=dt_timezone.utc)
                return dt.astimezone(get_app_tzinfo()).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return s[:10]


def format_db_date_for_edit(raw_date):
    """Formats a DB trade timestamp into a clean local YYYY-MM-DD HH:MM:SS (or YYYY-MM-DD) string for editing."""
    if not raw_date:
        return ""
    app_tz = get_app_tzinfo()
    if isinstance(raw_date, datetime):
        dt = raw_date if raw_date.tzinfo is not None else raw_date.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")
    s = str(raw_date).strip()
    if len(s) <= 10:
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=dt_timezone.utc)
                return dt.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return s


def parse_edit_date_to_utc(user_text):
    """Parses user-edited local date/datetime text into a UTC 'YYYY-MM-DD HH:MM:SS' DB string, or None if invalid."""
    raw = str(user_text or "").strip()
    if not raw:
        return None

    normalized = raw.replace("/", "-")
    app_tz = get_app_tzinfo()

    # Date-only input: anchor to 12:00:00 local so both SQLite and Postgres preserve the local calendar day cleanly
    if len(normalized) == 10:
        try:
            d = datetime.strptime(normalized, "%Y-%m-%d")
            local_dt = datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=app_tz)
            return local_dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(normalized, fmt)
            local_dt = dt.replace(tzinfo=app_tz)
            return local_dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=app_tz)
        return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_timezone_dropdown_options(auto_prefix="Auto (System"):
    """Builds searchable timezone dropdown options and bidirectional key mapping."""
    detected = detect_system_timezone()
    _, _, _, det_offset = resolve_tz_info(detected)

    auto_label = f"{auto_prefix}: {detected}, {det_offset})"
    options = [auto_label]
    display_to_key = {auto_label: "Auto"}
    key_to_display = {"Auto": auto_label}

    ordered_keys = list(TIMEZONE_PRESETS)
    if detected and detected not in ordered_keys and "/" in detected:
        ordered_keys.insert(1, detected)

    current_pref = get_app_timezone_pref()
    if current_pref and current_pref != "Auto" and current_pref not in ordered_keys:
        ordered_keys.insert(1, current_pref)

    for tz_name in ordered_keys:
        _, _, _, off_lbl = resolve_tz_info(tz_name)
        disp = f"{tz_name} ({off_lbl})"
        options.append(disp)
        display_to_key[disp] = tz_name
        key_to_display[tz_name] = disp

    return options, display_to_key, key_to_display


# ---------------- SHARED DOMAIN UTILITIES ---------------- #

def parse_fx_currencies(pair_str):
    """Extracts (base_ccy, quote_ccy) from display strings like 'EUR/GBP' or 'XAU/USD (Gold)'."""
    raw = str(pair_str or "").strip()
    token = raw.upper().split()[0] if raw else ""
    if "/" in token:
        parts = token.split("/", 1)
        return parts[0].strip(), parts[1].strip()
    alpha = re.sub(r"[^A-Z]", "", token)
    if len(alpha) == 6:
        return alpha[:3], alpha[3:]
    return token, ""


def get_pip_multiplier(pair):
    """Returns pip scaling factor based on asset class."""
    pair_u = str(pair or "").upper()
    if "JPY" in pair_u:
        return 100.0
    if "XAU" in pair_u or "GOLD" in pair_u:
        return 10.0
    if "BTC" in pair_u or "ETH" in pair_u:
        return 1.0
    return 10000.0


def normalize_timeframe_str(raw_tf, map_hours=False):
    """Formats numeric timeframe strings consistently across UI and AI extractors."""
    if not raw_tf:
        return ""
    s = str(raw_tf).strip()
    if s.isdigit():
        if map_hours:
            mins = int(s)
            if mins == 60:
                return "1H"
            if mins == 240:
                return "4H"
            return f"{mins}m"
        return f"{s}m"
    return s