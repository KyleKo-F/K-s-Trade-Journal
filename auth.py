import os
import sys
import time
import sqlite3
import hashlib
import secrets
import threading
from contextlib import contextmanager
import customtkinter as ctk
from tkinter import messagebox

import database as db
from theme import THEME, pair_color, resource_path, logger
from widgets import tr, load_language_preference

_PBKDF2_ITERATIONS = 600_000
_LEGACY_ITERATIONS = 100_000
_HASH_PREFIX = f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$"
_ALLOWED_ROLES = ("user", "admin")
_AUTH_SCHEMA_VERSION = "2"

# In-memory failed login throttling per username
_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = threading.Lock()
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60

# Precomputed dummy salt for constant-work verification when user does not exist
_DUMMY_SALT_HEX = "00" * 16
_DUMMY_KEY_HEX = "00" * 32

_GENERATED_ADMIN_PASSWORD = None
_GENERATED_PW_LOCK = threading.Lock()
_user_schema_initialized = False
_user_schema_lock = threading.Lock()


def consume_generated_admin_password():
    """Returns and clears any one-time auto-generated initial admin password."""
    global _GENERATED_ADMIN_PASSWORD
    with _GENERATED_PW_LOCK:
        pw = _GENERATED_ADMIN_PASSWORD
        _GENERATED_ADMIN_PASSWORD = None
        return pw


def get_ph():
    return "%s" if db.is_postgres() else "?"


@contextmanager
def get_db_cursor(commit=False):
    conn = db.get_connection()
    try:
        cursor = conn.cursor()
        yield cursor
        if commit:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def hash_password(password, salt=None, iterations=_PBKDF2_ITERATIONS):
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
    else:
        salt_bytes = bytes.fromhex(salt)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    stored_key = f"pbkdf2_sha256${iterations}${key.hex()}"
    return salt_bytes.hex(), stored_key


def _parse_stored_hash(stored_key):
    if stored_key and stored_key.startswith("pbkdf2_sha256$"):
        parts = stored_key.split("$", 2)
        if len(parts) == 3 and parts[1].isdigit():
            return int(parts[1]), parts[2], False
    return _LEGACY_ITERATIONS, stored_key or "", True


def verify_password(stored_salt, stored_key, password):
    salt_bytes = bytes.fromhex(stored_salt)
    iterations, digest_hex, is_legacy = _parse_stored_hash(stored_key)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
    return secrets.compare_digest(key.hex(), digest_hex), is_legacy


def _is_locked_out(clean_user):
    now = time.monotonic()
    with _LOGIN_LOCK:
        entry = _LOGIN_ATTEMPTS.get(clean_user)
        if not entry:
            return False
        fails, lock_until = entry
        if lock_until > now:
            return True
        if lock_until > 0 and now >= lock_until:
            _LOGIN_ATTEMPTS.pop(clean_user, None)
        return False


def _record_login_failure(clean_user):
    now = time.monotonic()
    with _LOGIN_LOCK:
        if len(_LOGIN_ATTEMPTS) > 1024:
            expired = [k for k, (_, lu) in _LOGIN_ATTEMPTS.items() if lu > 0 and now >= lu]
            for k in expired:
                _LOGIN_ATTEMPTS.pop(k, None)
            while len(_LOGIN_ATTEMPTS) > 1024:
                _LOGIN_ATTEMPTS.pop(next(iter(_LOGIN_ATTEMPTS)), None)
        fails, lock_until = _LOGIN_ATTEMPTS.get(clean_user, (0, 0.0))
        if lock_until > 0 and now >= lock_until:
            fails = 0
        fails += 1
        new_lock = (now + _LOCKOUT_SECONDS) if fails >= _MAX_FAILED_ATTEMPTS else 0.0
        _LOGIN_ATTEMPTS[clean_user] = (fails, new_lock)


def _clear_login_failures(clean_user):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(clean_user, None)


def init_user_db():
    global _GENERATED_ADMIN_PASSWORD, _user_schema_initialized
    if _user_schema_initialized:
        return

    with _user_schema_lock:
        if _user_schema_initialized:
            return
        ph = get_ph()
        with get_db_cursor(commit=True) as cursor:
            if db.get_schema_meta("auth_schema_version", cursor=cursor) == _AUTH_SCHEMA_VERSION:
                _user_schema_initialized = True
                return

            if db.is_postgres():
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT DEFAULT '',
                        salt TEXT NOT NULL,
                        key TEXT NOT NULL,
                        role TEXT DEFAULT 'user',
                        session_token TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    ALTER TABLE users ADD COLUMN IF NOT EXISTS password TEXT DEFAULT '';
                    CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                ''')
            else:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT DEFAULT '',
                        salt TEXT NOT NULL,
                        key TEXT NOT NULL,
                        role TEXT DEFAULT 'user',
                        session_token TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                try:
                    cursor.execute("ALTER TABLE users ADD COLUMN password TEXT DEFAULT '';")
                except Exception:
                    pass
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")

            # Scrub any legacy plaintext passwords stored by older app versions
            cursor.execute("SELECT 1 FROM users WHERE password IS NOT NULL AND password <> '' LIMIT 1;")
            if cursor.fetchone():
                cursor.execute("UPDATE users SET password = '' WHERE password IS NOT NULL AND password <> '';")

            cursor.execute(f"SELECT id FROM users WHERE username = {ph}", ("admin",))
            if not cursor.fetchone():
                env_admin_pw = os.environ.get("ADMIN_INIT_PASSWORD", "").strip()
                if env_admin_pw:
                    init_pw = env_admin_pw
                else:
                    init_pw = secrets.token_urlsafe(12)
                    with _GENERATED_PW_LOCK:
                        _GENERATED_ADMIN_PASSWORD = init_pw
                    logger.warning("Initial 'admin' account created with a generated one-time password; please change it after signing in.")

                salt, key = hash_password(init_pw)
                cursor.execute(
                    f"INSERT INTO users (username, password, salt, key, role) VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                    ("admin", "", salt, key, "admin")
                )

            db.set_schema_meta("auth_schema_version", _AUTH_SCHEMA_VERSION, cursor)
        _user_schema_initialized = True


def authenticate_user(username, password):
    ph = get_ph()
    clean_user = username.strip().lower()
    if _is_locked_out(clean_user):
        logger.warning(f"Blocked login attempt due to temporary lockout for '{clean_user}'.")
        return False, None, None

    with get_db_cursor(commit=True) as cursor:
        cursor.execute(f"SELECT salt, key, role FROM users WHERE username = {ph}", (clean_user,))
        row = cursor.fetchone()

        if not row:
            # Perform dummy PBKDF2 verification to mitigate username timing enumeration
            verify_password(_DUMMY_SALT_HEX, _HASH_PREFIX + _DUMMY_KEY_HEX, password)
            _record_login_failure(clean_user)
            return False, None, None

        is_valid, is_legacy = verify_password(row[0], row[1], password)
        if is_valid:
            _clear_login_failures(clean_user)
            new_token = secrets.token_urlsafe(32)
            if is_legacy:
                new_salt, new_key = hash_password(password)
                cursor.execute(
                    f"UPDATE users SET session_token = {ph}, salt = {ph}, key = {ph}, password = '' WHERE username = {ph}",
                    (new_token, new_salt, new_key, clean_user)
                )
            else:
                cursor.execute(
                    f"UPDATE users SET session_token = {ph} WHERE username = {ph}",
                    (new_token, clean_user)
                )
            return True, row[2], new_token

        _record_login_failure(clean_user)
        return False, None, None


def change_own_password(username, current_password, new_password, lang="English"):
    """Allows any authenticated user to change their own password and rotates their active session token."""
    is_nl = str(lang).strip().lower() == "dutch"
    clean_user = str(username or "").strip().lower()
    if not clean_user:
        return False, ("Geen actieve gebruikerssessie." if is_nl else "No active user session."), None
    if not current_password or not new_password:
        return False, ("Huidig en nieuw wachtwoord zijn verplicht." if is_nl else "Current and new passwords are required."), None
    if len(new_password) < 6:
        return False, ("Nieuw wachtwoord moet minimaal 6 tekens bevatten." if is_nl else "New password must be at least 6 characters."), None
    if _is_locked_out(clean_user):
        return False, ("Te veel mislukte pogingen. Wacht 60 seconden." if is_nl else "Too many failed attempts. Please wait 60 seconds."), None

    ph = get_ph()
    with get_db_cursor(commit=True) as cursor:
        cursor.execute(f"SELECT salt, key FROM users WHERE username = {ph}", (clean_user,))
        row = cursor.fetchone()
        if not row:
            verify_password(_DUMMY_SALT_HEX, _HASH_PREFIX + _DUMMY_KEY_HEX, current_password)
            return False, ("Gebruikersaccount niet gevonden." if is_nl else "User account not found."), None

        is_valid, _ = verify_password(row[0], row[1], current_password)
        if not is_valid:
            _record_login_failure(clean_user)
            return False, ("Huidig wachtwoord is onjuist." if is_nl else "Current password is incorrect."), None

        _clear_login_failures(clean_user)
        new_salt, new_key = hash_password(new_password)
        new_token = secrets.token_urlsafe(32)
        cursor.execute(
            f"UPDATE users SET password = '', salt = {ph}, key = {ph}, session_token = {ph} WHERE username = {ph}",
            (new_salt, new_key, new_token, clean_user)
        )
        return True, ("Wachtwoord succesvol bijgewerkt." if is_nl else "Password updated successfully."), new_token


def resume_saved_session(username, saved_token):
    """Validate stored session token and rotate it on startup."""
    if not username or not saved_token:
        return False, None, None
    ph = get_ph()
    clean_user = username.strip().lower()
    try:
        with get_db_cursor(commit=True) as cursor:
            cursor.execute(f"SELECT role, session_token FROM users WHERE username = {ph}", (clean_user,))
            row = cursor.fetchone()
            if row and row[1] and secrets.compare_digest(row[1], saved_token):
                new_token = secrets.token_urlsafe(32)
                cursor.execute(f"UPDATE users SET session_token = {ph} WHERE username = {ph}", (new_token, clean_user))
                return True, row[0], new_token
    except Exception as e:
        logger.warning(f"Session resume validation failed: {e}")
    return False, None, None


def validate_active_session_token(username, active_token):
    if not username or not active_token:
        return False
    ph = get_ph()
    with get_db_cursor(commit=False) as cursor:
        cursor.execute(f"SELECT session_token FROM users WHERE username = {ph}", (username.strip().lower(),))
        row = cursor.fetchone()
        if row and row[0]:
            return secrets.compare_digest(row[0], active_token)
        return False


def clear_user_session_token(username):
    if not username:
        return
    ph = get_ph()
    try:
        with get_db_cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE users SET session_token = '' WHERE username = {ph}", (username.strip().lower(),))
    except Exception as e:
        logger.warning(f"Failed clearing session token for {username}: {e}")


def add_new_user(username, password, role="user", lang="English"):
    is_nl = str(lang).strip().lower() == "dutch"
    username_clean = str(username or "").strip().lower()
    if not username_clean or not password:
        return False, ("Gebruikersnaam en wachtwoord mogen niet leeg zijn." if is_nl else "Username and password cannot be empty.")
    if username_clean == "__all__":
        return False, ("Deze gebruikersnaam is gereserveerd." if is_nl else "This username is reserved.")
    if len(password) < 6:
        return False, ("Wachtwoord moet minimaal 6 tekens bevatten." if is_nl else "Password must be at least 6 characters.")
    if role not in _ALLOWED_ROLES:
        return False, ("Ongeldige rol opgegeven." if is_nl else "Invalid role specified.")

    ph = get_ph()
    try:
        with get_db_cursor(commit=True) as cursor:
            salt, key = hash_password(password)
            cursor.execute(
                f"INSERT INTO users (username, password, salt, key, role) VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                (username_clean, "", salt, key, role)
            )
            return True, ("Gebruiker succesvol aangemaakt!" if is_nl else "User created successfully!")
    except Exception as e:
        logger.warning(f"Error creating user '{username_clean}': {e}")
        err_lower = str(e).lower()
        is_unique = (
            isinstance(e, sqlite3.IntegrityError)
            or getattr(e, "pgcode", None) == "23505"
            or "unique" in err_lower
            or "duplicate" in err_lower
        )
        if is_unique:
            return False, (
                f"Fout: Gebruiker '{username_clean}' bestaat al."
                if is_nl
                else f"Error: User '{username_clean}' already exists."
            )
        return False, (f"Fout bij aanmaken gebruiker: {e}" if is_nl else f"Error creating user: {e}")


def get_all_users():
    with get_db_cursor(commit=False) as cursor:
        cursor.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
        return cursor.fetchall()


def delete_user(username, lang="English"):
    is_nl = str(lang).strip().lower() == "dutch"
    clean_user = str(username or "").strip().lower()
    if clean_user == "admin":
        return False, ("Kan het primaire admin-account niet verwijderen." if is_nl else "Cannot delete primary admin account.")
    ph = get_ph()
    with get_db_cursor(commit=True) as cursor:
        cursor.execute(f"SELECT role FROM users WHERE username = {ph}", (clean_user,))
        row = cursor.fetchone()
        if row and row[0] == "admin":
            cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
            admin_count = cursor.fetchone()[0]
            if admin_count <= 1:
                return False, ("Kan het laatste overgebleven admin-account niet verwijderen." if is_nl else "Cannot delete the last remaining admin account.")
        cursor.execute(f"DELETE FROM users WHERE username = {ph}", (clean_user,))
        return True, ("Gebruiker succesvol verwijderd." if is_nl else "User removed successfully.")


def update_user_details(user_id, new_username, new_password, new_role, lang="English"):
    is_nl = str(lang).strip().lower() == "dutch"
    username_clean = str(new_username or "").strip().lower()
    if not username_clean:
        return False, ("Gebruikersnaam mag niet leeg zijn." if is_nl else "Username cannot be empty.")
    if username_clean == "__all__":
        return False, ("Deze gebruikersnaam is gereserveerd." if is_nl else "This username is reserved.")
    if new_role not in _ALLOWED_ROLES:
        return False, ("Ongeldige rol opgegeven." if is_nl else "Invalid role specified.")
    clean_new_pw = str(new_password or "").strip()
    if clean_new_pw and len(clean_new_pw) < 6:
        return False, ("Wachtwoord moet minimaal 6 tekens bevatten." if is_nl else "Password must be at least 6 characters.")

    ph = get_ph()
    try:
        with get_db_cursor(commit=True) as cursor:
            cursor.execute(f"SELECT username, role FROM users WHERE id = {ph}", (user_id,))
            current_row = cursor.fetchone()
            if not current_row:
                return False, ("Gebruikersaccount niet gevonden." if is_nl else "User account not found.")

            old_username = str(current_row[0] or "").strip().lower()
            old_role = str(current_row[1] or "user").strip()

            if old_role == "admin" and new_role != "admin":
                cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
                if cursor.fetchone()[0] <= 1:
                    return False, ("Kan het laatste overgebleven admin-account niet degraderen." if is_nl else "Cannot demote the last remaining admin account.")

            username_changed = (old_username != username_clean)
            role_changed = (old_role != new_role)

            if clean_new_pw:
                salt, key = hash_password(clean_new_pw)
                cursor.execute(
                    f"UPDATE users SET username = {ph}, password = '', salt = {ph}, key = {ph}, role = {ph}, session_token = '' WHERE id = {ph}",
                    (username_clean, salt, key, new_role, user_id)
                )
            elif username_changed or role_changed:
                cursor.execute(
                    f"UPDATE users SET username = {ph}, role = {ph}, session_token = '' WHERE id = {ph}",
                    (username_clean, new_role, user_id)
                )
            else:
                cursor.execute(
                    f"UPDATE users SET username = {ph}, role = {ph} WHERE id = {ph}",
                    (username_clean, new_role, user_id)
                )

            if username_changed and old_username:
                cursor.execute(
                    f"UPDATE trades SET username = {ph} WHERE LOWER(COALESCE(NULLIF(username, ''), 'admin')) = LOWER({ph})",
                    (username_clean, old_username)
                )

            return True, ("Gebruikersgegevens succesvol bijgewerkt." if is_nl else "User details updated successfully.")
    except Exception as e:
        logger.warning(f"Error updating user {user_id}: {e}")
        err_lower = str(e).lower()
        is_unique = (
            isinstance(e, sqlite3.IntegrityError)
            or getattr(e, "pgcode", None) == "23505"
            or "unique" in err_lower
            or "duplicate" in err_lower
        )
        if is_unique:
            return False, (
                f"Fout: Gebruiker '{username_clean}' bestaat al."
                if is_nl
                else f"Error: User '{username_clean}' already exists."
            )
        return False, (f"Fout bij bijwerken gebruiker: {e}" if is_nl else f"Error updating user: {e}")


def update_user_password(username, new_password, lang="English"):
    is_nl = str(lang).strip().lower() == "dutch"
    if not new_password:
        return False, ("Wachtwoord mag niet leeg zijn." if is_nl else "Password cannot be empty.")
    if len(new_password) < 6:
        return False, ("Wachtwoord moet minimaal 6 tekens bevatten." if is_nl else "Password must be at least 6 characters.")
    ph = get_ph()
    with get_db_cursor(commit=True) as cursor:
        salt, key = hash_password(new_password)
        cursor.execute(
            f"UPDATE users SET password = '', salt = {ph}, key = {ph}, session_token = '' WHERE username = {ph}",
            (salt, key, username.strip().lower())
        )
        return True, ("Wachtwoord succesvol bijgewerkt." if is_nl else "Password updated successfully.")


class LoginWindow(ctk.CTkToplevel):
    def __init__(self, parent, on_login_success):
        super().__init__(parent)
        self.parent = parent
        self.on_login_success = on_login_success
        self._logging_in = False
        self.lang = getattr(parent, "app_language", None) or load_language_preference()

        self.title(tr(self.lang, "login_title"))
        self.geometry("430x480")
        self.resizable(False, False)
        self.configure(fg_color=pair_color("bg_root"))

        icon_file = resource_path("icon.ico")
        if os.path.exists(icon_file):
            self.iconbitmap(icon_file)

        self.transient(parent)
        self.grab_set()

        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width() // 2) - 215
        py = parent.winfo_y() + (parent.winfo_height() // 2) - 240
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

        self._admin_notice_retries = 0
        self.build_ui()
        self.after(200, self._check_initial_admin_notice)

    def _check_initial_admin_notice(self):
        if not self.winfo_exists():
            return
        db_evt = getattr(self.parent, "_db_ready_event", None)
        if db_evt is not None and not db_evt.is_set():
            if self._admin_notice_retries < 100:
                self._admin_notice_retries += 1
                self.after(150, self._check_initial_admin_notice)
            return
        init_pw = consume_generated_admin_password()
        if init_pw:
            if not self.entry_user.get().strip():
                self.entry_user.insert(0, "admin")
            messagebox.showinfo(
                tr(self.lang, "login_admin_notice_title"),
                tr(self.lang, "login_admin_notice_msg", pw=init_pw),
                parent=self
            )

    def build_ui(self):
        container = ctk.CTkFrame(
            self,
            fg_color=pair_color("bg_card"),
            corner_radius=14,
            border_width=1,
            border_color=pair_color("border_strong")
        )
        container.pack(fill="both", expand=True, padx=22, pady=22)

        top_bar = ctk.CTkFrame(container, fg_color=pair_color("accent"), height=4, corner_radius=2)
        top_bar.pack(fill="x", padx=28, pady=(20, 12))

        lbl_title = ctk.CTkLabel(
            container,
            text="K's Trade Journal",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=pair_color("accent")
        )
        lbl_title.pack(pady=(4, 2))

        lbl_sub = ctk.CTkLabel(
            container,
            text=tr(self.lang, "login_subtitle"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary")
        )
        lbl_sub.pack(pady=(0, 22))

        self.entry_user = ctk.CTkEntry(
            container,
            placeholder_text=tr(self.lang, "login_username"),
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=40,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.entry_user.pack(fill="x", padx=26, pady=7)

        self.entry_pass = ctk.CTkEntry(
            container,
            placeholder_text=tr(self.lang, "login_password"),
            show="•",
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=40,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.entry_pass.pack(fill="x", padx=26, pady=7)

        self.remember_var = ctk.BooleanVar(value=True)
        self.chk_remember = ctk.CTkCheckBox(
            container,
            text=tr(self.lang, "login_remember"),
            variable=self.remember_var,
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            checkmark_color=pair_color("bg_root")
        )
        self.chk_remember.pack(anchor="w", padx=26, pady=10)

        self.lbl_error = ctk.CTkLabel(container, text="", font=ctk.CTkFont(size=12), text_color=pair_color("danger_text"))
        self.lbl_error.pack(pady=2)

        btn_login = ctk.CTkButton(
            container,
            text=tr(self.lang, "login_btn"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=pair_color("bg_root"),
            font=ctk.CTkFont(size=14, weight="bold"),
            height=40,
            corner_radius=8,
            command=self.attempt_login
        )
        btn_login.pack(fill="x", padx=26, pady=(10, 20))

        self.entry_pass.bind("<Return>", lambda e: self.attempt_login())
        self.entry_user.bind("<Return>", lambda e: self.attempt_login())
        self.entry_user.focus_set()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def attempt_login(self):
        if self._logging_in:
            return
        username = self.entry_user.get().strip().lower()
        password = self.entry_pass.get()
        remember = self.remember_var.get()

        if not username or not password:
            self.lbl_error.configure(text=tr(self.lang, "login_err_empty"))
            return

        if _is_locked_out(username):
            self.lbl_error.configure(text=tr(self.lang, "login_err_lockout"))
            return

        self._logging_in = True
        self.lbl_error.configure(text="")

        def worker():
            try:
                db_evt = getattr(self.parent, "_db_ready_event", None)
                if db_evt is not None:
                    db_evt.wait(timeout=15)
                success, role, session_token = authenticate_user(username, password)
            except Exception as e:
                logger.warning(f"Login authentication error: {e}")
                success, role, session_token = False, None, None

            def finish():
                self._logging_in = False
                if not self.winfo_exists():
                    return
                if success:
                    self.grab_release()
                    self.destroy()
                    self.on_login_success(username, role, remember, session_token=session_token)
                elif _is_locked_out(username):
                    self.lbl_error.configure(text=tr(self.lang, "login_err_lockout_retry"))
                else:
                    self.lbl_error.configure(text=tr(self.lang, "login_err_invalid"))

            if self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self.parent and self.parent.winfo_exists():
            if hasattr(self.parent, "on_closing"):
                self.parent.on_closing()
            else:
                self.parent.destroy()
