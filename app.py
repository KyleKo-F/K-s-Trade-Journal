import os
import io
import sys
import csv
import shutil
import tempfile
import threading
import time
import re
import ctypes
import json
import secrets
from datetime import datetime
from collections import defaultdict
import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from PIL import Image, ImageDraw, ImageTk
import requests
from requests.adapters import HTTPAdapter

import database as db
import tracker
import news as news_svc
import calculator as calc_svc
import updater as updater_svc
from views import AppViewsMixin
from theme import (
    THEME,
    DARK_THEME,
    LIGHT_THEME,
    pair_color,
    resource_path,
    load_theme_preference,
    save_theme_preference,
    get_app_tzinfo,
    get_app_tz_info,
    get_app_timezone_pref,
    set_app_timezone,
    detect_system_timezone,
    get_timezone_dropdown_options,
    utc_timestamp_to_local_short_date,
    format_db_date_for_edit,
    parse_edit_date_to_utc,
    logger,
)
from auth import (
    init_user_db,
    resume_saved_session,
    validate_active_session_token,
    clear_user_session_token,
    change_own_password,
    add_new_user,
    get_all_users,
    delete_user,
    update_user_details,
    LoginWindow,
)
from widgets import (
    TRADE_TYPE_CHECKLISTS,
    SUPPORTED_LANGUAGES,
    tr,
    load_language_preference,
    save_language_preference,
    TradeRecord,
    CustomDropdownButton,
    encode_chart_thumbnail_b64,
    decode_chart_thumbnail_b64,
    clean_pair,
    clean_direction,
    clean_status,
    format_timeframe,
    compute_rr_string,
    strip_markdown_links,
    grab_clipboard_pil_image,
    browse_pil_image,
    compute_dashboard_kpis,
    trade_to_csv_row,
)

CURRENT_VERSION = updater_svc.CURRENT_VERSION

# Thread-local Pooled HTTP Sessions so concurrent background threads stay isolated
_http_thread_local = threading.local()


def _get_http_session():
    sess = getattr(_http_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=6, pool_maxsize=12)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        _http_thread_local.session = sess
    return sess


http_session = _get_http_session()

_analyzer_module = None


def _get_analyzer():
    """Lazy-loads analyzer (and google.genai SDK) on first AI request so startup is instantaneous."""
    global _analyzer_module
    if _analyzer_module is None:
        import analyzer as _az
        _analyzer_module = _az
    return _analyzer_module


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"ks.tradejournal.app.{CURRENT_VERSION}")
except Exception:
    pass


class TradingJournalApp(AppViewsMixin, ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"K's Trade Journal v{CURRENT_VERSION}")

        self._db_ready_event = threading.Event()
        threading.Thread(target=self._init_databases_async, daemon=True).start()

        self._apply_window_icon(self)

        # Adaptive DPI scaling & screen-aware window bounds
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        scale = 1.10 if screen_w >= 1600 else (1.0 if screen_w >= 1366 else 0.92)
        ctk.set_widget_scaling(scale)

        target_w = min(1620, max(1220, int(screen_w * 0.90)))
        target_h = min(920, max(700, int(screen_h * 0.88)))
        min_w = min(1480, max(1160, int(screen_w * 0.82)))
        min_h = min(800, max(660, int(screen_h * 0.78)))
        self.geometry(f"{target_w}x{target_h}")
        self.minsize(min_w, min_h)

        self.theme_mode = load_theme_preference()
        THEME.clear()
        THEME.update(LIGHT_THEME if self.theme_mode == "Light" else DARK_THEME)
        ctk.set_appearance_mode(self.theme_mode)
        self.configure(fg_color=pair_color("bg_root"))

        self.app_language = load_language_preference()
        self.app_timezone = get_app_timezone_pref()

        self.current_user = None
        self.current_role = "user"
        self.active_session_token = None
        self._tracker_stop_event = threading.Event()
        self._tracker_thread = None

        self.saved_images_dir = os.path.join(os.path.expanduser("~"), ".trade_journal_images")
        os.makedirs(self.saved_images_dir, mode=0o700, exist_ok=True)

        self.selected_image_path = None
        self.current_preview_image = None
        self.calc_preview_image = None
        self.nav_buttons = {}
        self.nav_indicators = {}
        self.current_active_view = None
        self.active_dropdown_window = None
        self._settings_pw_submitting = False
        self._selected_cal_day_date = None
        self._admin_selected_user_id = None
        self._admin_selected_username = None
        self._trades_sort_col = None
        self._trades_sort_reverse = False
        self.favorite_pairs = set()
        self._is_closing = False
        self._update_check_state = {
            "status": "idle",
            "message": self._t("upd_idle"),
            "is_ok": True,
            "latest_tag": "",
            "download_url": "",
            "checksum_url": "",
            "expected_sha256": "",
            "release_notes": "",
            "downloading": False,
            "progress": None,
        }
        self._preview_return_view = "Dashboard"
        self._preview_request_seq = 0
        self._preview_nav_list = []
        self._preview_nav_idx = -1
        self._preview_display_num = None
        self._preview_resize_after_id = None
        self._preview_state = {
            "trade_id": None,
            "scope_user": None,
            "status": "OPEN",
            "before": None,
            "after": None,
            "tk_photo": None,
            "canvas_img_id": None,
            "zoom": "fit",
            "tab": "before",
            "loading": False,
            "pan_x": 0.5,
            "pan_y": 0.5,
            "img_w": 0,
            "img_h": 0,
            "canvas_w": 0,
            "canvas_h": 0,
            "_last_canvas_size": None,
            "_pan_last": None,
        }
        self._edit_trade_return_view = "Dashboard"
        self._edit_after_preview_ctk = None
        self._edit_after_load_seq = 0
        self._edit_trade_state = {
            "trade_id": None,
            "display_num": None,
            "scope_user": None,
            "existing_after_path": "",
            "existing_rr": "",
            "pil_after": None,
            "clear_after": False,
            "saving": False,
        }
        self.cached_news_events = []
        self._news_fetch_seq = 0
        self._last_news_fetch_ts = 0.0
        self._last_news_fetch_range = None
        self._last_news_widget_sig = None
        self._last_no_news_widget_sig = None
        self._last_sessions_sig = None
        self._sessions_timer_after_id = None
        self._last_show_user_col = None
        self._db_load_lock = threading.Lock()
        self._db_load_in_flight = False
        self._db_reload_queued = False
        self._chart_analyze_seq = 0
        self._calc_extract_seq = 0
        self._trades_filter_after_id = None
        self._records_version = 0
        self._last_cal_render_sig = None
        self._last_analytics_version = None
        self.all_db_records = []
        self.trades_scope_records = None
        self._trades_scope_load_seq = 0
        self._fx_quote_usd_rates = {}
        self._fx_rate_inflight = set()
        self._built_views = set()
        self._cal_day_tiles = []
        self.checklist_item_states = {}
        self.checklist_scores = {}

        now_app = datetime.now(get_app_tzinfo())
        self.current_year = now_app.year
        self.current_month = now_app.month

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Impact badge icons for Economic News table
        self.impact_icons = {
            "high": self._create_circle_icon("#ff3b30"),
            "medium": self._create_circle_icon("#ff9500"),
            "low": self._create_circle_icon("#ffd60a"),
            "none": self._create_circle_icon("#4d5766"),
            "passed": self._create_circle_icon("#2d3a4f"),
        }

        saved_session = self.get_saved_session()

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.setup_table_styles()
        self.build_sidebar()

        self.main_container = ctk.CTkFrame(self, fg_color=pair_color("bg_root"), corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew", padx=14, pady=14)
        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)

        self.content_area = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.content_area.grid(row=0, column=0, sticky="nsew")
        self.content_area.grid_rowconfigure(0, weight=1)
        self.content_area.grid_columnconfigure(0, weight=1)

        self.view_dashboard = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_calendar = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_news = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_trades = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_calculator = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_analytics = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_checklist = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_settings = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_admin_users = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_calendar_day = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_trade_preview = ctk.CTkFrame(self.content_area, fg_color="transparent")
        self.view_edit_trade = ctk.CTkFrame(self.content_area, fg_color="transparent")

        self._views_map = {
            "Dashboard": self.view_dashboard,
            "Calendar": self.view_calendar,
            "News": self.view_news,
            "Trades": self.view_trades,
            "Calculator": self.view_calculator,
            "Analytics": self.view_analytics,
            "Checklist": self.view_checklist,
            "Settings": self.view_settings,
            "AdminUsers": self.view_admin_users,
            "CalendarDay": self.view_calendar_day,
            "TradePreview": self.view_trade_preview,
            "EditTrade": self.view_edit_trade,
        }

        # Build primary Dashboard immediately; build secondary views lazily on first click
        self.build_dashboard_view()
        self._built_views.add("Dashboard")
        self._schedule_sessions_clock()

        self._rebuild_context_menus()

        self.bind("<Button-1>", self.on_global_click, add="+")
        self.bind("<Control-v>", self.on_ctrl_v_paste)
        self.bind("<Control-V>", self.on_ctrl_v_paste)

        if saved_session and "username" in saved_session and "session_token" in saved_session:
            self.withdraw()
            threading.Thread(
                target=self._resume_session_async,
                args=(saved_session["username"], saved_session["session_token"]),
                daemon=True
            ).start()
        else:
            self.clear_session()
            self.withdraw()
            LoginWindow(self, self.on_login_success)

    def _t(self, key, /, **kwargs):
        return tr(getattr(self, "app_language", "English"), key, **kwargs)

    def _rebuild_context_menus(self):
        for attr in ("dash_context_menu", "trades_context_menu"):
            old = getattr(self, attr, None)
            if old is not None:
                try:
                    old.destroy()
                except Exception:
                    pass
        self.dash_context_menu = tk.Menu(
            self,
            tearoff=0,
            background=THEME["bg_card_alt"],
            foreground=THEME["text_primary"],
            activebackground=THEME["bg_hover"],
            activeforeground=THEME["accent"],
            borderwidth=1,
            font=("Segoe UI", 10)
        )
        self.dash_context_menu.add_command(label=self._t("ctx_modify_trade"), command=self.modify_selected_trade)

        self.trades_context_menu = tk.Menu(
            self,
            tearoff=0,
            background=THEME["bg_card_alt"],
            foreground=THEME["text_primary"],
            activebackground=THEME["bg_hover"],
            activeforeground=THEME["accent"],
            borderwidth=1,
            font=("Segoe UI", 10)
        )
        self.trades_context_menu.add_command(label=self._t("ctx_modify_trade"), command=self.modify_selected_trade)
        self.trades_context_menu.add_command(label=self._t("ctx_delete_trades"), command=self.delete_selected)

    def _apply_window_icon(self, win):
        icon_file = resource_path("icon.ico")
        if os.path.exists(icon_file):
            try:
                win.iconbitmap(icon_file)
                win.after(200, lambda: win.iconbitmap(icon_file) if win.winfo_exists() else None)
            except Exception:
                pass

    def _cleanup_temp_image(self, path):
        if not path:
            return
        try:
            real_tmp = os.path.realpath(tempfile.gettempdir())
            real_path = os.path.realpath(path)
            if os.path.commonpath([real_tmp, real_path]) == real_tmp and os.path.isfile(real_path):
                os.remove(real_path)
        except Exception as e:
            logger.warning(f"Temp image cleanup failed: {e}")

    def _init_databases_async(self):
        try:
            db.init_db()
            init_user_db()
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
        finally:
            self._db_ready_event.set()

    def _resume_session_async(self, username, session_token):
        self._db_ready_event.wait(timeout=15)
        valid, role, new_token = resume_saved_session(username, session_token)

        def finish():
            if getattr(self, "_is_closing", False) or not self.winfo_exists():
                return
            if valid:
                self.on_login_success(username, role, remember=True, session_token=new_token)
            else:
                self.clear_session()
                LoginWindow(self, self.on_login_success)

        if not getattr(self, "_is_closing", False) and self.winfo_exists():
            self.after(0, finish)

    def _ensure_view_built(self, view_name):
        if view_name in self._built_views:
            return
        if view_name == "Calendar":
            self.build_calendar_view()
        elif view_name == "News":
            self.build_news_view()
        elif view_name == "Trades":
            self.build_trades_view()
        elif view_name == "Calculator":
            self.build_calculator_view()
        elif view_name == "Analytics":
            self.build_analytics_view()
        elif view_name == "Checklist":
            self.build_checklist_view()
        elif view_name == "Settings":
            self.build_settings_view()
        elif view_name == "AdminUsers":
            self.build_admin_users_view()
        elif view_name == "CalendarDay":
            self.build_calendar_day_view()
        elif view_name == "TradePreview":
            self.build_trade_preview_view()
        elif view_name == "EditTrade":
            self.build_edit_trade_view()
        self._built_views.add(view_name)

    def show_view(self, view_name):
        if self.current_active_view == view_name:
            return

        self.close_dropdown()
        self._ensure_view_built(view_name)

        for key, btn in self.nav_buttons.items():
            indicator = self.nav_indicators[key]
            if key == view_name or (key == "Calendar" and view_name == "CalendarDay"):
                btn.configure(
                    fg_color=pair_color("bg_active_nav"),
                    text_color=pair_color("text_primary"),
                    font=self._nav_font_active
                )
                indicator.configure(fg_color=pair_color("accent"))
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=pair_color("text_secondary"),
                    font=self._nav_font_normal
                )
                indicator.configure(fg_color="transparent")

        if hasattr(self, "btn_settings") and self.btn_settings.winfo_exists():
            if view_name == "Settings":
                self.btn_settings.configure(
                    fg_color=pair_color("bg_active_nav"),
                    border_color=pair_color("accent"),
                    text_color=pair_color("accent"),
                )
            else:
                self.btn_settings.configure(
                    fg_color=pair_color("bg_card_alt"),
                    border_color=pair_color("border_strong"),
                    text_color=pair_color("text_primary"),
                )

        if hasattr(self, "btn_admin_users") and self.btn_admin_users.winfo_exists():
            if view_name == "AdminUsers":
                self.btn_admin_users.configure(
                    fg_color=pair_color("bg_active_nav"),
                    border_color=pair_color("accent"),
                    text_color=pair_color("accent"),
                )
            else:
                self.btn_admin_users.configure(
                    fg_color=pair_color("bg_card_alt"),
                    border_color=pair_color("border_strong"),
                    text_color=pair_color("accent"),
                )

        if self.current_active_view and self.current_active_view in self._views_map:
            self._views_map[self.current_active_view].grid_remove()

        target_frame = self._views_map.get(view_name)
        if target_frame:
            target_frame.grid(row=0, column=0, sticky="nsew")

        if view_name == "Calendar":
            self.render_calendar()
        elif view_name == "News":
            self.fetch_and_render_news(force=False)
        elif view_name == "Trades":
            self._refresh_admin_user_scope_options()
            if self.current_role == "admin" and getattr(self, "trades_scope_filter", "My Trades") != "My Trades":
                self.load_trades_scope_records()
            else:
                self.apply_trades_filter()
        elif view_name == "Analytics":
            self.populate_analytics_dropdown()
        elif view_name == "Settings":
            self._refresh_settings_view_state()
        elif view_name == "AdminUsers":
            self.admin_refresh_users()
        elif view_name == "CalendarDay" and self._selected_cal_day_date:
            self._populate_calendar_day_table(self._selected_cal_day_date)

        self.current_active_view = view_name

    def on_ctrl_v_paste(self, event=None):
        focused = self.focus_get()
        if isinstance(focused, (tk.Entry, tk.Text)):
            return
        if self.current_active_view == "Dashboard":
            self.paste_image()
        elif self.current_active_view == "Analytics":
            self.run_ai_pasted_chart_review()
        elif self.current_active_view == "Calculator":
            self.paste_calculator_screenshot()
        elif self.current_active_view == "TradePreview":
            self.preview_attach_after("paste")
        elif self.current_active_view == "EditTrade":
            self.edit_trade_pick_after("paste")

    # ------------------ THEME SWITCHING MANAGEMENT ------------------ #
    def toggle_theme_mode(self):
        self.close_dropdown()
        new_mode = "Light" if self.theme_mode == "Dark" else "Dark"
        self.theme_mode = new_mode
        save_theme_preference(new_mode)

        THEME.clear()
        THEME.update(LIGHT_THEME if new_mode == "Light" else DARK_THEME)

        ctk.set_appearance_mode(new_mode)
        self.setup_table_styles()

        CustomDropdownButton.refresh_all_themes()

        if hasattr(self, "btn_theme_toggle") and self.btn_theme_toggle is not None:
            try:
                if self.btn_theme_toggle.winfo_exists():
                    self.btn_theme_toggle.configure(
                        text=self._t("btn_switch_light") if new_mode == "Dark" else self._t("btn_switch_dark")
                    )
            except Exception:
                pass

        if hasattr(self, "lbl_settings_theme_status") and self.lbl_settings_theme_status is not None:
            try:
                if self.lbl_settings_theme_status.winfo_exists():
                    self.lbl_settings_theme_status.configure(
                        text=self._t("settings_theme_dark") if new_mode == "Dark" else self._t("settings_theme_light")
                    )
            except Exception:
                pass

        for tree_attr in ("tree_dash", "tree_trades", "tree_cal_day"):
            if hasattr(self, tree_attr):
                tree_w = getattr(self, tree_attr)
                if tree_w is not None and tree_w.winfo_exists():
                    self._configure_trade_tree_tags(tree_w)

        if "TradePreview" in self._built_views:
            if hasattr(self, "preview_canvas") and self.preview_canvas.winfo_exists():
                self.preview_canvas.configure(bg=THEME["bg_input"])
            self._render_preview_outcome_buttons()
            self._render_preview_tab()

        if "News" in self._built_views and hasattr(self, "tree_news") and self.tree_news.winfo_exists():
            self.tree_news.tag_configure("evenrow", background=THEME["row_even"], foreground=THEME["text_primary"])
            self.tree_news.tag_configure("oddrow", background=THEME["row_odd"], foreground=THEME["text_primary"])
            self.tree_news.tag_configure("passed_evenrow", background=THEME["passed_even"], foreground=THEME["text_muted"])
            self.tree_news.tag_configure("passed_oddrow", background=THEME["passed_odd"], foreground=THEME["text_muted"])

        for menu_attr in ("dash_context_menu", "trades_context_menu"):
            if hasattr(self, menu_attr):
                getattr(self, menu_attr).configure(
                    background=THEME["bg_card_alt"],
                    foreground=THEME["text_primary"],
                    activebackground=THEME["bg_hover"],
                    activeforeground=THEME["accent"]
                )

        if "Analytics" in self._built_views and hasattr(self, "txt_analytics_log") and self.txt_analytics_log.winfo_exists():
            inner_txt = self.txt_analytics_log._textbox
            inner_txt.tag_configure("heading", foreground=THEME["accent"])
            inner_txt.tag_configure("bold", foreground=THEME["cyan"])
            inner_txt.tag_configure("rating", foreground=THEME["amber"])
            inner_txt.tag_configure("body", foreground=THEME["text_primary"])

        if self.current_active_view == "Calendar":
            self._last_cal_render_sig = None
            self.render_calendar()

    # ------------------ LANGUAGE SWITCHING MANAGEMENT ------------------ #
    def change_app_language(self, new_lang):
        if new_lang not in SUPPORTED_LANGUAGES or new_lang == self.app_language:
            return

        self.close_dropdown()

        if "Calculator" in self._built_views:
            self.save_calc_settings()

        dash_snap = None
        if "Dashboard" in self._built_views and hasattr(self, "entry_pair"):
            dash_snap = {
                "pair": self.entry_pair.get(),
                "tf": self.entry_tf.get(),
                "direction": self.entry_direction.get(),
                "entry": self.entry_entry.get(),
                "sl": self.entry_sl.get(),
                "tp": self.entry_tp.get(),
                "rr": self.entry_rr.get(),
                "notes": self.entry_notes.get(),
                "status": self.opt_status.get(),
                "type": self.opt_type.get(),
            }

        self.app_language = new_lang
        save_language_preference(new_lang)

        st = self._update_check_state
        status_code = st.get("status", "idle")
        if status_code == "idle":
            st["message"] = self._t("upd_idle")
        elif status_code == "checking":
            st["message"] = self._t("upd_checking")
        elif status_code == "update_available":
            st["message"] = self._t("upd_available", tag=st.get("latest_tag", ""))
        elif status_code == "up_to_date":
            st["message"] = self._t("upd_up_to_date", ver=CURRENT_VERSION)

        self._rebuild_context_menus()

        for after_attr in ("_trades_filter_after_id", "_preview_resize_after_id", "_sessions_timer_after_id"):
            aid = getattr(self, after_attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, after_attr, None)

        if hasattr(self, "sidebar_frame") and self.sidebar_frame.winfo_exists():
            self.sidebar_frame.destroy()
        self.nav_buttons = {}
        self.nav_indicators = {}
        self.build_sidebar()

        if self.current_user:
            role_badge = self._t("role_admin") if self.current_role == "admin" else self._t("role_pro")
            self.lbl_user_profile.configure(text=f"●  {self.current_user.upper()}  •  {role_badge}")
            self.btn_admin_users.pack_forget()
            self.btn_settings.pack_forget()
            self.btn_logout.pack_forget()
            self.lbl_ver.pack_forget()
            if self.current_role == "admin":
                self.btn_admin_users.pack(fill="x", padx=12, pady=(0, 6))
            self.btn_settings.pack(fill="x", padx=12, pady=(0, 6))
            self.btn_logout.pack(fill="x", padx=12, pady=(0, 8))
            self.lbl_ver.pack(fill="x", padx=14, pady=(0, 12))

        for vframe in self._views_map.values():
            try:
                vframe.grid_remove()
            except Exception:
                pass
            for child in list(vframe.winfo_children()):
                child.destroy()

        self._last_news_widget_sig = None
        self._last_no_news_widget_sig = None
        self._last_sessions_sig = None
        self._last_cal_render_sig = None
        self._last_analytics_version = None
        self._last_show_user_col = None
        self._cal_day_tiles = []

        self._built_views = set()
        self.build_dashboard_view()
        self._built_views.add("Dashboard")
        self._schedule_sessions_clock()

        if dash_snap is not None:
            field_pairs = [
                (self.entry_pair, dash_snap["pair"]),
                (self.entry_tf, dash_snap["tf"]),
                (self.entry_direction, dash_snap["direction"]),
                (self.entry_entry, dash_snap["entry"]),
                (self.entry_sl, dash_snap["sl"]),
                (self.entry_tp, dash_snap["tp"]),
                (self.entry_rr, dash_snap["rr"]),
                (self.entry_notes, dash_snap["notes"]),
            ]
            for widget, val in field_pairs:
                if val:
                    widget.insert(0, val)
            if dash_snap["status"]:
                self.opt_status.set(dash_snap["status"])
            if dash_snap["type"]:
                self.opt_type.set(dash_snap["type"])
            if self.current_preview_image is not None and self.selected_image_path and os.path.exists(self.selected_image_path):
                self.img_preview.configure(image=self.current_preview_image, text="")
            self._refresh_analyzer_checklist_badge()

        self._apply_loaded_records(self.all_db_records)
        if getattr(self, "cached_news_events", None):
            upcoming, today_events = news_svc.compute_upcoming_and_today(self.cached_news_events)
            self.update_dashboard_news_widget(upcoming)
            self.update_dashboard_no_news_widget(today_events)

        self.build_settings_view()
        self._built_views.add("Settings")
        self._refresh_settings_view_state(clear_password_fields=False)

        self.current_active_view = None
        self.show_view("Settings")

    # ------------------ SESSION & LOGOUT MANAGEMENT ------------------ #
    def get_session_file_path(self):
        return os.path.join(os.path.expanduser("~"), ".trade_journal_session.json")

    def get_saved_session(self):
        path = self.get_session_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "username" in data and "role" in data:
                    return data
            except Exception as e:
                logger.warning(f"Failed reading saved session: {e}")
        return None

    def save_session(self, username, role, session_token=""):
        try:
            path = self.get_session_file_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"username": username, "role": role, "session_token": session_token}, f)
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Error saving session: {e}")

    def clear_session(self):
        path = self.get_session_file_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"Error clearing session file: {e}")

    def _get_favorites_file_path(self, username=None):
        raw_user = username or self.current_user or "default"
        safe_user = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(raw_user))
        return os.path.join(os.path.expanduser("~"), f".trade_journal_favorites_{safe_user}.json")

    def _load_favorite_pairs(self):
        path = self._get_favorites_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.favorite_pairs = {str(x) for x in data if x}
                    return
            except Exception as e:
                logger.warning(f"Error loading favorite pairs: {e}")
        self.favorite_pairs = set()

    def _save_favorite_pairs(self):
        try:
            with open(self._get_favorites_file_path(), "w", encoding="utf-8") as f:
                json.dump(sorted(self.favorite_pairs), f)
        except Exception as e:
            logger.warning(f"Error saving favorite pairs: {e}")

    def on_login_success(self, username, role, remember=True, session_token=None):
        self.current_user = username
        self.current_role = role
        self.active_session_token = session_token
        self._load_favorite_pairs()

        if remember:
            self.save_session(username, role, session_token=session_token or "")
        else:
            self.clear_session()

        role_badge = self._t("role_admin") if role == "admin" else self._t("role_pro")
        self.lbl_user_profile.configure(text=f"●  {username.upper()}  •  {role_badge}")

        self.btn_admin_users.pack_forget()
        self.btn_settings.pack_forget()
        self.btn_logout.pack_forget()
        self.lbl_ver.pack_forget()

        if role == "admin":
            self.btn_admin_users.pack(fill="x", padx=12, pady=(0, 6))

        self.btn_settings.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_logout.pack(fill="x", padx=12, pady=(0, 8))
        self.lbl_ver.pack(fill="x", padx=14, pady=(0, 12))

        self._refresh_settings_view_state(clear_password_fields=True)
        self._selected_cal_day_date = None
        self._admin_selected_user_id = None
        self._admin_selected_username = None
        self.trades_scope_filter = "My Trades"
        self.trades_scope_records = None

        self.deiconify()
        self.show_view("Dashboard")
        self.load_database_records()
        self.after(200, lambda: self.fetch_and_render_news(force=True))
        self.start_background_tracker()
        self.after(700, self._prewarm_analyzer_async)
        self.after(2500, lambda: self.check_for_updates(silent=True))

    def _prewarm_analyzer_async(self):
        if getattr(self, "_is_closing", False) or not self.winfo_exists():
            return
        threading.Thread(target=lambda: _get_analyzer().prewarm_client(), daemon=True).start()

    def logout_user(self, force=False):
        if force or messagebox.askyesno(self._t("dlg_logout_title"), self._t("dlg_logout_msg")):
            for after_attr in ("_trades_filter_after_id", "_preview_resize_after_id"):
                aid = getattr(self, after_attr, None)
                if aid is not None:
                    try:
                        self.after_cancel(aid)
                    except Exception:
                        pass
                    setattr(self, after_attr, None)
            self._settings_pw_submitting = False
            self._selected_cal_day_date = None
            self._admin_selected_user_id = None
            self._admin_selected_username = None
            self.favorite_pairs = set()
            self.trades_scope_filter = "My Trades"
            self.trades_scope_records = None
            self.all_db_records = []
            self.checklist_item_states = {}
            self.checklist_scores = {}
            self.cached_news_events = []
            self._news_range_cache = {}
            news_svc.invalidate_news_cache()
            self._last_news_fetch_ts = 0.0
            self._last_news_fetch_range = None
            self._last_news_widget_sig = None
            self._last_no_news_widget_sig = None
            self._last_cal_render_sig = None
            self._last_analytics_version = None
            self.current_preview_image = None
            self.calc_preview_image = None
            self._edit_after_preview_ctk = None
            self._preview_nav_list = []
            self._preview_nav_idx = -1
            self._preview_display_num = None
            self._preview_request_seq += 1
            self._edit_after_load_seq += 1
            self._news_fetch_seq += 1
            self._chart_analyze_seq += 1
            self._calc_extract_seq += 1
            self._trades_scope_load_seq += 1
            self._preview_state.update({
                "trade_id": None,
                "scope_user": None,
                "status": "OPEN",
                "before": None,
                "after": None,
                "tk_photo": None,
                "canvas_img_id": None,
                "loading": False,
            })
            self._edit_trade_state.update({
                "trade_id": None,
                "display_num": None,
                "scope_user": None,
                "existing_after_path": "",
                "existing_rr": "",
                "pil_after": None,
                "clear_after": False,
                "saving": False,
            })
            self._cleanup_temp_image(self.selected_image_path)
            self.selected_image_path = None
            self.stop_background_tracker()
            if self.current_user:
                clear_user_session_token(self.current_user)
            self.clear_session()
            self.current_user = None
            self.active_session_token = None
            self._refresh_settings_view_state(clear_password_fields=True)
            self.withdraw()
            LoginWindow(self, self.on_login_success)

    def open_admin_panel(self):
        if self.current_role == "admin":
            self.show_view("AdminUsers")

    # ------------------ IN-APP ADMIN USER MANAGEMENT ------------------ #
    def admin_refresh_users(self, preserve_status=False):
        if "AdminUsers" not in self._built_views or self.current_role != "admin":
            return

        def worker():
            try:
                users = get_all_users()
            except Exception as e:
                logger.warning(f"Error fetching users: {e}")
                users = []

            def populate():
                if getattr(self, "_is_closing", False) or not self.winfo_exists() or not hasattr(self, "tree_admin_users"):
                    return
                for item in self.tree_admin_users.get_children():
                    self.tree_admin_users.delete(item)
                for u in users:
                    self.tree_admin_users.insert("", "end", values=(u[0], u[1], u[2], str(u[3])[:10]))
                self._admin_clear_edit_form(preserve_status=preserve_status)

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, populate)

        threading.Thread(target=worker, daemon=True).start()

    def _admin_clear_edit_form(self, preserve_status=False):
        self._admin_selected_user_id = None
        self._admin_selected_username = None
        if hasattr(self, "lbl_admin_edit_target") and self.lbl_admin_edit_target.winfo_exists():
            self.lbl_admin_edit_target.configure(text=self._t("admin_edit_prompt"))
        if hasattr(self, "ent_admin_edit_user") and self.ent_admin_edit_user.winfo_exists():
            self.ent_admin_edit_user.delete(0, "end")
        if hasattr(self, "ent_admin_edit_pass") and self.ent_admin_edit_pass.winfo_exists():
            self.ent_admin_edit_pass.delete(0, "end")
        if hasattr(self, "opt_admin_edit_role"):
            self.opt_admin_edit_role.set("user")
        if not preserve_status and hasattr(self, "lbl_admin_status") and self.lbl_admin_status.winfo_exists():
            self.lbl_admin_status.configure(text="")

    def on_admin_user_select(self, event=None):
        if not hasattr(self, "tree_admin_users"):
            return
        sel = self.tree_admin_users.selection()
        if not sel:
            return
        vals = self.tree_admin_users.item(sel[0]).get("values", [])
        if len(vals) < 3:
            return
        user_id = vals[0]
        curr_username = str(vals[1])
        curr_role = str(vals[2])

        self._admin_selected_user_id = user_id
        self._admin_selected_username = curr_username

        self.lbl_admin_edit_target.configure(text=self._t("admin_editing_account", id=user_id, user=curr_username))
        self.ent_admin_edit_user.delete(0, "end")
        self.ent_admin_edit_user.insert(0, curr_username)
        self.ent_admin_edit_pass.delete(0, "end")
        self.opt_admin_edit_role.set(curr_role if curr_role in ("user", "admin") else "user")
        self.lbl_admin_status.configure(text="")

    def admin_create_account(self):
        if self.current_role != "admin" or "AdminUsers" not in self._built_views:
            return
        u = self.ent_admin_new_user.get().strip().lower()
        p = self.ent_admin_new_pass.get().strip()
        lang_snapshot = self.app_language

        def worker():
            ok, msg = add_new_user(u, p, lang=lang_snapshot)

            def finish():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                if ok:
                    messagebox.showinfo(self._t("dlg_success"), self._t("dlg_user_created", user=u))
                    self.ent_admin_new_user.delete(0, "end")
                    self.ent_admin_new_pass.delete(0, "end")
                    self.admin_refresh_users()
                else:
                    messagebox.showerror(self._t("dlg_error"), msg)

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def admin_save_user_changes(self):
        if self.current_role != "admin" or "AdminUsers" not in self._built_views:
            return
        if self._admin_selected_user_id is None:
            self.lbl_admin_status.configure(
                text=self._t("admin_err_select_first"),
                text_color=pair_color("danger_text")
            )
            return

        user_id = self._admin_selected_user_id
        new_u = self.ent_admin_edit_user.get().strip().lower()
        new_p = self.ent_admin_edit_pass.get()
        new_r = self.opt_admin_edit_role.get()
        lang_snapshot = self.app_language

        self.btn_admin_save_edit.configure(state="disabled", text=self._t("btn_saving"))
        self.lbl_admin_status.configure(text=self._t("admin_saving"), text_color=pair_color("text_secondary"))

        def worker():
            ok, msg = update_user_details(user_id, new_u, new_p, new_r, lang=lang_snapshot)

            def finish():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                if hasattr(self, "btn_admin_save_edit") and self.btn_admin_save_edit.winfo_exists():
                    self.btn_admin_save_edit.configure(state="normal", text=self._t("btn_save_changes"))
                if ok:
                    self.admin_refresh_users(preserve_status=True)
                    if hasattr(self, "lbl_admin_status") and self.lbl_admin_status.winfo_exists():
                        self.lbl_admin_status.configure(
                            text=f"✓ {msg}",
                            text_color=pair_color("accent")
                        )
                else:
                    if hasattr(self, "lbl_admin_status") and self.lbl_admin_status.winfo_exists():
                        self.lbl_admin_status.configure(
                            text=msg or self._t("admin_err_update"),
                            text_color=pair_color("danger_text")
                        )

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def admin_remove_user(self):
        if self.current_role != "admin" or "AdminUsers" not in self._built_views:
            return
        sel = self.tree_admin_users.selection()
        if not sel:
            messagebox.showwarning(self._t("dlg_select_user_title"), self._t("dlg_select_user_msg"))
            return
        vals = self.tree_admin_users.item(sel[0])["values"]
        username = str(vals[1])
        lang_snapshot = self.app_language

        if messagebox.askyesno(self._t("dlg_confirm_del_title"), self._t("dlg_del_user_msg", user=username)):
            def worker():
                ok, msg = delete_user(username, lang=lang_snapshot)

                def finish():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    if ok:
                        messagebox.showinfo(self._t("dlg_deleted"), msg)
                        self.admin_refresh_users()
                    else:
                        messagebox.showerror(self._t("dlg_error"), msg)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

    def _refresh_settings_view_state(self, clear_password_fields=False):
        if "Settings" not in self._built_views:
            return
        user_label = (self.current_user or self._t("guest")).upper()
        role_label = self._t("role_admin") if self.current_role == "admin" else self._t("role_pro")
        if hasattr(self, "lbl_settings_user") and self.lbl_settings_user.winfo_exists():
            self.lbl_settings_user.configure(text=self._t("settings_signed_in", user=user_label, role=role_label))
        if hasattr(self, "lbl_settings_theme_status") and self.lbl_settings_theme_status.winfo_exists():
            self.lbl_settings_theme_status.configure(
                text=self._t("settings_theme_dark") if self.theme_mode == "Dark" else self._t("settings_theme_light")
            )
        if hasattr(self, "btn_theme_toggle") and self.btn_theme_toggle.winfo_exists():
            self.btn_theme_toggle.configure(
                text=self._t("btn_switch_light") if self.theme_mode == "Dark" else self._t("btn_switch_dark")
            )
        if hasattr(self, "lbl_settings_lang_status") and self.lbl_settings_lang_status.winfo_exists():
            self.lbl_settings_lang_status.configure(
                text=self._t("settings_lang_status", lang=self._t("lang_name"))
            )
        if clear_password_fields:
            for attr in ("ent_settings_curr_pw", "ent_settings_new_pw", "ent_settings_conf_pw"):
                ent = getattr(self, attr, None)
                if ent is not None and ent.winfo_exists():
                    ent.delete(0, "end")
            if hasattr(self, "lbl_settings_pw_status") and self.lbl_settings_pw_status.winfo_exists():
                self.lbl_settings_pw_status.configure(text="")
            if hasattr(self, "btn_settings_update_pw") and self.btn_settings_update_pw.winfo_exists():
                self.btn_settings_update_pw.configure(state="normal", text=self._t("btn_update_pw"))
        elif (
            not self._settings_pw_submitting
            and hasattr(self, "lbl_settings_pw_status")
            and self.lbl_settings_pw_status.winfo_exists()
        ):
            self.lbl_settings_pw_status.configure(text="")
        self._sync_settings_update_card()
        self._refresh_settings_tz_card()

    def _refresh_settings_tz_card(self):
        if "Settings" not in self._built_views:
            return
        tz_options, tz_disp_to_key, tz_key_to_disp = get_timezone_dropdown_options(
            auto_prefix=self._t("settings_tz_auto_prefix")
        )
        self._tz_display_to_key = tz_disp_to_key
        self._tz_key_to_display = tz_key_to_disp
        cur_key = get_app_timezone_pref()
        cur_disp = tz_key_to_disp.get(cur_key, tz_options[0])

        opt = getattr(self, "opt_settings_timezone", None)
        if opt is not None and opt.winfo_exists():
            opt.update_options(tz_options)
            opt.set(cur_disp)

        lbl = getattr(self, "lbl_settings_tz_status", None)
        if lbl is not None and lbl.winfo_exists():
            _, _, tz_abbr, offset_lbl = get_app_tz_info()
            detected_tz = detect_system_timezone()
            lbl.configure(
                text=self._t(
                    "settings_tz_status",
                    abbr=tz_abbr,
                    offset=offset_lbl,
                    detected=detected_tz,
                )
            )

    def on_app_timezone_change(self, display_val):
        tz_key = getattr(self, "_tz_display_to_key", {}).get(display_val)
        if not tz_key:
            tz_key = "Auto" if str(display_val).startswith("Auto") else str(display_val).split(" (")[0].strip()

        self.app_timezone = set_app_timezone(tz_key)
        self._refresh_settings_tz_card()

        self._last_sessions_sig = None
        self.update_active_sessions_widget()

        if getattr(self, "cached_news_events", None):
            news_svc.reproject_news_events(self.cached_news_events)
        if hasattr(self, "_news_range_cache") and isinstance(self._news_range_cache, dict):
            for entry in self._news_range_cache.values():
                if entry and entry.get("events"):
                    news_svc.reproject_news_events(entry["events"])

        dash_source = self.cached_news_events
        last_range = getattr(self, "_last_news_fetch_range", None)
        if last_range not in ("Today", "This Week") and hasattr(self, "_news_range_cache"):
            for r_name in ("Today", "This Week"):
                entry = self._news_range_cache.get(r_name)
                if entry and entry.get("events"):
                    dash_source = entry["events"]
                    break

        if dash_source:
            upcoming, today_events = news_svc.compute_upcoming_and_today(dash_source)
            self._last_news_widget_sig = None
            self._last_no_news_widget_sig = None
            self.update_dashboard_news_widget(upcoming)
            self.update_dashboard_no_news_widget(today_events)

        if "News" in self._built_views and hasattr(self, "tree_news") and self.tree_news.winfo_exists() and self.cached_news_events:
            self._render_news_table(self.cached_news_events)

        self._last_cal_render_sig = None
        if "Calendar" in self._built_views and self.current_active_view == "Calendar":
            self.render_calendar()
        if "Trades" in self._built_views and self.current_active_view == "Trades":
            self.apply_trades_filter()

    def submit_settings_password_change(self):
        if self._settings_pw_submitting or not self.current_user:
            return
        if "Settings" not in self._built_views:
            return

        curr_pw = self.ent_settings_curr_pw.get()
        new_pw = self.ent_settings_new_pw.get()
        conf_pw = self.ent_settings_conf_pw.get()

        if not curr_pw or not new_pw or not conf_pw:
            self.lbl_settings_pw_status.configure(
                text=self._t("pw_err_fill"),
                text_color=pair_color("danger_text")
            )
            return

        if len(new_pw) < 6:
            self.lbl_settings_pw_status.configure(
                text=self._t("pw_err_len"),
                text_color=pair_color("danger_text")
            )
            return

        if new_pw != conf_pw:
            self.lbl_settings_pw_status.configure(
                text=self._t("pw_err_match"),
                text_color=pair_color("danger_text")
            )
            return

        self._settings_pw_submitting = True
        self.btn_settings_update_pw.configure(state="disabled", text=self._t("btn_updating"))
        self.lbl_settings_pw_status.configure(
            text=self._t("pw_updating"),
            text_color=pair_color("text_secondary")
        )

        username_val = self.current_user
        lang_snapshot = self.app_language

        def worker():
            try:
                ok, msg, new_token = change_own_password(username_val, curr_pw, new_pw, lang=lang_snapshot)
            except Exception as err:
                ok, msg, new_token = False, str(err), None

            def finish():
                self._settings_pw_submitting = False
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                if hasattr(self, "btn_settings_update_pw") and self.btn_settings_update_pw.winfo_exists():
                    self.btn_settings_update_pw.configure(state="normal", text=self._t("btn_update_pw"))
                if ok:
                    self._on_own_password_changed(new_token)
                    for attr in ("ent_settings_curr_pw", "ent_settings_new_pw", "ent_settings_conf_pw"):
                        ent = getattr(self, attr, None)
                        if ent is not None and ent.winfo_exists():
                            ent.delete(0, "end")
                    if hasattr(self, "lbl_settings_pw_status") and self.lbl_settings_pw_status.winfo_exists():
                        self.lbl_settings_pw_status.configure(
                            text=self._t("pw_updated"),
                            text_color=pair_color("accent")
                        )
                else:
                    if hasattr(self, "lbl_settings_pw_status") and self.lbl_settings_pw_status.winfo_exists():
                        self.lbl_settings_pw_status.configure(
                            text=msg or self._t("pw_failed"),
                            text_color=pair_color("danger_text")
                        )

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _on_own_password_changed(self, new_token):
        self.active_session_token = new_token
        if self.get_saved_session() is not None and self.current_user:
            self.save_session(self.current_user, self.current_role, session_token=new_token)

    # ------------------ AUTO-UPDATER SYSTEM ------------------ #
    def _sync_settings_update_card(self):
        if "Settings" not in self._built_views:
            return
        st = self._update_check_state
        status_code = st.get("status", "idle")
        msg = st.get("message") or self._t("upd_idle")
        is_ok = bool(st.get("is_ok", True))

        if hasattr(self, "lbl_settings_update_status") and self.lbl_settings_update_status.winfo_exists():
            color = (
                pair_color("text_secondary")
                if status_code in ("idle", "checking")
                else (pair_color("accent") if is_ok else pair_color("danger_text"))
            )
            self.lbl_settings_update_status.configure(text=msg, text_color=color)

        notes_lbl = getattr(self, "lbl_settings_release_notes", None)
        prog_bar = getattr(self, "prog_settings_update", None)
        install_btn = getattr(self, "btn_settings_install_update", None)
        check_btn = getattr(self, "btn_settings_check_updates", None)

        if notes_lbl is not None and notes_lbl.winfo_exists():
            notes_lbl.pack_forget()
        if prog_bar is not None and prog_bar.winfo_exists():
            prog_bar.pack_forget()
        if install_btn is not None and install_btn.winfo_exists():
            install_btn.pack_forget()
        if check_btn is not None and check_btn.winfo_exists():
            check_btn.pack_forget()

        raw_notes = (st.get("release_notes") or "").strip()
        if raw_notes and status_code == "update_available" and notes_lbl is not None and notes_lbl.winfo_exists():
            compact_notes = raw_notes[:240] + ("..." if len(raw_notes) > 240 else "")
            notes_lbl.configure(text=compact_notes)
            notes_lbl.pack(anchor="w", padx=16, pady=(0, 8))

        if st.get("downloading") and prog_bar is not None and prog_bar.winfo_exists():
            p_val = st.get("progress")
            if p_val is None:
                if prog_bar.cget("mode") != "indeterminate":
                    prog_bar.configure(mode="indeterminate")
                    prog_bar.start()
            else:
                if prog_bar.cget("mode") != "determinate":
                    prog_bar.stop()
                    prog_bar.configure(mode="determinate")
                prog_bar.set(float(p_val))
            prog_bar.pack(fill="x", padx=16, pady=(0, 10))
        elif prog_bar is not None and prog_bar.winfo_exists():
            try:
                prog_bar.stop()
                prog_bar.configure(mode="determinate")
                prog_bar.set(0)
            except Exception:
                pass

        if (
            status_code == "update_available"
            and st.get("download_url")
            and not st.get("downloading")
            and install_btn is not None
            and install_btn.winfo_exists()
        ):
            install_btn.configure(text=self._t("btn_dl_install_ver", tag=st.get("latest_tag", "")))
            install_btn.pack(fill="x", padx=16, pady=(0, 8))

        if check_btn is not None and check_btn.winfo_exists():
            if st.get("downloading"):
                check_btn.configure(state="disabled", text=self._t("btn_downloading_updates"))
            elif status_code == "checking":
                check_btn.configure(state="disabled", text=self._t("btn_checking_updates"))
            else:
                check_btn.configure(state="normal", text=self._t("btn_check_updates"))
            check_btn.pack(fill="x", padx=16, pady=(2, 10))

    def install_available_update_from_settings(self):
        st = self._update_check_state
        if st.get("downloading") or not st.get("download_url"):
            return
        self.download_and_apply_update(
            st["download_url"],
            checksum_url=st.get("checksum_url"),
            expected_sha256=st.get("expected_sha256", ""),
        )

    def check_for_updates(self, silent=True, from_settings=False):
        if self._update_check_state.get("downloading"):
            return
        self._update_check_state.update({
            "status": "checking",
            "message": self._t("upd_checking"),
            "is_ok": True,
        })
        if from_settings:
            self._sync_settings_update_card()

        def worker():
            try:
                result = updater_svc.check_latest_release(_get_http_session(), CURRENT_VERSION)
                status = result.get("status")
                notes = result.get("release_notes", "")

                if status == "update_available" and not getattr(self, "_is_closing", False) and self.winfo_exists():
                    tag = result["latest_tag"]

                    def on_available():
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "status": "update_available",
                            "message": self._t("upd_available", tag=tag),
                            "is_ok": True,
                            "latest_tag": tag,
                            "download_url": result["download_url"],
                            "checksum_url": result.get("checksum_url", ""),
                            "expected_sha256": result.get("expected_sha256", ""),
                            "release_notes": notes,
                        })
                        self._sync_settings_update_card()
                        self.prompt_update(
                            tag,
                            result["download_url"],
                            result.get("checksum_url", ""),
                            expected_sha256=result.get("expected_sha256", ""),
                            release_notes=notes,
                        )

                    self.after(0, on_available)
                elif status == "up_to_date" and not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def on_uptodate():
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "status": "up_to_date",
                            "message": self._t("upd_up_to_date", ver=CURRENT_VERSION),
                            "is_ok": True,
                            "latest_tag": result.get("latest_tag", CURRENT_VERSION),
                            "download_url": "",
                            "checksum_url": "",
                            "expected_sha256": "",
                            "release_notes": notes,
                        })
                        self._sync_settings_update_card()
                        if not silent and not from_settings:
                            messagebox.showinfo(self._t("dlg_auto_upd_title"), self._t("dlg_auto_upd_latest", ver=CURRENT_VERSION))

                    self.after(0, on_uptodate)
                elif status == "http_error" and not getattr(self, "_is_closing", False) and self.winfo_exists():
                    code = result.get("status_code")

                    def on_http_err():
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "status": "error",
                            "message": self._t("upd_http_err", code=code),
                            "is_ok": False,
                        })
                        self._sync_settings_update_card()
                        if not silent and not from_settings:
                            messagebox.showerror(self._t("dlg_upd_check_fail_title"), self._t("dlg_upd_check_fail_msg", code=code))

                    self.after(0, on_http_err)
            except Exception as e:
                logger.warning(f"Update check error: {e}")
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def on_exc(err=e):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "status": "error",
                            "message": self._t("upd_check_fail", err=err) if not silent else self._t("upd_idle"),
                            "is_ok": False if not silent else True,
                        })
                        self._sync_settings_update_card()
                        if not silent and not from_settings:
                            messagebox.showerror(self._t("dlg_upd_err_title"), self._t("dlg_upd_err_msg", err=err))

                    self.after(0, on_exc)

        threading.Thread(target=worker, daemon=True).start()

    def prompt_update(self, latest_tag, download_url, checksum_url=None, expected_sha256="", release_notes=""):
        notes_block = ""
        if release_notes:
            trimmed = release_notes.strip()
            if len(trimmed) > 350:
                trimmed = trimmed[:350].rstrip() + "..."
            notes_block = self._t("dlg_upd_whats_new", notes=trimmed)
        if messagebox.askyesno(
            self._t("dlg_upd_avail_title"),
            self._t("dlg_upd_avail_msg", tag=latest_tag, cur=CURRENT_VERSION, notes_block=notes_block)
        ):
            self.download_and_apply_update(download_url, checksum_url=checksum_url, expected_sha256=expected_sha256)

    def download_and_apply_update(self, download_url, checksum_url=None, expected_sha256=""):
        if self._update_check_state.get("downloading"):
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo(
                self._t("dlg_auto_upd_title"),
                self._t("dlg_auto_upd_exe_only")
            )
            return

        if not updater_svc.is_trusted_update_url(download_url) or (checksum_url and not updater_svc.is_trusted_update_url(checksum_url)):
            messagebox.showerror(self._t("dlg_upd_err_title"), self._t("dlg_upd_untrusted"))
            return

        self._update_check_state.update({
            "downloading": True,
            "progress": 0.0,
            "message": self._t("upd_downloading"),
            "is_ok": True,
        })
        self.show_view("Settings")
        self._sync_settings_update_card()

        def _on_progress(p):
            if getattr(self, "_is_closing", False) or not self.winfo_exists():
                return

            def _apply():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                self._update_check_state["progress"] = p
                if p is None:
                    self._update_check_state["message"] = self._t("upd_downloading_prog")
                else:
                    pct = int(round(float(p) * 100))
                    self._update_check_state["message"] = self._t("upd_downloading_pct", pct=pct)
                self._sync_settings_update_card()

            self.after(0, _apply)

        def download_worker():
            try:
                updater_svc.download_and_install_update(
                    _get_http_session(),
                    download_url,
                    checksum_url=checksum_url,
                    expected_sha256=expected_sha256,
                    progress_cb=_on_progress,
                )
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _done():
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "downloading": False,
                            "progress": 1.0,
                            "message": self._t("upd_verified"),
                            "is_ok": True,
                        })
                        self._sync_settings_update_card()
                        self.after(300, self.on_closing)

                    self.after(0, _done)
            except Exception as e:
                logger.error(f"Update download error: {e}")
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _failed(err=e):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._update_check_state.update({
                            "downloading": False,
                            "progress": None,
                            "message": self._t("upd_dl_fail", err=err),
                            "is_ok": False,
                        })
                        self._sync_settings_update_card()
                        messagebox.showerror(self._t("dlg_upd_err_title"), self._t("dlg_upd_dl_fail_msg", err=err))

                    self.after(0, _failed)

        threading.Thread(target=download_worker, daemon=True).start()

    def on_closing(self):
        if getattr(self, "_is_closing", False):
            return
        self._is_closing = True
        self._preview_request_seq += 1
        self._edit_after_load_seq += 1
        self._news_fetch_seq += 1
        self._chart_analyze_seq += 1
        self._calc_extract_seq += 1
        self._trades_scope_load_seq += 1
        try:
            self.close_dropdown()
        except Exception:
            pass
        for after_attr in ("_trades_filter_after_id", "_preview_resize_after_id", "_sessions_timer_after_id"):
            aid = getattr(self, after_attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, after_attr, None)
        try:
            self._cleanup_temp_image(self.selected_image_path)
            self.selected_image_path = None
        except Exception:
            pass
        try:
            self.stop_background_tracker()
        except Exception:
            pass
        try:
            tracker.close_tv_sessions()
        except Exception:
            pass
        try:
            sess = getattr(_http_thread_local, "session", None)
            if sess is not None:
                sess.close()
        except Exception:
            pass
        try:
            self.save_calc_settings()
        except Exception:
            pass
        try:
            db.close_pool()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    def save_calc_settings(self, event=None):
        if "Calculator" not in getattr(self, "_built_views", set()):
            return
        bal_w = getattr(self, "calc_balance_entry", None)
        if bal_w is None:
            return
        try:
            if not bal_w.winfo_exists():
                return
            bal_val = bal_w.get()
            risk_w = getattr(self, "calc_risk_entry", None)
            risk_val = risk_w.get() if (risk_w is not None and risk_w.winfo_exists()) else "1.0"
            mode_val = getattr(self, "calc_risk_mode_var", "Risk %")
        except Exception:
            return
        calc_svc.save_calc_settings(self.current_user, bal_val, risk_val, mode_val)

    def _create_circle_icon(self, color_hex, size=14):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((1, 1, size - 2, size - 2), fill=color_hex)
        return ImageTk.PhotoImage(img)

    def setup_table_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.layout("Treeview", [('Treeview.treearea', {'sticky': 'nswe'})])

        style.configure(
            "Treeview",
            background=THEME["row_even"],
            foreground=THEME["text_primary"],
            fieldbackground=THEME["row_even"],
            bordercolor=THEME["bg_card"],
            borderwidth=0,
            rowheight=38,
            font=("Segoe UI", 10)
        )

        style.configure(
            "Treeview.Heading",
            background=THEME["bg_card_alt"],
            foreground=THEME["text_secondary"],
            bordercolor=THEME["border"],
            borderwidth=0,
            relief="flat",
            padding=(8, 9),
            font=("Segoe UI", 10, "bold")
        )

        style.map(
            "Treeview.Heading",
            background=[("active", THEME["bg_hover"]), ("pressed", THEME["border_strong"])],
            foreground=[("active", THEME["accent"])]
        )

        style.map(
            "Treeview",
            background=[("selected", THEME["row_selected"])],
            foreground=[("selected", THEME["row_selected_fg"])]
        )

        style.layout(
            "Dark.Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        )
        style.layout(
            "Dark.Horizontal.TScrollbar",
            [("Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        )
        style.layout(
            "Vertical.TScrollbar",
            [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        )
        style.layout(
            "Horizontal.TScrollbar",
            [("Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})]
        )

        for sb_style in ("Dark.Vertical.TScrollbar", "Dark.Horizontal.TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(
                sb_style,
                background=THEME["border_strong"],
                troughcolor=THEME["bg_card"],
                bordercolor=THEME["bg_card"],
                darkcolor=THEME["border_strong"],
                lightcolor=THEME["border_strong"],
                gripcount=0,
                width=8,
                relief="flat",
                borderwidth=0
            )
            style.map(
                sb_style,
                background=[("active", THEME["text_secondary"]), ("pressed", THEME["accent"])]
            )

    def _configure_trade_tree_tags(self, tree):
        tree.tag_configure("tp_even", background=THEME["row_even"], foreground=THEME["accent"])
        tree.tag_configure("tp_odd", background=THEME["row_odd"], foreground=THEME["accent"])
        tree.tag_configure("sl_even", background=THEME["row_even"], foreground=THEME["danger_text"])
        tree.tag_configure("sl_odd", background=THEME["row_odd"], foreground=THEME["danger_text"])
        tree.tag_configure("open_even", background=THEME["row_even"], foreground=THEME["text_primary"])
        tree.tag_configure("open_odd", background=THEME["row_odd"], foreground=THEME["text_primary"])

    def _get_trade_row_tag(self, status_val, idx):
        parity = "even" if idx % 2 == 0 else "odd"
        if status_val == "TP":
            return f"tp_{parity}"
        elif status_val == "SL":
            return f"sl_{parity}"
        return f"open_{parity}"

    def show_custom_popup_menu(self, target_btn, options, callback=None, is_searchable=False, enable_favorites=False, custom_max_h=None):
        if self.active_dropdown_window and self.active_dropdown_window.winfo_exists():
            prev_target = getattr(self.active_dropdown_window, "target_btn", None)
            self.close_dropdown()
            if prev_target == target_btn:
                return

        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.configure(bg=THEME["border_strong"])
        win.target_btn = target_btn
        self.active_dropdown_window = win

        target_btn.update_idletasks()
        x = target_btn.winfo_rootx()
        y = target_btn.winfo_rooty() + target_btn.winfo_height() + 3
        w = max(target_btn.winfo_width(), 250 if (is_searchable or enable_favorites) else 185)

        search_height = 42 if is_searchable else 0

        frame = tk.Frame(win, bg=THEME["bg_card_alt"], highlightbackground=THEME["border_strong"], highlightthickness=1)
        frame.pack(fill="both", expand=True)

        search_var = tk.StringVar() if is_searchable else None

        canvas = tk.Canvas(frame, bg=THEME["bg_card_alt"], highlightthickness=0)
        scrollbar = ctk.CTkScrollbar(
            frame,
            orientation="vertical",
            command=canvas.yview,
            width=8,
            corner_radius=8,
            fg_color="transparent",
            bg_color=THEME["bg_card_alt"],
            button_color=pair_color("border_strong"),
            button_hover_color=pair_color("accent"),
        )
        scroll_content = tk.Frame(canvas, bg=THEME["bg_card_alt"])

        def _on_mousewheel(event):
            if canvas.winfo_exists():
                if event.delta:
                    canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                elif event.num == 4:
                    canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    canvas.yview_scroll(1, "units")

        def _bind_mw(w_item):
            w_item.bind("<MouseWheel>", _on_mousewheel)
            w_item.bind("<Button-4>", _on_mousewheel)
            w_item.bind("<Button-5>", _on_mousewheel)

        _bind_mw(win)
        _bind_mw(frame)
        _bind_mw(canvas)
        _bind_mw(scroll_content)

        if is_searchable:
            search_frame = tk.Frame(frame, bg=THEME["bg_input"], highlightbackground=THEME["border_strong"], highlightthickness=1)
            search_frame.pack(fill="x", padx=6, pady=(6, 4))
            _bind_mw(search_frame)

            lbl_icon = tk.Label(search_frame, text="🔍", bg=THEME["bg_input"], fg=THEME["text_secondary"], font=("Segoe UI", 10))
            lbl_icon.pack(side="left", padx=(6, 2))
            _bind_mw(lbl_icon)

            search_entry = tk.Entry(
                search_frame,
                textvariable=search_var,
                bg=THEME["bg_input"],
                fg=THEME["text_primary"],
                insertbackground=THEME["accent"],
                relief="flat",
                font=("Segoe UI", 11)
            )
            search_entry.pack(side="left", fill="x", expand=True, padx=4, pady=4)
            search_entry.focus_set()
            _bind_mw(search_entry)

        canvas_window = canvas.create_window((0, 0), window=scroll_content, anchor="nw")

        def on_canvas_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(canvas_window, width=event.width)

        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=(0, 2), pady=2)
        scrollbar_visible = [False]

        def _sync_popup_scrollbar():
            if not win.winfo_exists() or not canvas.winfo_exists():
                return
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            first, last = canvas.yview()
            need_sb = (first > 0.0 or last < 0.999)
            if need_sb and not scrollbar_visible[0]:
                scrollbar.pack(side="right", fill="y", padx=(0, 3), pady=4)
                scrollbar_visible[0] = True
            elif not need_sb and scrollbar_visible[0]:
                scrollbar.pack_forget()
                scrollbar_visible[0] = False

        canvas.bind("<Configure>", lambda e: (on_canvas_configure(e), _sync_popup_scrollbar()))

        def render_items(filter_text=""):
            if not win.winfo_exists():
                return
            for child in scroll_content.winfo_children():
                child.destroy()

            query = filter_text.strip().lower()
            filtered = [opt for opt in options if query in opt.lower()] if query else list(options)

            if enable_favorites:
                favs = [opt for opt in filtered if opt in self.favorite_pairs]
                non_favs = [opt for opt in filtered if opt not in self.favorite_pairs]
                display_options = favs + non_favs
            else:
                display_options = filtered

            display_options = display_options[:100]

            if not display_options:
                no_res = tk.Label(
                    scroll_content,
                    text=self._t("popup_no_match"),
                    bg=THEME["bg_card_alt"],
                    fg=THEME["text_secondary"],
                    font=("Segoe UI", 10, "italic"),
                    pady=8
                )
                no_res.pack(fill="x")
                _bind_mw(no_res)
                win.after_idle(_sync_popup_scrollbar)
                return

            for opt in display_options:
                row_frame = tk.Frame(scroll_content, bg=THEME["bg_card_alt"], cursor="hand2")
                row_frame.pack(fill="x", expand=True)
                _bind_mw(row_frame)

                is_fav = opt in self.favorite_pairs if enable_favorites else False

                def on_row_enter(e, rf=row_frame):
                    rf.configure(bg=THEME["bg_hover"])
                    for c in rf.winfo_children():
                        c.configure(bg=THEME["bg_hover"])

                def on_row_leave(e, rf=row_frame):
                    rf.configure(bg=THEME["bg_card_alt"])
                    for c in rf.winfo_children():
                        c.configure(bg=THEME["bg_card_alt"])

                row_frame.bind("<Enter>", on_row_enter)
                row_frame.bind("<Leave>", on_row_leave)
                row_frame.bind("<Button-1>", lambda e, val=opt: self._select_custom_popup_option(target_btn, val, callback))

                if enable_favorites:
                    star_symbol = "★" if is_fav else "☆"
                    star_color = THEME["amber"] if is_fav else THEME["text_muted"]

                    star_btn = tk.Button(
                        row_frame,
                        text=star_symbol,
                        bg=THEME["bg_card_alt"],
                        fg=star_color,
                        activebackground=THEME["bg_hover"],
                        activeforeground=THEME["amber"],
                        relief="flat",
                        padx=10,
                        pady=4,
                        font=("Segoe UI", 12, "bold"),
                        cursor="hand2"
                    )
                    star_btn.pack(side="right", fill="y")
                    star_btn.bind("<Enter>", on_row_enter)
                    star_btn.bind("<Leave>", on_row_leave)
                    _bind_mw(star_btn)

                    def toggle_fav(e, val=opt):
                        if val in self.favorite_pairs:
                            self.favorite_pairs.remove(val)
                        else:
                            self.favorite_pairs.add(val)
                        self._save_favorite_pairs()
                        render_items(search_var.get() if search_var else "")
                        return "break"

                    star_btn.bind("<Button-1>", toggle_fav)

                item_btn = tk.Button(
                    row_frame,
                    text=opt,
                    bg=THEME["bg_card_alt"],
                    fg=THEME["text_primary"],
                    activebackground=THEME["bg_hover"],
                    activeforeground=THEME["accent"],
                    relief="flat",
                    anchor="w",
                    padx=10,
                    pady=4,
                    font=("Segoe UI", 11),
                    cursor="hand2",
                    command=lambda val=opt: self._select_custom_popup_option(target_btn, val, callback)
                )
                item_btn.pack(side="left", fill="both", expand=True)
                item_btn.bind("<Enter>", on_row_enter)
                item_btn.bind("<Leave>", on_row_leave)
                _bind_mw(item_btn)

            win.after_idle(_sync_popup_scrollbar)

        if is_searchable:
            popup_search_after_id = [None]

            def _on_popup_search(*args):
                if popup_search_after_id[0] is not None and win.winfo_exists():
                    try:
                        win.after_cancel(popup_search_after_id[0])
                    except Exception:
                        pass
                if win.winfo_exists():
                    popup_search_after_id[0] = win.after(
                        70,
                        lambda: render_items(search_var.get()) if win.winfo_exists() else None
                    )

            search_var.trace_add("write", _on_popup_search)

        render_items()

        try:
            scale_factor = ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            scale_factor = 1.10

        item_height = int(35 * scale_factor)
        padding_offset = int(14 * scale_factor)
        visible_count = min(len(options), 100)
        calculated_h = visible_count * item_height + padding_offset + search_height

        if custom_max_h:
            max_limit = int(custom_max_h * scale_factor)
        else:
            max_limit = int(350 * scale_factor) if is_searchable else int(400 * scale_factor)
        total_h = min(calculated_h, max_limit)
        screen_h = self.winfo_screenheight()
        if y + total_h > screen_h - 16:
            above_y = target_btn.winfo_rooty() - total_h - 3
            if above_y >= 12:
                y = above_y
            else:
                y = max(12, screen_h - total_h - 16)

        win.geometry(f"{w}x{total_h}+{x}+{y}")
        win.after(15, _sync_popup_scrollbar)

    def _select_custom_popup_option(self, btn, val, callback=None):
        if hasattr(btn, "set"):
            btn.set(val)
        self.close_dropdown()
        if callback:
            callback(val)

    def close_dropdown(self):
        if self.active_dropdown_window and self.active_dropdown_window.winfo_exists():
            self.active_dropdown_window.destroy()
        self.active_dropdown_window = None

    def on_global_click(self, event):
        if self.active_dropdown_window and self.active_dropdown_window.winfo_exists():
            widget = event.widget
            if str(widget).startswith(str(self.active_dropdown_window)):
                return
            target_btn = getattr(self.active_dropdown_window, "target_btn", None)
            if target_btn and (widget == target_btn or str(widget).startswith(str(target_btn))):
                return
            self.close_dropdown()

    def stop_background_tracker(self):
        self._tracker_stop_event.set()
        if self._tracker_thread and self._tracker_thread.is_alive():
            self._tracker_thread.join(timeout=1.5)
        self._tracker_thread = None

    def start_background_tracker(self):
        self.stop_background_tracker()
        stop_event = threading.Event()
        self._tracker_stop_event = stop_event

        def loop():
            while not stop_event.is_set() and not getattr(self, "_is_closing", False):
                try:
                    active_user = self.current_user
                    active_token = self.active_session_token
                    if active_user and active_token:
                        if not validate_active_session_token(active_user, active_token):
                            if not getattr(self, "_is_closing", False) and not stop_event.is_set() and self.winfo_exists():
                                def handle_session_conflict():
                                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                                        return
                                    messagebox.showwarning(
                                        self._t("dlg_session_title"),
                                        self._t("dlg_session_msg")
                                    )
                                    self.logout_user(force=True)

                                self.after(0, handle_session_conflict)
                            break

                    if active_user:
                        updated = tracker.check_open_trades(username=active_user)
                        if updated and not getattr(self, "_is_closing", False) and not stop_event.is_set() and self.winfo_exists():
                            def _reload_if_alive():
                                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                                    self.load_database_records()
                            self.after(0, _reload_if_alive)

                    range_cache = getattr(self, "_news_range_cache", {})
                    dash_news_src = None
                    if "This Week" in range_cache:
                        dash_news_src = range_cache["This Week"].get("events")
                    elif "Today" in range_cache:
                        dash_news_src = range_cache["Today"].get("events")
                    elif getattr(self, "_last_news_fetch_range", None) in ("Today", "This Week") and getattr(self, "cached_news_events", None):
                        dash_news_src = list(self.cached_news_events)

                    if dash_news_src:
                        upcoming, today_events = news_svc.compute_upcoming_and_today(dash_news_src)
                        if not getattr(self, "_is_closing", False) and not stop_event.is_set() and self.winfo_exists():
                            def _update_dash_news(u=upcoming, t=today_events):
                                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                                    return
                                self.update_dashboard_news_widget(u)
                                self.update_dashboard_no_news_widget(t)
                            self.after(0, _update_dash_news)
                except Exception as e:
                    logger.warning(f"Background Tracker error: {e}")

                if stop_event.wait(30):
                    break

        self._tracker_thread = threading.Thread(target=loop, daemon=True)
        self._tracker_thread.start()

    def _schedule_sessions_clock(self):
        if getattr(self, "_is_closing", False) or not self.winfo_exists():
            return
        if self._sessions_timer_after_id is not None:
            try:
                self.after_cancel(self._sessions_timer_after_id)
            except Exception:
                pass
            self._sessions_timer_after_id = None
        self.update_active_sessions_widget()
        self._sessions_timer_after_id = self.after(15000, self._schedule_sessions_clock)

    # ------------------ ECONOMIC NEWS ACTIONS ------------------ #
    def on_news_range_change(self, selected_range):
        self.news_range_var = selected_range
        self.fetch_and_render_news(force=False)

    def on_news_currency_change(self, selected_currency):
        self.news_currency_var = selected_currency
        if "News" in self._built_views and hasattr(self, "tree_news") and self.tree_news.winfo_exists():
            self._render_news_table(self.cached_news_events)

    def on_news_impact_change(self, selected_impact):
        self.news_impact_var = selected_impact
        if "News" in self._built_views and hasattr(self, "tree_news") and self.tree_news.winfo_exists():
            self._render_news_table(self.cached_news_events)

    def fetch_and_render_news(self, force=True):
        target_range = getattr(self, "news_range_var", "Today")
        if not hasattr(self, "_news_range_cache"):
            self._news_range_cache = {}

        self._news_fetch_seq = getattr(self, "_news_fetch_seq", 0) + 1
        req_seq = self._news_fetch_seq

        if not force:
            cached_entry = self._news_range_cache.get(target_range)
            if cached_entry and (time.time() - cached_entry["ts"]) < 300:
                self.cached_news_events = cached_entry["events"]
                self._last_news_fetch_ts = cached_entry["ts"]
                self._last_news_fetch_range = target_range
                if "News" in self._built_views:
                    self._render_news_table(self.cached_news_events)
                return

            shared_slices = news_svc.get_cached_news_range(target_range)
            if shared_slices is not None:
                events, upcoming_events, today_events = shared_slices
                fetched_ts = time.time()
                self._news_range_cache[target_range] = {
                    "events": events,
                    "ts": fetched_ts,
                }
                if target_range in ("Today", "This Week"):
                    self.update_dashboard_news_widget(upcoming_events)
                    self.update_dashboard_no_news_widget(today_events)
                self.cached_news_events = events
                self._last_news_fetch_ts = fetched_ts
                self._last_news_fetch_range = target_range
                if "News" in self._built_views:
                    self._render_news_table(events)
                return

        if "News" in self._built_views and hasattr(self, "tree_news") and self.tree_news.winfo_exists():
            for item in self.tree_news.get_children():
                self.tree_news.delete(item)

            range_key_map = {
                "Today": "news_range_today",
                "This Week": "news_range_this_week",
                "Next Week": "news_range_next_week",
                "Last Week": "news_range_last_week",
            }
            range_disp = self._t(range_key_map.get(target_range, "news_range_today"))
            self.tree_news.insert(
                "",
                "end",
                image=self.impact_icons["none"],
                values=("", "", "", self._t("news_loading", range=range_disp.lower()), "", "", "")
            )

        def worker():
            sess = _get_http_session()
            events, upcoming_events, today_events = news_svc.fetch_news_for_range(
                sess, target_range, force=force
            )
            if target_range in ("Today", "This Week") and events:
                news_svc.prefetch_adjacent_weeks(sess)

            def update_ui():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                fetched_ts = time.time()
                self._news_range_cache[target_range] = {
                    "events": events,
                    "ts": fetched_ts,
                }
                if target_range in ("Today", "This Week"):
                    self.update_dashboard_news_widget(upcoming_events)
                    self.update_dashboard_no_news_widget(today_events)
                if req_seq != self._news_fetch_seq:
                    return
                self.cached_news_events = events
                self._last_news_fetch_ts = fetched_ts
                self._last_news_fetch_range = target_range
                if "News" in self._built_views:
                    self._render_news_table(events)

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, update_ui)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ CHECKLIST LINKING ACTIONS ------------------ #
    def _on_analyzer_type_change(self, selected_type):
        if (
            "Checklist" in self._built_views
            and hasattr(self, "opt_checklist_type")
            and self.opt_checklist_type.winfo_exists()
            and selected_type in TRADE_TYPE_CHECKLISTS
        ):
            if self.opt_checklist_type.get() != selected_type:
                self.opt_checklist_type.set(selected_type)
                self.load_checklist_for_type(selected_type)
        self._refresh_analyzer_checklist_badge()

    def _get_active_checklist_score(self):
        trade_type = self.opt_type.get().strip() if hasattr(self, "opt_type") else ""
        if trade_type and trade_type in self.checklist_scores:
            return self.checklist_scores[trade_type]
        return ""

    def _refresh_analyzer_checklist_badge(self):
        if not hasattr(self, "lbl_checklist_badge"):
            return
        score = self._get_active_checklist_score()
        if score:
            self.lbl_checklist_badge.configure(
                text=self._t("checklist_badge_val", score=score),
                text_color=pair_color("accent"),
                border_color=pair_color("accent")
            )
        else:
            self.lbl_checklist_badge.configure(
                text=self._t("checklist_badge_none"),
                text_color=pair_color("text_secondary"),
                border_color=pair_color("border_strong")
            )

    def open_checklist_for_current_setup(self):
        selected_type = self.opt_type.get().strip() if hasattr(self, "opt_type") else ""
        self.show_view("Checklist")
        if selected_type in TRADE_TYPE_CHECKLISTS and hasattr(self, "opt_checklist_type"):
            self.opt_checklist_type.set(selected_type)
            self.load_checklist_for_type(selected_type)

    # ------------------ TRADES SORTING & IMPORT / EXPORT ------------------ #
    def sort_trades_by_column(self, col_name):
        if getattr(self, "_trades_sort_col", None) == col_name:
            self._trades_sort_reverse = not getattr(self, "_trades_sort_reverse", False)
        else:
            self._trades_sort_col = col_name
            self._trades_sort_reverse = False
        self.apply_trades_filter()

    def show_import_export_options(self):
        self.show_custom_popup_menu(
            self.btn_io,
            [self._t("io_export_zip"), self._t("io_export_csv"), self._t("io_import_zip")],
            callback=self.handle_import_export_choice
        )

    def handle_import_export_choice(self, choice):
        action_map = {
            self._t("io_export_csv"): self.export_trades_csv,
            self._t("io_export_zip"): self.export_journal,
            self._t("io_import_zip"): self.import_journal,
        }
        handler = action_map.get(choice)
        if handler is not None:
            handler()
        elif ".csv" in str(choice):
            self.export_trades_csv()

    def export_trades_csv(self):
        file_path = filedialog.asksaveasfilename(
            title=self._t("dlg_csv_title"),
            defaultextension=".csv",
            filetypes=[(self._t("dlg_csv_type"), "*.csv")]
        )
        if not file_path:
            return

        records_snapshot = list(self._get_trades_view_records()) if self._get_trades_view_records() else []
        try:
            total = len(records_snapshot)
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "ID", "User", "Pair", "Timeframe", "Direction",
                    "Entry", "Stop Loss", "Take Profit", "R:R",
                    "Status", "Setup Type", "Checklist", "Date", "Notes"
                ])
                for idx, r in enumerate(records_snapshot):
                    writer.writerow(trade_to_csv_row(total - idx, r))
            messagebox.showinfo(self._t("dlg_csv_done_title"), self._t("dlg_csv_done_msg", total=total, path=file_path))
        except Exception as e:
            logger.error(f"CSV export error: {e}")
            messagebox.showerror(self._t("dlg_export_err_title"), self._t("dlg_csv_fail", err=e))

    def export_journal(self):
        import zipfile
        file_path = filedialog.asksaveasfilename(
            title=self._t("dlg_zip_export_title"),
            defaultextension=".zip",
            filetypes=[(self._t("dlg_zip_type"), "*.zip")]
        )
        if not file_path:
            return

        records_snapshot = list(self._get_trades_view_records()) if self._get_trades_view_records() else None
        username_scope = self.current_user or "admin"
        is_admin = self.current_role == "admin"

        def worker():
            try:
                records = records_snapshot if records_snapshot is not None else [
                    TradeRecord.from_row(row) for row in db.fetch_all_trades(username=username_scope)
                ]
                total_records = len(records)
                export_data = []
                user_filter = None if is_admin else username_scope

                missing_img_ids = [
                    r.id for r in records
                    if not (r.image_path and os.path.exists(r.image_path))
                ]
                bulk_img_map = {}
                if missing_img_ids:
                    if hasattr(db, "get_trade_images_bulk"):
                        bulk_img_map = db.get_trade_images_bulk(missing_img_ids, username=user_filter)
                    else:
                        for tid in missing_img_ids:
                            bulk_img_map[tid] = db.get_trade_image_data(tid, username=user_filter)

                missing_after_ids = [
                    r.id for r in records
                    if not (r.after_image_path and os.path.exists(r.after_image_path))
                ]
                bulk_after_map = {}
                if missing_after_ids and hasattr(db, "get_trade_after_images_bulk"):
                    bulk_after_map = db.get_trade_after_images_bulk(missing_after_ids, username=user_filter)

                with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for idx, r in enumerate(records):
                        display_id = total_records - idx
                        img_filename = f"chart_{display_id}.png"
                        has_img = False

                        if r.image_path and os.path.exists(r.image_path):
                            zipf.write(r.image_path, f"images/{img_filename}")
                            has_img = True
                        else:
                            b64_data = bulk_img_map.get(r.id, "")
                            pil_synced = decode_chart_thumbnail_b64(b64_data)
                            if pil_synced is not None:
                                img_filename = f"chart_{display_id}.jpg"
                                buf = io.BytesIO()
                                pil_synced.save(buf, format="JPEG", quality=85)
                                zipf.writestr(f"images/{img_filename}", buf.getvalue())
                                has_img = True

                        after_filename = f"after_chart_{display_id}.png"
                        has_after = False
                        if r.after_image_path and os.path.exists(r.after_image_path):
                            zipf.write(r.after_image_path, f"images/{after_filename}")
                            has_after = True
                        else:
                            after_b64 = bulk_after_map.get(r.id, "")
                            pil_after = decode_chart_thumbnail_b64(after_b64)
                            if pil_after is not None:
                                after_filename = f"after_chart_{display_id}.jpg"
                                buf = io.BytesIO()
                                pil_after.save(buf, format="JPEG", quality=85)
                                zipf.writestr(f"images/{after_filename}", buf.getvalue())
                                has_after = True

                        trade_dict = {
                            "display_id": display_id,
                            "pair": clean_pair(r.pair),
                            "timeframe": r.timeframe,
                            "direction": clean_direction(r.direction),
                            "entry_price": r.entry_price,
                            "stop_loss": r.stop_loss,
                            "take_profit": r.take_profit,
                            "rr_ratio": r.rr_ratio,
                            "status": clean_status(r.status),
                            "date": r.date,
                            "notes": r.notes,
                            "trade_type": r.trade_type,
                            "checklist_score": r.checklist_score,
                            "image_filename": img_filename if has_img else "",
                            "after_image_filename": after_filename if has_after else "",
                        }
                        export_data.append(trade_dict)

                    json_str = json.dumps(export_data, indent=2)
                    zipf.writestr("journal_trades.json", json_str)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _show_export_ok(count=len(export_data)):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        messagebox.showinfo(
                            self._t("dlg_export_done_title"),
                            self._t("dlg_export_done_msg", count=count, path=file_path)
                        )
                    self.after(0, _show_export_ok)
            except Exception as e:
                logger.error(f"Export error: {e}")
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _show_export_err(err=e):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        messagebox.showerror(self._t("dlg_export_err_title"), self._t("dlg_export_fail", err=err))
                    self.after(0, _show_export_err)

        threading.Thread(target=worker, daemon=True).start()

    def import_journal(self):
        import zipfile
        file_path = filedialog.askopenfilename(
            title=self._t("dlg_zip_import_title"),
            filetypes=[(self._t("dlg_zip_type"), "*.zip")]
        )
        if not file_path:
            return

        current_user = self.current_user or "admin"
        saved_images_dir = self.saved_images_dir

        def worker():
            MAX_ZIP_ENTRIES = 10000
            MAX_ENTRY_BYTES = 25 * 1024 * 1024
            MAX_TOTAL_BYTES = 250 * 1024 * 1024

            try:
                imported_count = 0
                with tempfile.TemporaryDirectory() as temp_dir:
                    real_temp_dir = os.path.realpath(temp_dir)
                    real_images_dir = os.path.realpath(os.path.join(temp_dir, "images"))

                    with zipfile.ZipFile(file_path, "r") as zipf:
                        infos = zipf.infolist()
                        if len(infos) > MAX_ZIP_ENTRIES:
                            raise ValueError("Archive contains too many entries.")

                        total_uncompressed = 0
                        for info in infos:
                            if info.is_dir():
                                continue
                            if info.file_size > MAX_ENTRY_BYTES:
                                raise ValueError(f"Archive entry exceeds maximum allowed size: {info.filename}")
                            total_uncompressed += info.file_size
                            if total_uncompressed > MAX_TOTAL_BYTES:
                                raise ValueError("Archive exceeds maximum total uncompressed size limit.")

                            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                                raise ValueError(f"Unsafe symlink entry in archive: {info.filename}")

                            norm_name = info.filename.replace("\\", "/")
                            if norm_name.startswith("/") or ":" in norm_name.split("/")[0]:
                                raise ValueError(f"Unsafe path in archive: {info.filename}")

                            target_path = os.path.realpath(os.path.join(real_temp_dir, *norm_name.split("/")))
                            if os.path.commonpath([real_temp_dir, target_path]) != real_temp_dir:
                                raise ValueError(f"Unsafe Zip Slip path detected: {info.filename}")

                            os.makedirs(os.path.dirname(target_path), exist_ok=True)
                            with zipf.open(info, "r") as src_f, open(target_path, "wb") as dst_f:
                                remaining = MAX_ENTRY_BYTES
                                while True:
                                    chunk = src_f.read(min(65536, remaining))
                                    if not chunk:
                                        break
                                    remaining -= len(chunk)
                                    dst_f.write(chunk)
                                if src_f.read(1):
                                    raise ValueError(f"Archive entry exceeded declared size: {info.filename}")

                    json_path = os.path.join(real_temp_dir, "journal_trades.json")
                    if not os.path.exists(json_path):
                        if not getattr(self, "_is_closing", False) and self.winfo_exists():
                            def _show_missing_json():
                                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                                    return
                                messagebox.showerror(self._t("dlg_import_err_title"), self._t("dlg_import_missing_json"))
                            self.after(0, _show_missing_json)
                        return

                    with open(json_path, "r", encoding="utf-8") as f:
                        trades_list = json.load(f)

                    if not isinstance(trades_list, list):
                        raise ValueError("Invalid 'journal_trades.json' format.")

                    trades_list.reverse()
                    existing_keys = db.fetch_trade_dedup_keys(current_user)
                    payloads = []
                    skipped_duplicates = 0

                    def _extract_archive_image(filename_val, prefix="chart"):
                        clean_name = os.path.basename(str(filename_val or "").strip())
                        if not clean_name or not os.path.isdir(real_images_dir):
                            return "", ""
                        extracted_img = os.path.realpath(os.path.join(real_images_dir, clean_name))
                        if os.path.commonpath([real_images_dir, extracted_img]) == real_images_dir and os.path.isfile(extracted_img):
                            unique_name = f"{prefix}_{int(time.time())}_{secrets.token_hex(4)}.png"
                            dest_path = os.path.join(saved_images_dir, unique_name)
                            shutil.copy(extracted_img, dest_path)
                            b64 = encode_chart_thumbnail_b64(dest_path)
                            return dest_path, b64
                        return "", ""

                    for item in trades_list:
                        if not isinstance(item, dict):
                            continue
                        clean_p = clean_pair(item.get("pair", ""))
                        clean_d = clean_direction(item.get("direction", ""))
                        raw_date = str(item.get("date") or "").strip()
                        dedup_key = (
                            clean_p.upper(),
                            clean_d.upper(),
                            str(item.get("entry_price") or "").strip(),
                            str(item.get("stop_loss") or "").strip(),
                            str(item.get("take_profit") or "").strip(),
                            utc_timestamp_to_local_short_date(raw_date),
                        )
                        if dedup_key in existing_keys:
                            skipped_duplicates += 1
                            continue
                        existing_keys.add(dedup_key)

                        dest_img_path, img_b64 = _extract_archive_image(item.get("image_filename", ""), prefix="chart")
                        dest_after_path, after_b64 = _extract_archive_image(item.get("after_image_filename", ""), prefix="after_chart")

                        payloads.append({
                            "pair": clean_p,
                            "timeframe": item.get("timeframe", ""),
                            "direction": clean_d,
                            "entry_price": item.get("entry_price", ""),
                            "stop_loss": item.get("stop_loss", ""),
                            "take_profit": item.get("take_profit", ""),
                            "rr_ratio": item.get("rr_ratio", ""),
                            "status": clean_status(item.get("status", "OPEN")),
                            "date": raw_date,
                            "notes": item.get("notes", ""),
                            "trade_type": item.get("trade_type", "HnS"),
                            "checklist_score": item.get("checklist_score", ""),
                            "image_path": dest_img_path,
                            "image_data": img_b64,
                            "after_image_path": dest_after_path,
                            "after_image_data": after_b64,
                            "username": current_user
                        })

                    if payloads:
                        if hasattr(db, "add_trades_bulk"):
                            res = db.add_trades_bulk(payloads)
                            if isinstance(res, tuple) and len(res) >= 2:
                                if isinstance(res[0], bool):
                                    if res[0]:
                                        imported_count = res[1] if isinstance(res[1], int) else len(payloads)
                                    else:
                                        raise RuntimeError(str(res[1]))
                                elif isinstance(res[0], int):
                                    imported_count = res[0]
                            elif isinstance(res, int):
                                imported_count = res
                        else:
                            for payload in payloads:
                                success, _ = db.add_trade(payload)
                                if success:
                                    imported_count += 1

                def on_done():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    self.load_database_records()
                    summary = self._t("dlg_import_done_msg", count=imported_count)
                    if skipped_duplicates > 0:
                        summary += self._t("dlg_import_skipped", skipped=skipped_duplicates)
                    messagebox.showinfo(self._t("dlg_import_done_title"), summary)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, on_done)
            except Exception as e:
                logger.error(f"Import error: {e}")
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _show_import_err(err=e):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        messagebox.showerror(self._t("dlg_import_err_title"), self._t("dlg_import_fail", err=err))
                    self.after(0, _show_import_err)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------ ANALYTICS ACTIONS ------------------ #
    def on_analytics_language_change(self, selected_lang=None):
        self.txt_analytics_log.delete("1.0", "end")
        self.txt_analytics_log.insert("end", tr(selected_lang or self.app_language, "analytics_intro"))

    def populate_analytics_dropdown(self):
        if not hasattr(self, "opt_analytics_trades"):
            return

        no_trades_lbl = self._t("analytics_no_trades")
        records = self.all_db_records
        records_ver = getattr(self, "_records_version", 0)
        if (
            getattr(self, "_last_analytics_version", None) == records_ver
            and (getattr(self, "analytics_trade_map", None) or (not records and self.opt_analytics_trades.get() == no_trades_lbl))
        ):
            return

        total_trades = len(records)
        self.analytics_trade_map = {}
        options = []

        for idx, r in enumerate(records):
            display_id = total_trades - idx
            pair = clean_pair(r.pair)
            direction = clean_direction(r.direction)
            status = clean_status(r.status)

            label = self._t(
                "analytics_trade_fmt",
                id=display_id,
                pair=pair,
                dir=direction,
                status=status,
                date=r.short_date,
            )
            options.append(label)
            self.analytics_trade_map[label] = r

        if options:
            self.opt_analytics_trades.update_options(options)
            self.opt_analytics_trades.set(options[0])
        else:
            self.opt_analytics_trades.update_options([no_trades_lbl])
            self.opt_analytics_trades.set(no_trades_lbl)

        self._last_analytics_version = records_ver

    def _resolve_trade_image_file(self, trade_id, local_path="", kind="before"):
        if local_path and os.path.exists(local_path):
            return local_path

        scope_user = None if self.current_role == "admin" else (self.current_user or "admin")

        if kind == "after":
            db_path = db.get_trade_after_image_path(trade_id, username=scope_user)
            if db_path and os.path.exists(db_path):
                return db_path

            cached_synced_path = os.path.join(self.saved_images_dir, f"synced_after_chart_{trade_id}.jpg")
            if os.path.exists(cached_synced_path):
                return cached_synced_path

            b64_data = db.get_trade_after_image_data(trade_id, username=scope_user)
            if b64_data:
                pil_img = decode_chart_thumbnail_b64(
                    b64_data,
                    cache_dir=self.saved_images_dir,
                    trade_id=trade_id,
                    filename_prefix="synced_after_chart_",
                )
                if pil_img is not None and os.path.exists(cached_synced_path):
                    return cached_synced_path
            return ""

        db_path = db.get_trade_image_path(trade_id, username=scope_user)
        if db_path and os.path.exists(db_path):
            return db_path

        cached_synced_path = os.path.join(self.saved_images_dir, f"synced_chart_{trade_id}.jpg")
        if os.path.exists(cached_synced_path):
            return cached_synced_path

        b64_data = db.get_trade_image_data(trade_id, username=scope_user)
        if b64_data:
            pil_img = decode_chart_thumbnail_b64(
                b64_data,
                cache_dir=self.saved_images_dir,
                trade_id=trade_id,
                filename_prefix="synced_chart_",
            )
            if pil_img is not None and os.path.exists(cached_synced_path):
                return cached_synced_path

        return ""

    def run_ai_trade_review(self):
        selected_label = self.opt_analytics_trades.get()
        if not selected_label or selected_label not in self.analytics_trade_map:
            messagebox.showwarning(self._t("dlg_analytics_warn"), self._t("dlg_analytics_select"))
            return

        trade_record = self.analytics_trade_map[selected_label]
        selected_lang = self.app_language

        self.btn_run_analytics.configure(text=self._t("btn_reviewing"), state="disabled", fg_color=pair_color("border_strong"))
        self.txt_analytics_log.delete("1.0", "end")

        loading_msg = self._t(
            "analytics_loading_review",
            lang=self._t("lang_name"),
            id=selected_label.split(" | ")[0],
            pair=clean_pair(trade_record.pair)
        )
        self.txt_analytics_log.insert("end", loading_msg)

        def worker():
            try:
                before_path = self._resolve_trade_image_file(trade_record.id, trade_record.image_path, kind="before")
                after_path = self._resolve_trade_image_file(trade_record.id, trade_record.after_image_path, kind="after")
                review_text = _get_analyzer().analyze_trade_performance(
                    image_path=before_path if before_path else "",
                    after_image_path=after_path if after_path else "",
                    pair=clean_pair(trade_record.pair),
                    direction=clean_direction(trade_record.direction),
                    entry=trade_record.entry_price,
                    sl=trade_record.stop_loss,
                    tp=trade_record.take_profit,
                    status=clean_status(trade_record.status),
                    trade_type=trade_record.trade_type or "N/A",
                    language=selected_lang,
                    timeframe=format_timeframe(trade_record.timeframe),
                    rr_ratio=trade_record.rr_ratio,
                    checklist_score=trade_record.checklist_score,
                    notes=trade_record.notes,
                )

                def update_ui():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    self._render_formatted_analytics_text(review_text)
                    self.btn_run_analytics.configure(text=self._t("btn_run_review"), state="normal", fg_color=pair_color("accent"))

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, update_ui)
            except Exception as err:
                clean_err = strip_markdown_links(err)

                def on_err(msg=clean_err):
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    self.btn_run_analytics.configure(text=self._t("btn_run_review"), state="normal", fg_color=pair_color("accent"))
                    messagebox.showerror(self._t("dlg_analytics_err"), msg)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, on_err)

        threading.Thread(target=worker, daemon=True).start()

    def run_ai_pasted_chart_review(self):
        img = grab_clipboard_pil_image(lang=self.app_language)
        if img is None:
            return

        try:
            pil_for_ai = img.copy()
            selected_lang = self.app_language

            self.btn_analytics_paste.configure(text=self._t("btn_analyzing"), state="disabled")
            self.txt_analytics_log.delete("1.0", "end")
            self.txt_analytics_log.insert("end", self._t("analytics_loading_paste", lang=self._t("lang_name")))

            def worker():
                try:
                    az = _get_analyzer()
                    parsed_data = {}
                    try:
                        parsed_data = az.analyze_chart("", pil_img=pil_for_ai)
                    except Exception as e:
                        logger.warning(f"Chart auto-parse error: {e}")

                    lang_prompt = (
                        "Rate this trade setup on a scale of 1 to 10 (e.g. ⭐ Overall Setup Rating: 8/10) "
                        "and explain why based on market structure, liquidity, entry quality, and risk management."
                        if selected_lang == "English"
                        else "Beoordeel deze trade setup op een schaal van 1 tot 10 (bijv. ⭐ Overall Setup Rating: 8/10) "
                        "en leg uit waarom op basis van marktstructuur, liquiditeit, entry-kwaliteit en risicobeheer."
                    )

                    review_text = az.analyze_trade_performance(
                        image_path="",
                        pil_img=pil_for_ai,
                        pair=clean_pair(parsed_data.get("pair", "")),
                        direction=clean_direction(parsed_data.get("direction", "")),
                        entry=parsed_data.get("entry_price", ""),
                        sl=parsed_data.get("stop_loss", ""),
                        tp=parsed_data.get("take_profit", ""),
                        status=clean_status(parsed_data.get("status", "OPEN")),
                        trade_type=f"{parsed_data.get('trade_type', 'N/A')} - {lang_prompt}",
                        language=selected_lang
                    )

                    def update_ui():
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self._render_formatted_analytics_text(review_text)
                        self.btn_analytics_paste.configure(text=self._t("btn_paste_rate"), state="normal")

                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.after(0, update_ui)
                except Exception as err:
                    clean_err = strip_markdown_links(err)

                    def on_err(msg=clean_err):
                        if getattr(self, "_is_closing", False) or not self.winfo_exists():
                            return
                        self.btn_analytics_paste.configure(text=self._t("btn_paste_rate"), state="normal")
                        messagebox.showerror(self._t("dlg_paste_review_err"), msg)

                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.after(0, on_err)

            threading.Thread(target=worker, daemon=True).start()

        except Exception as e:
            self.btn_analytics_paste.configure(text=self._t("btn_paste_rate"), state="normal")
            messagebox.showerror(self._t("dlg_paste_review_err"), self._t("dlg_paste_review_fail", err=e))

    # ------------------ CALCULATOR LOGIC & DROPDOWNS ------------------ #
    def toggle_risk_dropdown(self):
        self.show_custom_popup_menu(
            self.btn_risk_dropdown,
            [self._t("calc_risk_pct"), self._t("calc_risk_cash")],
            self.select_risk_mode
        )

    def select_risk_mode(self, mode):
        canon_mode = "Risk %" if mode in ("Risk %", self._t("calc_risk_pct")) else "Cash $"
        self.calc_risk_mode_var = canon_mode
        symbol = "% ▾" if canon_mode == "Risk %" else "$ ▾"
        self.btn_risk_dropdown.configure(text=symbol)
        self.close_dropdown()
        self.run_lot_calculator()
        self.save_calc_settings()

    def _get_quote_usd_conversion_pair(self, pair_str):
        base_ccy, quote_ccy = calc_svc.parse_fx_currencies(pair_str)
        if not quote_ccy or quote_ccy == "USD" or base_ccy in ("XAU", "BTC", "ETH"):
            return None, False, quote_ccy
        direct_pair = f"{quote_ccy}/USD"
        if tracker.resolve_tv_symbol(direct_pair):
            return direct_pair, False, quote_ccy
        inverse_pair = f"USD/{quote_ccy}"
        if tracker.resolve_tv_symbol(inverse_pair):
            return inverse_pair, True, quote_ccy
        return None, False, quote_ccy

    def _get_calc_quote_usd_rate(self, pair_str):
        conv_pair, _invert, quote_ccy = self._get_quote_usd_conversion_pair(pair_str)
        if not conv_pair:
            return None
        cached = self._fx_quote_usd_rates.get(quote_ccy)
        if cached is not None:
            rate, ts = cached
            if (time.time() - ts) < 600 and rate > 0:
                return rate
            return rate if rate > 0 else None
        return None

    def _ensure_calc_quote_rate_async(self, pair_str):
        conv_pair, invert, quote_ccy = self._get_quote_usd_conversion_pair(pair_str)
        if not conv_pair:
            return
        cached = self._fx_quote_usd_rates.get(quote_ccy)
        if cached is not None and (time.time() - cached[1]) < 300:
            return
        if quote_ccy in self._fx_rate_inflight:
            return
        self._fx_rate_inflight.add(quote_ccy)

        def fetch_rate_worker():
            try:
                _, _, conv_price = tracker.get_tradingview_prices(conv_pair)
                if conv_price and conv_price > 0:
                    usd_rate = (1.0 / float(conv_price)) if invert else float(conv_price)
                    self._fx_quote_usd_rates[quote_ccy] = (usd_rate, time.time())
                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.after(0, self.run_lot_calculator)
            except Exception as e:
                logger.warning(f"Quote conversion rate lookup failed for {conv_pair}: {e}")
            finally:
                self._fx_rate_inflight.discard(quote_ccy)

        threading.Thread(target=fetch_rate_worker, daemon=True).start()

    def select_pair_option(self, pair):
        self.calc_pair_entry.set(pair)
        self._ensure_calc_quote_rate_async(pair)
        self.fetch_live_entry_price()

    def fetch_live_entry_price(self):
        pair_text = self.calc_pair_entry.get().strip()
        if not tracker.resolve_tv_symbol(pair_text):
            return

        self.btn_live_price.configure(text="...")
        self._ensure_calc_quote_rate_async(pair_text)

        def run_fetch():
            _, _, price = tracker.get_tradingview_prices(pair_text)

            def update_ui():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                self.btn_live_price.configure(text=self._t("calc_live"))
                if price is not None:
                    self.calc_entry_price.delete(0, "end")
                    self.calc_entry_price.insert(0, f"{price}")
                    self.run_lot_calculator()

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, update_ui)

        threading.Thread(target=run_fetch, daemon=True).start()

    def run_lot_calculator(self):
        pair_val = self.calc_pair_entry.get()
        quote_to_usd_rate = self._get_calc_quote_usd_rate(pair_val)
        if quote_to_usd_rate is None:
            self._ensure_calc_quote_rate_async(pair_val)
        lots_text, cash_text, reward_text = calc_svc.calculate_lot_size(
            self.calc_balance_entry.get(),
            self.calc_risk_entry.get(),
            self.calc_risk_mode_var,
            pair_val,
            self.calc_entry_price.get(),
            self.calc_sl_price.get(),
            self.calc_tp_price.get() if hasattr(self, "calc_tp_price") else "",
            quote_to_usd_rate=quote_to_usd_rate,
        )
        self.calc_res_lots.configure(text=lots_text)
        self.calc_res_cash.configure(text=cash_text)
        if hasattr(self, "calc_res_reward"):
            self.calc_res_reward.configure(text=reward_text)

    def _match_calculator_pair_option(self, raw_pair):
        matched, new_options = calc_svc.match_calculator_pair_option(
            raw_pair,
            self.calc_pair_entry.options,
            clean_pair,
        )
        if new_options is not None:
            self.calc_pair_entry.update_options(new_options)
        return matched

    def reset_calculator_screenshot(self):
        self._calc_extract_seq = getattr(self, "_calc_extract_seq", 0) + 1
        self.calc_preview_image = None
        self.calc_img_preview.configure(image="", text=self._t("paste_placeholder"))
        self.btn_calc_paste.configure(text=self._t("btn_calc_paste"), state="normal", fg_color=pair_color("accent"))
        self.lbl_calc_extract_status.configure(text="")
        self.calc_entry_price.delete(0, "end")
        self.calc_sl_price.delete(0, "end")
        self.calc_tp_price.delete(0, "end")
        self.run_lot_calculator()

    def _process_calculator_screenshot(self, pil_img):
        self._calc_extract_seq = getattr(self, "_calc_extract_seq", 0) + 1
        req_seq = self._calc_extract_seq

        thumb = pil_img.copy()
        thumb.thumbnail((320, 74), Image.Resampling.BILINEAR)
        self.calc_preview_image = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
        self.calc_img_preview.configure(image=self.calc_preview_image, text="")
        pil_for_ai = pil_img.copy()

        self.btn_calc_paste.configure(text=self._t("btn_calc_extracting"), state="disabled", fg_color=pair_color("border_strong"))
        self.lbl_calc_extract_status.configure(text=self._t("calc_extracting_msg"), text_color=pair_color("text_secondary"))

        def worker():
            try:
                levels = _get_analyzer().extract_calculator_levels("", pil_img=pil_for_ai)

                def apply_levels():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists() or req_seq != self._calc_extract_seq:
                        return
                    self.btn_calc_paste.configure(text=self._t("btn_calc_paste"), state="normal", fg_color=pair_color("accent"))

                    matched_pair = self._match_calculator_pair_option(levels.get("pair", ""))
                    if matched_pair:
                        self.calc_pair_entry.set(matched_pair)
                        self._ensure_calc_quote_rate_async(matched_pair)

                    if levels.get("entry_price"):
                        self.calc_entry_price.delete(0, "end")
                        self.calc_entry_price.insert(0, levels["entry_price"])

                    if levels.get("stop_loss"):
                        self.calc_sl_price.delete(0, "end")
                        self.calc_sl_price.insert(0, levels["stop_loss"])

                    if levels.get("take_profit"):
                        self.calc_tp_price.delete(0, "end")
                        self.calc_tp_price.insert(0, levels["take_profit"])

                    self.run_lot_calculator()

                    extracted_parts = []
                    if matched_pair:
                        extracted_parts.append(matched_pair)
                    if levels.get("entry_price"):
                        extracted_parts.append("Entry")
                    if levels.get("stop_loss"):
                        extracted_parts.append("SL")
                    if levels.get("take_profit"):
                        extracted_parts.append("TP")

                    status_msg = (
                        self._t("calc_extracted_msg", parts=", ".join(extracted_parts))
                        if extracted_parts
                        else self._t("calc_extract_warn")
                    )
                    self.lbl_calc_extract_status.configure(
                        text=status_msg,
                        text_color=pair_color("accent") if extracted_parts else pair_color("amber")
                    )
                    self.after(
                        4500,
                        lambda: self.lbl_calc_extract_status.configure(text="") if not getattr(self, "_is_closing", False) and self.winfo_exists() and req_seq == self._calc_extract_seq else None
                    )

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, apply_levels)
            except Exception as err:
                clean_err = strip_markdown_links(err)

                def on_err(msg=clean_err):
                    if getattr(self, "_is_closing", False) or not self.winfo_exists() or req_seq != self._calc_extract_seq:
                        return
                    self.btn_calc_paste.configure(text=self._t("btn_calc_paste"), state="normal", fg_color=pair_color("accent"))
                    self.lbl_calc_extract_status.configure(text="")
                    messagebox.showerror(self._t("dlg_extract_err"), msg)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, on_err)

        threading.Thread(target=worker, daemon=True).start()

    def paste_calculator_screenshot(self):
        img = grab_clipboard_pil_image(lang=self.app_language)
        if img is not None:
            self._process_calculator_screenshot(img)

    def browse_calculator_screenshot(self):
        img = browse_pil_image(lang=self.app_language)
        if img is not None:
            self._process_calculator_screenshot(img)

    # ------------------ FORM & IMAGE ACTIONS ------------------ #
    def _process_chart_image(self, pil_img):
        self._chart_analyze_seq = getattr(self, "_chart_analyze_seq", 0) + 1
        req_seq = self._chart_analyze_seq
        old_path = self.selected_image_path

        target_path = os.path.join(
            tempfile.gettempdir(),
            f"pasted_chart_{int(time.time())}_{secrets.token_hex(4)}.png"
        )
        thumb = pil_img.copy()
        thumb.thumbnail((330, 125), Image.Resampling.BILINEAR)
        self.current_preview_image = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
        self.img_preview.configure(image=self.current_preview_image, text="")
        self.selected_image_path = target_path
        if old_path and old_path != target_path:
            self._cleanup_temp_image(old_path)

        pil_for_ai = pil_img.copy()
        pil_for_disk = pil_img.copy()

        self.btn_paste.configure(text=self._t("btn_analyzing_chart"), state="disabled", fg_color=pair_color("border_strong"))

        def run_analysis():
            part_path = f"{target_path}.part"

            def save_disk():
                try:
                    pil_for_disk.save(part_path, format="PNG", compress_level=1)
                    os.replace(part_path, target_path)
                except Exception as save_err:
                    logger.warning(f"Chart temp PNG save failed: {save_err}")
                    self._cleanup_temp_image(part_path)

            disk_thread = threading.Thread(target=save_disk, daemon=True)
            disk_thread.start()

            try:
                res = _get_analyzer().analyze_chart(target_path, pil_img=pil_for_ai)
                disk_thread.join(timeout=10)
                if getattr(self, "_is_closing", False) or req_seq != self._chart_analyze_seq:
                    self._cleanup_temp_image(target_path)
                    return

                def apply_result():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists() or req_seq != self._chart_analyze_seq:
                        return
                    self.populate_form(res)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, apply_result)
            except Exception as err:
                disk_thread.join(timeout=10)
                if getattr(self, "_is_closing", False) or req_seq != self._chart_analyze_seq:
                    self._cleanup_temp_image(target_path)
                    return
                clean_err = strip_markdown_links(err)

                def reset_on_err(msg=clean_err):
                    if getattr(self, "_is_closing", False) or not self.winfo_exists() or req_seq != self._chart_analyze_seq:
                        return
                    self.btn_paste.configure(text=self._t("btn_paste_chart"), state="normal", fg_color=pair_color("accent"))
                    messagebox.showerror(self._t("dlg_analysis_err"), msg)

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, reset_on_err)

        threading.Thread(target=run_analysis, daemon=True).start()

    def browse_chart_image(self):
        img = browse_pil_image(lang=self.app_language)
        if img is not None:
            self._process_chart_image(img)

    def paste_image(self):
        img = grab_clipboard_pil_image(lang=self.app_language)
        if img is not None:
            self._process_chart_image(img)

    def reset_analyzer_form(self):
        self._chart_analyze_seq = getattr(self, "_chart_analyze_seq", 0) + 1
        old_path = self.selected_image_path
        self.selected_image_path = None
        self.current_preview_image = None
        self._cleanup_temp_image(old_path)
        self.img_preview.configure(image="", text=self._t("paste_placeholder"))
        self.btn_paste.configure(text=self._t("btn_paste_chart"), state="normal", fg_color=pair_color("accent"))

        for entry in (
            self.entry_pair,
            self.entry_tf,
            self.entry_direction,
            self.entry_entry,
            self.entry_sl,
            self.entry_tp,
            self.entry_rr,
            self.entry_notes
        ):
            entry.delete(0, "end")

        self.opt_status.set("")
        self.opt_type.set("")
        self._refresh_analyzer_checklist_badge()

    def populate_form(self, data):
        for entry in (
            self.entry_pair,
            self.entry_tf,
            self.entry_direction,
            self.entry_entry,
            self.entry_sl,
            self.entry_tp,
            self.entry_rr,
            self.entry_notes
        ):
            entry.delete(0, "end")

        self.entry_pair.insert(0, clean_pair(data.get("pair", "")))
        self.entry_tf.insert(0, format_timeframe(data.get("timeframe", "")))
        self.entry_direction.insert(0, clean_direction(data.get("direction", "")))
        self.entry_entry.insert(0, data.get("entry_price", ""))
        self.entry_sl.insert(0, data.get("stop_loss", ""))
        self.entry_tp.insert(0, data.get("take_profit", ""))
        self.entry_rr.insert(0, data.get("rr_ratio", ""))

        self.opt_status.set("")
        self.opt_type.set("")
        self._refresh_analyzer_checklist_badge()

        self.btn_paste.configure(text=self._t("btn_paste_chart"), state="normal", fg_color=pair_color("accent"))

    def calculate_live_rr(self, event=None):
        rr_str = compute_rr_string(self.entry_entry.get(), self.entry_sl.get(), self.entry_tp.get())
        if rr_str:
            self.entry_rr.delete(0, "end")
            self.entry_rr.insert(0, rr_str)

    def save_trade(self):
        if not self.selected_image_path or not os.path.exists(self.selected_image_path):
            messagebox.showwarning(
                self._t("dlg_validation_err"),
                self._t("dlg_save_no_img")
            )
            return

        selected_type = self.opt_type.get()
        checklist_score = self._get_active_checklist_score()

        payload = {
            "pair": clean_pair(self.entry_pair.get()),
            "timeframe": self.entry_tf.get(),
            "direction": self.entry_direction.get(),
            "entry_price": self.entry_entry.get(),
            "stop_loss": self.entry_sl.get(),
            "take_profit": self.entry_tp.get(),
            "rr_ratio": self.entry_rr.get(),
            "status": clean_status(self.opt_status.get()),
            "notes": self.entry_notes.get(),
            "trade_type": selected_type,
            "checklist_score": checklist_score,
            "username": self.current_user or "admin"
        }

        src_path = self.selected_image_path
        unique_filename = f"chart_{int(time.time())}_{secrets.token_hex(4)}.png"
        dest = os.path.join(self.saved_images_dir, unique_filename)
        payload["image_path"] = dest

        self.btn_save.configure(state="disabled", text=self._t("btn_saving"))

        def worker():
            try:
                shutil.copy(src_path, dest)
                payload["image_data"] = encode_chart_thumbnail_b64(dest)
                success, msg = db.add_trade(payload)
                if not success and os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
            except Exception as e:
                success, msg = False, str(e)
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass

            def finish():
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                self.btn_save.configure(state="normal", text=self._t("btn_save_trade"))
                if success:
                    if selected_type:
                        self.checklist_scores.pop(selected_type, None)
                        self.checklist_item_states.pop(selected_type, None)
                        if (
                            "Checklist" in self._built_views
                            and hasattr(self, "opt_checklist_type")
                            and self.opt_checklist_type.winfo_exists()
                            and self.opt_checklist_type.get() == selected_type
                            and hasattr(self, "reset_checklist")
                        ):
                            self.reset_checklist()
                    self.reset_analyzer_form()
                    self.load_database_records()
                    self.lbl_save_status.configure(text=self._t("msg_trade_saved"))
                    self.after(3000, lambda: self.lbl_save_status.configure(text="") if not getattr(self, "_is_closing", False) and self.winfo_exists() else None)
                else:
                    messagebox.showerror(self._t("dlg_db_err"), self._t("dlg_db_save_fail", err=msg))

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _get_trades_view_records(self):
        if (
            self.current_role == "admin"
            and getattr(self, "trades_scope_filter", "My Trades") != "My Trades"
            and self.trades_scope_records is not None
        ):
            return self.trades_scope_records
        return self.all_db_records

    def _find_trade_record(self, trade_id):
        if self.trades_scope_records is not None and self.current_role == "admin":
            rec = next((r for r in self.trades_scope_records if r.id == trade_id), None)
            if rec is not None:
                return rec
        return next((r for r in self.all_db_records if r.id == trade_id), None)

    def on_trades_scope_filter_change(self, val):
        self.trades_scope_filter = val
        if val == "My Trades":
            self.trades_scope_records = None
            self.apply_trades_filter()
        else:
            self.load_trades_scope_records()

    def load_trades_scope_records(self):
        if self.current_role != "admin":
            self.trades_scope_records = None
            return
        scope_val = getattr(self, "trades_scope_filter", "My Trades")
        if scope_val == "My Trades":
            self.trades_scope_records = None
            self.apply_trades_filter()
            return

        if scope_val == "All Users":
            scope_user = "__ALL__"
        elif scope_val.startswith("User: "):
            scope_user = scope_val.split("User: ", 1)[1].strip().lower()
        else:
            scope_user = self.current_user or "admin"

        self._trades_scope_load_seq = getattr(self, "_trades_scope_load_seq", 0) + 1
        seq = self._trades_scope_load_seq

        def worker():
            try:
                self._db_ready_event.wait(timeout=15)
                raw_rows = db.fetch_all_trades(username=scope_user)
                records = [TradeRecord.from_row(row) for row in raw_rows]

                def apply_scoped():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    if seq != self._trades_scope_load_seq:
                        return
                    self.trades_scope_records = records
                    if self.current_active_view == "Trades":
                        self.apply_trades_filter()

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, apply_scoped)
            except Exception as e:
                logger.error(f"Error loading scoped trades records: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def load_database_records(self):
        with self._db_load_lock:
            if self._db_load_in_flight:
                self._db_reload_queued = True
                return
            self._db_load_in_flight = True

        personal_user = self.current_user or "admin"

        def worker():
            try:
                self._db_ready_event.wait(timeout=15)
                raw_rows = db.fetch_all_trades(username=personal_user)
                records = [TradeRecord.from_row(row) for row in raw_rows]
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _apply(r=records):
                        if not getattr(self, "_is_closing", False) and self.winfo_exists():
                            self._apply_loaded_records(r)
                    self.after(0, _apply)
            except Exception as e:
                logger.error(f"Error loading database records: {e}")
            finally:
                should_reload = False
                with self._db_load_lock:
                    self._db_load_in_flight = False
                    if self._db_reload_queued:
                        self._db_reload_queued = False
                        should_reload = True
                if should_reload and not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _trigger_reload():
                        if not getattr(self, "_is_closing", False) and self.winfo_exists():
                            self.load_database_records()
                    self.after(0, _trigger_reload)

        threading.Thread(target=worker, daemon=True).start()
        if self.current_role == "admin" and getattr(self, "trades_scope_filter", "My Trades") != "My Trades":
            self.load_trades_scope_records()

    def _apply_loaded_records(self, records):
        self.all_db_records = records
        self._records_version = getattr(self, "_records_version", 0) + 1
        total_trades = len(records)

        for item in self.tree_dash.get_children():
            self.tree_dash.delete(item)

        for idx, r in enumerate(records):
            display_id = total_trades - idx
            tf_val = format_timeframe(r.timeframe)
            status = clean_status(r.status)

            display_row = [
                display_id, clean_pair(r.pair), tf_val, clean_direction(r.direction),
                r.entry_price, r.stop_loss, r.take_profit, r.rr_ratio,
                status, r.trade_type, r.short_date
            ]

            row_tag = self._get_trade_row_tag(status, idx)
            self.tree_dash.insert("", "end", values=display_row, tags=(str(r.id), row_tag))

        if self.current_active_view == "Trades":
            self.apply_trades_filter()
        elif self.current_active_view == "Calendar":
            self.render_calendar()
        elif self.current_active_view == "CalendarDay" and self._selected_cal_day_date:
            self._populate_calendar_day_table(self._selected_cal_day_date)
        elif self.current_active_view == "Analytics":
            self.populate_analytics_dropdown()

        kpis = compute_dashboard_kpis(records)
        wr_txt = kpis["wr_txt"]
        rr_txt = kpis["rr_txt"]
        best_setup_str = kpis["best_setup_str"]
        wr_sub_txt = self._t(
            "kpi_winrate_sub",
            w=kpis["total_wins"],
            l=kpis["total_losses"],
            o=kpis["total_open"],
        )
        best_sub_txt = self._t("kpi_best_setup_sub", val=kpis["avg_chk_str"])

        if kpis["realized_r_count"] > 0:
            exp_r = kpis["exp_r"]
            realized_r = kpis["realized_r"]
            exp_str = f"{exp_r:+.2f}R"
            net_str = f"{realized_r:+.0f}R" if abs(realized_r) >= 100 else f"{realized_r:+.1f}R"
            rr_sub_txt = self._t("kpi_rr_sub", exp=exp_str, net=net_str)
        else:
            rr_sub_txt = self._t("kpi_rr_sub_empty")

        if self.card_winrate_val.cget("text") != wr_txt:
            self.card_winrate_val.configure(text=wr_txt)
        if self.card_winrate_sub.cget("text") != wr_sub_txt:
            self.card_winrate_sub.configure(text=wr_sub_txt)
        if self.card_rr_val.cget("text") != rr_txt:
            self.card_rr_val.configure(text=rr_txt)
        if self.card_rr_sub.cget("text") != rr_sub_txt:
            self.card_rr_sub.configure(text=rr_sub_txt)
        if self.card_best_trade_val.cget("text") != best_setup_str:
            self.card_best_trade_val.configure(text=best_setup_str)
        if self.card_best_trade_sub.cget("text") != best_sub_txt:
            self.card_best_trade_sub.configure(text=best_sub_txt)

    # ------------------ CONTEXT MENUS & TRADE DIALOGS ------------------ #
    def show_dash_context_menu(self, event, tree):
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            self.dash_context_menu.post(event.x_root, event.y_root)

    def show_trades_context_menu(self, event, tree):
        item = tree.identify_row(event.y)
        if item:
            current_selection = tree.selection()
            if item not in current_selection:
                tree.selection_set(item)
            self.trades_context_menu.post(event.x_root, event.y_root)

    def delete_selected(self):
        if self.current_active_view == "Trades":
            active_tree = self.tree_trades
        elif self.current_active_view == "CalendarDay" and hasattr(self, "tree_cal_day"):
            active_tree = self.tree_cal_day
        else:
            active_tree = self.tree_dash
        selected_items = active_tree.selection()
        if not selected_items:
            return

        trade_ids = []
        for sel in selected_items:
            item = active_tree.item(sel)
            tags = item.get("tags", [])
            raw_id = tags[0] if tags else item["values"][0]
            try:
                trade_ids.append(int(raw_id))
            except (ValueError, TypeError):
                continue

        if not trade_ids:
            return

        msg = (
            self._t("dlg_confirm_del_multi", n=len(trade_ids))
            if len(trade_ids) > 1
            else self._t("dlg_confirm_del_one")
        )

        scope_user = None if self.current_role == "admin" else (self.current_user or "admin")

        if messagebox.askyesno(self._t("dlg_confirm_del_title"), msg):
            def worker():
                db.delete_trades(trade_ids, username=scope_user)
                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    def _reload():
                        if not getattr(self, "_is_closing", False) and self.winfo_exists():
                            self.load_database_records()
                    self.after(0, _reload)

            threading.Thread(target=worker, daemon=True).start()

    def _cleanup_saved_after_files(self, trade_id, local_after_path=""):
        """Deletes orphaned local and cached synced After-chart files inside saved_images_dir."""
        saved_dir = os.path.realpath(self.saved_images_dir)
        candidates = []
        if local_after_path:
            candidates.append(local_after_path)
        if trade_id is not None:
            candidates.append(os.path.join(self.saved_images_dir, f"synced_after_chart_{trade_id}.jpg"))

        for cand in candidates:
            try:
                real_cand = os.path.realpath(cand)
                if os.path.commonpath([saved_dir, real_cand]) == saved_dir and os.path.isfile(real_cand):
                    os.remove(real_cand)
            except Exception as err:
                logger.warning(f"Failed cleaning up after-chart file '{cand}': {err}")

    def _save_after_pil_for_trade(self, trade_id, pil_img, scope_user):
        """Persists a PIL After-chart image locally + synced base64 DB column and returns the saved path."""
        unique_filename = f"after_chart_{int(time.time())}_{secrets.token_hex(4)}.png"
        dest = os.path.join(self.saved_images_dir, unique_filename)
        pil_img.save(dest, format="PNG", compress_level=1)
        b64 = encode_chart_thumbnail_b64(dest)
        ok = db.update_trade_after_image(trade_id, dest, b64, username=scope_user)
        if not ok:
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            return None
        return dest

    def _load_preview_pil(self, path, max_size=(1060, 620)):
        if not path or not os.path.exists(path):
            return None
        try:
            with Image.open(path) as raw_img:
                pil_img = raw_img.copy()
            pil_img.thumbnail(max_size, Image.Resampling.BILINEAR)
            return pil_img
        except Exception as err:
            logger.warning(f"Failed to load preview image '{path}': {err}")
            return None

    # ------------------ IN-APP EDIT TRADE ACTIONS ------------------ #
    def modify_selected_trade(self):
        if self.current_active_view == "Trades":
            active_tree = self.tree_trades
        elif self.current_active_view == "CalendarDay" and hasattr(self, "tree_cal_day"):
            active_tree = self.tree_cal_day
        else:
            active_tree = self.tree_dash
        selected = active_tree.selection()
        if not selected:
            return

        item = active_tree.item(selected[0])
        tags = item.get("tags", [])
        trade_id = int(tags[0]) if tags else int(item["values"][0])
        display_num = item["values"][0]

        record = self._find_trade_record(trade_id)
        if not record:
            return
        self._open_edit_trade_for_record(record, display_num, return_view=self.current_active_view)

    def open_edit_trade_from_preview(self):
        trade_id = self._preview_state.get("trade_id")
        if trade_id is None:
            return
        record = self._find_trade_record(trade_id)
        if not record:
            return
        display_num = self._preview_display_num or trade_id
        self._open_edit_trade_for_record(record, display_num, return_view="TradePreview")

    def _open_edit_trade_for_record(self, record, display_num, return_view=None):
        if return_view and return_view != "EditTrade":
            self._edit_trade_return_view = return_view
        elif self.current_active_view and self.current_active_view != "EditTrade":
            self._edit_trade_return_view = self.current_active_view

        scope_user = None if self.current_role == "admin" else (self.current_user or "admin")
        self._edit_after_load_seq += 1
        load_seq = self._edit_after_load_seq
        self._edit_after_preview_ctk = None

        existing_date_raw = str(record.date or "")
        existing_date_display = format_db_date_for_edit(existing_date_raw)

        self._edit_trade_state = {
            "trade_id": record.id,
            "display_num": display_num,
            "scope_user": scope_user,
            "existing_after_path": record.after_image_path or "",
            "existing_rr": str(record.rr_ratio or ""),
            "existing_date_raw": existing_date_raw,
            "existing_date_display": existing_date_display,
            "pil_after": None,
            "clear_after": False,
            "saving": False,
        }

        self.show_view("EditTrade")

        if hasattr(self, "lbl_edit_trade_title") and self.lbl_edit_trade_title.winfo_exists():
            self.lbl_edit_trade_title.configure(text=self._t("edit_title_fmt", num=display_num, pair=clean_pair(record.pair)))
        if hasattr(self, "btn_edit_trade_back") and self.btn_edit_trade_back.winfo_exists():
            back_labels = {
                "Dashboard": self._t("btn_back_dashboard"),
                "Trades": self._t("btn_back_trades"),
                "CalendarDay": self._t("btn_back_day"),
                "TradePreview": self._t("btn_back_preview"),
            }
            self.btn_edit_trade_back.configure(text=back_labels.get(self._edit_trade_return_view, self._t("btn_back")))

        val_map = {
            "Pair": clean_pair(record.pair),
            "Timeframe": record.timeframe or "",
            "Direction": clean_direction(record.direction),
            "Entry": record.entry_price or "",
            "SL": record.stop_loss or "",
            "TP": record.take_profit or "",
            "R:R": record.rr_ratio or "",
            "Date": existing_date_display,
            "Checklist": record.checklist_score or "",
            "Notes": record.notes or "",
        }
        for fname, ent in self.edit_trade_fields.items():
            ent.delete(0, "end")
            if val_map.get(fname):
                ent.insert(0, str(val_map[fname]))

        self.opt_edit_trade_status.set(clean_status(record.status))
        self.opt_edit_trade_type.set(str(record.trade_type or ""))
        self.lbl_edit_trade_status.configure(text="")
        self.btn_edit_trade_save.configure(state="normal", text=self._t("btn_save_changes"))

        has_after = bool(record.after_image_path)
        self.lbl_edit_after_status.configure(
            text=self._t("edit_after_attached") if has_after else self._t("edit_after_none"),
            text_color=pair_color("accent") if has_after else pair_color("text_secondary")
        )
        self.lbl_edit_after_preview.configure(
            image="",
            text=self._t("edit_after_loading") if has_after else self._t("edit_after_empty")
        )

        def load_after_worker():
            resolved = self._resolve_trade_image_file(record.id, record.after_image_path or "", kind="after")
            pil_img = self._load_preview_pil(resolved, max_size=(460, 320)) if resolved else None

            def finish():
                if getattr(self, "_is_closing", False) or not self.winfo_exists() or self._edit_after_load_seq != load_seq:
                    return
                if pil_img is not None:
                    self._edit_after_preview_ctk = ctk.CTkImage(
                        light_image=pil_img, dark_image=pil_img, size=pil_img.size
                    )
                    self.lbl_edit_after_preview.configure(image=self._edit_after_preview_ctk, text="")
                    self.lbl_edit_after_status.configure(text=self._t("edit_after_attached"), text_color=pair_color("accent"))
                else:
                    self._edit_after_preview_ctk = None
                    self.lbl_edit_after_preview.configure(
                        image="",
                        text=self._t("edit_after_empty")
                    )
                    self.lbl_edit_after_status.configure(text=self._t("edit_after_none"), text_color=pair_color("text_secondary"))

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=load_after_worker, daemon=True).start()

    def close_edit_trade_view(self):
        target = self._edit_trade_return_view or "Dashboard"
        if target not in self._views_map or target == "EditTrade":
            target = "Dashboard"
        self.show_view(target)

    def calculate_edit_live_rr(self, event=None):
        if "EditTrade" not in self._built_views:
            return
        rr_str = compute_rr_string(
            self.edit_trade_fields["Entry"].get(),
            self.edit_trade_fields["SL"].get(),
            self.edit_trade_fields["TP"].get(),
        )
        if rr_str:
            self.edit_trade_fields["R:R"].delete(0, "end")
            self.edit_trade_fields["R:R"].insert(0, rr_str)

    def edit_trade_pick_after(self, mode):
        if "EditTrade" not in self._built_views:
            return
        img = grab_clipboard_pil_image(lang=self.app_language) if mode == "paste" else browse_pil_image(lang=self.app_language)
        if img is None:
            return

        self._edit_trade_state["pil_after"] = img
        self._edit_trade_state["clear_after"] = False
        thumb = img.copy()
        thumb.thumbnail((460, 320), Image.Resampling.BILINEAR)
        self._edit_after_preview_ctk = ctk.CTkImage(light_image=thumb, dark_image=thumb, size=thumb.size)
        self.lbl_edit_after_preview.configure(image=self._edit_after_preview_ctk, text="")
        self.lbl_edit_after_status.configure(text=self._t("edit_after_new_ready"), text_color=pair_color("accent"))

    def edit_trade_clear_after(self):
        if "EditTrade" not in self._built_views:
            return
        self._edit_trade_state["pil_after"] = None
        self._edit_trade_state["clear_after"] = True
        self._edit_after_preview_ctk = None
        self.lbl_edit_after_preview.configure(
            image="",
            text=self._t("edit_after_removed_msg")
        )
        self.lbl_edit_after_status.configure(text=self._t("edit_after_will_remove"), text_color=pair_color("danger_text"))

    def save_edited_trade(self):
        state = self._edit_trade_state
        if state.get("saving") or state.get("trade_id") is None:
            return

        trade_id = state["trade_id"]
        scope_user = state["scope_user"]
        entry_val = self.edit_trade_fields["Entry"].get()
        sl_val = self.edit_trade_fields["SL"].get()
        tp_val = self.edit_trade_fields["TP"].get()

        raw_rr = self.edit_trade_fields["R:R"].get().strip()
        rr_ratio_str = raw_rr if raw_rr else compute_rr_string(entry_val, sl_val, tp_val)

        raw_date_input = self.edit_trade_fields["Date"].get().strip() if "Date" in self.edit_trade_fields else ""
        existing_date_display = state.get("existing_date_display", "")
        existing_date_raw = state.get("existing_date_raw", "")

        payload = {
            "pair": clean_pair(self.edit_trade_fields["Pair"].get()),
            "timeframe": self.edit_trade_fields["Timeframe"].get(),
            "direction": self.edit_trade_fields["Direction"].get(),
            "entry_price": entry_val,
            "stop_loss": sl_val,
            "take_profit": tp_val,
            "rr_ratio": rr_ratio_str,
            "status": clean_status(self.opt_edit_trade_status.get()),
            "trade_type": self.opt_edit_trade_type.get(),
            "checklist_score": self.edit_trade_fields["Checklist"].get().strip(),
            "notes": self.edit_trade_fields["Notes"].get(),
        }
        if "Date" in self.edit_trade_fields:
            if raw_date_input == existing_date_display and existing_date_raw:
                pass
            else:
                parsed_utc_date = parse_edit_date_to_utc(raw_date_input)
                if parsed_utc_date is None:
                    self.lbl_edit_trade_status.configure(
                        text=self._t("edit_err_date"),
                        text_color=pair_color("danger_text")
                    )
                    return
                payload["date"] = parsed_utc_date

        pil_after = state.get("pil_after")
        clear_after = bool(state.get("clear_after"))
        old_after_path = state.get("existing_after_path", "")

        state["saving"] = True
        self.btn_edit_trade_save.configure(state="disabled", text=self._t("btn_saving"))
        self.lbl_edit_trade_status.configure(text=self._t("admin_saving"), text_color=pair_color("text_secondary"))

        def worker():
            ok = db.update_trade(trade_id, payload, username=scope_user)
            new_after_path = old_after_path
            if ok:
                if pil_after is not None:
                    saved_path = self._save_after_pil_for_trade(trade_id, pil_after, scope_user)
                    if saved_path:
                        self._cleanup_saved_after_files(trade_id, old_after_path)
                        new_after_path = saved_path
                elif clear_after:
                    if db.update_trade_after_image(trade_id, "", "", username=scope_user):
                        self._cleanup_saved_after_files(trade_id, old_after_path)
                        new_after_path = ""

            def finish():
                state["saving"] = False
                if getattr(self, "_is_closing", False) or not self.winfo_exists():
                    return
                if hasattr(self, "btn_edit_trade_save") and self.btn_edit_trade_save.winfo_exists():
                    self.btn_edit_trade_save.configure(state="normal", text=self._t("btn_save_changes"))
                if ok:
                    self.load_database_records()
                    if self._edit_trade_return_view == "TradePreview" and self._preview_state.get("trade_id") == trade_id:
                        self._sync_preview_after_edit(trade_id, payload, new_after_path, pil_after, clear_after)
                    self.close_edit_trade_view()
                else:
                    self.lbl_edit_trade_status.configure(
                        text=self._t("edit_failed"),
                        text_color=pair_color("danger_text")
                    )

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_preview_after_edit(self, trade_id, payload, new_after_path, pil_after, clear_after):
        if "TradePreview" not in self._built_views:
            return
        display_num = self._preview_display_num or trade_id
        self._preview_state["status"] = clean_status(payload.get("status", "OPEN"))
        if hasattr(self, "lbl_preview_title") and self.lbl_preview_title.winfo_exists():
            self.lbl_preview_title.configure(text=self._t("preview_title_fmt", num=display_num, pair=payload.get("pair", "")))
        if hasattr(self, "lbl_preview_meta") and self.lbl_preview_meta.winfo_exists():
            meta_parts = [
                clean_direction(payload.get("direction", "")),
                format_timeframe(payload.get("timeframe", "")),
                self._t("preview_meta_status", status=self._preview_state["status"]),
                f"R:R {payload.get('rr_ratio', '')}" if payload.get("rr_ratio") else "",
                payload.get("trade_type", "") or "",
            ]
            self.lbl_preview_meta.configure(text="  •  ".join(p for p in meta_parts if p))

        if pil_after is not None:
            full_after = pil_after.copy()
            full_after.thumbnail((3200, 3200), Image.Resampling.BILINEAR)
            self._preview_state["after"] = full_after
        elif clear_after:
            self._preview_state["after"] = None
        self._render_preview_outcome_buttons()
        self._render_preview_tab()

    # ------------------ IN-APP TRADE CHART PREVIEW ------------------ #
    def on_trade_double_click(self, event):
        tree = event.widget
        selected = tree.selection()
        if not selected:
            return

        nav_list = []
        selected_iid = selected[0]
        selected_idx = -1
        for idx, iid in enumerate(tree.get_children()):
            row_item = tree.item(iid)
            row_tags = row_item.get("tags", [])
            row_vals = row_item.get("values", [])
            if not row_vals:
                continue
            try:
                tid = int(row_tags[0]) if row_tags else int(row_vals[0])
            except (ValueError, TypeError):
                continue
            d_num = row_vals[0]
            if iid == selected_iid:
                selected_idx = len(nav_list)
            nav_list.append((tid, d_num))

        item = tree.item(selected_iid)
        tags = item.get("tags", [])
        trade_id = int(tags[0]) if tags else int(item["values"][0])
        display_num = item["values"][0]
        fallback_pair = str(item["values"][1])

        self._preview_nav_list = nav_list if nav_list else [(trade_id, display_num)]
        self._preview_nav_idx = selected_idx if selected_idx >= 0 else 0

        if self.current_active_view and self.current_active_view != "TradePreview":
            self._preview_return_view = self.current_active_view

        self._open_trade_preview_by_id(trade_id, display_num, fallback_pair=fallback_pair)

    def navigate_preview_trade(self, delta):
        if not self._preview_nav_list:
            return
        new_idx = self._preview_nav_idx + delta
        if new_idx < 0 or new_idx >= len(self._preview_nav_list):
            return
        self._preview_nav_idx = new_idx
        trade_id, display_num = self._preview_nav_list[new_idx]
        self._open_trade_preview_by_id(trade_id, display_num)

    def _update_preview_nav_buttons(self):
        if "TradePreview" not in self._built_views:
            return
        total = len(self._preview_nav_list)
        idx = self._preview_nav_idx
        if hasattr(self, "btn_preview_prev") and self.btn_preview_prev.winfo_exists():
            self.btn_preview_prev.configure(state="normal" if idx > 0 else "disabled")
        if hasattr(self, "btn_preview_next") and self.btn_preview_next.winfo_exists():
            self.btn_preview_next.configure(state="normal" if (0 <= idx < total - 1) else "disabled")

    def _open_trade_preview_by_id(self, trade_id, display_num, fallback_pair=""):
        record = self._find_trade_record(trade_id)
        local_before = record.image_path if (record and record.image_path) else ""
        local_after = record.after_image_path if (record and record.after_image_path) else ""
        pair_label = clean_pair(record.pair) if record else fallback_pair
        status_label = clean_status(record.status) if record else "OPEN"
        scope_user = None if self.current_role == "admin" else (self.current_user or "admin")

        self._preview_display_num = display_num
        self._preview_request_seq += 1
        req_seq = self._preview_request_seq

        self._preview_state = {
            "trade_id": trade_id,
            "scope_user": scope_user,
            "status": status_label,
            "before": None,
            "after": None,
            "tk_photo": None,
            "canvas_img_id": None,
            "zoom": "fit",
            "tab": "before",
            "loading": True,
            "pan_x": 0.5,
            "pan_y": 0.5,
            "img_w": 0,
            "img_h": 0,
            "canvas_w": 0,
            "canvas_h": 0,
            "_last_canvas_size": None,
            "_pan_last": None,
        }

        self.show_view("TradePreview")
        self._update_preview_nav_buttons()
        self._render_preview_outcome_buttons()

        if hasattr(self, "lbl_preview_title") and self.lbl_preview_title.winfo_exists():
            self.lbl_preview_title.configure(text=self._t("preview_title_fmt", num=display_num, pair=pair_label))
        if hasattr(self, "lbl_preview_meta") and self.lbl_preview_meta.winfo_exists():
            if record:
                meta_parts = [
                    clean_direction(record.direction),
                    format_timeframe(record.timeframe),
                    self._t("preview_meta_status", status=status_label),
                    f"R:R {record.rr_ratio}" if record.rr_ratio else "",
                    record.trade_type or "",
                ]
                meta_text = "  •  ".join(p for p in meta_parts if p)
            else:
                meta_text = ""
            self.lbl_preview_meta.configure(text=meta_text)
        if hasattr(self, "btn_preview_back") and self.btn_preview_back.winfo_exists():
            back_labels = {
                "Dashboard": self._t("btn_back_dashboard"),
                "Trades": self._t("btn_back_trades"),
                "CalendarDay": self._t("btn_back_day"),
                "Calendar": self._t("btn_back_calendar"),
            }
            self.btn_preview_back.configure(text=back_labels.get(self._preview_return_view, self._t("btn_back")))

        self._render_preview_tab()

        def worker():
            before_path = self._resolve_trade_image_file(trade_id, local_before, kind="before")
            after_path = self._resolve_trade_image_file(trade_id, local_after, kind="after")
            pil_before = self._load_preview_pil(before_path, max_size=(3200, 3200))
            pil_after = self._load_preview_pil(after_path, max_size=(3200, 3200))

            def show_preview():
                if getattr(self, "_is_closing", False) or not self.winfo_exists() or self._preview_request_seq != req_seq:
                    return

                self._preview_state["before"] = pil_before
                self._preview_state["after"] = pil_after
                self._preview_state["loading"] = False
                self._preview_state["zoom"] = "fit"
                self._preview_state["pan_x"] = 0.5
                self._preview_state["pan_y"] = 0.5
                self._preview_state["tab"] = (
                    "before" if pil_before is not None else ("after" if pil_after is not None else "before")
                )
                self._render_preview_tab()

            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                self.after(0, show_preview)

        threading.Thread(target=worker, daemon=True).start()

    def close_trade_preview_view(self):
        target = self._preview_return_view or "Dashboard"
        if target not in self._views_map or target == "TradePreview":
            target = "Dashboard"
        self.show_view(target)

    def switch_preview_tab(self, tab_name):
        if "TradePreview" not in self._built_views:
            return
        self._preview_state["tab"] = tab_name
        self._preview_state["zoom"] = "fit"
        self._preview_state["pan_x"] = 0.5
        self._preview_state["pan_y"] = 0.5
        self._render_preview_tab()

    def _render_preview_outcome_buttons(self):
        if "TradePreview" not in self._built_views or not hasattr(self, "preview_status_buttons"):
            return
        curr = clean_status(self._preview_state.get("status", "OPEN"))
        for st_val, btn in self.preview_status_buttons.items():
            if not btn.winfo_exists():
                continue
            if st_val == curr:
                if st_val == "TP":
                    fg = pair_color("accent")
                    hov = pair_color("accent_hover")
                    tc = ("#ffffff", DARK_THEME["bg_root"])
                elif st_val == "SL":
                    fg = pair_color("danger")
                    hov = pair_color("danger_hover")
                    tc = "#ffffff"
                else:
                    fg = pair_color("bg_active_nav")
                    hov = pair_color("bg_hover")
                    tc = pair_color("accent")
                btn.configure(fg_color=fg, hover_color=hov, text_color=tc)
            else:
                btn.configure(
                    fg_color="transparent",
                    hover_color=pair_color("bg_hover"),
                    text_color=pair_color("text_secondary"),
                )

    def preview_set_quick_status(self, new_status):
        state = self._preview_state
        trade_id = state.get("trade_id")
        scope_user = state.get("scope_user")
        clean_st = clean_status(new_status)
        if trade_id is None or clean_st == state.get("status"):
            return

        state["status"] = clean_st
        self._render_preview_outcome_buttons()

        rec = self._find_trade_record(trade_id)
        if rec is not None:
            rec.status = clean_st
            if hasattr(self, "lbl_preview_meta") and self.lbl_preview_meta.winfo_exists():
                meta_parts = [
                    clean_direction(rec.direction),
                    format_timeframe(rec.timeframe),
                    self._t("preview_meta_status", status=clean_st),
                    f"R:R {rec.rr_ratio}" if rec.rr_ratio else "",
                    rec.trade_type or "",
                ]
                self.lbl_preview_meta.configure(text="  •  ".join(p for p in meta_parts if p))

        def worker():
            db.update_trade_status(trade_id, clean_st, username=scope_user)
            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                def _reload():
                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.load_database_records()
                self.after(0, _reload)

        threading.Thread(target=worker, daemon=True).start()

    def preview_adjust_zoom(self, delta):
        state = self._preview_state
        if state.get("loading"):
            return
        tab = state.get("tab", "before")
        if state.get(tab) is None:
            return
        curr_zoom = state.get("zoom", "fit")
        base = 1.0 if curr_zoom == "fit" else float(curr_zoom)
        new_zoom = round(max(1.0, min(4.0, base + delta)), 2)
        state["zoom"] = "fit" if new_zoom <= 1.01 else new_zoom
        if state["zoom"] == "fit":
            state["pan_x"] = 0.5
            state["pan_y"] = 0.5
        self._render_preview_tab()

    def preview_reset_zoom(self):
        if self._preview_state.get("zoom") != "fit":
            self._preview_state["zoom"] = "fit"
            self._preview_state["pan_x"] = 0.5
            self._preview_state["pan_y"] = 0.5
            self._render_preview_tab()

    def _on_preview_canvas_configure(self, event=None):
        if "TradePreview" not in self._built_views or self.current_active_view != "TradePreview":
            return
        if event is not None:
            new_size = (int(getattr(event, "width", 0)), int(getattr(event, "height", 0)))
            if new_size[0] <= 20 or new_size[1] <= 20:
                return
            if self._preview_state.get("_last_canvas_size") == new_size:
                return
        if self._preview_resize_after_id is not None:
            try:
                self.after_cancel(self._preview_resize_after_id)
            except Exception:
                pass
        self._preview_resize_after_id = self.after(80, self._render_preview_tab)

    def _on_preview_mousewheel(self, event):
        if event.num == 4 or (getattr(event, "delta", 0) > 0):
            self.preview_adjust_zoom(0.25)
        elif event.num == 5 or (getattr(event, "delta", 0) < 0):
            self.preview_adjust_zoom(-0.25)

    def _on_preview_pan_start(self, event):
        state = self._preview_state
        if state.get("zoom", "fit") != "fit":
            state["_pan_last"] = (event.x, event.y)
        else:
            state["_pan_last"] = None

    def _on_preview_pan_move(self, event):
        state = self._preview_state
        if state.get("zoom", "fit") == "fit" or not hasattr(self, "preview_canvas"):
            return
        img_id = state.get("canvas_img_id")
        if not img_id:
            return
        last_pt = state.get("_pan_last")
        if last_pt is None:
            state["_pan_last"] = (event.x, event.y)
            return

        dx = event.x - last_pt[0]
        dy = event.y - last_pt[1]
        state["_pan_last"] = (event.x, event.y)

        canvas = self.preview_canvas
        coords = canvas.coords(img_id)
        if len(coords) < 2:
            return

        cw = state.get("canvas_w") or canvas.winfo_width()
        ch = state.get("canvas_h") or canvas.winfo_height()
        tw = state.get("img_w") or 0
        th = state.get("img_h") or 0

        if tw > cw:
            min_x = cw - tw
            nx = max(min_x, min(0.0, coords[0] + dx))
            state["pan_x"] = (nx / float(min_x)) if min_x != 0 else 0.5
        else:
            nx = (cw - tw) // 2

        if th > ch:
            min_y = ch - th
            ny = max(min_y, min(0.0, coords[1] + dy))
            state["pan_y"] = (ny / float(min_y)) if min_y != 0 else 0.5
        else:
            ny = (ch - th) // 2

        canvas.coords(img_id, nx, ny)

    def _on_preview_canvas_double_click(self, event=None):
        if self._preview_state.get("zoom", "fit") == "fit":
            self.preview_adjust_zoom(1.0)
        else:
            self.preview_reset_zoom()

    def _render_preview_tab(self):
        self._preview_resize_after_id = None
        if "TradePreview" not in self._built_views or not hasattr(self, "preview_canvas"):
            return

        state = self._preview_state
        tab = state.get("tab", "before")
        zoom = state.get("zoom", "fit")

        if tab == "before":
            self.btn_preview_before.configure(
                fg_color=pair_color("accent"),
                hover_color=pair_color("accent_hover"),
                text_color=("#ffffff", DARK_THEME["bg_root"])
            )
            self.btn_preview_after.configure(
                fg_color=pair_color("bg_card_alt"),
                hover_color=pair_color("bg_hover"),
                text_color=pair_color("text_primary")
            )
        else:
            self.btn_preview_after.configure(
                fg_color=pair_color("accent"),
                hover_color=pair_color("accent_hover"),
                text_color=("#ffffff", DARK_THEME["bg_root"])
            )
            self.btn_preview_before.configure(
                fg_color=pair_color("bg_card_alt"),
                hover_color=pair_color("bg_hover"),
                text_color=pair_color("text_primary")
            )

        if hasattr(self, "btn_preview_zoom_reset") and self.btn_preview_zoom_reset.winfo_exists():
            self.btn_preview_zoom_reset.configure(
                text=self._t("btn_zoom_fit") if zoom == "fit" else f"{int(round(float(zoom) * 100))}%"
            )

        canvas = self.preview_canvas

        if state.get("loading"):
            canvas.delete("all")
            state["tk_photo"] = None
            state["canvas_img_id"] = None
            state["img_w"] = 0
            state["img_h"] = 0
            canvas.configure(cursor="")
            self.lbl_preview_empty_msg.configure(text=self._t("preview_loading"))
            self.lbl_preview_empty_msg.grid(row=0, column=0, padx=12, pady=12)
            self.lbl_preview_empty_msg.tkraise()
            return

        full_pil = state.get("before") if tab == "before" else state.get("after")
        if full_pil is None:
            canvas.delete("all")
            state["tk_photo"] = None
            state["canvas_img_id"] = None
            state["img_w"] = 0
            state["img_h"] = 0
            canvas.configure(cursor="")
            self.lbl_preview_empty_msg.configure(
                text=self._t("preview_empty_after") if tab == "after" else self._t("preview_empty_before")
            )
            self.lbl_preview_empty_msg.grid(row=0, column=0, padx=12, pady=12)
            self.lbl_preview_empty_msg.tkraise()
            return

        self.lbl_preview_empty_msg.grid_forget()
        raw_cw = canvas.winfo_width()
        raw_ch = canvas.winfo_height()
        if raw_cw <= 20 or raw_ch <= 20:
            canvas.update_idletasks()
            raw_cw = canvas.winfo_width()
            raw_ch = canvas.winfo_height()
        cw = raw_cw if raw_cw > 20 else 960
        ch = raw_ch if raw_ch > 20 else 520

        img_w, img_h = full_pil.size
        fit_scale = min((cw - 16) / float(img_w), (ch - 16) / float(img_h), 1.0)
        fit_scale = max(fit_scale, 0.1)

        if zoom == "fit":
            target_scale = fit_scale
        else:
            target_scale = fit_scale * float(zoom)

        target_w = max(1, int(round(img_w * target_scale)))
        target_h = max(1, int(round(img_h * target_scale)))
        resample = Image.Resampling.BILINEAR if zoom != "fit" else Image.Resampling.LANCZOS
        resized = full_pil.resize((target_w, target_h), resample) if (target_w, target_h) != (img_w, img_h) else full_pil

        photo = ImageTk.PhotoImage(resized)
        state["tk_photo"] = photo
        state["img_w"] = target_w
        state["img_h"] = target_h
        state["canvas_w"] = cw
        state["canvas_h"] = ch
        state["_last_canvas_size"] = (cw, ch)

        pan_x = max(0.0, min(1.0, float(state.get("pan_x", 0.5))))
        pan_y = max(0.0, min(1.0, float(state.get("pan_y", 0.5))))

        if target_w <= cw:
            pos_x = (cw - target_w) // 2
        else:
            pos_x = int(round((cw - target_w) * pan_x))

        if target_h <= ch:
            pos_y = (ch - target_h) // 2
        else:
            pos_y = int(round((ch - target_h) * pan_y))

        can_pan = (target_w > cw) or (target_h > ch)
        canvas.delete("all")
        canvas.configure(cursor="fleur" if can_pan else "hand2")
        state["canvas_img_id"] = canvas.create_image(pos_x, pos_y, anchor="nw", image=photo)

    def preview_attach_after(self, mode):
        state = self._preview_state
        trade_id = state.get("trade_id")
        scope_user = state.get("scope_user")
        if trade_id is None or "TradePreview" not in self._built_views:
            return

        img = grab_clipboard_pil_image(lang=self.app_language) if mode == "paste" else browse_pil_image(lang=self.app_language)
        if img is None:
            return

        full_after = img.copy()
        full_after.thumbnail((3200, 3200), Image.Resampling.BILINEAR)
        state["after"] = full_after
        state["loading"] = False
        state["zoom"] = "fit"
        state["pan_x"] = 0.5
        state["pan_y"] = 0.5
        state["tab"] = "after"
        self._render_preview_tab()

        rec = self._find_trade_record(trade_id)
        old_after_path = rec.after_image_path if rec else ""

        def save_worker():
            saved_path = self._save_after_pil_for_trade(trade_id, img, scope_user)
            if saved_path and old_after_path and old_after_path != saved_path:
                self._cleanup_saved_after_files(trade_id, old_after_path)
            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                def _reload():
                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.load_database_records()
                self.after(0, _reload)

        threading.Thread(target=save_worker, daemon=True).start()

    def preview_remove_after(self):
        state = self._preview_state
        trade_id = state.get("trade_id")
        scope_user = state.get("scope_user")
        if trade_id is None or state.get("after") is None:
            return

        rec = self._find_trade_record(trade_id)
        old_after_path = rec.after_image_path if rec else ""

        state["after"] = None
        state["zoom"] = "fit"
        self._render_preview_tab()

        def clear_worker():
            if db.update_trade_after_image(trade_id, "", "", username=scope_user):
                self._cleanup_saved_after_files(trade_id, old_after_path)
            if not getattr(self, "_is_closing", False) and self.winfo_exists():
                def _reload():
                    if not getattr(self, "_is_closing", False) and self.winfo_exists():
                        self.load_database_records()
                self.after(0, _reload)

        threading.Thread(target=clear_worker, daemon=True).start()



if __name__ == "__main__":
    app = TradingJournalApp()
    app.mainloop()
