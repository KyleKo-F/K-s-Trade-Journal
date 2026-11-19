import os
import sys
import json
import time
import sqlite3
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlparse, parse_qs
import psycopg2
from psycopg2 import pool

from theme import logger, read_env_value, utc_timestamp_to_local_short_date, get_app_tzinfo

SCHEMA_VERSION = "3"


def _read_env_value(key):
    """Reads a key from process environment, .env files, or ~/.trade_journal_config.json."""
    val = read_env_value(key)
    if val:
        return val

    cfg_path = os.path.join(os.path.expanduser("~"), ".trade_journal_config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg_val = str(data.get(key.lower(), "")).strip()
            if cfg_val:
                return cfg_val
        except Exception:
            pass

    return ""


_CACHED_FILE_DB_URL = _read_env_value("DATABASE_URL")


def get_database_url():
    """Returns configured Cloud PostgreSQL URL, or empty string for local SQLite fallback."""
    env_val = os.getenv("DATABASE_URL", "").strip()
    return env_val if env_val else _CACHED_FILE_DB_URL


DATABASE_URL = get_database_url()

_pg_pool = None
_pg_pool_lock = threading.Lock()
_sqlite_local = threading.local()
_sqlite_conns = []
_sqlite_conns_lock = threading.Lock()
_sqlite_wal_paths = set()
_sqlite_wal_lock = threading.Lock()
_schema_initialized = False
_schema_lock = threading.Lock()


def _build_pg_pool_kwargs(dsn):
    """Builds hardened PostgreSQL connection arguments with SSL enforcement and TCP keepalives."""
    kwargs = {
        "minconn": 1,
        "maxconn": 8,
        "dsn": dsn,
        "connect_timeout": 8,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    lower_dsn = dsn.lower()
    has_sslmode = "sslmode=" in lower_dsn
    if not has_sslmode:
        host = ""
        try:
            if dsn.startswith(("postgres://", "postgresql://")):
                parsed = urlparse(dsn)
                host = (parsed.hostname or "").lower()
                qs = parse_qs(parsed.query)
                if "sslmode" in qs:
                    has_sslmode = True
        except Exception:
            pass
        if not has_sslmode:
            kwargs["sslmode"] = "prefer" if host in ("localhost", "127.0.0.1", "::1", "") else "require"
    return kwargs


def _validate_pg_connection(raw_conn):
    """Verifies a pooled PostgreSQL connection is still alive and clean before returning it."""
    if raw_conn is None or raw_conn.closed:
        return False
    try:
        cur = raw_conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        raw_conn.rollback()
        return True
    except Exception:
        return False


class _PooledPgConnection:
    """Wraps a pooled psycopg2 connection so .close() returns it cleanly to the pool."""
    def __init__(self, conn, conn_pool):
        self._conn = conn
        self._pool = conn_pool

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._conn is None:
            return
        try:
            is_broken = bool(self._conn.closed)
            if not is_broken:
                try:
                    self._conn.rollback()
                except Exception:
                    is_broken = True
            self._pool.putconn(self._conn, close=is_broken)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
        finally:
            self._conn = None


class _PooledSqliteConnection:
    """Wraps a thread-local SQLite connection so .close() rolls back and returns it for reuse."""
    def __init__(self, conn, db_path):
        self._conn = conn
        self._path = db_path

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._conn is None:
            return
        try:
            if getattr(self._conn, "in_transaction", False):
                self._conn.rollback()
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            entry = getattr(_sqlite_local, "conn_entry", None)
            if entry is not None and entry[0] is self._conn:
                _sqlite_local.conn_entry = None
        finally:
            self._conn = None


def _close_all_sqlite_conns():
    with _sqlite_conns_lock:
        conns = list(_sqlite_conns)
        _sqlite_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    with _sqlite_wal_lock:
        _sqlite_wal_paths.clear()


def close_pool():
    """Closes any active PostgreSQL and SQLite connection pools cleanly on application exit."""
    global _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None:
            try:
                _pg_pool.closeall()
            except Exception:
                pass
            _pg_pool = None
    _close_all_sqlite_conns()


def reload_db_config():
    """Reloads DATABASE_URL and resets any active connection pools."""
    global DATABASE_URL, _CACHED_FILE_DB_URL, _pg_pool, _schema_initialized
    with _pg_pool_lock:
        if _pg_pool is not None:
            try:
                _pg_pool.closeall()
            except Exception:
                pass
            _pg_pool = None
        _CACHED_FILE_DB_URL = _read_env_value("DATABASE_URL")
        DATABASE_URL = get_database_url()
    _close_all_sqlite_conns()
    with _schema_lock:
        _schema_initialized = False


def is_postgres():
    url = get_database_url()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _get_pg_pool(force_reset=False):
    global _pg_pool
    if force_reset or _pg_pool is None:
        with _pg_pool_lock:
            if force_reset and _pg_pool is not None:
                try:
                    _pg_pool.closeall()
                except Exception:
                    pass
                _pg_pool = None
            if _pg_pool is None:
                _pg_pool = pool.ThreadedConnectionPool(**_build_pg_pool_kwargs(get_database_url()))
    return _pg_pool


def get_sqlite_db_path():
    """Returns a deterministic SQLite path while staying compatible with existing local database.db files."""
    env_path = os.environ.get("DATABASE_PATH", "").strip()
    if env_path:
        resolved = os.path.abspath(os.path.expanduser(env_path))
    else:
        if getattr(sys, "frozen", False):
            legacy_path = os.path.join(os.path.dirname(sys.executable), "database.db")
        else:
            legacy_path = os.path.abspath("database.db")
        if os.path.exists(legacy_path):
            resolved = legacy_path
        else:
            resolved = os.path.join(os.path.expanduser("~"), ".trade_journal.db")
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return resolved


def get_connection():
    if is_postgres():
        conn_pool = _get_pg_pool()
        last_err = None
        for _attempt in range(4):
            raw_conn = None
            try:
                raw_conn = conn_pool.getconn()
            except pool.PoolError as err:
                last_err = err
                time.sleep(0.15)
                continue
            except Exception as err:
                last_err = err
                time.sleep(0.1)
                continue

            if _validate_pg_connection(raw_conn):
                return _PooledPgConnection(raw_conn, conn_pool)
            try:
                conn_pool.putconn(raw_conn, close=True)
            except Exception:
                pass
        if last_err:
            raise last_err
        raise RuntimeError("Unable to acquire a healthy PostgreSQL connection from pool.")

    db_path = get_sqlite_db_path()
    entry = getattr(_sqlite_local, "conn_entry", None)
    if entry is not None:
        raw_conn, cached_path = entry
        if cached_path == db_path:
            try:
                raw_conn.execute("SELECT 1")
                if getattr(raw_conn, "in_transaction", False):
                    raw_conn.rollback()
                return _PooledSqliteConnection(raw_conn, db_path)
            except Exception:
                pass
        try:
            raw_conn.close()
        except Exception:
            pass
        with _sqlite_conns_lock:
            if raw_conn in _sqlite_conns:
                _sqlite_conns.remove(raw_conn)
        _sqlite_local.conn_entry = None

    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout = 10000;")
    except Exception:
        pass
    with _sqlite_wal_lock:
        need_wal = db_path not in _sqlite_wal_paths
        if need_wal:
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                _sqlite_wal_paths.add(db_path)
            except Exception:
                pass
    try:
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-16000;")
    except Exception:
        pass
    with _sqlite_conns_lock:
        _sqlite_conns.append(conn)
    _sqlite_local.conn_entry = (conn, db_path)
    return _PooledSqliteConnection(conn, db_path)


def get_schema_meta(key, cursor=None):
    """Reads a metadata key from schema_meta if the table exists."""
    own_conn = None
    try:
        if cursor is None:
            own_conn = get_connection()
            cursor = own_conn.cursor()
        ph = "%s" if is_postgres() else "?"
        cursor.execute(f"SELECT value FROM schema_meta WHERE key = {ph}", (str(key),))
        row = cursor.fetchone()
        return str(row[0]) if row and row[0] is not None else ""
    except Exception:
        if own_conn is not None:
            try:
                own_conn.rollback()
            except Exception:
                pass
        return ""
    finally:
        if own_conn is not None:
            own_conn.close()


def set_schema_meta(key, value, cursor, commit_conn=None):
    """Persists a metadata key in schema_meta (creating schema_meta if needed)."""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    ph = "%s" if is_postgres() else "?"
    if is_postgres():
        cursor.execute(
            f"INSERT INTO schema_meta (key, value) VALUES ({ph}, {ph}) "
            f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(key), str(value))
        )
    else:
        cursor.execute(
            f"INSERT OR REPLACE INTO schema_meta (key, value) VALUES ({ph}, {ph})",
            (str(key), str(value))
        )
    if commit_conn is not None:
        commit_conn.commit()


def _cleanup_local_trade_images(rows):
    """Safely removes orphaned before/after chart screenshots inside ~/.trade_journal_images when trades are deleted."""
    if not rows:
        return
    saved_dir = os.path.realpath(os.path.join(os.path.expanduser("~"), ".trade_journal_images"))
    if not os.path.isdir(saved_dir):
        return

    for row in rows:
        try:
            tid = int(row[0])
            for prefix in ("synced_chart_", "synced_after_chart_"):
                cached_path = os.path.realpath(os.path.join(saved_dir, f"{prefix}{tid}.jpg"))
                if os.path.commonpath([saved_dir, cached_path]) == saved_dir and os.path.isfile(cached_path):
                    os.remove(cached_path)
        except Exception:
            pass

        for col_idx in (1, 2):
            img_path = str(row[col_idx] or "").strip() if len(row) > col_idx else ""
            if img_path:
                try:
                    real_img = os.path.realpath(img_path)
                    if os.path.commonpath([saved_dir, real_img]) == saved_dir and os.path.isfile(real_img):
                        os.remove(real_img)
                except Exception:
                    pass


def init_db():
    global _schema_initialized
    if _schema_initialized:
        return

    with _schema_lock:
        if _schema_initialized:
            return
        conn = get_connection()
        try:
            cursor = conn.cursor()
            if get_schema_meta("trades_schema_version", cursor=cursor) == SCHEMA_VERSION:
                _schema_initialized = True
                return
            try:
                conn.rollback()
            except Exception:
                pass

            if is_postgres():
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        pair TEXT NOT NULL,
                        timeframe TEXT,
                        direction TEXT,
                        entry_price TEXT,
                        stop_loss TEXT,
                        take_profit TEXT,
                        rr_ratio TEXT,
                        status TEXT,
                        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notes TEXT,
                        image_path TEXT,
                        trade_type TEXT,
                        pnl_pips TEXT,
                        username TEXT DEFAULT 'admin',
                        image_data TEXT,
                        checklist_score TEXT DEFAULT '',
                        after_image_path TEXT DEFAULT '',
                        after_image_data TEXT DEFAULT ''
                    );
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS pnl_pips TEXT;
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS username TEXT DEFAULT 'admin';
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS image_data TEXT;
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS checklist_score TEXT DEFAULT '';
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS after_image_path TEXT DEFAULT '';
                    ALTER TABLE trades ADD COLUMN IF NOT EXISTS after_image_data TEXT DEFAULT '';
                    CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);
                    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                    CREATE INDEX IF NOT EXISTS idx_trades_username ON trades(username);
                    CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades(LOWER(COALESCE(NULLIF(username, ''), 'admin')), id DESC);
                    CREATE INDEX IF NOT EXISTS idx_trades_upper_status ON trades(UPPER(status));
                ''')
                cursor.execute("SELECT 1 FROM trades WHERE username IS NULL OR username = '' LIMIT 1;")
                if cursor.fetchone():
                    cursor.execute("UPDATE trades SET username = 'admin' WHERE username IS NULL OR username = '';")
            else:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pair TEXT NOT NULL,
                        timeframe TEXT,
                        direction TEXT,
                        entry_price TEXT,
                        stop_loss TEXT,
                        take_profit TEXT,
                        rr_ratio TEXT,
                        status TEXT,
                        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notes TEXT,
                        image_path TEXT,
                        trade_type TEXT,
                        pnl_pips TEXT,
                        username TEXT DEFAULT 'admin',
                        image_data TEXT,
                        checklist_score TEXT DEFAULT '',
                        after_image_path TEXT DEFAULT '',
                        after_image_data TEXT DEFAULT ''
                    )
                ''')
                for col_sql in (
                    "ALTER TABLE trades ADD COLUMN pnl_pips TEXT;",
                    "ALTER TABLE trades ADD COLUMN username TEXT DEFAULT 'admin';",
                    "ALTER TABLE trades ADD COLUMN image_data TEXT;",
                    "ALTER TABLE trades ADD COLUMN checklist_score TEXT DEFAULT '';",
                    "ALTER TABLE trades ADD COLUMN after_image_path TEXT DEFAULT '';",
                    "ALTER TABLE trades ADD COLUMN after_image_data TEXT DEFAULT '';",
                ):
                    try:
                        cursor.execute(col_sql)
                    except sqlite3.OperationalError:
                        pass
                cursor.execute("SELECT 1 FROM trades WHERE username IS NULL OR username = '' LIMIT 1;")
                if cursor.fetchone():
                    cursor.execute("UPDATE trades SET username = 'admin' WHERE username IS NULL OR username = '';")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_username ON trades(username);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades(LOWER(COALESCE(NULLIF(username, ''), 'admin')), id DESC);")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_upper_status ON trades(UPPER(status));")

            set_schema_meta("trades_schema_version", SCHEMA_VERSION, cursor)
            conn.commit()
            _schema_initialized = True
        finally:
            conn.close()


def fetch_all_trades(username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        base_select = (
            "SELECT id, pair, timeframe, direction, entry_price, stop_loss, take_profit, "
            "rr_ratio, status, date, notes, image_path, trade_type, "
            "COALESCE(NULLIF(username, ''), 'admin'), "
            "COALESCE(checklist_score, ''), COALESCE(after_image_path, ''), COALESCE(pnl_pips, '') "
            "FROM trades "
        )
        if username and username != "__ALL__":
            cursor.execute(
                base_select
                + f"WHERE LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder}) "
                + "ORDER BY id DESC",
                (username.strip(),)
            )
        else:
            cursor.execute(base_select + "ORDER BY id DESC")
        return cursor.fetchall()
    finally:
        conn.close()


def fetch_open_trades(username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"SELECT id, pair, direction, entry_price, stop_loss, take_profit "
                f"FROM trades WHERE UPPER(status) = 'OPEN' "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (username.strip(),)
            )
        else:
            cursor.execute(
                "SELECT id, pair, direction, entry_price, stop_loss, take_profit "
                "FROM trades WHERE UPPER(status) = 'OPEN'"
            )
        return cursor.fetchall()
    finally:
        conn.close()


def fetch_trades_by_date(date_str, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        base_select = (
            "SELECT id, pair, timeframe, direction, entry_price, stop_loss, take_profit, "
            "rr_ratio, status, date, notes, image_path, trade_type, "
            "COALESCE(NULLIF(username, ''), 'admin'), "
            "COALESCE(checklist_score, ''), COALESCE(after_image_path, ''), COALESCE(pnl_pips, '') "
            "FROM trades "
        )
        clean_date = str(date_str or "").strip()[:10]
        parsed_day = None
        try:
            parsed_day = datetime.strptime(clean_date, "%Y-%m-%d").date()
        except ValueError:
            parsed_day = None

        if parsed_day is not None:
            local_tz = get_app_tzinfo()
            start_local = datetime(parsed_day.year, parsed_day.month, parsed_day.day, 0, 0, 0, tzinfo=local_tz)
            end_local = start_local + timedelta(days=1)
            start_utc = start_local.astimezone(dt_timezone.utc).replace(tzinfo=None)
            end_utc = end_local.astimezone(dt_timezone.utc).replace(tzinfo=None)

            if is_postgres():
                date_filter_sql = f"((date >= {placeholder} AND date < {placeholder}) OR CAST(date AS TEXT) = {placeholder})"
                date_params = (start_utc, end_utc, clean_date)
            else:
                start_utc_str = start_utc.strftime("%Y-%m-%d %H:%M:%S")
                end_utc_str = end_utc.strftime("%Y-%m-%d %H:%M:%S")
                # Index-friendly outer date prefix envelope + exact UTC timestamp normalization
                env_lo = start_utc.strftime("%Y-%m-%d")
                env_hi = end_utc.strftime("%Y-%m-%d") + "\uffff"
                date_filter_sql = (
                    f"((LENGTH(date) > 10 AND date >= {placeholder} AND date <= {placeholder} "
                    f"AND REPLACE(SUBSTR(date, 1, 19), 'T', ' ') >= {placeholder} "
                    f"AND REPLACE(SUBSTR(date, 1, 19), 'T', ' ') < {placeholder}) "
                    f"OR date = {placeholder})"
                )
                date_params = (env_lo, env_hi, start_utc_str, end_utc_str, clean_date)
        else:
            date_filter_sql = f"DATE(date) = {placeholder}"
            date_params = (clean_date,)

        if username and username != "__ALL__":
            cursor.execute(
                base_select
                + f"WHERE {date_filter_sql} "
                + f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder}) "
                + "ORDER BY id DESC",
                date_params + (username.strip(),)
            )
        else:
            cursor.execute(
                base_select + f"WHERE {date_filter_sql} ORDER BY id DESC",
                date_params
            )
        return cursor.fetchall()
    finally:
        conn.close()


def fetch_trade_dedup_keys(username=None):
    """Returns existing (pair, direction, entry, sl, tp, short_date) tuples for duplicate import prevention."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        base = "SELECT pair, direction, entry_price, stop_loss, take_profit, date FROM trades "
        if username and username != "__ALL__":
            cursor.execute(
                base + f"WHERE LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (username.strip(),)
            )
        else:
            cursor.execute(base)
        keys = set()
        for row in cursor.fetchall():
            short_d = utc_timestamp_to_local_short_date(row[5])
            keys.add((
                str(row[0] or "").strip().upper(),
                str(row[1] or "").strip().upper(),
                str(row[2] or "").strip(),
                str(row[3] or "").strip(),
                str(row[4] or "").strip(),
                short_d,
            ))
        return keys
    finally:
        conn.close()


def add_trade(payload):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        owner = str(payload.get("username") or "admin").strip().lower()
        raw_date = str(payload.get("date") or "").strip()

        cols = (
            "pair, timeframe, direction, entry_price, stop_loss, take_profit, "
            "rr_ratio, status, notes, image_path, trade_type, username, image_data, "
            "checklist_score, after_image_path, after_image_data"
        )
        params = [
            payload.get("pair"),
            payload.get("timeframe"),
            payload.get("direction"),
            payload.get("entry_price"),
            payload.get("stop_loss"),
            payload.get("take_profit"),
            payload.get("rr_ratio"),
            payload.get("status", "OPEN"),
            payload.get("notes", ""),
            payload.get("image_path", ""),
            payload.get("trade_type", "HnS"),
            owner,
            payload.get("image_data", ""),
            payload.get("checklist_score", ""),
            payload.get("after_image_path", ""),
            payload.get("after_image_data", ""),
        ]
        if raw_date:
            cols += ", date"
            params.append(raw_date)

        phs = ", ".join(placeholder for _ in params)
        cursor.execute(f"INSERT INTO trades ({cols}) VALUES ({phs})", tuple(params))
        conn.commit()
        return True, "Trade added successfully"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"add_trade failed: {e}")
        return False, str(e)
    finally:
        conn.close()


def add_trades_bulk(payloads):
    """Inserts multiple trades inside a single transaction while preserving original dates when provided."""
    if not payloads:
        return True, 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"

        rows_with_date = []
        rows_without_date = []
        for payload in payloads:
            owner = str(payload.get("username") or "admin").strip().lower()
            base_row = (
                payload.get("pair"),
                payload.get("timeframe"),
                payload.get("direction"),
                payload.get("entry_price"),
                payload.get("stop_loss"),
                payload.get("take_profit"),
                payload.get("rr_ratio"),
                payload.get("status", "OPEN"),
                payload.get("notes", ""),
                payload.get("image_path", ""),
                payload.get("trade_type", "HnS"),
                owner,
                payload.get("image_data", ""),
                payload.get("checklist_score", ""),
                payload.get("after_image_path", ""),
                payload.get("after_image_data", ""),
            )
            raw_date = str(payload.get("date") or "").strip()
            if raw_date:
                rows_with_date.append(base_row + (raw_date,))
            else:
                rows_without_date.append(base_row)

        base_cols = (
            "pair, timeframe, direction, entry_price, stop_loss, take_profit, "
            "rr_ratio, status, notes, image_path, trade_type, username, image_data, "
            "checklist_score, after_image_path, after_image_data"
        )
        if rows_with_date:
            phs_d = ", ".join(placeholder for _ in range(17))
            cursor.executemany(
                f"INSERT INTO trades ({base_cols}, date) VALUES ({phs_d})",
                rows_with_date,
            )
        if rows_without_date:
            phs_n = ", ".join(placeholder for _ in range(16))
            cursor.executemany(
                f"INSERT INTO trades ({base_cols}) VALUES ({phs_n})",
                rows_without_date,
            )

        conn.commit()
        return True, len(rows_with_date) + len(rows_without_date)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"add_trades_bulk failed: {e}")
        return False, str(e)
    finally:
        conn.close()


def update_trade(trade_id, payload, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"

        fields = [
            f"pair={placeholder}",
            f"timeframe={placeholder}",
            f"direction={placeholder}",
            f"entry_price={placeholder}",
            f"stop_loss={placeholder}",
            f"take_profit={placeholder}",
        ]
        params = [
            payload.get("pair"),
            payload.get("timeframe"),
            payload.get("direction"),
            payload.get("entry_price"),
            payload.get("stop_loss"),
            payload.get("take_profit"),
        ]

        if "rr_ratio" in payload:
            fields.append(f"rr_ratio={placeholder}")
            params.append(payload.get("rr_ratio") or "")

        fields.extend([
            f"status={placeholder}",
            f"notes={placeholder}",
            f"trade_type={placeholder}",
        ])
        params.extend([
            payload.get("status"),
            payload.get("notes"),
            payload.get("trade_type"),
        ])

        if "checklist_score" in payload:
            fields.append(f"checklist_score={placeholder}")
            params.append(payload.get("checklist_score") or "")

        set_clause = "SET " + ", ".join(fields)
        params.append(int(trade_id))

        where_clause = f"WHERE id={placeholder}"
        if username and username != "__ALL__":
            where_clause += f" AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})"
            params.append(username.strip())

        cursor.execute(f"UPDATE trades {set_clause} {where_clause}", tuple(params))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"update_trade failed for id={trade_id}: {e}")
        return False
    finally:
        conn.close()


def update_trade_after_image(trade_id, after_image_path, after_image_data, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        tid = int(trade_id)
        if username and username != "__ALL__":
            cursor.execute(
                f"UPDATE trades SET after_image_path = {placeholder}, after_image_data = {placeholder} "
                f"WHERE id = {placeholder} AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (after_image_path or "", after_image_data or "", tid, username.strip())
            )
        else:
            cursor.execute(
                f"UPDATE trades SET after_image_path = {placeholder}, after_image_data = {placeholder} "
                f"WHERE id = {placeholder}",
                (after_image_path or "", after_image_data or "", tid)
            )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"update_trade_after_image failed for id={trade_id}: {e}")
        return False
    finally:
        conn.close()


def update_trade_status(trade_id, status, username=None):
    """Updates trade outcome status (OPEN / TP / SL) cleanly from quick toggles."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        clean_st = str(status or "OPEN").strip().upper()
        tid = int(trade_id)
        if clean_st == "OPEN":
            set_sql = f"status = {placeholder}, pnl_pips = ''"
            params = [clean_st, tid]
        else:
            set_sql = f"status = {placeholder}"
            params = [clean_st, tid]

        if username and username != "__ALL__":
            cursor.execute(
                f"UPDATE trades SET {set_sql} WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                tuple(params) + (username.strip(),),
            )
        else:
            cursor.execute(f"UPDATE trades SET {set_sql} WHERE id = {placeholder}", tuple(params))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"update_trade_status failed for id={trade_id}: {e}")
        return False
    finally:
        conn.close()


def close_trade_status(trade_id, status, pnl_pips, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"UPDATE trades SET status = {placeholder}, pnl_pips = {placeholder} "
                f"WHERE id = {placeholder} AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (status, str(pnl_pips), int(trade_id), username.strip())
            )
        else:
            cursor.execute(
                f"UPDATE trades SET status = {placeholder}, pnl_pips = {placeholder} WHERE id = {placeholder}",
                (status, str(pnl_pips), int(trade_id))
            )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"close_trade_status failed for id={trade_id}: {e}")
        return False
    finally:
        conn.close()


def delete_trade(trade_id, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        tid = int(trade_id)
        if username and username != "__ALL__":
            owner = username.strip()
            cursor.execute(
                f"SELECT id, image_path, after_image_path FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (tid, owner)
            )
            img_rows = cursor.fetchall()
            cursor.execute(
                f"DELETE FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (tid, owner)
            )
        else:
            cursor.execute(f"SELECT id, image_path, after_image_path FROM trades WHERE id = {placeholder}", (tid,))
            img_rows = cursor.fetchall()
            cursor.execute(f"DELETE FROM trades WHERE id = {placeholder}", (tid,))
        conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            _cleanup_local_trade_images(img_rows)
        return deleted
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"delete_trade failed for id={trade_id}: {e}")
        return False
    finally:
        conn.close()


def delete_trades(trade_ids, username=None):
    if not trade_ids:
        return True
    try:
        clean_ids = [int(tid) for tid in trade_ids]
    except (ValueError, TypeError):
        return False

    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        placeholders = ", ".join(placeholder for _ in clean_ids)
        if username and username != "__ALL__":
            params = tuple(clean_ids) + (username.strip(),)
            cursor.execute(
                f"SELECT id, image_path, after_image_path FROM trades WHERE id IN ({placeholders}) "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                params
            )
            img_rows = cursor.fetchall()
            cursor.execute(
                f"DELETE FROM trades WHERE id IN ({placeholders}) "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                params
            )
        else:
            cursor.execute(
                f"SELECT id, image_path, after_image_path FROM trades WHERE id IN ({placeholders})",
                tuple(clean_ids)
            )
            img_rows = cursor.fetchall()
            cursor.execute(f"DELETE FROM trades WHERE id IN ({placeholders})", tuple(clean_ids))
        conn.commit()
        if cursor.rowcount > 0:
            _cleanup_local_trade_images(img_rows)
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"delete_trades failed: {e}")
        return False
    finally:
        conn.close()


def get_trade_image_path(trade_id, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"SELECT image_path FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (int(trade_id), username.strip())
            )
        else:
            cursor.execute(f"SELECT image_path FROM trades WHERE id = {placeholder}", (int(trade_id),))
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def get_trade_image_data(trade_id, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"SELECT image_data FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (int(trade_id), username.strip())
            )
        else:
            cursor.execute(f"SELECT image_data FROM trades WHERE id = {placeholder}", (int(trade_id),))
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def get_trade_after_image_path(trade_id, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"SELECT after_image_path FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (int(trade_id), username.strip())
            )
        else:
            cursor.execute(f"SELECT after_image_path FROM trades WHERE id = {placeholder}", (int(trade_id),))
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def get_trade_after_image_data(trade_id, username=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        if username and username != "__ALL__":
            cursor.execute(
                f"SELECT after_image_data FROM trades WHERE id = {placeholder} "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                (int(trade_id), username.strip())
            )
        else:
            cursor.execute(f"SELECT after_image_data FROM trades WHERE id = {placeholder}", (int(trade_id),))
        row = cursor.fetchone()
        return row[0] if row and row[0] else ""
    finally:
        conn.close()


def get_trade_images_bulk(trade_ids, username=None):
    """Fetches base64 chart thumbnails for multiple trade IDs in one query."""
    if not trade_ids:
        return {}
    try:
        clean_ids = [int(tid) for tid in trade_ids]
    except (ValueError, TypeError):
        return {}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        placeholders = ", ".join(placeholder for _ in clean_ids)
        if username and username != "__ALL__":
            params = tuple(clean_ids) + (username.strip(),)
            cursor.execute(
                f"SELECT id, image_data FROM trades WHERE id IN ({placeholders}) "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                params
            )
        else:
            cursor.execute(
                f"SELECT id, image_data FROM trades WHERE id IN ({placeholders})",
                tuple(clean_ids)
            )
        return {int(row[0]): (row[1] or "") for row in cursor.fetchall()}
    finally:
        conn.close()


def get_trade_after_images_bulk(trade_ids, username=None):
    """Fetches base64 AFTER chart thumbnails for multiple trade IDs in one query."""
    if not trade_ids:
        return {}
    try:
        clean_ids = [int(tid) for tid in trade_ids]
    except (ValueError, TypeError):
        return {}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        placeholder = "%s" if is_postgres() else "?"
        placeholders = ", ".join(placeholder for _ in clean_ids)
        if username and username != "__ALL__":
            params = tuple(clean_ids) + (username.strip(),)
            cursor.execute(
                f"SELECT id, after_image_data FROM trades WHERE id IN ({placeholders}) "
                f"AND LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({placeholder})",
                params
            )
        else:
            cursor.execute(
                f"SELECT id, after_image_data FROM trades WHERE id IN ({placeholders})",
                tuple(clean_ids)
            )
        return {int(row[0]): (row[1] or "") for row in cursor.fetchall()}
    finally:
        conn.close()
