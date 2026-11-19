import os
import re
import calendar
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image

from auth import get_all_users
from theme import (
    THEME,
    DARK_THEME,
    pair_color,
    resource_path,
    logger,
    get_app_tzinfo,
    get_app_timezone_pref,
    get_timezone_dropdown_options,
)
from widgets import (
    FOREX_PAIRS_LIST,
    PARSED_FOREX_PAIRS,
    TRADE_TYPE_CHECKLISTS,
    TRADES_COL_INDEX_MAP,
    get_trade_checklist,
    tr,
    CustomDropdownButton,
    clean_pair,
    clean_direction,
    clean_status,
    format_timeframe,
    filter_and_sort_trades,
)
import news as news_svc
import calculator as calc_svc
import updater as updater_svc


class AppViewsMixin:
    """View and panel builder mixin extracted from TradingJournalApp to keep UI layout modular."""

    # ------------------ SIDEBAR & NAVIGATION ------------------ #
    def build_sidebar(self):
        sidebar = ctk.CTkFrame(
            self,
            width=230,
            fg_color=pair_color("bg_sidebar"),
            corner_radius=0,
            border_width=0
        )
        self.sidebar_frame = sidebar
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        border_line = ctk.CTkFrame(sidebar, width=1, fg_color=pair_color("border"), corner_radius=0)
        border_line.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        brand_frame = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand_frame.pack(anchor="w", padx=18, pady=(22, 12))

        icon_file = resource_path("icon.ico")
        if os.path.exists(icon_file):
            try:
                pil_icon = Image.open(icon_file)
                self.brand_icon_img = ctk.CTkImage(light_image=pil_icon, dark_image=pil_icon, size=(26, 26))
                lbl_icon = ctk.CTkLabel(brand_frame, image=self.brand_icon_img, text="")
                lbl_icon.pack(side="left", padx=(0, 10))
            except Exception as e:
                logger.warning(f"Error loading brand icon: {e}")

        brand_lbl = ctk.CTkLabel(
            brand_frame,
            text="K's Trade Journal",
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color=pair_color("accent")
        )
        brand_lbl.pack(side="left")

        profile_pill = ctk.CTkFrame(
            sidebar,
            fg_color=pair_color("bg_card"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border")
        )
        profile_pill.pack(fill="x", padx=14, pady=(0, 18))

        self.lbl_user_profile = ctk.CTkLabel(
            profile_pill,
            text=f"●  {self._t('guest')}",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_user_profile.pack(fill="x", padx=12, pady=7)

        nav_items = [
            ("Dashboard", self._t("nav_dashboard")),
            ("Analytics", self._t("nav_analytics")),
            ("News", self._t("nav_news")),
            ("Checklist", self._t("nav_checklist")),
            ("Trades", self._t("nav_trades")),
            ("Calendar", self._t("nav_calendar")),
            ("Calculator", self._t("nav_calculator")),
        ]

        self._nav_font_active = ctk.CTkFont(size=13, weight="bold")
        self._nav_font_normal = ctk.CTkFont(size=13, weight="normal")

        for key, label_text in nav_items:
            nav_row = ctk.CTkFrame(sidebar, fg_color="transparent", height=40)
            nav_row.pack(fill="x", padx=12, pady=2)

            indicator = ctk.CTkFrame(nav_row, width=3, height=22, fg_color="transparent", corner_radius=2)
            indicator.place(x=2, rely=0.5, anchor="w")

            btn = ctk.CTkButton(
                nav_row,
                text=label_text,
                anchor="w",
                fg_color="transparent",
                hover_color=pair_color("bg_hover"),
                text_color=pair_color("text_secondary"),
                height=38,
                corner_radius=8,
                font=self._nav_font_normal,
                command=lambda name=key: self.show_view(name)
            )
            btn.pack(fill="x", padx=(8, 0))
            self.nav_buttons[key] = btn
            self.nav_indicators[key] = indicator

        self.sidebar_footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        self.sidebar_footer.pack(side="bottom", fill="x")

        spacer = ctk.CTkFrame(sidebar, fg_color="transparent")
        spacer.pack(fill="both", expand=True)

        self.btn_admin_users = ctk.CTkButton(
            self.sidebar_footer,
            text=self._t("btn_manage_users"),
            anchor="w",
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("accent"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.open_admin_panel
        )

        self.btn_settings = ctk.CTkButton(
            self.sidebar_footer,
            text=self._t("btn_settings"),
            anchor="w",
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.show_view("Settings")
        )
        self.btn_settings.pack(fill="x", padx=12, pady=(0, 6))

        self.btn_logout = ctk.CTkButton(
            self.sidebar_footer,
            text=self._t("btn_logout"),
            anchor="w",
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("danger_text"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.logout_user
        )
        self.btn_logout.pack(fill="x", padx=12, pady=(0, 8))

        self.lbl_ver = ctk.CTkLabel(
            self.sidebar_footer,
            text=self._t("sidebar_version", ver=updater_svc.CURRENT_VERSION),
            font=ctk.CTkFont(size=10),
            text_color=pair_color("text_muted"),
            anchor="center"
        )
        self.lbl_ver.pack(fill="x", padx=14, pady=(0, 12))

    # ------------------ DASHBOARD VIEW ------------------ #
    def build_dashboard_view(self):
        self.view_dashboard.grid_rowconfigure(1, weight=1)
        self.view_dashboard.grid_columnconfigure(0, weight=1)

        kpi_frame = ctk.CTkFrame(self.view_dashboard, fg_color="transparent")
        kpi_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        kpi_frame.grid_columnconfigure(0, weight=31)
        kpi_frame.grid_columnconfigure(1, weight=0)
        kpi_frame.grid_columnconfigure(2, weight=33)
        kpi_frame.grid_columnconfigure(3, weight=36)
        kpi_frame.grid_rowconfigure(0, weight=1)

        stats_col = ctk.CTkFrame(kpi_frame, fg_color="transparent")
        stats_col.grid(row=0, column=0, padx=(0, 5), sticky="nsew")
        stats_col.grid_rowconfigure(0, weight=1, uniform="kpi_stats_row")
        stats_col.grid_rowconfigure(1, weight=1, uniform="kpi_stats_row")
        stats_col.grid_columnconfigure(0, weight=1, uniform="kpi_stats_col")
        stats_col.grid_columnconfigure(1, weight=1, uniform="kpi_stats_col")

        self.card_best_trade_val, self.card_best_trade_sub = self.create_kpi_card_stacked(
            stats_col, 0, self._t("kpi_best_setup"), "N/A", pair_color("cyan"),
            col_idx=0, colspan=2, initial_sub=self._t("kpi_best_setup_sub", val="—")
        )
        self.card_winrate_val, self.card_winrate_sub = self.create_kpi_card_stacked(
            stats_col, 1, self._t("kpi_winrate"), "0.0%", pair_color("accent"),
            col_idx=0, colspan=1, initial_sub=self._t("kpi_winrate_sub", w=0, l=0, o=0)
        )
        self.card_rr_val, self.card_rr_sub = self.create_kpi_card_stacked(
            stats_col, 1, self._t("kpi_avg_rr"), "1:0.00", pair_color("purple"),
            col_idx=1, colspan=1, initial_sub=self._t("kpi_rr_sub_empty")
        )

        self.create_active_sessions_kpi_card(kpi_frame, 1)
        self.card_news_title, self.card_news_container = self.create_news_kpi_card(kpi_frame, 2)
        self.card_no_news_title, self.card_no_news_container = self.create_no_news_kpi_card(kpi_frame, 3)

        body_frame = ctk.CTkFrame(self.view_dashboard, fg_color="transparent")
        body_frame.grid(row=1, column=0, sticky="nsew")

        body_frame.grid_columnconfigure(0, weight=3)
        body_frame.grid_columnconfigure(1, weight=7)
        body_frame.grid_rowconfigure(0, weight=1)

        self.build_analyzer_panel(body_frame)
        self.build_dashboard_table_panel(body_frame)

    def create_kpi_card_stacked(self, parent, row_idx, title, initial_val, color, col_idx=0, colspan=1, initial_sub=""):
        card = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_card"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        pady_tuple = (0, 2) if row_idx == 0 else (2, 0)
        if colspan > 1:
            padx_tuple = (0, 0)
        elif col_idx == 0:
            padx_tuple = (0, 3)
        else:
            padx_tuple = (3, 0)
        card.grid(row=row_idx, column=col_idx, columnspan=colspan, padx=padx_tuple, pady=pady_tuple, sticky="nsew")

        card.grid_rowconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)
        card.grid_rowconfigure(2, weight=1)
        card.grid_columnconfigure(0, weight=1)

        lbl_t = ctk.CTkLabel(
            card,
            text=title,
            height=16,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="center"
        )
        lbl_t.grid(row=0, column=0, sticky="s", padx=6, pady=(5, 0))

        lbl_v = ctk.CTkLabel(
            card,
            text=initial_val,
            height=22,
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=color,
            anchor="center"
        )
        lbl_v.grid(row=1, column=0, sticky="ew", padx=6, pady=0)

        lbl_sub = ctk.CTkLabel(
            card,
            text=initial_sub,
            height=15,
            font=ctk.CTkFont(size=10),
            text_color=pair_color("text_muted"),
            anchor="center"
        )
        lbl_sub.grid(row=2, column=0, sticky="n", padx=6, pady=(0, 5))
        return lbl_v, lbl_sub

    def create_news_kpi_card(self, parent, col):
        card = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_card"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        card.grid(row=0, column=col, padx=5, sticky="nsew")

        lbl_t = ctk.CTkLabel(
            card,
            text=self._t("kpi_upcoming_news"),
            height=22,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="center"
        )
        lbl_t.pack(anchor="center", padx=10, pady=(6, 2))

        news_container = ctk.CTkFrame(card, fg_color="transparent")
        news_container.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        lbl_none = ctk.CTkLabel(news_container, text=self._t("no_news_today"), height=24, font=ctk.CTkFont(size=11), text_color=pair_color("text_secondary"))
        lbl_none.pack(expand=True)

        return lbl_t, news_container

    def create_no_news_kpi_card(self, parent, col):
        card = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_card"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        card.grid(row=0, column=col, padx=(5, 0), sticky="nsew")

        lbl_t = ctk.CTkLabel(
            card,
            text=self._t("kpi_clear_pairs"),
            height=22,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("accent"),
            anchor="center"
        )
        lbl_t.pack(anchor="center", padx=6, pady=(6, 2))

        no_news_container = ctk.CTkFrame(card, fg_color="transparent")
        no_news_container.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        lbl_none = ctk.CTkLabel(no_news_container, text=self._t("calculating"), height=24, font=ctk.CTkFont(size=11), text_color=pair_color("text_secondary"))
        lbl_none.pack(expand=True)

        return lbl_t, no_news_container

    def create_active_sessions_kpi_card(self, parent, col):
        card = ctk.CTkFrame(
            parent,
            width=148,
            height=140,
            fg_color=pair_color("bg_card"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        card.pack_propagate(False)
        card.grid(row=0, column=col, padx=5, sticky="ns")
        self.card_active_sessions = card

        lbl_t = ctk.CTkLabel(
            card,
            text=self._t("kpi_active_sessions"),
            height=22,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="center"
        )
        lbl_t.pack(anchor="center", padx=10, pady=(6, 2))

        sessions_container = ctk.CTkFrame(card, fg_color="transparent")
        sessions_container.pack(anchor="center", fill="y", expand=True, padx=14, pady=(0, 6))

        self._session_font_active = ctk.CTkFont(size=11, weight="bold")
        self._session_font_inactive = ctk.CTkFont(size=11, weight="normal")
        dot_font = ctk.CTkFont(size=10, weight="bold")

        session_defs = (
            ("Sydney", 21, 6),
            ("Tokyo", 0, 9),
            ("London", 8, 17),
            ("New York", 13, 22),
        )

        self._session_row_widgets = []
        sessions_container.grid_columnconfigure(0, weight=1)
        for idx, (name, start_h, end_h) in enumerate(session_defs):
            sessions_container.grid_rowconfigure(idx, weight=1)
            row = ctk.CTkFrame(sessions_container, fg_color="transparent")
            row.grid(row=idx, column=0, sticky="w")

            dot = ctk.CTkLabel(
                row,
                text="●",
                height=26,
                font=dot_font,
                text_color=pair_color("text_muted"),
                width=12
            )
            dot.pack(side="left", padx=(0, 6))

            lbl_name = ctk.CTkLabel(
                row,
                text=name,
                height=26,
                font=self._session_font_inactive,
                text_color=pair_color("text_secondary"),
                anchor="w"
            )
            lbl_name.pack(side="left")

            self._session_row_widgets.append({
                "name": name,
                "start_h": start_h,
                "end_h": end_h,
                "dot": dot,
                "lbl_name": lbl_name,
            })

        self._last_sessions_sig = None
        self.update_active_sessions_widget()

    def update_active_sessions_widget(self):
        if not hasattr(self, "_session_row_widgets") or not self._session_row_widgets:
            return
        card = getattr(self, "card_active_sessions", None)
        if card is not None and not card.winfo_exists():
            return

        now_utc = datetime.now(timezone.utc)
        hour_utc = now_utc.hour

        active_flags = []
        for item in self._session_row_widgets:
            s_h = item["start_h"]
            e_h = item["end_h"]
            is_active = (hour_utc >= s_h or hour_utc < e_h) if s_h > e_h else (s_h <= hour_utc < e_h)
            active_flags.append(is_active)

        sig = tuple(active_flags)
        if sig == getattr(self, "_last_sessions_sig", None):
            return
        self._last_sessions_sig = sig

        for item, is_active in zip(self._session_row_widgets, active_flags):
            if not item["dot"].winfo_exists():
                continue
            if is_active:
                item["dot"].configure(text_color=pair_color("accent"))
                item["lbl_name"].configure(
                    text_color=pair_color("text_primary"),
                    font=self._session_font_active
                )
            else:
                item["dot"].configure(text_color=pair_color("text_muted"))
                item["lbl_name"].configure(
                    text_color=pair_color("text_secondary"),
                    font=self._session_font_inactive
                )

    def update_dashboard_news_widget(self, upcoming_events):
        first_3 = upcoming_events[:3] if upcoming_events else []
        new_sig = tuple(
            (ev.get("time", ""), ev.get("currency", ""), ev.get("title", ""), str(ev.get("impact", "")))
            for ev in first_3
        )
        if new_sig == self._last_news_widget_sig:
            return
        self._last_news_widget_sig = new_sig

        for widget in self.card_news_container.winfo_children():
            widget.destroy()

        if first_3:
            self.card_news_title.configure(text_color=pair_color("danger_text"))
            if not hasattr(self, "_news_dot_font"):
                self._news_dot_font = ctk.CTkFont(size=10, weight="bold")
            if not hasattr(self, "_news_txt_font"):
                self._news_txt_font = ctk.CTkFont(size=11)
            dot_font = self._news_dot_font
            txt_font = self._news_txt_font
            for ev in first_3:
                row = ctk.CTkFrame(self.card_news_container, fg_color="transparent")
                row.pack(anchor="center", pady=1)

                impact_str = str(ev.get("impact", "")).capitalize()
                if "High" in impact_str or impact_str in ["Red", "3"]:
                    dot_color = "#ff3b30"
                elif "Medium" in impact_str or impact_str in ["Orange", "2"]:
                    dot_color = "#ff9500"
                elif "Low" in impact_str or impact_str in ["Yellow", "1"]:
                    dot_color = "#ffff33"
                else:
                    dot_color = pair_color("text_secondary")

                dot = ctk.CTkLabel(row, text="●", height=25, text_color=dot_color, font=dot_font)
                dot.pack(side="left", padx=(0, 5))

                time_str = ev.get("time", "N/A")
                curr_str = ev.get("currency", "")
                title_str = ev.get("title", "")

                text_lbl = ctk.CTkLabel(
                    row,
                    text=f"{time_str}  {curr_str}  {title_str}",
                    height=25,
                    font=txt_font,
                    text_color=pair_color("text_primary"),
                    anchor="center"
                )
                text_lbl.pack(side="left")
        else:
            self.card_news_title.configure(text_color=pair_color("text_secondary"))
            if not hasattr(self, "_news_txt_font"):
                self._news_txt_font = ctk.CTkFont(size=11)
            lbl_none = ctk.CTkLabel(self.card_news_container, text=self._t("no_upcoming_news"), height=24, font=self._news_txt_font, text_color=pair_color("text_secondary"))
            lbl_none.pack(expand=True, pady=4)

    def update_dashboard_no_news_widget(self, today_events):
        display_pairs = news_svc.get_clear_pairs(today_events, PARSED_FOREX_PAIRS)

        if display_pairs == self._last_no_news_widget_sig:
            return
        self._last_no_news_widget_sig = display_pairs

        for widget in self.card_no_news_container.winfo_children():
            widget.destroy()

        if display_pairs:
            if not hasattr(self, "_no_news_pair_font"):
                self._no_news_pair_font = ctk.CTkFont(size=10)
            pair_font = self._no_news_pair_font
            for c in range(5):
                self.card_no_news_container.grid_columnconfigure(c, weight=1)
            for r in range(4):
                self.card_no_news_container.grid_rowconfigure(r, weight=1)

            for idx, pair_str in enumerate(display_pairs):
                r = idx // 5
                c = idx % 5
                lbl = ctk.CTkLabel(
                    self.card_no_news_container,
                    text=pair_str,
                    height=26,
                    font=pair_font,
                    text_color=pair_color("text_primary"),
                    anchor="center"
                )
                lbl.grid(row=r, column=c, padx=1, pady=0, sticky="nsew")
        else:
            if not hasattr(self, "_news_txt_font"):
                self._news_txt_font = ctk.CTkFont(size=11)
            lbl_none = ctk.CTkLabel(self.card_no_news_container, text=self._t("no_clear_pairs_today"), height=24, font=self._news_txt_font, text_color=pair_color("text_secondary"))
            lbl_none.pack(expand=True, pady=4)

    def build_analyzer_panel(self, parent):
        panel = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        panel.grid(row=0, column=0, padx=(0, 6), sticky="nsew")

        center_wrap = ctk.CTkFrame(panel, fg_color="transparent")
        center_wrap.pack(fill="both", expand=True, padx=14, pady=14)

        btn_frame = ctk.CTkFrame(center_wrap, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(0, 8))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=0)

        self.btn_paste = ctk.CTkButton(
            btn_frame,
            text=self._t("btn_paste_chart"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8,
            height=34,
            command=self.paste_image
        )
        self.btn_paste.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.btn_clear = ctk.CTkButton(
            btn_frame,
            text="✕",
            width=40,
            height=34,
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(weight="bold", size=14),
            corner_radius=8,
            command=self.reset_analyzer_form
        )
        self.btn_clear.grid(row=0, column=1, sticky="e")

        preview_box = ctk.CTkFrame(
            center_wrap,
            fg_color=pair_color("bg_input"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border_strong"),
            height=135
        )
        preview_box.pack(fill="x", pady=6)
        preview_box.pack_propagate(False)

        self.img_preview = ctk.CTkLabel(
            preview_box,
            text=self._t("paste_placeholder"),
            fg_color="transparent",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=12),
            cursor="hand2"
        )
        self.img_preview.pack(fill="both", expand=True, padx=4, pady=4)
        self.img_preview.bind("<Button-1>", lambda e: self.browse_chart_image())

        fields_frame = ctk.CTkFrame(center_wrap, fg_color="transparent")
        fields_frame.pack(fill="both", expand=True, pady=2)

        self.entry_pair = self.create_dark_field(fields_frame, self._t("lbl_pair_asset"))
        self.entry_tf = self.create_dark_field(fields_frame, self._t("lbl_timeframe"))
        self.entry_direction = self.create_dark_field(fields_frame, self._t("lbl_direction"))
        self.entry_entry = self.create_dark_field(fields_frame, self._t("lbl_entry_price"))
        self.entry_sl = self.create_dark_field(fields_frame, self._t("lbl_stop_loss"))
        self.entry_tp = self.create_dark_field(fields_frame, self._t("lbl_take_profit"))
        self.entry_rr = self.create_dark_field(fields_frame, self._t("lbl_rr"))

        self.opt_status = self.create_dark_dropdown(fields_frame, self._t("lbl_status"), ["OPEN", "TP", "SL"], height=29)
        self.opt_type = self.create_dark_dropdown(
            fields_frame,
            self._t("lbl_setup_type"),
            ["HnS", "i HnS", "FVG", "S & R", "Breakout"],
            height=29,
            command_callback=self._on_analyzer_type_change
        )

        self.opt_status.set("")
        self.opt_type.set("")

        self.entry_pair.bind("<KeyRelease>", self.calculate_live_rr)
        self.entry_entry.bind("<KeyRelease>", self.calculate_live_rr)
        self.entry_sl.bind("<KeyRelease>", self.calculate_live_rr)
        self.entry_tp.bind("<KeyRelease>", self.calculate_live_rr)

        self.entry_notes = ctk.CTkEntry(
            center_wrap,
            placeholder_text=self._t("notes_placeholder"),
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=32,
            corner_radius=7,
            font=ctk.CTkFont(size=12)
        )
        self.entry_notes.pack(fill="x", pady=(6, 5))

        self.lbl_checklist_badge = ctk.CTkButton(
            center_wrap,
            text=self._t("checklist_badge_none"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=7,
            height=29,
            command=self.open_checklist_for_current_setup
        )
        self.lbl_checklist_badge.pack(fill="x", pady=(0, 3))

        self.lbl_save_status = ctk.CTkLabel(
            center_wrap,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("accent"),
            height=14
        )
        self.lbl_save_status.pack(anchor="e", pady=(0, 2))

        self.btn_save = ctk.CTkButton(
            center_wrap,
            text=self._t("btn_save_trade"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=8,
            height=34,
            command=self.save_trade
        )
        self.btn_save.pack(fill="x", pady=(1, 0))

    def create_dark_field(self, parent, label_text):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", expand=True, pady=3)
        lbl = ctk.CTkLabel(
            row,
            text=label_text,
            width=135,
            anchor="w",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=12)
        )
        lbl.pack(side="left")
        entry = ctk.CTkEntry(
            row,
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=29,
            corner_radius=7,
            font=ctk.CTkFont(size=12)
        )
        entry.pack(side="right", expand=True, fill="x")
        return entry

    def create_dark_dropdown(self, parent, label_text, options, height=29, command_callback=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", expand=True, pady=3)
        lbl = ctk.CTkLabel(
            row,
            text=label_text,
            width=135,
            anchor="w",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=12)
        )
        lbl.pack(side="left")

        opt_menu = CustomDropdownButton(
            row,
            options,
            "",
            app_instance=self,
            height=height,
            command_callback=command_callback
        )
        opt_menu.pack(side="right", expand=True, fill="x")
        return opt_menu

    def build_dashboard_table_panel(self, parent):
        panel = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        panel.grid(row=0, column=1, padx=(6, 0), sticky="nsew")

        header_row = ctk.CTkFrame(panel, fg_color="transparent")
        header_row.pack(fill="x", padx=14, pady=(12, 6))

        title = ctk.CTkLabel(
            header_row,
            text=self._t("recent_trades"),
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=pair_color("text_primary")
        )
        title.pack(side="left")

        hint = ctk.CTkLabel(
            header_row,
            text=self._t("recent_trades_hint"),
            font=ctk.CTkFont(size=11),
            text_color=pair_color("text_secondary")
        )
        hint.pack(side="right")

        columns = ("ID", "Pair", "TF", "Direction", "Entry", "SL", "TP", "R:R", "Status", "Type", "Date")
        self.tree_dash = ttk.Treeview(panel, columns=columns, show="headings", height=16)
        self._configure_trade_tree_tags(self.tree_dash)

        col_widths = {
            "ID": 42, "Pair": 90, "TF": 50, "Direction": 80,
            "Entry": 85, "SL": 85, "TP": 85, "R:R": 65,
            "Status": 70, "Type": 90, "Date": 105
        }
        col_label_map = {
            "ID": self._t("col_id"), "Pair": self._t("col_pair"), "TF": self._t("col_tf"),
            "Direction": self._t("col_direction"), "Entry": self._t("col_entry"), "SL": self._t("col_sl"),
            "TP": self._t("col_tp"), "R:R": self._t("col_rr"), "Status": self._t("col_status"),
            "Type": self._t("col_type"), "Date": self._t("col_date")
        }

        for col in columns:
            self.tree_dash.heading(col, text=col_label_map.get(col, col))
            self.tree_dash.column(col, width=col_widths.get(col, 75), minwidth=40, stretch=True, anchor="center")

        self.tree_dash.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.tree_dash.bind("<Double-1>", self.on_trade_double_click)
        self.tree_dash.bind("<Button-3>", lambda event: self.show_dash_context_menu(event, self.tree_dash))
        self.tree_dash.bind("<Button-2>", lambda event: self.show_dash_context_menu(event, self.tree_dash))

    # ------------------ ECONOMIC NEWS VIEW ------------------ #
    def build_news_view(self):
        self.view_news.grid_rowconfigure(1, weight=1)
        self.view_news.grid_columnconfigure(0, weight=1)

        ctrl_frame = ctk.CTkFrame(
            self.view_news,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        ctrl_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=4)
        ctrl_frame.grid_columnconfigure(0, weight=0)
        ctrl_frame.grid_columnconfigure(1, weight=1)
        ctrl_frame.grid_columnconfigure(2, weight=0)

        left_ctrls = ctk.CTkFrame(ctrl_frame, fg_color="transparent")
        left_ctrls.grid(row=0, column=0, sticky="w", padx=15, pady=12)

        self._news_range_display_to_canon = {
            self._t("news_range_today"): "Today",
            self._t("news_range_this_week"): "This Week",
            self._t("news_range_next_week"): "Next Week",
            self._t("news_range_last_week"): "Last Week",
        }
        self._news_range_canon_to_display = {v: k for k, v in self._news_range_display_to_canon.items()}
        if getattr(self, "news_range_var", None) not in self._news_range_canon_to_display:
            self.news_range_var = "Today"
        range_options = [
            self._t("news_range_today"),
            self._t("news_range_this_week"),
            self._t("news_range_next_week"),
            self._t("news_range_last_week"),
        ]
        self.opt_news_range = CustomDropdownButton(
            left_ctrls,
            range_options,
            self._news_range_canon_to_display.get(self.news_range_var, self._t("news_range_today")),
            app_instance=self,
            width=140,
            height=32,
            command_callback=lambda val: self.on_news_range_change(self._news_range_display_to_canon.get(val, val))
        )
        self.opt_news_range.pack(side="left", padx=(0, 8))

        all_ccy_label = self._t("news_all_currencies")
        self._news_ccy_all_label = all_ccy_label
        if not getattr(self, "news_currency_var", None):
            self.news_currency_var = "All Currencies"
        ccy_options = [all_ccy_label, "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"]
        ccy_default = all_ccy_label if self.news_currency_var == "All Currencies" else self.news_currency_var
        self.opt_news_currency = CustomDropdownButton(
            left_ctrls,
            ccy_options,
            ccy_default,
            app_instance=self,
            width=145,
            height=32,
            command_callback=lambda val: self.on_news_currency_change("All Currencies" if val == self._news_ccy_all_label else val),
            custom_max_h=440
        )
        self.opt_news_currency.pack(side="left", padx=(0, 8))

        self._news_impact_display_to_canon = {
            self._t("news_all_impacts"): "All Impacts",
            self._t("news_high_impact"): "High Impact",
            self._t("news_high_medium"): "High & Medium",
        }
        self._news_impact_canon_to_display = {v: k for k, v in self._news_impact_display_to_canon.items()}
        if getattr(self, "news_impact_var", None) not in self._news_impact_canon_to_display:
            self.news_impact_var = "All Impacts"
        impact_options = [
            self._t("news_all_impacts"),
            self._t("news_high_impact"),
            self._t("news_high_medium"),
        ]
        self.opt_news_impact = CustomDropdownButton(
            left_ctrls,
            impact_options,
            self._news_impact_canon_to_display.get(self.news_impact_var, self._t("news_all_impacts")),
            app_instance=self,
            width=155,
            height=32,
            command_callback=lambda val: self.on_news_impact_change(self._news_impact_display_to_canon.get(val, val))
        )
        self.opt_news_impact.pack(side="left")

        lbl_title = ctk.CTkLabel(
            ctrl_frame,
            text=self._t("news_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="center"
        )
        lbl_title.grid(row=0, column=1, padx=8, pady=12, sticky="ew")

        btn_refresh_news = ctk.CTkButton(
            ctrl_frame,
            text=self._t("btn_refresh_news"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            width=150,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.fetch_and_render_news(force=True)
        )
        btn_refresh_news.grid(row=0, column=2, sticky="e", padx=15, pady=12)

        table_panel = ctk.CTkFrame(
            self.view_news,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        table_panel.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)

        columns = ("Date", "Time", "Currency", "Event", "Actual", "Forecast", "Previous")
        self.tree_news = ttk.Treeview(table_panel, columns=columns, show=("tree", "headings"), height=20)

        self.tree_news.tag_configure("evenrow", background=THEME["row_even"], foreground=THEME["text_primary"])
        self.tree_news.tag_configure("oddrow", background=THEME["row_odd"], foreground=THEME["text_primary"])
        self.tree_news.tag_configure("passed_evenrow", background=THEME["passed_even"], foreground=THEME["text_muted"])
        self.tree_news.tag_configure("passed_oddrow", background=THEME["passed_odd"], foreground=THEME["text_muted"])

        self.tree_news.heading("#0", text=self._t("col_impact"))
        self.tree_news.column("#0", width=65, minwidth=50, stretch=False, anchor="center")

        col_widths = {
            "Date": 110,
            "Time": 90,
            "Currency": 80,
            "Event": 350,
            "Actual": 90,
            "Forecast": 90,
            "Previous": 90
        }
        news_col_labels = {
            "Date": self._t("col_date"),
            "Time": self._t("col_time"),
            "Currency": self._t("col_currency"),
            "Event": self._t("col_event"),
            "Actual": self._t("col_actual"),
            "Forecast": self._t("col_forecast"),
            "Previous": self._t("col_previous"),
        }

        for col in columns:
            self.tree_news.heading(col, text=news_col_labels.get(col, col))
            self.tree_news.column(
                col,
                width=col_widths.get(col, 100),
                minwidth=60,
                stretch=(col == "Event"),
                anchor="w" if col == "Event" else "center"
            )

        self.tree_news.pack(fill="both", expand=True, padx=14, pady=14)

    def _render_news_table(self, events):
        if not hasattr(self, "tree_news"):
            return

        for item in self.tree_news.get_children():
            self.tree_news.delete(item)

        filtered_events = news_svc.filter_news_events(
            events,
            getattr(self, "news_currency_var", "All Currencies"),
            getattr(self, "news_impact_var", "All Impacts"),
        )

        if not filtered_events:
            self.tree_news.insert(
                "",
                "end",
                image=self.impact_icons["none"],
                values=("", "", "", self._t("news_empty"), "", "", "")
            )
            return

        current_time = datetime.now(get_app_tzinfo())
        first_active_item = None

        for idx, ev in enumerate(filtered_events):
            ev_dt = ev.get("dt")
            has_passed = False
            if ev_dt:
                comp_dt = ev_dt.replace(tzinfo=current_time.tzinfo) if (ev_dt.tzinfo is None and current_time.tzinfo is not None) else ev_dt
                if comp_dt < current_time:
                    has_passed = True

            impact_str = str(ev.get("impact", "")).capitalize()

            if has_passed:
                icon = self.impact_icons["passed"]
                row_tag = "passed_evenrow" if idx % 2 == 0 else "passed_oddrow"
            else:
                if "High" in impact_str or impact_str in ["Red", "3"]:
                    icon = self.impact_icons["high"]
                elif "Medium" in impact_str or impact_str in ["Orange", "2"]:
                    icon = self.impact_icons["medium"]
                elif "Low" in impact_str or impact_str in ["Yellow", "1"]:
                    icon = self.impact_icons["low"]
                else:
                    icon = self.impact_icons["none"]
                row_tag = "evenrow" if idx % 2 == 0 else "oddrow"

            item_id = self.tree_news.insert(
                "",
                "end",
                image=icon,
                values=(
                    ev["date"],
                    ev["time"],
                    ev["currency"],
                    ev["title"],
                    ev["actual"],
                    ev["forecast"],
                    ev["previous"]
                ),
                tags=(row_tag,)
            )

            if not has_passed and first_active_item is None:
                first_active_item = item_id

        if first_active_item:
            self.tree_news.see(first_active_item)

    # ------------------ CALENDAR VIEW ------------------ #
    def build_calendar_view(self):
        self.view_calendar.grid_rowconfigure(1, weight=1)
        self.view_calendar.grid_columnconfigure(0, weight=1)

        ctrl_frame = ctk.CTkFrame(
            self.view_calendar,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        ctrl_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=4)

        btn_prev = ctk.CTkButton(
            ctrl_frame,
            text="❮",
            width=44,
            height=34,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            command=self.prev_month,
            font=ctk.CTkFont(size=13, weight="bold")
        )
        btn_prev.pack(side="left", padx=12, pady=10)

        self.lbl_month_year = ctk.CTkLabel(
            ctrl_frame,
            text="",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=pair_color("text_primary")
        )
        self.lbl_month_year.pack(side="left", expand=True)

        btn_next = ctk.CTkButton(
            ctrl_frame,
            text="❯",
            width=44,
            height=34,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            command=self.next_month,
            font=ctk.CTkFont(size=13, weight="bold")
        )
        btn_next.pack(side="right", padx=12, pady=10)

        self.cal_grid_frame = ctk.CTkFrame(
            self.view_calendar,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        self.cal_grid_frame._keep_child_borders = True
        self.cal_grid_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)

        for col in range(7):
            self.cal_grid_frame.grid_columnconfigure(col, weight=1, uniform="cal_col")
        for row in range(7):
            self.cal_grid_frame.grid_rowconfigure(row, weight=1, uniform="cal_row")

        days = [
            self._t("day_mon"), self._t("day_tue"), self._t("day_wed"),
            self._t("day_thu"), self._t("day_fri"), self._t("day_sat"), self._t("day_sun")
        ]
        hdr_font = ctk.CTkFont(size=13, weight="bold")
        for idx, day in enumerate(days):
            lbl = ctk.CTkLabel(
                self.cal_grid_frame,
                text=day,
                font=hdr_font,
                text_color=pair_color("text_secondary")
            )
            lbl.grid(row=0, column=idx, pady=(8, 4), sticky="nsew")

        # Pre-create the 42 day cells once so month switches update in-place without flicker
        self._cal_day_tiles = []
        day_font = ctk.CTkFont(size=12, weight="bold")
        count_font = ctk.CTkFont(size=11, weight="bold")

        for r in range(1, 7):
            for c in range(7):
                tile = ctk.CTkFrame(
                    self.cal_grid_frame,
                    fg_color=pair_color("bg_card_alt"),
                    border_color=pair_color("border"),
                    border_width=1,
                    corner_radius=8
                )
                tile.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")

                lbl_day = ctk.CTkLabel(tile, text="", font=day_font, text_color=pair_color("text_primary"))
                lbl_day.place(x=9, y=6, anchor="nw")

                lbl_count = ctk.CTkLabel(tile, text="", font=count_font, text_color=pair_color("text_muted"))
                lbl_count.place(relx=0.5, rely=0.55, anchor="center")

                self._cal_day_tiles.append({
                    "tile": tile,
                    "lbl_day": lbl_day,
                    "lbl_count": lbl_count,
                    "date_str": None
                })

    def render_calendar(self):
        if not hasattr(self, "cal_grid_frame") or not self._cal_day_tiles:
            return

        today_str = datetime.now(get_app_tzinfo()).strftime("%Y-%m-%d")
        cal_sig = (self.current_year, self.current_month, self._records_version, today_str, self.app_language)
        if self._last_cal_render_sig == cal_sig:
            return
        self._last_cal_render_sig = cal_sig

        month_list = self._t("months")
        month_name = month_list[self.current_month] if 0 < self.current_month < len(month_list) else calendar.month_name[self.current_month]
        self.lbl_month_year.configure(text=f"{month_name} {self.current_year}")

        daily_trades_map = defaultdict(list)
        for t in self.all_db_records:
            daily_trades_map[t.short_date].append(t)

        cal = calendar.Calendar(firstweekday=0)
        month_days = cal.monthdayscalendar(self.current_year, self.current_month)

        while len(month_days) < 6:
            month_days.append([0] * 7)

        flat_days = [day_num for week in month_days for day_num in week]

        for idx, day_num in enumerate(flat_days):
            cell = self._cal_day_tiles[idx]
            tile = cell["tile"]
            lbl_day = cell["lbl_day"]
            lbl_count = cell["lbl_count"]

            if day_num == 0:
                tile.configure(
                    fg_color=pair_color("bg_root"),
                    border_width=0,
                    cursor=""
                )
                lbl_day.configure(text="")
                lbl_count.configure(text="")
                for w in (tile, lbl_day, lbl_count):
                    w.unbind("<Button-1>")
                cell["date_str"] = None
                continue

            date_str = f"{self.current_year:04d}-{self.current_month:02d}-{day_num:02d}"
            day_trades = daily_trades_map.get(date_str, [])

            bg_pair = pair_color("bg_card_alt")
            border_pair = pair_color("accent") if date_str == today_str else pair_color("border")
            txt_pair = pair_color("text_primary")

            if day_trades:
                count = len(day_trades)
                tp_count = sum(1 for t in day_trades if clean_status(t.status) == "TP")
                sl_count = sum(1 for t in day_trades if clean_status(t.status) == "SL")
                count_line = self._t("cal_one_trade", count=count) if count == 1 else self._t("cal_many_trades", count=count)
                trades_count_str = f"{count_line}\n({tp_count}W / {sl_count}L)"

                if sl_count > tp_count:
                    bg_pair = pair_color("cal_loss_bg")
                    border_pair = pair_color("cal_loss_border")
                else:
                    bg_pair = pair_color("cal_win_bg")
                    border_pair = pair_color("cal_win_border")
            else:
                trades_count_str = self._t("cal_zero_trades")

            tile.configure(
                fg_color=bg_pair,
                border_color=border_pair,
                border_width=2 if date_str == today_str else 1,
                cursor="hand2" if day_trades else ""
            )
            lbl_day.configure(
                text=str(day_num),
                text_color=pair_color("accent") if date_str == today_str else txt_pair
            )
            lbl_count.configure(
                text=trades_count_str,
                text_color=txt_pair if day_trades else pair_color("text_muted")
            )

            for widget in (tile, lbl_day, lbl_count):
                if day_trades:
                    widget.bind("<Button-1>", lambda e, d=date_str: self.open_day_trades_dialog(d))
                else:
                    widget.unbind("<Button-1>")
            cell["date_str"] = date_str

    def open_day_trades_dialog(self, date_str):
        trades = [t for t in self.all_db_records if t.short_date == date_str]
        if not trades:
            return

        self._selected_cal_day_date = date_str
        self.show_view("CalendarDay")
        self._populate_calendar_day_table(date_str)

    def _populate_calendar_day_table(self, date_str):
        if not date_str or not hasattr(self, "tree_cal_day"):
            return

        trades = [t for t in self.all_db_records if t.short_date == date_str]
        total_records = len(self.all_db_records)
        id_map = {r.id: total_records - i for i, r in enumerate(self.all_db_records)}

        if hasattr(self, "lbl_cal_day_title") and self.lbl_cal_day_title.winfo_exists():
            self.lbl_cal_day_title.configure(text=self._t("cal_day_title", date=date_str))
        if hasattr(self, "lbl_cal_day_subtitle") and self.lbl_cal_day_subtitle.winfo_exists():
            self.lbl_cal_day_subtitle.configure(
                text=self._t("cal_day_subtitle", count=len(trades))
            )

        for item in self.tree_cal_day.get_children():
            self.tree_cal_day.delete(item)

        for idx, t in enumerate(trades):
            display_id = id_map.get(t.id, t.id)
            st = clean_status(t.status)
            row_tag = self._get_trade_row_tag(st, idx)
            display_row = [
                display_id, clean_pair(t.pair), format_timeframe(t.timeframe), clean_direction(t.direction),
                t.entry_price, t.stop_loss, t.take_profit, t.rr_ratio,
                st, t.trade_type, t.short_date
            ]
            self.tree_cal_day.insert("", "end", values=display_row, tags=(str(t.id), row_tag))

    def prev_month(self):
        if self.current_month == 1:
            self.current_month = 12
            self.current_year -= 1
        else:
            self.current_month -= 1
        self.render_calendar()

    def next_month(self):
        if self.current_month == 12:
            self.current_month = 1
            self.current_year += 1
        else:
            self.current_month += 1
        self.render_calendar()

    # ------------------ TRADES VIEW ------------------ #
    def build_trades_view(self):
        self.view_trades.grid_rowconfigure(0, weight=1)
        self.view_trades.grid_columnconfigure(0, weight=1)

        panel = ctk.CTkFrame(
            self.view_trades,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        panel.grid(row=0, column=0, sticky="nsew")

        header_frame = ctk.CTkFrame(panel, fg_color="transparent")
        header_frame.pack(fill="x", padx=15, pady=(14, 8))

        title = ctk.CTkLabel(
            header_frame,
            text=self._t("trades_db_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary")
        )
        title.pack(side="left")

        self.lbl_trades_count = ctk.CTkLabel(
            header_frame,
            text=self._t("trades_zero"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("accent")
        )
        self.lbl_trades_count.pack(side="left", padx=(12, 0))

        btn_delete = ctk.CTkButton(
            header_frame,
            text=self._t("btn_delete_selected"),
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            command=self.delete_selected,
            width=160,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold")
        )
        btn_delete.pack(side="right", padx=(6, 0))

        btn_refresh = ctk.CTkButton(
            header_frame,
            text=self._t("btn_refresh"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            command=self.load_database_records,
            width=120,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold")
        )
        btn_refresh.pack(side="right", padx=6)

        self.btn_io = ctk.CTkButton(
            header_frame,
            text=self._t("btn_import_export"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            command=self.show_import_export_options,
            width=170,
            height=32,
            font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_io.pack(side="right", padx=6)

        filter_bar = ctk.CTkFrame(
            panel,
            fg_color=pair_color("bg_input"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border")
        )
        filter_bar.pack(fill="x", padx=15, pady=(0, 8))

        self.trades_search_var = tk.StringVar()
        self.trades_search_var.trace_add("write", lambda *args: self._schedule_trades_filter())

        ent_search = ctk.CTkEntry(
            filter_bar,
            textvariable=self.trades_search_var,
            placeholder_text=self._t("trades_search_ph"),
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=30,
            width=230,
            font=ctk.CTkFont(size=12)
        )
        ent_search.pack(side="left", padx=10, pady=8)

        all_statuses_lbl = self._t("filter_all_statuses")
        self._trades_status_display_to_canon = {
            all_statuses_lbl: "All Statuses",
            "OPEN": "OPEN",
            "TP": "TP",
            "SL": "SL",
        }
        if getattr(self, "trades_status_filter", None) not in ("All Statuses", "OPEN", "TP", "SL"):
            self.trades_status_filter = "All Statuses"
        status_default = all_statuses_lbl if self.trades_status_filter == "All Statuses" else self.trades_status_filter
        self.opt_trades_status_filter = CustomDropdownButton(
            filter_bar,
            [all_statuses_lbl, "OPEN", "TP", "SL"],
            status_default,
            app_instance=self,
            width=130,
            height=30,
            command_callback=lambda val: self.on_trades_status_filter_change(self._trades_status_display_to_canon.get(val, val))
        )
        self.opt_trades_status_filter.pack(side="left", padx=4, pady=8)

        all_dirs_lbl = self._t("filter_all_directions")
        self._trades_dir_display_to_canon = {
            all_dirs_lbl: "All Directions",
            "BUY": "BUY",
            "SELL": "SELL",
        }
        if getattr(self, "trades_dir_filter", None) not in ("All Directions", "BUY", "SELL"):
            self.trades_dir_filter = "All Directions"
        dir_default = all_dirs_lbl if self.trades_dir_filter == "All Directions" else self.trades_dir_filter
        self.opt_trades_dir_filter = CustomDropdownButton(
            filter_bar,
            [all_dirs_lbl, "BUY", "SELL"],
            dir_default,
            app_instance=self,
            width=135,
            height=30,
            command_callback=lambda val: self.on_trades_dir_filter_change(self._trades_dir_display_to_canon.get(val, val))
        )
        self.opt_trades_dir_filter.pack(side="left", padx=4, pady=8)

        self._trades_date_display_to_canon = {
            self._t("date_all_time"): "All Time",
            self._t("date_today"): "Today",
            self._t("date_last_7"): "Last 7 Days",
            self._t("date_this_month"): "This Month",
            self._t("date_last_30"): "Last 30 Days",
            self._t("date_last_90"): "Last 90 Days",
        }
        self._trades_date_canon_to_display = {v: k for k, v in self._trades_date_display_to_canon.items()}
        if getattr(self, "trades_date_filter", None) not in self._trades_date_canon_to_display:
            self.trades_date_filter = "All Time"
        date_options = [
            self._t("date_all_time"),
            self._t("date_today"),
            self._t("date_last_7"),
            self._t("date_this_month"),
            self._t("date_last_30"),
            self._t("date_last_90"),
        ]
        self.opt_trades_date_filter = CustomDropdownButton(
            filter_bar,
            date_options,
            self._trades_date_canon_to_display.get(self.trades_date_filter, self._t("date_all_time")),
            app_instance=self,
            width=135,
            height=30,
            command_callback=lambda val: self.on_trades_date_filter_change(self._trades_date_display_to_canon.get(val, val))
        )
        self.opt_trades_date_filter.pack(side="left", padx=4, pady=8)

        self._trades_scope_display_to_canon = {
            self._t("scope_my_trades"): "My Trades",
            self._t("scope_all_users"): "All Users",
        }
        if not getattr(self, "trades_scope_filter", None):
            self.trades_scope_filter = "My Trades"
        scope_default = (
            self._t("scope_all_users") if self.trades_scope_filter == "All Users"
            else (self._t("scope_user_prefix", user=self.trades_scope_filter.split("User: ", 1)[1])
                  if str(self.trades_scope_filter).startswith("User: ")
                  else self._t("scope_my_trades"))
        )
        self.opt_trades_scope_filter = CustomDropdownButton(
            filter_bar,
            [self._t("scope_my_trades"), self._t("scope_all_users")],
            scope_default,
            app_instance=self,
            width=145,
            height=30,
            command_callback=self._on_trades_scope_display_selected,
            is_searchable=True
        )
        if self.current_role == "admin":
            self._refresh_admin_user_scope_options()
            self.opt_trades_scope_filter.pack(side="left", padx=4, pady=8)

        lbl_tip = ctk.CTkLabel(
            filter_bar,
            text=self._t("trades_tip"),
            font=ctk.CTkFont(size=11),
            text_color=pair_color("text_secondary")
        )
        lbl_tip.pack(side="right", padx=12)

        columns = ("ID", "User", "Pair", "TF", "Direction", "Entry", "SL", "TP", "R:R", "Status", "Type", "Checklist", "Date")
        self.tree_trades = ttk.Treeview(panel, columns=columns, show="headings", height=20, selectmode="extended")
        self._configure_trade_tree_tags(self.tree_trades)

        trades_col_widths = {
            "ID": 46, "User": 90, "Pair": 88, "TF": 52, "Direction": 76,
            "Entry": 82, "SL": 82, "TP": 82, "R:R": 62,
            "Status": 66, "Type": 84, "Checklist": 72, "Date": 100
        }
        self._trades_col_labels = {
            "ID": self._t("col_id"), "User": self._t("col_user"), "Pair": self._t("col_pair"),
            "TF": self._t("col_tf"), "Direction": self._t("col_direction"), "Entry": self._t("col_entry"),
            "SL": self._t("col_sl"), "TP": self._t("col_tp"), "R:R": self._t("col_rr"),
            "Status": self._t("col_status"), "Type": self._t("col_type"),
            "Checklist": self._t("col_checklist"), "Date": self._t("col_date")
        }
        for col in columns:
            self.tree_trades.heading(col, text=self._trades_col_labels.get(col, col), command=lambda c=col: self.sort_trades_by_column(c))
            self.tree_trades.column(col, width=trades_col_widths.get(col, 85), stretch=True, anchor="center")

        self.tree_trades.pack(fill="both", expand=True, padx=15, pady=(2, 15))
        self.tree_trades.bind("<Double-1>", self.on_trade_double_click)
        self.tree_trades.bind("<Button-3>", lambda event: self.show_trades_context_menu(event, self.tree_trades))
        self.tree_trades.bind("<Button-2>", lambda event: self.show_trades_context_menu(event, self.tree_trades))

    def _on_trades_scope_display_selected(self, display_val):
        canon = getattr(self, "_trades_scope_display_to_canon", {}).get(display_val, "My Trades")
        self.on_trades_scope_filter_change(canon)

    def _schedule_trades_filter(self):
        if getattr(self, "_trades_filter_after_id", None) is not None:
            try:
                self.after_cancel(self._trades_filter_after_id)
            except Exception:
                pass
            self._trades_filter_after_id = None
        if self.winfo_exists():
            self._trades_filter_after_id = self.after(120, self._run_debounced_trades_filter)

    def _run_debounced_trades_filter(self):
        self._trades_filter_after_id = None
        self.apply_trades_filter()

    def _refresh_admin_user_scope_options(self):
        if not hasattr(self, "opt_trades_scope_filter") or self.current_role != "admin":
            return

        def worker():
            try:
                users = get_all_users()
                usernames = sorted({str(u[1]).strip().lower() for u in users if len(u) > 1 and u[1]})

                def update_opts():
                    if getattr(self, "_is_closing", False) or not self.winfo_exists():
                        return
                    if hasattr(self, "opt_trades_scope_filter"):
                        display_map = {
                            self._t("scope_my_trades"): "My Trades",
                            self._t("scope_all_users"): "All Users",
                        }
                        user_options = [self._t("scope_my_trades"), self._t("scope_all_users")]
                        for u in usernames:
                            disp = self._t("scope_user_prefix", user=u)
                            canon = f"User: {u}"
                            display_map[disp] = canon
                            user_options.append(disp)
                        self._trades_scope_display_to_canon = display_map
                        canon_to_disp = {v: k for k, v in display_map.items()}
                        self.opt_trades_scope_filter.update_options(user_options)
                        if self.trades_scope_filter not in canon_to_disp:
                            self.trades_scope_filter = "My Trades"
                        self.opt_trades_scope_filter.set(canon_to_disp.get(self.trades_scope_filter, self._t("scope_my_trades")))

                if not getattr(self, "_is_closing", False) and self.winfo_exists():
                    self.after(0, update_opts)
            except Exception as e:
                logger.warning(f"Failed refreshing admin user scope options: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def on_trades_status_filter_change(self, val):
        self.trades_status_filter = val
        self.apply_trades_filter()

    def on_trades_dir_filter_change(self, val):
        self.trades_dir_filter = val
        self.apply_trades_filter()

    def on_trades_date_filter_change(self, val):
        self.trades_date_filter = val
        self.apply_trades_filter()

    def apply_trades_filter(self):
        if not hasattr(self, "tree_trades"):
            return

        if hasattr(self, "opt_trades_scope_filter"):
            if self.current_role == "admin":
                if not self.opt_trades_scope_filter.winfo_ismapped():
                    self.opt_trades_scope_filter.pack(side="left", padx=6, pady=8)
            else:
                if self.opt_trades_scope_filter.winfo_ismapped():
                    self.opt_trades_scope_filter.pack_forget()

        scope_val = getattr(self, "trades_scope_filter", "My Trades")
        show_user_col = self.current_role == "admin" and scope_val != "My Trades"
        if self._last_show_user_col != show_user_col:
            self._last_show_user_col = show_user_col
            if show_user_col:
                self.tree_trades.column("User", width=90, minwidth=65, stretch=True)
            else:
                self.tree_trades.column("User", width=0, minwidth=0, stretch=False)

        for item in self.tree_trades.get_children():
            self.tree_trades.delete(item)

        query = self.trades_search_var.get().strip().lower() if hasattr(self, "trades_search_var") else ""
        status_f = getattr(self, "trades_status_filter", "All Statuses")
        dir_f = getattr(self, "trades_dir_filter", "All Directions")
        date_f = getattr(self, "trades_date_filter", "All Time")

        records_source = (
            self._get_trades_view_records()
            if hasattr(self, "_get_trades_view_records")
            else self.all_db_records
        )
        sort_col = getattr(self, "_trades_sort_col", None)
        sort_rev = bool(getattr(self, "_trades_sort_reverse", False))

        rows_to_render, total_trades = filter_and_sort_trades(
            records_source,
            query=query,
            status_filter=status_f,
            dir_filter=dir_f,
            date_filter=date_f,
            sort_col=sort_col,
            sort_reverse=sort_rev,
        )

        col_labels = getattr(self, "_trades_col_labels", {})
        for col_name in TRADES_COL_INDEX_MAP:
            arrow = (" ▼" if sort_rev else " ▲") if col_name == sort_col else ""
            base_label = col_labels.get(col_name, col_name)
            self.tree_trades.heading(col_name, text=f"{base_label}{arrow}")

        for shown, (r, status_val, display_row) in enumerate(rows_to_render):
            row_tag = self._get_trade_row_tag(status_val, shown)
            self.tree_trades.insert("", "end", values=display_row, tags=(str(r.id), row_tag))

        shown = len(rows_to_render)
        if hasattr(self, "lbl_trades_count"):
            if shown == total_trades:
                self.lbl_trades_count.configure(text=self._t("trades_total", total=total_trades))
            else:
                self.lbl_trades_count.configure(text=self._t("trades_showing", shown=shown, total=total_trades))

    # ------------------ CALCULATOR VIEW ------------------ #
    def build_calculator_view(self):
        self.view_calculator.grid_rowconfigure(0, weight=1)
        self.view_calculator.grid_columnconfigure(0, weight=1)

        saved_balance, saved_risk, saved_mode = calc_svc.load_calc_settings(self.current_user)

        container = ctk.CTkFrame(self.view_calculator, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        container.grid_columnconfigure(0, weight=1)
        container.grid_columnconfigure(1, weight=1)
        container.grid_rowconfigure(0, weight=1)

        input_card = ctk.CTkFrame(
            container,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        input_card.grid(row=0, column=0, padx=(0, 6), pady=0, sticky="nsew")

        lbl_header = ctk.CTkLabel(
            input_card,
            text=self._t("calc_inputs_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="center"
        )
        lbl_header.pack(fill="x", padx=18, pady=(14, 8))

        paste_bar = ctk.CTkFrame(input_card, fg_color="transparent")
        paste_bar.pack(fill="x", padx=18, pady=(0, 4))
        paste_bar.grid_columnconfigure(0, weight=1)
        paste_bar.grid_columnconfigure(1, weight=0)

        self.btn_calc_paste = ctk.CTkButton(
            paste_bar,
            text=self._t("btn_calc_paste"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=13, weight="bold"),
            height=34,
            corner_radius=8,
            command=self.paste_calculator_screenshot
        )
        self.btn_calc_paste.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.btn_calc_clear = ctk.CTkButton(
            paste_bar,
            text="✕",
            width=40,
            height=34,
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(weight="bold", size=14),
            corner_radius=8,
            command=self.reset_calculator_screenshot
        )
        self.btn_calc_clear.grid(row=0, column=1, sticky="e")

        calc_preview_box = ctk.CTkFrame(
            input_card,
            fg_color=pair_color("bg_input"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border_strong"),
            height=82
        )
        calc_preview_box.pack(fill="x", padx=18, pady=(2, 2))
        calc_preview_box.pack_propagate(False)

        self.calc_img_preview = ctk.CTkLabel(
            calc_preview_box,
            text=self._t("paste_placeholder"),
            fg_color="transparent",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=12),
            cursor="hand2"
        )
        self.calc_img_preview.pack(fill="both", expand=True, padx=4, pady=4)
        self.calc_img_preview.bind("<Button-1>", lambda e: self.browse_calculator_screenshot())

        self.lbl_calc_extract_status = ctk.CTkLabel(
            input_card,
            text="",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("accent"),
            height=16
        )
        self.lbl_calc_extract_status.pack(fill="x", padx=18, pady=(1, 1))

        self.calc_balance_entry = self.create_calc_field(input_card, self._t("calc_balance"), saved_balance)

        row_risk = ctk.CTkFrame(input_card, fg_color="transparent")
        row_risk.pack(fill="x", padx=18, pady=5)

        lbl_r = ctk.CTkLabel(row_risk, text=self._t("calc_risk"), width=145, anchor="w", text_color=pair_color("text_secondary"), font=ctk.CTkFont(size=13))
        lbl_r.pack(side="left")

        risk_wrap = ctk.CTkFrame(row_risk, fg_color="transparent")
        risk_wrap.pack(side="right", expand=True, fill="x")

        self.calc_risk_entry = ctk.CTkEntry(
            risk_wrap,
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            border_width=1,
            corner_radius=7,
            text_color=pair_color("text_primary"),
            height=34,
            font=ctk.CTkFont(size=13)
        )
        self.calc_risk_entry.pack(fill="x", expand=True)
        self.calc_risk_entry.insert(0, saved_risk)

        self.calc_risk_mode_var = saved_mode
        symbol = "% ▾" if saved_mode == "Risk %" else "$ ▾"

        self.btn_risk_dropdown = ctk.CTkButton(
            self.calc_risk_entry,
            text=symbol,
            width=48,
            height=26,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            bg_color=pair_color("bg_input"),
            corner_radius=5,
            text_color=pair_color("accent"),
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.toggle_risk_dropdown
        )
        self.btn_risk_dropdown.place(relx=1.0, rely=0.5, anchor="e", x=-4)

        row_pair = ctk.CTkFrame(input_card, fg_color="transparent")
        row_pair.pack(fill="x", padx=18, pady=5)

        lbl_p = ctk.CTkLabel(row_pair, text=self._t("calc_pair"), width=145, anchor="w", text_color=pair_color("text_secondary"), font=ctk.CTkFont(size=13))
        lbl_p.pack(side="left")

        pair_wrap = ctk.CTkFrame(row_pair, fg_color="transparent")
        pair_wrap.pack(side="right", expand=True, fill="x")

        self.calc_pair_entry = CustomDropdownButton(
            pair_wrap,
            FOREX_PAIRS_LIST,
            "XAU/USD (Gold)",
            app_instance=self,
            height=34,
            command_callback=lambda val: self.select_pair_option(val),
            is_searchable=True,
            enable_favorites=True
        )
        self.calc_pair_entry.pack(fill="x", expand=True)

        row_entry = ctk.CTkFrame(input_card, fg_color="transparent")
        row_entry.pack(fill="x", padx=18, pady=5)

        lbl_e = ctk.CTkLabel(row_entry, text=self._t("calc_entry"), width=145, anchor="w", text_color=pair_color("text_secondary"), font=ctk.CTkFont(size=13))
        lbl_e.pack(side="left")

        entry_wrap = ctk.CTkFrame(row_entry, fg_color="transparent")
        entry_wrap.pack(side="right", expand=True, fill="x")

        self.calc_entry_price = ctk.CTkEntry(
            entry_wrap,
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            border_width=1,
            corner_radius=7,
            text_color=pair_color("text_primary"),
            height=34,
            font=ctk.CTkFont(size=13)
        )
        self.calc_entry_price.pack(fill="x", expand=True)
        self.calc_entry_price.insert(0, "2730.50")

        self.btn_live_price = ctk.CTkButton(
            self.calc_entry_price,
            text=self._t("calc_live"),
            width=48,
            height=26,
            fg_color="#1f7a58",
            hover_color="#26996e",
            bg_color=pair_color("bg_input"),
            corner_radius=5,
            text_color="#ffffff",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.fetch_live_entry_price
        )
        self.btn_live_price.place(relx=1.0, rely=0.5, anchor="e", x=-4)

        self.calc_sl_price = self.create_calc_field(input_card, self._t("calc_sl"), "2720.50")
        self.calc_tp_price = self.create_calc_field(input_card, self._t("calc_tp"), "2750.50")

        btn_calc = ctk.CTkButton(
            input_card,
            text=self._t("btn_calculate"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=14, weight="bold"),
            height=38,
            corner_radius=8,
            command=self.run_lot_calculator
        )
        btn_calc.pack(fill="x", padx=18, pady=(12, 16))

        out_card = ctk.CTkFrame(
            container,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        out_card.grid(row=0, column=1, padx=(6, 0), pady=0, sticky="nsew")

        lbl_out_hdr = ctk.CTkLabel(
            out_card,
            text=self._t("calc_results_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="center"
        )
        lbl_out_hdr.pack(fill="x", padx=18, pady=(16, 12))

        self.calc_res_lots = self.create_result_display(out_card, self._t("calc_res_lots_title"), "0.00 Lots", pair_color("accent"), size=22)
        self.calc_res_cash = self.create_result_display(out_card, self._t("calc_res_cash_title"), "$0.00", pair_color("danger_text"), size=20)
        self.calc_res_reward = self.create_result_display(out_card, self._t("calc_res_reward_title"), "$0.00 (1:0.00)", pair_color("cyan"), size=18)

        for entry_field in (self.calc_balance_entry, self.calc_risk_entry, self.calc_entry_price, self.calc_sl_price, self.calc_tp_price):
            entry_field.bind("<KeyRelease>", lambda e: (self.run_lot_calculator(), self.save_calc_settings()))

        self.run_lot_calculator()

    def create_calc_field(self, parent, label_text, default_val):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=5)
        lbl = ctk.CTkLabel(row, text=label_text, width=145, anchor="w", text_color=pair_color("text_secondary"), font=ctk.CTkFont(size=13))
        lbl.pack(side="left")
        entry = ctk.CTkEntry(
            row,
            fg_color=pair_color("bg_input"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            corner_radius=7,
            font=ctk.CTkFont(size=13)
        )
        entry.pack(side="right", expand=True, fill="x")
        entry.insert(0, default_val)
        return entry

    def create_result_display(self, parent, title, default_val, color, size=18):
        box = ctk.CTkFrame(
            parent,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        box.pack(fill="x", padx=18, pady=8)

        lbl_t = ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=11, weight="bold"), text_color=pair_color("text_secondary"), anchor="center")
        lbl_t.pack(anchor="center", padx=12, pady=(10, 2))

        lbl_v = ctk.CTkLabel(box, text=default_val, font=ctk.CTkFont(size=size, weight="bold"), text_color=color, anchor="center")
        lbl_v.pack(anchor="center", padx=12, pady=(0, 10))
        return lbl_v

    # ------------------ ANALYTICS VIEW ------------------ #
    def build_analytics_view(self):
        self.view_analytics.grid_rowconfigure(1, weight=1)
        self.view_analytics.grid_columnconfigure(0, weight=1)

        ctrl_card = ctk.CTkFrame(
            self.view_analytics,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        ctrl_card.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=4)

        lbl_select = ctk.CTkLabel(
            ctrl_card,
            text=self._t("analytics_select_label"),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pair_color("text_secondary")
        )
        lbl_select.pack(side="left", padx=(15, 8), pady=12)

        no_trades_lbl = self._t("analytics_no_trades")
        self.opt_analytics_trades = CustomDropdownButton(
            ctrl_card,
            [no_trades_lbl],
            no_trades_lbl,
            app_instance=self,
            width=410,
            height=34,
            is_searchable=True
        )
        self.opt_analytics_trades.pack(side="left", padx=(4, 8), pady=12)

        self.btn_run_analytics = ctk.CTkButton(
            ctrl_card,
            text=self._t("btn_run_review"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34,
            corner_radius=8,
            command=self.run_ai_trade_review
        )
        self.btn_run_analytics.pack(side="left", padx=6, pady=12)

        self.btn_analytics_paste = ctk.CTkButton(
            ctrl_card,
            text=self._t("btn_paste_rate"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("accent"),
            text_color=pair_color("accent"),
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34,
            corner_radius=8,
            command=self.run_ai_pasted_chart_review
        )
        self.btn_analytics_paste.pack(side="left", padx=6, pady=12)

        self.btn_analytics_clear = ctk.CTkButton(
            ctrl_card,
            text="✕",
            width=40,
            height=34,
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(weight="bold", size=14),
            corner_radius=8,
            command=self.reset_analytics_output
        )
        self.btn_analytics_clear.pack(side="right", padx=(6, 15), pady=12)

        output_card = ctk.CTkFrame(
            self.view_analytics,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        output_card.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)
        output_card.grid_rowconfigure(0, weight=1)
        output_card.grid_columnconfigure(0, weight=1)

        self.txt_analytics_log = ctk.CTkTextbox(
            output_card,
            fg_color=pair_color("bg_input"),
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(family="Segoe UI", size=14),
            border_color=pair_color("border"),
            border_width=1,
            corner_radius=10,
            scrollbar_button_color=pair_color("border_strong"),
            scrollbar_button_hover_color=pair_color("accent"),
            wrap="word",
            padx=16,
            pady=14
        )
        self.txt_analytics_log.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        try:
            self.txt_analytics_log._y_scrollbar.configure(
                width=9,
                corner_radius=6,
                fg_color="transparent"
            )
        except Exception:
            pass

        inner_txt = self.txt_analytics_log._textbox
        inner_txt.tag_configure("heading", foreground=THEME["accent"], font=("Segoe UI", 15, "bold"))
        inner_txt.tag_configure("bold", foreground=THEME["cyan"], font=("Segoe UI", 14, "bold"))
        inner_txt.tag_configure("rating", foreground=THEME["amber"], font=("Segoe UI", 16, "bold"))
        inner_txt.tag_configure("body", foreground=THEME["text_primary"], font=("Segoe UI", 14))

        self.txt_analytics_log.insert("end", self._t("analytics_intro"))

    def _render_formatted_analytics_text(self, raw_text):
        self.txt_analytics_log.delete("1.0", "end")
        inner_txt = self.txt_analytics_log._textbox

        for line in raw_text.splitlines():
            stripped = line.strip()
            clean_line = stripped.replace("**", "")
            if not clean_line:
                inner_txt.insert("end", "\n")
                continue

            if "⭐" in clean_line or "Rating:" in clean_line or "Beoordeling:" in clean_line:
                inner_txt.insert("end", clean_line + "\n", "rating")
            elif re.match(r"^(#+\s*|\d+\.\s+[A-Z])", clean_line):
                inner_txt.insert("end", "\n" + clean_line.lstrip("# ") + "\n", "heading")
            elif ":" in clean_line and clean_line.startswith(("-", "•", "*")):
                parts = clean_line.split(":", 1)
                inner_txt.insert("end", "  " + parts[0] + ":", "bold")
                inner_txt.insert("end", parts[1] + "\n", "body")
            else:
                inner_txt.insert("end", clean_line + "\n", "body")

    def reset_analytics_output(self):
        self.on_analytics_language_change(self.app_language)

    # ------------------ CHECKLIST VIEW ------------------ #
    def build_checklist_view(self):
        self.view_checklist.grid_rowconfigure(1, weight=1)
        self.view_checklist.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(
            self.view_checklist,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        card.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=4, pady=2)
        card.grid_rowconfigure(2, weight=1)
        card.grid_columnconfigure(0, weight=1)

        header_frame = ctk.CTkFrame(card, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 10))

        lbl_title = ctk.CTkLabel(
            header_frame,
            text=self._t("checklist_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent")
        )
        lbl_title.pack(side="left")

        lbl_select_type = ctk.CTkLabel(
            header_frame,
            text=self._t("checklist_strategy"),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pair_color("text_secondary")
        )
        lbl_select_type.pack(side="left", padx=(25, 8))

        self.opt_checklist_type = CustomDropdownButton(
            header_frame,
            list(TRADE_TYPE_CHECKLISTS.keys()),
            "HnS",
            app_instance=self,
            width=145,
            height=34,
            command_callback=lambda selected_type: self.load_checklist_for_type(selected_type)
        )
        self.opt_checklist_type.pack(side="left")

        btn_apply = ctk.CTkButton(
            header_frame,
            text=self._t("btn_apply_trade"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            width=155,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.apply_checklist_to_trade
        )
        btn_apply.pack(side="right", padx=(8, 0))

        btn_reset = ctk.CTkButton(
            header_frame,
            text=self._t("btn_reset_checklist"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            width=145,
            height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.reset_checklist
        )
        btn_reset.pack(side="right")

        progress_frame = ctk.CTkFrame(
            card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        progress_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))
        progress_frame.grid_columnconfigure(0, weight=1)

        self.lbl_checklist_status = ctk.CTkLabel(
            progress_frame,
            text=self._t("checklist_readiness", pct=0, checked=0, total=8),
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=pair_color("text_secondary")
        )
        self.lbl_checklist_status.pack(anchor="w", padx=15, pady=(10, 5))

        self.chk_progress = ctk.CTkProgressBar(progress_frame, progress_color=pair_color("accent"), fg_color=pair_color("bg_hover"), height=10)
        self.chk_progress.pack(fill="x", padx=15, pady=(0, 12))
        self.chk_progress.set(0)

        self.scroll_items = ctk.CTkScrollableFrame(
            card,
            fg_color="transparent",
            scrollbar_fg_color="transparent",
            scrollbar_button_color=pair_color("border_strong"),
            scrollbar_button_hover_color=pair_color("accent")
        )
        self.scroll_items.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 15))
        try:
            self.scroll_items._scrollbar.configure(width=9, corner_radius=6)
        except Exception:
            pass

        self.load_checklist_for_type("HnS")

    def load_checklist_for_type(self, trade_type):
        for widget in self.scroll_items.winfo_children():
            widget.destroy()

        items = get_trade_checklist(trade_type, self.app_language)
        saved_states = self.checklist_item_states.get(trade_type, [False] * len(items))
        if len(saved_states) != len(items):
            saved_states = [False] * len(items)

        self.checklist_vars = []
        self._active_checklist_type = trade_type

        if not hasattr(self, "_checklist_title_font"):
            self._checklist_title_font = ctk.CTkFont(size=14, weight="bold")
        if not hasattr(self, "_checklist_sub_font"):
            self._checklist_sub_font = ctk.CTkFont(size=12)

        for idx, (title, subtitle) in enumerate(items):
            checked_init = bool(saved_states[idx])
            item_frame = ctk.CTkFrame(
                self.scroll_items,
                fg_color=pair_color("bg_input"),
                corner_radius=10,
                border_width=1,
                border_color=pair_color("accent") if checked_init else pair_color("border")
            )
            item_frame.pack(fill="x", pady=5, padx=2)

            var = ctk.BooleanVar(value=checked_init)
            self.checklist_vars.append(var)

            chk = ctk.CTkCheckBox(
                item_frame,
                text=title,
                variable=var,
                command=lambda f=item_frame, v=var: self._on_checklist_item_toggle(f, v),
                font=self._checklist_title_font,
                text_color=pair_color("text_primary"),
                fg_color=pair_color("accent"),
                hover_color=pair_color("accent_hover"),
                checkmark_color=("#ffffff", DARK_THEME["bg_root"])
            )
            chk.pack(anchor="w", padx=15, pady=(10, 2))

            sub = ctk.CTkLabel(item_frame, text=subtitle, font=self._checklist_sub_font, text_color=pair_color("text_secondary"))
            sub.pack(anchor="w", padx=42, pady=(0, 10))

        self.update_checklist_progress()

    def _on_checklist_item_toggle(self, frame, var):
        frame.configure(border_color=pair_color("accent") if var.get() else pair_color("border"))
        self.update_checklist_progress()

    def update_checklist_progress(self):
        total = len(self.checklist_vars)
        checked = sum(1 for v in self.checklist_vars if v.get())
        pct = checked / total if total > 0 else 0

        active_type = getattr(self, "_active_checklist_type", None) or (
            self.opt_checklist_type.get() if hasattr(self, "opt_checklist_type") else "HnS"
        )
        self.checklist_item_states[active_type] = [bool(v.get()) for v in self.checklist_vars]
        if checked > 0:
            self.checklist_scores[active_type] = f"{checked}/{total} ({int(pct * 100)}%)"
        else:
            self.checklist_scores.pop(active_type, None)

        self.chk_progress.set(pct)
        if total > 0 and checked == total:
            self.lbl_checklist_status.configure(
                text=self._t("checklist_validated", checked=checked, total=total),
                text_color=pair_color("accent")
            )
        else:
            self.lbl_checklist_status.configure(
                text=self._t("checklist_readiness", pct=int(pct * 100), checked=checked, total=total),
                text_color=pair_color("text_secondary")
            )

        if hasattr(self, "_refresh_analyzer_checklist_badge"):
            self._refresh_analyzer_checklist_badge()

    def reset_checklist(self):
        for var in self.checklist_vars:
            var.set(False)
        for item_frame in self.scroll_items.winfo_children():
            if isinstance(item_frame, ctk.CTkFrame):
                item_frame.configure(border_color=pair_color("border"))
        self.update_checklist_progress()

    def apply_checklist_to_trade(self):
        """Syncs the active checklist setup type and readiness score back to the Dashboard trade form."""
        selected_type = self.opt_checklist_type.get() if hasattr(self, "opt_checklist_type") else "HnS"
        self.update_checklist_progress()
        if hasattr(self, "opt_type"):
            self.opt_type.set(selected_type)
        if hasattr(self, "_refresh_analyzer_checklist_badge"):
            self._refresh_analyzer_checklist_badge()
        self.show_view("Dashboard")

    # ------------------ SETTINGS VIEW ------------------ #
    def build_settings_view(self):
        self.view_settings.grid_rowconfigure(1, weight=1)
        self.view_settings.grid_columnconfigure(0, weight=1)

        header_card = ctk.CTkFrame(
            self.view_settings,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        header_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 12))

        header_left = ctk.CTkFrame(header_card, fg_color="transparent")
        header_left.pack(side="left", padx=20, pady=14)

        ctk.CTkLabel(
            header_left,
            text=self._t("settings_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        ).pack(anchor="w")

        user_label = (self.current_user or self._t("guest")).upper()
        role_label = self._t("role_admin") if self.current_role == "admin" else self._t("role_pro")
        self.lbl_settings_user = ctk.CTkLabel(
            header_left,
            text=self._t("settings_signed_in", user=user_label, role=role_label),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_settings_user.pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header_card,
            text=self._t("btn_back_dashboard"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=185,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.show_view("Dashboard")
        ).pack(side="right", padx=20, pady=14)

        body_grid = ctk.CTkFrame(self.view_settings, fg_color="transparent")
        body_grid.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        body_grid.grid_columnconfigure(0, weight=1)
        body_grid.grid_columnconfigure(1, weight=1)
        body_grid.grid_rowconfigure(0, weight=1)
        body_grid.grid_rowconfigure(1, weight=1)

        # Appearance Card (Light / Dark Mode) — Row 0, Col 0
        appearance_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        appearance_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        ctk.CTkLabel(
            appearance_card,
            text=self._t("settings_appearance_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(16, 4))

        ctk.CTkLabel(
            appearance_card,
            text=self._t("settings_appearance_desc"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            justify="left",
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(0, 12))

        theme_box = ctk.CTkFrame(
            appearance_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        theme_box.pack(fill="x", padx=22, pady=(0, 10))

        self.lbl_settings_theme_status = ctk.CTkLabel(
            theme_box,
            text=self._t("settings_theme_dark") if self.theme_mode == "Dark" else self._t("settings_theme_light"),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_settings_theme_status.pack(anchor="w", padx=16, pady=(10, 6))

        self.btn_theme_toggle = ctk.CTkButton(
            theme_box,
            text=self._t("btn_switch_light") if self.theme_mode == "Dark" else self._t("btn_switch_dark"),
            anchor="w",
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.toggle_theme_mode
        )
        self.btn_theme_toggle.pack(fill="x", padx=16, pady=(0, 12))

        lang_box = ctk.CTkFrame(
            appearance_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        lang_box.pack(fill="x", padx=22, pady=(0, 14))

        self.lbl_settings_lang_status = ctk.CTkLabel(
            lang_box,
            text=self._t("settings_lang_status", lang=self._t("lang_name")),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_settings_lang_status.pack(anchor="w", padx=16, pady=(10, 6))

        lang_btns = ctk.CTkFrame(lang_box, fg_color="transparent")
        lang_btns.pack(fill="x", padx=16, pady=(0, 12))
        lang_btns.grid_columnconfigure(0, weight=1)
        lang_btns.grid_columnconfigure(1, weight=1)

        is_en = self.app_language == "English"
        self.btn_lang_en = ctk.CTkButton(
            lang_btns,
            text=self._t("btn_lang_en"),
            fg_color=pair_color("accent") if is_en else pair_color("bg_card_alt"),
            hover_color=pair_color("accent_hover") if is_en else pair_color("bg_hover"),
            border_width=0 if is_en else 1,
            border_color=pair_color("border_strong"),
            text_color=("#ffffff", DARK_THEME["bg_root"]) if is_en else pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.change_app_language("English")
        )
        self.btn_lang_en.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        self.btn_lang_nl = ctk.CTkButton(
            lang_btns,
            text=self._t("btn_lang_nl"),
            fg_color=pair_color("accent") if not is_en else pair_color("bg_card_alt"),
            hover_color=pair_color("accent_hover") if not is_en else pair_color("bg_hover"),
            border_width=0 if not is_en else 1,
            border_color=pair_color("border_strong"),
            text_color=("#ffffff", DARK_THEME["bg_root"]) if not is_en else pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.change_app_language("Dutch")
        )
        self.btn_lang_nl.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        # App Info & Updates Card — Row 1, Col 0
        updates_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        updates_card.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(6, 0))

        ctk.CTkLabel(
            updates_card,
            text=self._t("settings_updates_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(12, 2))

        ctk.CTkLabel(
            updates_card,
            text=self._t("settings_installed_ver", ver=updater_svc.CURRENT_VERSION),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(0, 8))

        upd_box = ctk.CTkFrame(
            updates_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        upd_box.pack(fill="x", padx=22, pady=(0, 8))

        self.lbl_settings_update_status = ctk.CTkLabel(
            upd_box,
            text=self._t("upd_idle"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_settings_update_status.pack(anchor="w", padx=16, pady=(10, 4))

        self.lbl_settings_release_notes = ctk.CTkLabel(
            upd_box,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=pair_color("text_muted"),
            justify="left",
            wraplength=420,
            anchor="w"
        )

        self.prog_settings_update = ctk.CTkProgressBar(
            upd_box,
            progress_color=pair_color("accent"),
            fg_color=pair_color("bg_hover"),
            height=8
        )
        self.prog_settings_update.set(0)

        self.btn_settings_install_update = ctk.CTkButton(
            upd_box,
            text=self._t("btn_dl_install"),
            anchor="w",
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.install_available_update_from_settings
        )

        self.btn_settings_check_updates = ctk.CTkButton(
            upd_box,
            text=self._t("btn_check_updates"),
            anchor="w",
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.check_for_updates(silent=False, from_settings=True)
        )
        self.btn_settings_check_updates.pack(fill="x", padx=16, pady=(2, 10))

        # Timezone Selector Box under App Info & Updates
        tz_box = ctk.CTkFrame(
            updates_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        tz_box.pack(fill="x", padx=22, pady=(0, 12))

        tz_hdr = ctk.CTkFrame(tz_box, fg_color="transparent")
        tz_hdr.pack(fill="x", padx=16, pady=(10, 4))

        ctk.CTkLabel(
            tz_hdr,
            text=self._t("settings_tz_title"),
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        ).pack(side="left")

        self.lbl_settings_tz_status = ctk.CTkLabel(
            tz_hdr,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=pair_color("text_secondary"),
            anchor="e"
        )
        self.lbl_settings_tz_status.pack(side="right")

        tz_options, tz_disp_to_key, tz_key_to_disp = get_timezone_dropdown_options(
            auto_prefix=self._t("settings_tz_auto_prefix")
        )
        self._tz_display_to_key = tz_disp_to_key
        self._tz_key_to_display = tz_key_to_disp
        cur_tz_key = get_app_timezone_pref()
        cur_tz_disp = tz_key_to_disp.get(cur_tz_key, tz_options[0])

        self.opt_settings_timezone = CustomDropdownButton(
            tz_box,
            tz_options,
            cur_tz_disp,
            app_instance=self,
            height=34,
            command_callback=self.on_app_timezone_change,
            is_searchable=True
        )
        self.opt_settings_timezone.pack(fill="x", padx=16, pady=(2, 10))

        # Account Security Card (Change Password) — Row 0-1, Col 1
        security_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        security_card.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(6, 0), pady=0)

        ctk.CTkLabel(
            security_card,
            text=self._t("settings_pw_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(16, 4))

        ctk.CTkLabel(
            security_card,
            text=self._t("settings_pw_desc"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            justify="left",
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(0, 12))

        pw_box = ctk.CTkFrame(
            security_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        pw_box.pack(fill="x", padx=22, pady=(0, 14))

        self.ent_settings_curr_pw = ctk.CTkEntry(
            pw_box,
            placeholder_text=self._t("pw_curr_ph"),
            show="•",
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.ent_settings_curr_pw.pack(fill="x", padx=16, pady=(14, 8))

        self.ent_settings_new_pw = ctk.CTkEntry(
            pw_box,
            placeholder_text=self._t("pw_new_ph"),
            show="•",
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.ent_settings_new_pw.pack(fill="x", padx=16, pady=8)

        self.ent_settings_conf_pw = ctk.CTkEntry(
            pw_box,
            placeholder_text=self._t("pw_conf_ph"),
            show="•",
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.ent_settings_conf_pw.pack(fill="x", padx=16, pady=(8, 6))
        self.ent_settings_conf_pw.bind("<Return>", lambda e: self.submit_settings_password_change())

        self.lbl_settings_pw_status = ctk.CTkLabel(
            pw_box,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("danger_text"),
            anchor="w"
        )
        self.lbl_settings_pw_status.pack(fill="x", padx=18, pady=(2, 6))

        self.btn_settings_update_pw = ctk.CTkButton(
            pw_box,
            text=self._t("btn_update_pw"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            height=40,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.submit_settings_password_change
        )
        self.btn_settings_update_pw.pack(fill="x", padx=16, pady=(4, 14))

    # ------------------ ADMIN MANAGE USERS VIEW ------------------ #
    def build_admin_users_view(self):
        self.view_admin_users.grid_rowconfigure(1, weight=1)
        self.view_admin_users.grid_columnconfigure(0, weight=1)

        header_card = ctk.CTkFrame(
            self.view_admin_users,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        header_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 12))

        header_left = ctk.CTkFrame(header_card, fg_color="transparent")
        header_left.pack(side="left", padx=20, pady=14)

        ctk.CTkLabel(
            header_left,
            text=self._t("admin_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        ).pack(anchor="w")

        ctk.CTkLabel(
            header_left,
            text=self._t("admin_subtitle"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header_card,
            text=self._t("btn_back_dashboard"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=185,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.show_view("Dashboard")
        ).pack(side="right", padx=(6, 20), pady=14)

        ctk.CTkButton(
            header_card,
            text=self._t("btn_refresh"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=120,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.admin_refresh_users
        ).pack(side="right", padx=6, pady=14)

        body_grid = ctk.CTkFrame(self.view_admin_users, fg_color="transparent")
        body_grid.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        body_grid.grid_columnconfigure(0, weight=3)
        body_grid.grid_columnconfigure(1, weight=2)
        body_grid.grid_rowconfigure(0, weight=1)

        # Left Panel: Create Account + User Directory
        left_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left_card.grid_rowconfigure(1, weight=1)
        left_card.grid_columnconfigure(0, weight=1)

        add_bar = ctk.CTkFrame(
            left_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        add_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))

        self.ent_admin_new_user = ctk.CTkEntry(
            add_bar,
            placeholder_text=self._t("admin_new_user_ph"),
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=12)
        )
        self.ent_admin_new_user.pack(side="left", padx=(12, 6), pady=10, expand=True, fill="x")

        self.ent_admin_new_pass = ctk.CTkEntry(
            add_bar,
            placeholder_text=self._t("admin_new_pass_ph"),
            show="•",
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(size=12)
        )
        self.ent_admin_new_pass.pack(side="left", padx=6, pady=10, expand=True, fill="x")
        self.ent_admin_new_pass.bind("<Return>", lambda e: self.admin_create_account())

        self.btn_admin_create = ctk.CTkButton(
            add_bar,
            text=self._t("btn_create_account"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            font=ctk.CTkFont(size=12, weight="bold"),
            height=36,
            corner_radius=8,
            command=self.admin_create_account
        )
        self.btn_admin_create.pack(side="right", padx=(6, 12), pady=10)

        tree_wrap = ctk.CTkFrame(left_card, fg_color="transparent")
        tree_wrap.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))

        columns = ("ID", "Username", "Role", "Created")
        self.tree_admin_users = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=12)
        col_widths = {"ID": 60, "Username": 200, "Role": 110, "Created": 140}
        admin_col_labels = {
            "ID": self._t("col_id"),
            "Username": self._t("col_username"),
            "Role": self._t("col_role"),
            "Created": self._t("col_created"),
        }
        for c in columns:
            self.tree_admin_users.heading(c, text=admin_col_labels.get(c, c))
            self.tree_admin_users.column(c, width=col_widths.get(c, 110), anchor="center")
        self.tree_admin_users.pack(fill="both", expand=True)
        self.tree_admin_users.bind("<<TreeviewSelect>>", self.on_admin_user_select)

        footer_bar = ctk.CTkFrame(left_card, fg_color="transparent")
        footer_bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))

        ctk.CTkLabel(
            footer_bar,
            text=self._t("admin_table_hint"),
            font=ctk.CTkFont(size=11),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(side="left")

        self.btn_admin_del_user = ctk.CTkButton(
            footer_bar,
            text=self._t("btn_remove_user"),
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"),
            height=34,
            corner_radius=8,
            command=self.admin_remove_user
        )
        self.btn_admin_del_user.pack(side="right")

        # Right Panel: Modify Selected User (Inline)
        right_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        right_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        ctk.CTkLabel(
            right_card,
            text=self._t("admin_edit_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(20, 4))

        self.lbl_admin_edit_target = ctk.CTkLabel(
            right_card,
            text=self._t("admin_edit_prompt"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_admin_edit_target.pack(anchor="w", padx=22, pady=(0, 16))

        edit_box = ctk.CTkFrame(
            right_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        edit_box.pack(fill="x", padx=22, pady=(0, 14))

        ctk.CTkLabel(
            edit_box,
            text=self._t("admin_lbl_username"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(anchor="w", padx=16, pady=(14, 4))

        self.ent_admin_edit_user = ctk.CTkEntry(
            edit_box,
            placeholder_text=self._t("admin_lbl_username"),
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.ent_admin_edit_user.pack(fill="x", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            edit_box,
            text=self._t("admin_lbl_new_pw"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(anchor="w", padx=16, pady=(0, 4))

        self.ent_admin_edit_pass = ctk.CTkEntry(
            edit_box,
            placeholder_text=self._t("admin_keep_pw_ph"),
            show="•",
            fg_color=pair_color("bg_card"),
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13)
        )
        self.ent_admin_edit_pass.pack(fill="x", padx=16, pady=(0, 10))

        ctk.CTkLabel(
            edit_box,
            text=self._t("admin_lbl_role"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="w"
        ).pack(anchor="w", padx=16, pady=(0, 4))

        self.opt_admin_edit_role = CustomDropdownButton(
            edit_box,
            ["user", "admin"],
            "user",
            app_instance=self,
            height=38
        )
        self.opt_admin_edit_role.pack(fill="x", padx=16, pady=(0, 10))

        self.lbl_admin_status = ctk.CTkLabel(
            edit_box,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_admin_status.pack(fill="x", padx=18, pady=(2, 4))

        self.btn_admin_save_edit = ctk.CTkButton(
            edit_box,
            text=self._t("btn_save_changes"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            height=38,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.admin_save_user_changes
        )
        self.btn_admin_save_edit.pack(fill="x", padx=16, pady=(4, 16))

    # ------------------ CALENDAR DAY TRADES VIEW ------------------ #
    def build_calendar_day_view(self):
        self.view_calendar_day.grid_rowconfigure(1, weight=1)
        self.view_calendar_day.grid_columnconfigure(0, weight=1)

        header_card = ctk.CTkFrame(
            self.view_calendar_day,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        header_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 12))

        header_left = ctk.CTkFrame(header_card, fg_color="transparent")
        header_left.pack(side="left", padx=20, pady=14)

        self.lbl_cal_day_title = ctk.CTkLabel(
            header_left,
            text=self._t("cal_day_default_title"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_cal_day_title.pack(anchor="w")

        self.lbl_cal_day_subtitle = ctk.CTkLabel(
            header_left,
            text=self._t("cal_day_default_sub"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_cal_day_subtitle.pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header_card,
            text=self._t("btn_back_calendar"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=185,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.show_view("Calendar")
        ).pack(side="right", padx=20, pady=14)

        table_card = ctk.CTkFrame(
            self.view_calendar_day,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        table_card.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        columns = ("ID", "Pair", "TF", "Direction", "Entry", "SL", "TP", "R:R", "Status", "Type", "Date")
        self.tree_cal_day = ttk.Treeview(table_card, columns=columns, show="headings", height=16)
        self._configure_trade_tree_tags(self.tree_cal_day)

        col_widths = {
            "ID": 52, "Pair": 95, "TF": 58, "Direction": 85,
            "Entry": 90, "SL": 90, "TP": 90, "R:R": 70,
            "Status": 75, "Type": 95, "Date": 110
        }
        cal_day_col_labels = {
            "ID": self._t("col_id"), "Pair": self._t("col_pair"), "TF": self._t("col_tf"),
            "Direction": self._t("col_direction"), "Entry": self._t("col_entry"),
            "SL": self._t("col_sl"), "TP": self._t("col_tp"), "R:R": self._t("col_rr"),
            "Status": self._t("col_status"), "Type": self._t("col_type"), "Date": self._t("col_date")
        }
        for col in columns:
            self.tree_cal_day.heading(col, text=cal_day_col_labels.get(col, col))
            self.tree_cal_day.column(col, width=col_widths.get(col, 90), minwidth=45, stretch=True, anchor="center")

        self.tree_cal_day.pack(fill="both", expand=True, padx=14, pady=14)
        self.tree_cal_day.bind("<Double-1>", self.on_trade_double_click)
        self.tree_cal_day.bind("<Button-3>", lambda event: self.show_dash_context_menu(event, self.tree_cal_day))
        self.tree_cal_day.bind("<Button-2>", lambda event: self.show_dash_context_menu(event, self.tree_cal_day))

    # ------------------ TRADE CHART PREVIEW VIEW ------------------ #
    def build_trade_preview_view(self):
        self.view_trade_preview.grid_rowconfigure(2, weight=1)
        self.view_trade_preview.grid_columnconfigure(0, weight=1)

        header_card = ctk.CTkFrame(
            self.view_trade_preview,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        header_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 10))

        header_left = ctk.CTkFrame(header_card, fg_color="transparent")
        header_left.pack(side="left", padx=20, pady=12)

        self.lbl_preview_title = ctk.CTkLabel(
            header_left,
            text=self._t("preview_title_default"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_preview_title.pack(anchor="w")

        self.lbl_preview_meta = ctk.CTkLabel(
            header_left,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_preview_meta.pack(anchor="w", pady=(2, 0))

        self.btn_preview_back = ctk.CTkButton(
            header_card,
            text=self._t("btn_back"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=185,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.close_trade_preview_view
        )
        self.btn_preview_back.pack(side="right", padx=(6, 20), pady=12)

        self.btn_preview_edit = ctk.CTkButton(
            header_card,
            text=self._t("btn_edit_trade"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("accent"),
            text_color=pair_color("accent"),
            height=34,
            width=135,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.open_edit_trade_from_preview
        )
        self.btn_preview_edit.pack(side="right", padx=6, pady=12)

        self.btn_preview_next = ctk.CTkButton(
            header_card,
            text=self._t("btn_next"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=88,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.navigate_preview_trade(1)
        )
        self.btn_preview_next.pack(side="right", padx=(4, 6), pady=12)

        self.btn_preview_prev = ctk.CTkButton(
            header_card,
            text=self._t("btn_prev"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=88,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.navigate_preview_trade(-1)
        )
        self.btn_preview_prev.pack(side="right", padx=4, pady=12)

        bar = ctk.CTkFrame(
            self.view_trade_preview,
            fg_color=pair_color("bg_card"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        bar.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 8))

        self.btn_preview_before = ctk.CTkButton(
            bar,
            text=self._t("btn_tab_before"),
            width=96,
            height=30,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.switch_preview_tab("before")
        )
        self.btn_preview_before.pack(side="left", padx=(10, 3), pady=8)

        self.btn_preview_after = ctk.CTkButton(
            bar,
            text=self._t("btn_tab_after"),
            width=92,
            height=30,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.switch_preview_tab("after")
        )
        self.btn_preview_after.pack(side="left", padx=3, pady=8)

        # Quick Outcome Toggle (OPEN / TP / SL)
        outcome_wrap = ctk.CTkFrame(
            bar,
            fg_color=pair_color("bg_input"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border")
        )
        outcome_wrap.pack(side="left", padx=(8, 4), pady=8)

        ctk.CTkLabel(
            outcome_wrap,
            text=self._t("preview_status_lbl"),
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=pair_color("text_secondary")
        ).pack(side="left", padx=(7, 3), pady=3)

        self.preview_status_buttons = {}
        for st_val in ("OPEN", "TP", "SL"):
            st_btn = ctk.CTkButton(
                outcome_wrap,
                text=st_val,
                width=42,
                height=22,
                corner_radius=6,
                fg_color="transparent",
                hover_color=pair_color("bg_hover"),
                text_color=pair_color("text_secondary"),
                font=ctk.CTkFont(size=11, weight="bold"),
                command=lambda s=st_val: self.preview_set_quick_status(s)
            )
            st_btn.pack(side="left", padx=2, pady=3)
            self.preview_status_buttons[st_val] = st_btn

        # Right side controls: After actions + Zoom controls
        ctk.CTkButton(
            bar,
            text=self._t("btn_remove"),
            width=98,
            height=30,
            corner_radius=8,
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.preview_remove_after
        ).pack(side="right", padx=(3, 10), pady=8)

        ctk.CTkButton(
            bar,
            text=self._t("btn_upload"),
            width=96,
            height=30,
            corner_radius=8,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.preview_attach_after("upload")
        ).pack(side="right", padx=3, pady=8)

        ctk.CTkButton(
            bar,
            text=self._t("btn_paste_after"),
            width=106,
            height=30,
            corner_radius=8,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("accent"),
            text_color=pair_color("accent"),
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.preview_attach_after("paste")
        ).pack(side="right", padx=3, pady=8)

        zoom_wrap = ctk.CTkFrame(
            bar,
            fg_color=pair_color("bg_input"),
            corner_radius=8,
            border_width=1,
            border_color=pair_color("border")
        )
        zoom_wrap.pack(side="right", padx=(4, 6), pady=8)

        ctk.CTkButton(
            zoom_wrap,
            text="−",
            width=26,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=pair_color("bg_hover"),
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.preview_adjust_zoom(-0.25)
        ).pack(side="left", padx=(3, 1), pady=3)

        self.btn_preview_zoom_reset = ctk.CTkButton(
            zoom_wrap,
            text=self._t("btn_zoom_fit"),
            width=58,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=pair_color("bg_hover"),
            text_color=pair_color("accent"),
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.preview_reset_zoom
        )
        self.btn_preview_zoom_reset.pack(side="left", padx=1, pady=3)

        ctk.CTkButton(
            zoom_wrap,
            text="+",
            width=26,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=pair_color("bg_hover"),
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(size=13, weight="bold"),
            command=lambda: self.preview_adjust_zoom(0.25)
        ).pack(side="left", padx=(1, 3), pady=3)

        img_container = ctk.CTkFrame(
            self.view_trade_preview,
            fg_color=pair_color("bg_input"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        img_container.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        img_container.grid_rowconfigure(0, weight=1)
        img_container.grid_columnconfigure(0, weight=1)

        self.preview_canvas = tk.Canvas(
            img_container,
            bg=THEME["bg_input"],
            highlightthickness=0,
            bd=0
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        self.preview_canvas.bind("<Configure>", self._on_preview_canvas_configure)
        self.preview_canvas.bind("<MouseWheel>", self._on_preview_mousewheel)
        self.preview_canvas.bind("<Button-4>", self._on_preview_mousewheel)
        self.preview_canvas.bind("<Button-5>", self._on_preview_mousewheel)
        self.preview_canvas.bind("<ButtonPress-1>", self._on_preview_pan_start)
        self.preview_canvas.bind("<B1-Motion>", self._on_preview_pan_move)
        self.preview_canvas.bind("<Double-1>", self._on_preview_canvas_double_click)

        self.lbl_preview_empty_msg = ctk.CTkLabel(
            img_container,
            text="",
            font=ctk.CTkFont(size=14),
            text_color=pair_color("text_secondary")
        )

    # ------------------ IN-APP EDIT TRADE VIEW ------------------ #
    def build_edit_trade_view(self):
        self.view_edit_trade.grid_rowconfigure(1, weight=1)
        self.view_edit_trade.grid_columnconfigure(0, weight=1)

        header_card = ctk.CTkFrame(
            self.view_edit_trade,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        header_card.grid(row=0, column=0, sticky="ew", padx=4, pady=(0, 12))

        header_left = ctk.CTkFrame(header_card, fg_color="transparent")
        header_left.pack(side="left", padx=20, pady=14)

        self.lbl_edit_trade_title = ctk.CTkLabel(
            header_left,
            text=self._t("edit_title_default"),
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_edit_trade_title.pack(anchor="w")

        self.lbl_edit_trade_subtitle = ctk.CTkLabel(
            header_left,
            text=self._t("edit_subtitle"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary"),
            anchor="w"
        )
        self.lbl_edit_trade_subtitle.pack(anchor="w", pady=(2, 0))

        self.btn_edit_trade_back = ctk.CTkButton(
            header_card,
            text=self._t("btn_back"),
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            height=34,
            width=185,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.close_edit_trade_view
        )
        self.btn_edit_trade_back.pack(side="right", padx=20, pady=14)

        body_grid = ctk.CTkFrame(self.view_edit_trade, fg_color="transparent")
        body_grid.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        body_grid.grid_columnconfigure(0, weight=1)
        body_grid.grid_columnconfigure(1, weight=1)
        body_grid.grid_rowconfigure(0, weight=1)

        # Left Card: Trade Parameters
        left_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        ctk.CTkLabel(
            left_card,
            text=self._t("edit_params_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(anchor="w", padx=22, pady=(18, 10))

        self.edit_trade_fields = {}
        param_defs = [
            ("Pair", self._t("edit_lbl_pair"), self._t("edit_ph_pair")),
            ("Timeframe", self._t("edit_lbl_tf"), self._t("edit_ph_tf")),
            ("Direction", self._t("edit_lbl_dir"), self._t("edit_ph_dir")),
            ("Entry", self._t("edit_lbl_entry"), self._t("edit_ph_entry")),
            ("SL", self._t("edit_lbl_sl"), self._t("edit_ph_sl")),
            ("TP", self._t("edit_lbl_tp"), self._t("edit_ph_tp")),
            ("R:R", self._t("edit_lbl_rr"), self._t("edit_ph_rr")),
            ("Date", self._t("edit_lbl_date"), self._t("edit_ph_date")),
            ("Checklist", self._t("edit_lbl_checklist"), self._t("edit_ph_checklist")),
            ("Notes", self._t("edit_lbl_notes"), self._t("edit_ph_notes")),
        ]

        for field_name, field_lbl, ph in param_defs:
            row = ctk.CTkFrame(left_card, fg_color="transparent")
            row.pack(fill="x", padx=22, pady=3)
            ctk.CTkLabel(
                row,
                text=field_lbl,
                width=110,
                anchor="w",
                text_color=pair_color("text_secondary"),
                font=ctk.CTkFont(size=13)
            ).pack(side="left")
            ent = ctk.CTkEntry(
                row,
                placeholder_text=ph,
                fg_color=pair_color("bg_input"),
                border_color=pair_color("border_strong"),
                text_color=pair_color("text_primary"),
                height=34,
                corner_radius=8,
                font=ctk.CTkFont(size=13)
            )
            ent.pack(side="right", expand=True, fill="x")
            if field_name in ("Entry", "SL", "TP"):
                ent.bind("<KeyRelease>", self.calculate_edit_live_rr)
            self.edit_trade_fields[field_name] = ent

        row_s = ctk.CTkFrame(left_card, fg_color="transparent")
        row_s.pack(fill="x", padx=22, pady=(6, 4))
        ctk.CTkLabel(
            row_s,
            text=self._t("edit_lbl_status"),
            width=110,
            anchor="w",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=13)
        ).pack(side="left")
        self.opt_edit_trade_status = CustomDropdownButton(
            row_s,
            ["OPEN", "TP", "SL"],
            "OPEN",
            app_instance=self,
            height=34
        )
        self.opt_edit_trade_status.pack(side="right", expand=True, fill="x")

        row_t = ctk.CTkFrame(left_card, fg_color="transparent")
        row_t.pack(fill="x", padx=22, pady=(4, 16))
        ctk.CTkLabel(
            row_t,
            text=self._t("edit_lbl_type"),
            width=110,
            anchor="w",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=13)
        ).pack(side="left")
        self.opt_edit_trade_type = CustomDropdownButton(
            row_t,
            ["HnS", "i HnS", "FVG", "S & R", "Breakout"],
            "HnS",
            app_instance=self,
            height=34
        )
        self.opt_edit_trade_type.pack(side="right", expand=True, fill="x")

        # Right Card: After-Trade Chart & Save
        right_card = ctk.CTkFrame(
            body_grid,
            fg_color=pair_color("bg_card"),
            corner_radius=12,
            border_width=1,
            border_color=pair_color("border")
        )
        right_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        right_card.grid_rowconfigure(2, weight=1)
        right_card.grid_columnconfigure(0, weight=1)

        right_hdr = ctk.CTkFrame(right_card, fg_color="transparent")
        right_hdr.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 8))

        ctk.CTkLabel(
            right_hdr,
            text=self._t("edit_after_title"),
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=pair_color("text_primary"),
            anchor="w"
        ).pack(side="left")

        self.lbl_edit_after_status = ctk.CTkLabel(
            right_hdr,
            text=self._t("edit_after_none"),
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("text_secondary"),
            anchor="e"
        )
        self.lbl_edit_after_status.pack(side="right")

        btn_bar = ctk.CTkFrame(right_card, fg_color="transparent")
        btn_bar.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 10))

        ctk.CTkButton(
            btn_bar,
            text=self._t("btn_paste_after_ctrlv"),
            height=34,
            corner_radius=8,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("accent"),
            text_color=pair_color("accent"),
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.edit_trade_pick_after("paste")
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_bar,
            text=self._t("btn_upload_after"),
            height=34,
            corner_radius=8,
            fg_color=pair_color("bg_card_alt"),
            hover_color=pair_color("bg_hover"),
            border_width=1,
            border_color=pair_color("border_strong"),
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self.edit_trade_pick_after("upload")
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            btn_bar,
            text=self._t("btn_remove"),
            height=34,
            corner_radius=8,
            fg_color=pair_color("danger"),
            hover_color=pair_color("danger_hover"),
            text_color="#ffffff",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.edit_trade_clear_after
        ).pack(side="right")

        preview_box = ctk.CTkFrame(
            right_card,
            fg_color=pair_color("bg_input"),
            corner_radius=10,
            border_width=1,
            border_color=pair_color("border")
        )
        preview_box.grid(row=2, column=0, sticky="nsew", padx=22, pady=(0, 10))

        self.lbl_edit_after_preview = ctk.CTkLabel(
            preview_box,
            text=self._t("edit_after_empty"),
            font=ctk.CTkFont(size=12),
            text_color=pair_color("text_secondary")
        )
        self.lbl_edit_after_preview.pack(expand=True, padx=10, pady=10)

        save_bar = ctk.CTkFrame(right_card, fg_color="transparent")
        save_bar.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 18))

        self.lbl_edit_trade_status = ctk.CTkLabel(
            save_bar,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=pair_color("accent"),
            anchor="w"
        )
        self.lbl_edit_trade_status.pack(fill="x", pady=(0, 6))

        self.btn_edit_trade_save = ctk.CTkButton(
            save_bar,
            text=self._t("btn_save_changes"),
            fg_color=pair_color("accent"),
            hover_color=pair_color("accent_hover"),
            text_color=("#ffffff", DARK_THEME["bg_root"]),
            height=40,
            corner_radius=8,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.save_edited_trade
        )
        self.btn_edit_trade_save.pack(fill="x")


