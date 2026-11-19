import time
import threading
from datetime import datetime, timedelta, timezone

import database as db
from theme import logger, get_app_tzinfo
from widgets import parse_news_event

_news_history_table_ready = False
_news_history_lock = threading.Lock()
_last_news_prune_ts = 0.0
_NEWS_RETENTION_SECONDS = 45 * 86400

_NEWS_CACHE_LOCK = threading.Lock()
_NEWS_CACHE_TTL_SECONDS = 300.0
_WEEK_FEED_CACHE = {"items": None, "ts": 0.0, "tz_sig": None}
_RANGE_FEED_CACHE = {}
_PREFETCH_LOCK = threading.Lock()
_PREFETCH_IN_FLIGHT = False


def invalidate_news_cache():
    """Clears in-memory economic calendar feed caches."""
    with _NEWS_CACHE_LOCK:
        _WEEK_FEED_CACHE["items"] = None
        _WEEK_FEED_CACHE["ts"] = 0.0
        _WEEK_FEED_CACHE["tz_sig"] = None
        _RANGE_FEED_CACHE.clear()


def _get_range_bounds(target_range, now):
    days_since_sunday = (now.weekday() + 1) % 7
    current_sunday = (now - timedelta(days=days_since_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    if target_range == "Today":
        start_date = today_start
        end_date = today_end
    elif target_range == "Next Week":
        start_date = current_sunday + timedelta(days=7)
        end_date = (start_date + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)
    elif target_range == "Last Week":
        start_date = current_sunday - timedelta(days=7)
        end_date = (start_date + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)
    else:  # "This Week"
        start_date = current_sunday
        end_date = (current_sunday + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=999999)

    return start_date, end_date, today_start, today_end


def _slice_parsed_events(all_parsed, target_range, now, app_tz):
    start_date, end_date, today_start, today_end = _get_range_bounds(target_range, now)
    need_dash_slices = target_range in ("Today", "This Week")
    events = []
    upcoming_events = []
    today_events = []

    for item_dict in all_parsed:
        ev_dt = item_dict.get("dt")
        if ev_dt:
            comp_dt = ev_dt.replace(tzinfo=now.tzinfo) if (ev_dt.tzinfo is None and now.tzinfo is not None) else ev_dt
            if comp_dt.tzinfo != app_tz and app_tz is not None:
                comp_dt = comp_dt.astimezone(app_tz)
            if need_dash_slices:
                if (comp_dt + timedelta(minutes=30)) >= now:
                    upcoming_events.append(item_dict)
                if today_start <= comp_dt <= today_end:
                    today_events.append(item_dict)
            if start_date <= comp_dt <= end_date:
                events.append(item_dict)

    events.sort(key=lambda x: x["dt"] if x["dt"] else start_date)
    if need_dash_slices:
        upcoming_events.sort(key=lambda x: x["dt"] if x["dt"] else now)
    return events, upcoming_events, today_events


def get_cached_news_range(target_range, max_age=_NEWS_CACHE_TTL_SECONDS):
    """Returns (events, upcoming_events, today_events) immediately from memory cache if fresh, else None."""
    app_tz = get_app_tzinfo()
    now = datetime.now(app_tz)
    tz_sig = str(getattr(app_tz, "key", None) or app_tz)
    now_ts = time.time()

    with _NEWS_CACHE_LOCK:
        if target_range in ("Today", "This Week"):
            wk_items = _WEEK_FEED_CACHE.get("items")
            wk_ts = _WEEK_FEED_CACHE.get("ts", 0.0)
            wk_tz = _WEEK_FEED_CACHE.get("tz_sig")
            if wk_items is not None and (now_ts - wk_ts) < max_age:
                items_copy = list(wk_items)
                if wk_tz != tz_sig:
                    reproject_news_events(items_copy)
                return _slice_parsed_events(items_copy, target_range, now, app_tz)

        entry = _RANGE_FEED_CACHE.get(target_range)
        if entry and (now_ts - entry.get("ts", 0.0)) < max_age:
            items_copy = list(entry.get("items") or [])
            if entry.get("tz_sig") != tz_sig:
                reproject_news_events(items_copy)
            return _slice_parsed_events(items_copy, target_range, now, app_tz)

    return None


def _persist_history_async(parsed_items, start_dt, end_dt):
    if not parsed_items:
        return

    def _job():
        save_and_load_local_history(parsed_items, start_dt, end_dt)

    threading.Thread(target=_job, daemon=True).start()


def prefetch_adjacent_weeks(http_session):
    """Quietly warms Next Week & Last Week in the background so switching ranges is instantaneous."""
    global _PREFETCH_IN_FLIGHT
    with _PREFETCH_LOCK:
        if _PREFETCH_IN_FLIGHT:
            return
        _PREFETCH_IN_FLIGHT = True

    def _run():
        global _PREFETCH_IN_FLIGHT
        try:
            for r_name in ("Next Week", "Last Week"):
                if get_cached_news_range(r_name) is None:
                    try:
                        fetch_news_for_range(http_session, r_name, force=False)
                    except Exception as e:
                        logger.warning(f"Background news prefetch failed for {r_name}: {e}")
        finally:
            with _PREFETCH_LOCK:
                _PREFETCH_IN_FLIGHT = False

    threading.Thread(target=_run, daemon=True).start()


def save_and_load_local_history(parsed_items, start_dt, end_dt):
    """Persist fetched events in DB (PostgreSQL or SQLite) so historical events are available offline."""
    global _news_history_table_ready
    conn = None
    try:
        conn = db.get_connection()
        cur = conn.cursor()
        is_pg = db.is_postgres()
        ph = "%s" if is_pg else "?"

        if not _news_history_table_ready:
            with _news_history_lock:
                if not _news_history_table_ready:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS news_history (
                            event_key TEXT PRIMARY KEY,
                            ts DOUBLE PRECISION,
                            date_str TEXT,
                            time_str TEXT,
                            currency TEXT,
                            impact TEXT,
                            title TEXT,
                            actual TEXT,
                            forecast TEXT,
                            previous TEXT
                        )
                    """)
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_news_history_ts ON news_history(ts);")
                    conn.commit()
                    _news_history_table_ready = True

        if parsed_items:
            rows_to_upsert = []
            for item in parsed_items:
                dt_obj = item.get("dt")
                if not dt_obj:
                    continue
                ts_val = dt_obj.timestamp()
                ev_key = f"{int(ts_val)}_{item.get('currency', '')}_{item.get('title', '')}"
                rows_to_upsert.append((
                    ev_key,
                    ts_val,
                    item.get("date", ""),
                    item.get("time", ""),
                    item.get("currency", ""),
                    item.get("impact", "Low"),
                    item.get("title", ""),
                    item.get("actual", ""),
                    item.get("forecast", ""),
                    item.get("previous", ""),
                ))

            if rows_to_upsert:
                if is_pg:
                    cur.executemany(f"""
                        INSERT INTO news_history
                        (event_key, ts, date_str, time_str, currency, impact, title, actual, forecast, previous)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                        ON CONFLICT (event_key) DO UPDATE SET
                            ts = EXCLUDED.ts,
                            date_str = EXCLUDED.date_str,
                            time_str = EXCLUDED.time_str,
                            currency = EXCLUDED.currency,
                            impact = EXCLUDED.impact,
                            title = EXCLUDED.title,
                            actual = EXCLUDED.actual,
                            forecast = EXCLUDED.forecast,
                            previous = EXCLUDED.previous
                    """, rows_to_upsert)
                else:
                    cur.executemany(f"""
                        INSERT OR REPLACE INTO news_history
                        (event_key, ts, date_str, time_str, currency, impact, title, actual, forecast, previous)
                        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                    """, rows_to_upsert)

                global _last_news_prune_ts
                now_ts = time.time()
                should_prune = False
                with _news_history_lock:
                    if (now_ts - _last_news_prune_ts) >= 3600:
                        _last_news_prune_ts = now_ts
                        should_prune = True
                if should_prune:
                    cutoff_ts = now_ts - _NEWS_RETENTION_SECONDS
                    cur.execute(f"DELETE FROM news_history WHERE ts < {ph}", (cutoff_ts,))
            conn.commit()
            return []

        cur.execute(
            f"SELECT ts, date_str, time_str, currency, impact, title, actual, forecast, previous "
            f"FROM news_history WHERE ts >= {ph} AND ts <= {ph} ORDER BY ts ASC",
            (start_dt.timestamp(), end_dt.timestamp())
        )
        rows = cur.fetchall()

        app_tz = get_app_tzinfo()
        restored = []
        for r in rows:
            local_dt = datetime.fromtimestamp(r[0], tz=timezone.utc).astimezone(app_tz)
            restored.append({
                "dt": local_dt,
                "date": local_dt.strftime("%a %b %d").replace(" 0", " "),
                "time": local_dt.strftime("%I:%M%p").lstrip("0").lower(),
                "currency": r[3],
                "impact": r[4],
                "title": r[5],
                "actual": r[6],
                "forecast": r[7],
                "previous": r[8],
            })
        return restored
    except Exception as db_err:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning(f"Local news_history cache error: {db_err}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def fetch_tradingview_range(http_session, start_dt, end_dt, app_tz=None):
    """Fetch any arbitrary date window (Last Week, This Week, Next Week) from TradingView public calendar."""
    from_iso = start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to_iso = end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    tv_url = f"https://economic-calendar.tradingview.com/events?from={from_iso}&to={to_iso}"
    headers = {
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "Accept": "application/json",
    }
    res = http_session.get(tv_url, headers=headers, timeout=6)
    if res.status_code != 200:
        return []
    try:
        payload = res.json()
    except ValueError:
        return []
    raw_list = payload.get("result", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])

    country_to_ccy = {
        "US": "USD", "EU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
        "GB": "GBP", "UK": "GBP", "JP": "JPY", "AU": "AUD", "NZ": "NZD",
        "CA": "CAD", "CH": "CHF", "CN": "CNY",
    }
    imp_map = {1: "High", 0: "Medium", -1: "Low"}
    if app_tz is None:
        app_tz = get_app_tzinfo()

    def _fmt_num(val, u=""):
        if val is None or val == "":
            return ""
        return f"{val}{u or ''}"

    parsed_list = []
    for ev in raw_list:
        ccy = str(ev.get("currency") or country_to_ccy.get(str(ev.get("country", "")).upper(), "")).upper().strip()
        if ccy not in ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"):
            continue
        date_raw = ev.get("date", "")
        if not date_raw:
            continue
        try:
            dt_utc = datetime.fromisoformat(str(date_raw).replace("Z", "+00:00"))
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(app_tz)
        except Exception:
            continue

        imp_val = ev.get("importance", -1)
        impact_str = imp_map.get(imp_val, "Low") if isinstance(imp_val, int) else str(imp_val).title()

        unit = ev.get("unit") or ""
        parsed_list.append({
            "dt": dt_local,
            "date": dt_local.strftime("%a %b %d").replace(" 0", " "),
            "time": dt_local.strftime("%I:%M%p").lstrip("0").lower(),
            "currency": ccy,
            "impact": impact_str,
            "title": str(ev.get("title") or ev.get("indicator") or "").strip(),
            "actual": _fmt_num(ev.get("actual"), unit),
            "forecast": _fmt_num(ev.get("forecast"), unit),
            "previous": _fmt_num(ev.get("previous"), unit),
        })
    return parsed_list


def fetch_news_for_range(http_session, target_range, force=False):
    """Fetches economic calendar events for the requested range and computes upcoming & today slices."""
    if not force:
        cached_slices = get_cached_news_range(target_range)
        if cached_slices is not None:
            return cached_slices

    app_tz = get_app_tzinfo()
    now = datetime.now(app_tz)
    tz_sig = str(getattr(app_tz, "key", None) or app_tz)
    start_date, end_date, _, _ = _get_range_bounds(target_range, now)

    all_parsed = []

    if target_range in ("Today", "This Week"):
        for target_url in (
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            "https://pipatlas.com/api/calendar",
        ):
            try:
                res = http_session.get(target_url, timeout=4)
                if res.status_code == 200:
                    data = res.json()
                    raw_list = data["events"] if isinstance(data, dict) and "events" in data else (data if isinstance(data, list) else [])
                    for ev in raw_list:
                        item_dict = parse_news_event(ev, app_tz=app_tz)
                        if item_dict:
                            all_parsed.append(item_dict)
                    if all_parsed:
                        break
            except Exception as e:
                logger.warning(f"Error fetching calendar JSON from {target_url}: {e}")

    if not all_parsed:
        try:
            all_parsed = fetch_tradingview_range(http_session, start_date, end_date, app_tz=app_tz)
        except Exception as e:
            logger.warning(f"Error fetching TradingView calendar for {target_range}: {e}")

    if all_parsed:
        now_ts = time.time()
        with _NEWS_CACHE_LOCK:
            if target_range in ("Today", "This Week"):
                _WEEK_FEED_CACHE["items"] = list(all_parsed)
                _WEEK_FEED_CACHE["ts"] = now_ts
                _WEEK_FEED_CACHE["tz_sig"] = tz_sig
            _RANGE_FEED_CACHE[target_range] = {
                "items": list(all_parsed),
                "ts": now_ts,
                "tz_sig": tz_sig,
            }
        _persist_history_async(list(all_parsed), start_date, end_date)
    else:
        db_events = save_and_load_local_history([], start_date, end_date)
        if db_events:
            all_parsed = db_events

    return _slice_parsed_events(all_parsed, target_range, now, app_tz)


def compute_upcoming_and_today(cached_events):
    """Re-slices cached news events against the current clock for live dashboard widgets."""
    now = datetime.now(get_app_tzinfo())
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    upcoming = []
    today_events = []
    for e in cached_events:
        ev_dt = e.get("dt")
        if ev_dt:
            comp_dt = ev_dt.replace(tzinfo=now.tzinfo) if (ev_dt.tzinfo is None and now.tzinfo is not None) else ev_dt
            if (comp_dt + timedelta(minutes=30)) >= now:
                upcoming.append(e)
            if today_start <= comp_dt <= today_end:
                today_events.append(e)

    upcoming.sort(key=lambda x: x["dt"] if x["dt"] else now)
    return upcoming, today_events


def reproject_news_events(events):
    """Converts cached news event timestamps and display strings into the active app timezone."""
    if not events:
        return []
    app_tz = get_app_tzinfo()
    for ev in events:
        ev_dt = ev.get("dt")
        if ev_dt is None:
            continue
        if ev_dt.tzinfo is None:
            ev_dt = ev_dt.replace(tzinfo=app_tz)
        else:
            ev_dt = ev_dt.astimezone(app_tz)
        ev["dt"] = ev_dt
        ev["date"] = ev_dt.strftime("%a %b %d").replace(" 0", " ")
        raw_time = ev_dt.strftime("%I:%M%p").lower()
        ev["time"] = raw_time.lstrip("0") if raw_time.startswith("0") else raw_time
    return events


def get_clear_pairs(today_events, parsed_forex_pairs):
    """Returns up to 20 forex pairs with no remaining/recent news today."""
    now = datetime.now(get_app_tzinfo())
    active_news_currencies = set()

    for ev in today_events:
        curr = str(ev.get("currency", "")).upper().strip()
        ev_dt = ev.get("dt")
        if curr and ev_dt:
            comp_dt = ev_dt.replace(tzinfo=now.tzinfo) if (ev_dt.tzinfo is None and now.tzinfo is not None) else ev_dt
            if comp_dt > now or (comp_dt + timedelta(hours=1)) > now:
                active_news_currencies.add(curr)
        elif curr:
            active_news_currencies.add(curr)

    no_news_pairs = [
        clean_p for clean_p, c1, c2 in parsed_forex_pairs
        if c1 not in active_news_currencies and c2 not in active_news_currencies
    ]
    return tuple(no_news_pairs[:20])


def filter_news_events(events, currency_filter="All Currencies", impact_filter="All Impacts"):
    filtered = list(events)
    if currency_filter != "All Currencies":
        filtered = [e for e in filtered if e["currency"].upper() == currency_filter.upper()]

    if impact_filter == "High Impact":
        filtered = [
            e for e in filtered
            if "High" in str(e.get("impact", "")).capitalize() or str(e.get("impact", "")) in ["Red", "3"]
        ]
    elif impact_filter == "High & Medium":
        filtered = [
            e for e in filtered
            if any(k in str(e.get("impact", "")).capitalize() for k in ["High", "Medium"])
            or str(e.get("impact", "")) in ["Red", "Orange", "3", "2"]
        ]
    return filtered