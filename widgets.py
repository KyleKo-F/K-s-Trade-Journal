import os
import io
import re
import json
import time
import base64
import tempfile
import secrets
import weakref
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import customtkinter as ctk
from customtkinter.windows.widgets.core_rendering import DrawEngine, CTkCanvas
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw, ImageGrab, ImageTk

from theme import (
    THEME,
    pair_color,
    logger,
    get_app_tzinfo,
    get_app_timezone_pref,
    get_tz_cache_epoch,
    utc_timestamp_to_local_short_date,
    normalize_timeframe_str,
)

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_DECODED_IMAGE_B64_LEN = 10 * 1024 * 1024


def _resolve_parent_bg(widget):
    """Walks up parent widgets (including CTkScrollableFrame internals) to find the true solid background color."""
    curr = widget
    while curr is not None:
        for getter in (
            lambda w: w.cget("fg_color"),
            lambda w: getattr(w, "_fg_color", None),
        ):
            try:
                fg = getter(curr)
                if fg and fg != "transparent":
                    return fg
            except Exception:
                pass
        curr = getattr(curr, "master", None)
    return pair_color("bg_card")


_RGBA_CACHE = OrderedDict()
_RGBA_CACHE_MAX = 256
_PIL_BORDER_CACHE = OrderedDict()
_PIL_BORDER_CACHE_MAX = 512


def set_pil_border_defer(active):
    """Kept for backwards compatibility; PIL borders render coalesced per idle tick."""
    return None


def _parse_hex_rgba(canvas, color):
    """Converts any Tk color name or #RRGGBB string into a cached (R, G, B, A) tuple."""
    if not color or color == "transparent":
        return (0, 0, 0, 0)
    if isinstance(color, (tuple, list)):
        idx = 1 if ctk.get_appearance_mode().lower() == "dark" else 0
        color = color[idx]
    cached = _RGBA_CACHE.get(color)
    if cached is not None:
        _RGBA_CACHE.move_to_end(color)
        return cached
    try:
        r16, g16, b16 = canvas.winfo_rgb(color)
        rgba = (r16 >> 8, g16 >> 8, b16 >> 8, 255)
    except Exception:
        rgba = (241, 245, 249, 255)
    _RGBA_CACHE[color] = rgba
    _RGBA_CACHE.move_to_end(color)
    while len(_RGBA_CACHE) > _RGBA_CACHE_MAX:
        _RGBA_CACHE.popitem(last=False)
    return rgba


def _get_cached_border_photo(w, h, cr, bw, fill_rgba, stroke_rgba):
    """Returns an LRU-cached ImageTk.PhotoImage for identical widget dimensions and theme colors."""
    key = (w, h, cr, bw, fill_rgba, stroke_rgba)
    photo = _PIL_BORDER_CACHE.get(key)
    if photo is not None:
        _PIL_BORDER_CACHE.move_to_end(key)
        return photo

    scale = 2
    sw, sh = w * scale, h * scale
    sbw = max(int(round(max(bw, 1.5) * scale)), 3)
    scr = min(cr * scale, min(sw, sh) // 2)

    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [1, 1, sw - 2, sh - 2],
        radius=scr,
        fill=fill_rgba if fill_rgba[3] > 0 else None,
        outline=stroke_rgba if stroke_rgba[3] > 0 else None,
        width=sbw,
    )
    img = img.resize((w, h), Image.Resampling.BILINEAR)
    photo = ImageTk.PhotoImage(img)

    _PIL_BORDER_CACHE[key] = photo
    _PIL_BORDER_CACHE.move_to_end(key)
    while len(_PIL_BORDER_CACHE) > _PIL_BORDER_CACHE_MAX:
        _PIL_BORDER_CACHE.popitem(last=False)
    return photo


def _render_pil_border_on_canvas(canvas):
    """Renders a cached anti-aliased rounded rectangle onto the canvas using stored geometry & colors."""
    canvas._pil_render_pending = False

    spec = getattr(canvas, "_pil_border_spec", None)
    if not spec:
        return
    w, h, cr, bw = spec["w"], spec["h"], spec["cr"], spec["bw"]
    if w <= 4 or h <= 4:
        return

    fill_rgba = _parse_hex_rgba(canvas, spec.get("fill"))
    stroke_rgba = _parse_hex_rgba(canvas, spec.get("border"))
    render_sig = (w, h, cr, bw, fill_rgba, stroke_rgba)
    if getattr(canvas, "_pil_last_sig", None) == render_sig:
        return

    tk_img = _get_cached_border_photo(w, h, cr, bw, fill_rgba, stroke_rgba)
    canvas._pil_last_sig = render_sig
    canvas._pil_border_photo = tk_img

    img_id = getattr(canvas, "_pil_border_item_id", None)
    if img_id:
        try:
            canvas.itemconfigure(img_id, image=tk_img)
            return
        except Exception:
            pass

    canvas.delete("pil_border_img")
    canvas._pil_border_item_id = canvas.create_image(
        0, 0, anchor="nw", image=tk_img, tags=("pil_border_img",)
    )
    canvas.tag_lower("pil_border_img")


def _schedule_pil_border_render(canvas):
    """Coalesces multiple itemconfig calls (inner_parts + border_parts) into one render per idle tick."""
    if getattr(canvas, "_pil_render_pending", False):
        return
    canvas._pil_render_pending = True
    try:
        canvas.after_idle(lambda c=canvas: _render_pil_border_on_canvas(c) if c.winfo_exists() else None)
    except Exception:
        _render_pil_border_on_canvas(canvas)


_SUPPORTED_CTK_VERSION_PREFIXES = ("5.2.",)


def _is_compatible_ctk_release():
    """Checks CustomTkinter version compatibility and verifies required internal hooks exist."""
    ver = str(getattr(ctk, "__version__", "") or "").strip()
    if ver and not ver.startswith(_SUPPORTED_CTK_VERSION_PREFIXES):
        logger.warning(
            f"Skipping crisp border patch: unsupported CustomTkinter version '{ver}' "
            f"(tested with {_SUPPORTED_CTK_VERSION_PREFIXES})."
        )
        return False

    required_hooks = (
        (DrawEngine, "draw_rounded_rect_with_border"),
        (CTkCanvas, "itemconfig"),
        (ctk.CTkEntry, "_create_grid"),
        (ctk.CTkEntry, "_draw"),
        (ctk.CTkLabel, "__init__"),
        (ctk.CTkLabel, "_create_grid"),
        (ctk.CTkLabel, "_draw"),
        (ctk.CTkLabel, "grid"),
        (ctk.CTkLabel, "pack"),
        (ctk.CTkLabel, "place"),
        (ctk.CTkFrame, "__init__"),
        (ctk.CTkFrame, "configure"),
    )
    for owner, attr in required_hooks:
        if not hasattr(owner, attr):
            logger.warning(f"Skipping crisp border patch: missing {owner.__name__}.{attr}")
            return False
    return True


def _install_crisp_border_patch():
    """
    Fixes CustomTkinter's Light Mode border rendering globally with version & attribute guards:
    1. Renders bordered rounded rectangles via cached PIL ImageDraw.rounded_rectangle synced with CTkCanvas.itemconfig.
    2. Places CTkEntry's inner tk.Entry with exact margins so it never overlaps the bottom border.
    3. Places bordered/rounded CTkLabel's internal tk.Label strictly inside the border box.
    4. Resolves true parent bg_color (including inside CTkScrollableFrame) & keeps borders on Calendar empty tiles.
    """
    if os.environ.get("KS_DISABLE_BORDER_PATCH", "").strip() == "1":
        return
    if getattr(DrawEngine, "_crisp_border_patched", False):
        return
    if not _is_compatible_ctk_release():
        return

    try:
        orig_draw_rounded_rect_with_border = DrawEngine.draw_rounded_rect_with_border
        orig_canvas_itemconfig = CTkCanvas.itemconfig

        def patched_draw_rounded_rect_with_border(self, width, height, corner_radius, border_width, *args, **kwargs):
            canvas = getattr(self, "_canvas", None)
            if canvas is None:
                return orig_draw_rounded_rect_with_border(
                    self, width, height, corner_radius, border_width, *args, **kwargs
                )

            w = int(round(width))
            h = int(round(height))
            bw = int(round(border_width or 0))
            cr = int(round(corner_radius or 0))

            if bw > 0 and cr > 0 and w > 4 and h > 4:
                if getattr(canvas, "_pil_border_spec", None) is None:
                    canvas.delete("inner_parts", "border_parts")
                    prev = {}
                else:
                    prev = canvas._pil_border_spec

                canvas._pil_border_spec = {
                    "w": w,
                    "h": h,
                    "cr": cr,
                    "bw": bw,
                    "fill": prev.get("fill"),
                    "border": prev.get("border"),
                }
                _schedule_pil_border_render(canvas)
                return True

            if getattr(canvas, "_pil_border_spec", None) is not None:
                canvas._pil_border_spec = None
                canvas._pil_last_sig = None
                canvas._pil_border_item_id = None
                canvas.delete("pil_border_img")
            return orig_draw_rounded_rect_with_border(
                self, width, height, corner_radius, border_width, *args, **kwargs
            )

        def patched_canvas_itemconfig(self, tagOrId, *args, **kwargs):
            spec = getattr(self, "_pil_border_spec", None)
            if spec is not None and tagOrId in ("inner_parts", "border_parts"):
                if "fill" in kwargs:
                    if tagOrId == "inner_parts":
                        spec["fill"] = kwargs["fill"]
                    elif tagOrId == "border_parts":
                        spec["border"] = kwargs["fill"]
                    _schedule_pil_border_render(self)
                return None
            return orig_canvas_itemconfig(self, tagOrId, *args, **kwargs)

        DrawEngine.draw_rounded_rect_with_border = patched_draw_rounded_rect_with_border
        CTkCanvas.itemconfig = patched_canvas_itemconfig

        # 2. Place CTkEntry's inner tk.Entry strictly inside the border box so all 4 sides show
        orig_entry_create_grid = ctk.CTkEntry._create_grid
        orig_entry_draw = ctk.CTkEntry._draw

        def _place_entry_inside_border(entry_self):
            try:
                bw = getattr(entry_self, "_border_width", 0) or 0
                inner_entry = getattr(entry_self, "_entry", None)
                if bw > 0 and inner_entry is not None and hasattr(entry_self, "_apply_widget_scaling"):
                    if inner_entry.winfo_manager() == "grid":
                        inner_entry.grid_forget()
                    cr = getattr(entry_self, "_corner_radius", 6) or 6
                    pad_x = max(int(entry_self._apply_widget_scaling(max(cr * 0.55, 6))), 6)
                    pad_y = max(int(entry_self._apply_widget_scaling(5)), 5)
                    inner_entry.place(
                        x=pad_x,
                        y=pad_y,
                        relwidth=1.0,
                        width=-(pad_x * 2),
                        relheight=1.0,
                        height=-(pad_y * 2),
                    )
            except Exception:
                pass

        def patched_entry_create_grid(self):
            orig_entry_create_grid(self)
            _place_entry_inside_border(self)

        def patched_entry_draw(self, no_color_updates=False):
            orig_entry_draw(self, no_color_updates=no_color_updates)
            _place_entry_inside_border(self)

        ctk.CTkEntry._create_grid = patched_entry_create_grid
        ctk.CTkEntry._draw = patched_entry_draw

        # 3. Place bordered/rounded CTkLabel's internal tk.Label strictly inside the border box
        orig_label_init = ctk.CTkLabel.__init__
        orig_label_create_grid = ctk.CTkLabel._create_grid
        orig_label_draw = ctk.CTkLabel._draw
        orig_label_grid = ctk.CTkLabel.grid
        orig_label_pack = ctk.CTkLabel.pack
        orig_label_place = ctk.CTkLabel.place

        def _place_inner_tk_label(lbl_self):
            try:
                bw = getattr(lbl_self, "_border_width", 0) or 0
                cr = getattr(lbl_self, "_corner_radius", 0) or 0
                inner_lbl = getattr(lbl_self, "_label", None)
                if (bw > 0 or cr > 0) and inner_lbl is not None and hasattr(lbl_self, "_apply_widget_scaling"):
                    if inner_lbl.winfo_manager() == "grid":
                        inner_lbl.grid_forget()
                    inset = max(int(lbl_self._apply_widget_scaling(max(cr * 0.75, bw + 5, 7))), 7)
                    inner_lbl.place(
                        x=inset,
                        y=inset,
                        relwidth=1.0,
                        width=-(inset * 2),
                        relheight=1.0,
                        height=-(inset * 2),
                    )
            except Exception:
                pass

        def patched_label_init(self, master=None, *args, **kwargs):
            if (
                master is not None
                and kwargs.get("bg_color") in (None, "transparent")
                and ((kwargs.get("corner_radius") or 0) > 0 or (kwargs.get("border_width") or 0) > 0)
            ):
                kwargs["bg_color"] = _resolve_parent_bg(master)
            return orig_label_init(self, master=master, *args, **kwargs)

        def patched_label_create_grid(self):
            orig_label_create_grid(self)
            _place_inner_tk_label(self)

        def patched_label_draw(self, no_color_updates=False):
            orig_label_draw(self, no_color_updates=no_color_updates)
            _place_inner_tk_label(self)

        def patched_label_grid(self, **kwargs):
            try:
                master = getattr(self, "master", None)
                if (
                    isinstance(master, ctk.CTkFrame)
                    and (getattr(master, "_border_width", 0) or 0) > 0
                    and "e" in str(kwargs.get("sticky", ""))
                    and "w" in str(kwargs.get("sticky", ""))
                    and "padx" not in kwargs
                ):
                    col = kwargs.get("column", None)
                    if col == 0:
                        kwargs["padx"] = (4, 0)
                    elif col == 6:
                        kwargs["padx"] = (0, 4)
            except Exception:
                pass
            return orig_label_grid(self, **kwargs)

        def patched_label_pack(self, **kwargs):
            try:
                master = getattr(self, "master", None)
                if (
                    isinstance(master, ctk.CTkFrame)
                    and (getattr(master, "_border_width", 0) or 0) > 0
                    and (getattr(master, "_corner_radius", 0) or 0) > 0
                    and kwargs.get("fill") in ("both", "x", "y")
                ):
                    if "padx" not in kwargs or kwargs["padx"] in (0, 1, 2):
                        kwargs["padx"] = 7
                    if "pady" not in kwargs or kwargs["pady"] in (0, 1, 2):
                        kwargs["pady"] = 7
            except Exception:
                pass
            return orig_label_pack(self, **kwargs)

        def patched_label_place(self, **kwargs):
            try:
                master = getattr(self, "master", None)
                if (
                    isinstance(master, ctk.CTkFrame)
                    and (getattr(master, "_border_width", 0) or 0) > 0
                    and (getattr(master, "_corner_radius", 0) or 0) > 0
                    and kwargs.get("relwidth") == 1.0
                    and kwargs.get("relheight") == 1.0
                ):
                    kwargs.setdefault("x", 7)
                    kwargs.setdefault("y", 7)
                    kwargs.setdefault("width", -14)
                    kwargs.setdefault("height", -14)
            except Exception:
                pass
            return orig_label_place(self, **kwargs)

        ctk.CTkLabel.__init__ = patched_label_init
        ctk.CTkLabel._create_grid = patched_label_create_grid
        ctk.CTkLabel._draw = patched_label_draw
        ctk.CTkLabel.grid = patched_label_grid
        ctk.CTkLabel.pack = patched_label_pack
        ctk.CTkLabel.place = patched_label_place

        # 4. Auto-resolve bg_color on rounded CTkFrames & keep borders on Calendar empty tiles
        orig_frame_init = ctk.CTkFrame.__init__
        orig_frame_configure = ctk.CTkFrame.configure

        def patched_frame_init(self, master=None, *args, **kwargs):
            if master is not None and isinstance(master, ctk.CTkFrame):
                try:
                    if (
                        kwargs.get("border_width") == 0
                        and getattr(master, "_keep_child_borders", False)
                        and (getattr(master, "_border_width", 0) or 0) > 0
                    ):
                        kwargs["border_width"] = 1
                        kwargs.setdefault("border_color", pair_color("border"))
                except Exception:
                    pass
            if (
                master is not None
                and kwargs.get("bg_color") in (None, "transparent")
                and ((kwargs.get("corner_radius") or 0) > 0 or (kwargs.get("border_width") or 0) > 0)
            ):
                kwargs["bg_color"] = _resolve_parent_bg(master)
            return orig_frame_init(self, master=master, *args, **kwargs)

        def patched_frame_configure(self, require_redraw=False, **kwargs):
            try:
                master = getattr(self, "master", None)
                if (
                    kwargs.get("border_width") == 0
                    and master is not None
                    and isinstance(master, ctk.CTkFrame)
                    and getattr(master, "_keep_child_borders", False)
                    and (getattr(master, "_border_width", 0) or 0) > 0
                ):
                    kwargs["border_width"] = 1
                    kwargs["border_color"] = pair_color("border")
            except Exception:
                pass
            return orig_frame_configure(self, require_redraw=require_redraw, **kwargs)

        ctk.CTkFrame.__init__ = patched_frame_init
        ctk.CTkFrame.configure = patched_frame_configure
        DrawEngine._crisp_border_patched = True
    except Exception as e:
        logger.warning(f"Failed to install crisp border patch cleanly: {e}")


_install_crisp_border_patch()


FOREX_PAIRS_LIST = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/CAD", "EUR/AUD", "EUR/NZD",
    "GBP/JPY", "GBP/CHF", "GBP/CAD", "GBP/AUD", "GBP/NZD",
    "AUD/JPY", "AUD/NZD", "AUD/CAD", "AUD/CHF",
    "NZD/JPY", "NZD/CAD", "NZD/CHF",
    "CAD/JPY", "CAD/CHF", "CHF/JPY",
    "XAU/USD (Gold)", "BTC/USD (Crypto)", "ETH/USD (Crypto)"
]

PARSED_FOREX_PAIRS = []
for pair in FOREX_PAIRS_LIST:
    clean_p = pair.split()[0].strip()
    if "/" in clean_p:
        parts = clean_p.split("/")
        PARSED_FOREX_PAIRS.append((clean_p, parts[0].upper().strip(), parts[1].upper().strip()))

RE_MARKDOWN_LINK = re.compile(r'\[([^\]]+)\]\([^\)]+\)')

TRADE_TYPE_CHECKLISTS = {
    "HnS": [
        ("Higher Timeframe Trend & Supply", "Confirm 4H/1D structural bias is bearish or rejecting a major HTF supply zone (avoid mid-range chop)."),
        ("Prior Uptrend & Liquidity Sweep", "Left shoulder/head formed after a bullish leg, with the head sweeping buy-side liquidity or prior swing highs."),
        ("Economic News & Session Liquidity", "No imminent red-folder news (CPI, NFP, FOMC); trade executed in active London/NY session with tight spreads."),
        ("Shoulder Symmetry & Exhaustion", "Left shoulder, higher head, and proportional right shoulder show weakening buyer momentum or bearish divergence."),
        ("Neckline Break & Candle Close", "Wait for a decisive full candle close below the neckline and confirm neckline isn't steeply angled against you."),
        ("Retest & Bearish Rejection", "Price retested broken neckline as new resistance and printed clear bearish rejection wicks or engulfing confirmation."),
        ("Clear Runway & Correlation Check", "Clean traffic down to measured-move target (min 1:2 R:R) and no duplicate correlated exposure already open."),
        ("Stop Loss & Lot Calculator Risk", "Stop loss placed above right shoulder peak (+ spread buffer) with lot size verified via Lot Calculator.")
    ],
    "i HnS": [
        ("Higher Timeframe Trend & Demand", "Confirm 4H/1D structural bias is bullish or bouncing from a major HTF demand zone (avoid mid-range chop)."),
        ("Prior Downtrend & Liquidity Sweep", "Structure follows a bearish leg, with the deeper head sweeping sell-side liquidity or key swing lows."),
        ("Economic News & Session Liquidity", "No imminent red-folder news (CPI, NFP, FOMC); trade executed in active London/NY session with tight spreads."),
        ("Inverted Structure & Exhaustion", "Left shoulder, deeper head, and higher right shoulder show weakening selling pressure or bullish divergence."),
        ("Neckline Break & Candle Close", "Full candle body close above the resistance neckline confirmed — avoid anticipating inside the right shoulder."),
        ("Retest & Bullish Confirmation", "Former resistance held as fresh support on pullback with bullish rejection wicks, pinbar, or engulfing close."),
        ("Clear Runway & Correlation Check", "Clean upside space to target before next HTF supply (min 1:2 R:R) and no overlapping correlated pair risk."),
        ("Stop Loss & Lot Calculator Risk", "Stop loss placed safely below right shoulder low (- spread buffer) with position size verified via Lot Calculator.")
    ],
    "FVG": [
        ("Higher Timeframe Order Flow", "Confirm 4H/1D market structure and daily directional bias align strictly with the FVG trade."),
        ("Economic News & Killzone Timing", "No imminent red-folder news; displacement and entry occur during high-volume London or NY Killzones."),
        ("Fresh 3-Candle Imbalance Gap", "Strong displacement created an unmitigated Fair Value Gap between Candle 1 & 3 (avoid already-filled gaps)."),
        ("Liquidity Sweep Prior to FVG", "Price swept an external/internal high, low, or session level immediately before creating the FVG."),
        ("Premium / Discount Alignment", "Buying FVGs strictly in Discount (<50% of dealing range) or selling strictly in Premium (>50% of range)."),
        ("LTF Market Structure Shift (MSS)", "Inside the FVG (near 50% CE), wait for an explicit 1m/5m MSS or engulfing close rather than a blind touch."),
        ("Draw on Liquidity & Correlation", "Clear opposing liquidity pool as target (min 1:2 R:R) with no duplicate correlated exposure open."),
        ("Stop Loss & Lot Calculator Risk", "Stop loss protected behind FVG/swing extreme (+ spread buffer) with Lot Calculator risk verified.")
    ],
    "S & R": [
        ("Higher Timeframe Trend Alignment", "Align zone play with 4H/1D structure (buying HTF support in uptrends or shorting HTF resistance in downtrends)."),
        ("Economic News & Session Spreads", "No imminent red-folder news; active session liquidity with normal spreads (avoid rollover spread spikes)."),
        ("Zone Quality (2–3 Touches / Flip)", "Clean S/R flip or fresh 2–3 touch HTF zone — avoid over-tested levels (4–5+ touches) likely to break."),
        ("Liquidity Sweep Protection", "Confirm stops beyond the zone were swept & reclaimed, or no obvious equal highs/lows sit directly behind SL."),
        ("Closed Rejection Candle Trigger", "Wait for a completed pinbar, engulfing, or strong rejection close at the zone — never enter on an open wick."),
        ("Clear Traffic & Space to Target", "Verify no minor opposing S/R zone blocks price before reaching at least a 1:2 Risk-to-Reward target."),
        ("Correlation & Exposure Check", "Ensure combined risk across correlated pairs (e.g., EUR/USD + GBP/USD) stays within daily drawdown limits."),
        ("Wick Buffer & Lot Calculator Risk", "Stop loss placed beyond rejection wick tip (+ spread/ATR buffer) with lot size verified via Lot Calculator.")
    ],
    "Breakout": [
        ("Higher Timeframe Bias & Momentum", "Breakout direction aligns with macro 4H/1D structure (avoid breaking straight into a major opposing HTF wall)."),
        ("Economic News & Active Session", "No imminent red-folder trap risk; expansion occurs during London/NY open volume with tight spreads."),
        ("Consolidation Squeeze & Liquidity", "Price compressed cleanly in a range, flag, or triangle with built-up liquidity resting outside the boundary."),
        ("Daily ATR Exhaustion Filter", "Verify the pair still has room left in its Daily ATR (avoid chasing if >80–90% of daily range is already spent)."),
        ("Decisive Breakout Candle Close", "Wait for a strong, full-bodied candle close past the boundary — never enter mid-candle on a spike."),
        ("Retest / Anti-Fakeout Check", "Wait for a clean retest holding the broken boundary, or confirm immediate continuation without deep snap-back."),
        ("Clean Target Runway & Correlation", "Open runway to next HTF key level supports min 1:2 R:R, with no overlapping correlated positions."),
        ("Protected SL & Lot Calculator Risk", "Stop loss tucked behind breakout candle / retest low (not mid-chop) with position size verified via Lot Calculator.")
    ]
}

TRADE_TYPE_CHECKLISTS_NL = {
    "HnS": [
        ("Hogere Timeframe Trend & Supply", "Bevestig dat de 4H/1D-structuur bearish is of afwijst vanaf een belangrijke HTF-supplyzone (vermijd zijwaartse chop)."),
        ("Voorafgaande Uptrend & Liquiditeitssweep", "Linkerschouder/hoofd gevormd na een bullish beweging, waarbij het hoofd buy-side liquiditeit of eerdere highs heeft opgehaald."),
        ("Economisch Nieuws & Sessieliquiditeit", "Geen naderend high-impact nieuws (CPI, NFP, FOMC); voer de trade uit tijdens actieve Londen/NY-sessies met krappe spreads."),
        ("Schoudersymmetrie & Uitputting", "Linkerschouder, hoger hoofd en proportionele rechterschouder tonen afnemend koopmomentum of bearish divergentie."),
        ("Neckline-uitbraak & Candle Close", "Wacht op een overtuigende volledige candle close onder de neckline en controleer of de neckline niet steil tegen je in loopt."),
        ("Retest & Bearish Afwijzing", "Prijs heeft de doorbroken neckline hertest als nieuwe weerstand met duidelijke bearish afwijzingswicks of engulfing close."),
        ("Vrije Ruimte & Correlatiecheck", "Vrije baan tot aan het koersdoel (minimaal 1:2 R:R) en geen dubbele blootstelling op gecorreleerde paren."),
        ("Stop Loss & Lot Calculator Risico", "Stop loss boven de piek van de rechterschouder (+ spreadbuffer) met positiegrootte berekend via de Lot Calculator.")
    ],
    "i HnS": [
        ("Hogere Timeframe Trend & Demand", "Bevestig dat de 4H/1D-structuur bullish is of opveert vanaf een belangrijke HTF-demandzone (vermijd zijwaartse chop)."),
        ("Voorafgaande Downtrend & Liquiditeitssweep", "Structuur volgt op een bearish beweging, waarbij het diepere hoofd sell-side liquiditeit of belangrijke lows heeft opgehaald."),
        ("Economisch Nieuws & Sessieliquiditeit", "Geen naderend high-impact nieuws (CPI, NFP, FOMC); voer de trade uit tijdens actieve Londen/NY-sessies met krappe spreads."),
        ("Omgekeerde Structuur & Uitputting", "Linkerschouder, dieper hoofd en hogere rechterschouder tonen afnemende verkoopdruk of bullish divergentie."),
        ("Neckline-uitbraak & Candle Close", "Volledige candle close boven de weerstands-neckline bevestigd — anticipeer niet te vroeg in de rechterschouder."),
        ("Retest & Bullish Bevestiging", "Voormalige weerstand houdt stand als nieuwe steun bij pullback met bullish afwijzingswicks, pinbar of engulfing close."),
        ("Vrije Ruimte & Correlatiecheck", "Voldoende opwaartse ruimte tot de volgende HTF-supplyzone (min. 1:2 R:R) en geen overlappend gecorreleerd risico."),
        ("Stop Loss & Lot Calculator Risico", "Stop loss veilig onder de low van de rechterschouder (- spreadbuffer) met positiegrootte berekend via de Lot Calculator.")
    ],
    "FVG": [
        ("Hogere Timeframe Orderflow", "Bevestig dat de 4H/1D-marktstructuur en dagelijkse bias volledig overeenkomen met de richting van de FVG-trade."),
        ("Economisch Nieuws & Killzone Timing", "Geen naderend high-impact nieuws; displacement en entry vinden plaats tijdens de liquide Londen- of NY-Killzone."),
        ("Verse 3-Candle Imbalance Gap", "Sterke displacement creëerde een ongevulde Fair Value Gap tussen Candle 1 & 3 (vermijd reeds opgevulde gaps)."),
        ("Liquiditeitssweep vóór FVG", "Prijs heeft direct vóór het vormen van de FVG een externe/interne high, low of sessieniveau opgehaald."),
        ("Premium / Discount Uitlijning", "Koop FVG's uitsluitend in Discount (<50% van de range) of verkoop uitsluitend in Premium (>50% van de range)."),
        ("LTF Market Structure Shift (MSS)", "Wacht binnen de FVG (rond 50% CE) op een duidelijke 1m/5m MSS of engulfing close in plaats van blind in te stappen."),
        ("Liquiditeitsdoel & Correlatie", "Duidelijk tegenoverliggend liquiditeitsdoel (min. 1:2 R:R) zonder dubbele gecorreleerde posities open."),
        ("Stop Loss & Lot Calculator Risico", "Stop loss beschermd achter de FVG/swing-extreem (+ spreadbuffer) met risico berekend via de Lot Calculator.")
    ],
    "S & R": [
        ("Hogere Timeframe Trenduitlijning", "Lijn de zone-trade uit met de 4H/1D-structuur (koop HTF-steun in uptrends of short HTF-weerstand in downtrends)."),
        ("Economisch Nieuws & Sessiespreads", "Geen naderend high-impact nieuws; actieve sessieliquiditeit met normale spreads (vermijd rollover-spreadpieken)."),
        ("Zonekwaliteit (2–3 Touches / Flip)", "Zuivere S/R-flip of verse 2–3 touch HTF-zone — vermijd te vaak geteste niveaus (4–5+ touches) die kunnen breken."),
        ("Liquiditeitssweep Bescherming", "Bevestig dat stops achter de zone zijn opgehaald en teruggewonnen, of dat er geen gelijke highs/lows direct achter de SL liggen."),
        ("Gesloten Afwijzingscandle Trigger", "Wacht op een afgeronde pinbar, engulfing of sterke afwijzingssluiting bij de zone — stap nooit in op een open wick."),
        ("Vrije Ruimte tot Koersdoel", "Controleer of er geen kleine tegenliggende S/R-zone in de weg zit vóór minimaal een 1:2 Risk-to-Reward doel."),
        ("Correlatie & Blootstellingscheck", "Zorg dat gecombineerd risico op gecorreleerde paren (bijv. EUR/USD + GBP/USD) binnen je daglimiet blijft."),
        ("Wick-buffer & Lot Calculator Risico", "Stop loss voorbij de afwijzingswick (+ spread/ATR-buffer) met lotgrootte berekend via de Lot Calculator.")
    ],
    "Breakout": [
        ("Hogere Timeframe Bias & Momentum", "Richting van de uitbraak klopt met de macro 4H/1D-structuur (breek niet direct in een zware HTF-weerstand/steun)."),
        ("Economisch Nieuws & Actieve Sessie", "Geen naderend nieuws-valrisico; expansie vindt plaats bij Londen/NY-opening met krappe spreads."),
        ("Consolidatie-squeeze & Liquiditeit", "Prijs is strak gecomprimeerd in een range, vlag of driehoek met opgebouwde liquiditeit buiten de grens."),
        ("Dagelijkse ATR Uitputtingsfilter", "Controleer of het paar nog ruimte over heeft binnen de Dagelijkse ATR (stap niet in als >80–90% al verbruikt is)."),
        ("Overtuigende Uitbraak Candle Close", "Wacht op een krachtige, volle candle close voorbij de grens — stap nooit midden in een candle tijdens een spike."),
        ("Retest / Anti-Fakeout Check", "Wacht op een zuivere retest van het doorbroken niveau of bevestig directe voortzetting zonder diepe terugval."),
        ("Vrij Koersdoel & Correlatiecheck", "Vrije ruimte tot het volgende HTF-niveau ondersteunt min. 1:2 R:R, zonder overlappende gecorreleerde trades."),
        ("Beschermde SL & Lot Calculator Risico", "Stop loss geplaatst achter de uitbraakcandle / retest-low (niet in ruis) met positiegrootte via de Lot Calculator.")
    ]
}


def get_trade_checklist(trade_type, lang="English"):
    source = TRADE_TYPE_CHECKLISTS_NL if str(lang).strip().lower() == "dutch" else TRADE_TYPE_CHECKLISTS
    return source.get(trade_type, source["HnS"])


SUPPORTED_LANGUAGES = ("English", "Dutch")


def _get_language_pref_path():
    return os.path.join(os.path.expanduser("~"), ".trade_journal_lang.json")


def load_language_preference():
    path = _get_language_pref_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            lang = str(data.get("language", "English")).strip()
            if lang in SUPPORTED_LANGUAGES:
                return lang
        except Exception as e:
            logger.warning(f"Error loading language preference: {e}")
    return "English"


def save_language_preference(lang):
    clean_lang = lang if lang in SUPPORTED_LANGUAGES else "English"
    try:
        with open(_get_language_pref_path(), "w", encoding="utf-8") as f:
            json.dump({"language": clean_lang}, f)
    except Exception as e:
        logger.warning(f"Error saving language preference: {e}")


TRANSLATIONS = {
    "English": {
        "lang_name": "English",
        "guest": "GUEST",
        "role_admin": "ADMIN",
        "role_pro": "PRO",
        "nav_dashboard": "📊  Dashboard",
        "nav_analytics": "🔍  AI Analytics",
        "nav_news": "📰  Economic News",
        "nav_checklist": "📋  Pre-trade Checklist",
        "nav_trades": "📈  Trades Log",
        "nav_calendar": "📅  Calendar",
        "nav_calculator": "🧮  Lot Calculator",
        "btn_manage_users": "👥  Manage Users",
        "btn_settings": "⚙️  Settings",
        "btn_logout": "🚪  Log Out",
        "sidebar_version": "Version {ver} • Live Sync",

        # Login window
        "login_title": "K's Trade Journal - Login",
        "login_subtitle": "Sign in to access your trading workspace",
        "login_username": "Username",
        "login_password": "Password",
        "login_remember": "Keep me signed in",
        "login_btn": "Sign In",
        "login_err_empty": "Please enter both username and password.",
        "login_err_lockout": "Too many failed attempts. Please wait 60 seconds.",
        "login_err_lockout_retry": "Too many failed attempts. Try again in 60 seconds.",
        "login_err_invalid": "Invalid username or password.",
        "login_admin_notice_title": "Initial Admin Setup",
        "login_admin_notice_msg": (
            "First-time setup created the 'admin' account with a generated password:\n\n"
            "Username: admin\nPassword: {pw}\n\n"
            "Please sign in and change this password in Settings."
        ),

        # Dashboard KPIs & Analyzer
        "kpi_winrate": "WIN RATE",
        "kpi_winrate_sub": "{w}W · {l}L · {o} Open",
        "kpi_avg_rr": "AVG R:R",
        "kpi_rr_sub": "Exp: {exp} · Net {net}",
        "kpi_rr_sub_empty": "Exp: — · Net: —",
        "kpi_best_setup": "BEST SETUP & DISCIPLINE",
        "kpi_best_setup_sub": "Avg Checklist: {val}",
        "kpi_upcoming_news": "UPCOMING NEWS (NEXT 3)",
        "kpi_clear_pairs": "CLEAR PAIRS (NO NEWS)",
        "kpi_active_sessions": "ACTIVE SESSIONS",
        "no_news_today": "No news today",
        "calculating": "Calculating...",
        "no_upcoming_news": "No upcoming news",
        "no_clear_pairs_today": "No clear pairs today",
        "btn_paste_chart": "📋 Paste Chart (Ctrl+V)",
        "btn_analyzing_chart": "⏳ Analyzing Chart...",
        "paste_placeholder": "Paste screenshot (Ctrl+V) or click to upload",
        "lbl_pair_asset": "Pair / Asset:",
        "lbl_timeframe": "Timeframe:",
        "lbl_direction": "Direction (BUY/SELL):",
        "lbl_entry_price": "Entry Price:",
        "lbl_stop_loss": "Stop Loss:",
        "lbl_take_profit": "Take Profit:",
        "lbl_rr": "Risk / Reward:",
        "lbl_status": "Status (OPEN/TP/SL):",
        "lbl_setup_type": "Setup Type:",
        "notes_placeholder": "Optional Trade Confluence Notes...",
        "checklist_badge_none": "Checklist: --  ›",
        "checklist_badge_val": "Checklist: {score}  ›",
        "btn_save_trade": "Save Trade to Journal",
        "btn_saving": "⏳ Saving...",
        "msg_trade_saved": "✓ Trade saved to journal",
        "recent_trades": "Recent Trades",
        "recent_trades_hint": "Double-click row to inspect chart  •  Right-click to edit",

        # Table Column Headings
        "col_id": "ID",
        "col_user": "User",
        "col_pair": "Pair",
        "col_tf": "TF",
        "col_direction": "Direction",
        "col_entry": "Entry",
        "col_sl": "SL",
        "col_tp": "TP",
        "col_rr": "R:R",
        "col_status": "Status",
        "col_type": "Type",
        "col_checklist": "Checklist",
        "col_date": "Date",
        "col_impact": "Impact",
        "col_time": "Time",
        "col_currency": "Currency",
        "col_event": "Event",
        "col_actual": "Actual",
        "col_forecast": "Forecast",
        "col_previous": "Previous",
        "col_username": "Username",
        "col_role": "Role",
        "col_created": "Created",

        # Context Menus & Popups
        "ctx_modify_trade": "✏️ Modify Trade",
        "ctx_delete_trades": "🗑️ Delete Trade(s)",
        "popup_no_match": "No items match search",

        # Economic News View
        "news_title": "Forex Economic Calendar",
        "btn_refresh_news": "↻ Refresh News",
        "news_range_today": "Today",
        "news_range_this_week": "This Week",
        "news_range_next_week": "Next Week",
        "news_range_last_week": "Last Week",
        "news_all_currencies": "All Currencies",
        "news_all_impacts": "All Impacts",
        "news_high_impact": "High Impact",
        "news_high_medium": "High & Medium",
        "news_loading": "Loading {range} economic calendar events...",
        "news_empty": "No news events match current filter.",

        # Calendar & CalendarDay View
        "day_mon": "Mon",
        "day_tue": "Tue",
        "day_wed": "Wed",
        "day_thu": "Thu",
        "day_fri": "Fri",
        "day_sat": "Sat",
        "day_sun": "Sun",
        "months": [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ],
        "cal_zero_trades": "0 trades",
        "cal_one_trade": "{count} trade",
        "cal_many_trades": "{count} trades",
        "cal_day_title": "📊 Trades Recorded on {date}",
        "cal_day_subtitle": "{count} trade(s) • Double-click row to inspect chart • Right-click to edit",
        "btn_back_calendar": "← Back to Calendar",

        # Trades Log View
        "trades_db_title": "Complete Journal Database",
        "trades_zero": "0 trades",
        "trades_total": "({total} total)",
        "trades_showing": "(Showing {shown} of {total})",
        "btn_delete_selected": "🗑️ Delete Selected",
        "btn_refresh": "↻ Refresh",
        "btn_import_export": "⇅ Import / Export",
        "io_export_zip": "Export Journal (.zip)",
        "io_export_csv": "Export Trades (.csv)",
        "io_import_zip": "Import Journal (.zip)",
        "trades_search_ph": "🔍 Search pair, setup, date, or notes...",
        "filter_all_statuses": "All Statuses",
        "filter_all_directions": "All Directions",
        "date_all_time": "All Time",
        "date_today": "Today",
        "date_last_7": "Last 7 Days",
        "date_this_month": "This Month",
        "date_last_30": "Last 30 Days",
        "date_last_90": "Last 90 Days",
        "scope_my_trades": "My Trades",
        "scope_all_users": "All Users",
        "scope_user_prefix": "User: {user}",
        "trades_tip": "Double-click to view chart  •  Right-click for options",

        # Lot Calculator View
        "calc_inputs_title": "Account & Trade Inputs",
        "btn_calc_paste": "📋 Paste Screenshot (Ctrl+V)",
        "btn_calc_extracting": "⏳ Extracting Trade...",
        "calc_extracting_msg": "⏳ Reading Pair, Entry, SL & TP...",
        "calc_extracted_msg": "✓ Extracted {parts}",
        "calc_extract_warn": "⚠️ Could not detect trade details on chart",
        "calc_balance": "Account Balance ($):",
        "calc_risk": "Risk:",
        "calc_risk_pct": "Risk %",
        "calc_risk_cash": "Cash $",
        "calc_pair": "Pair:",
        "calc_entry": "Entry Price:",
        "calc_sl": "Stop Loss Price:",
        "calc_tp": "Take Profit (Optional):",
        "btn_calculate": "Calculate Lot Size",
        "calc_results_title": "Position Results",
        "calc_res_lots_title": "RECOMMENDED LOT SIZE",
        "calc_res_cash_title": "TOTAL CASH RISK",
        "calc_res_reward_title": "PROJECTED PROFIT / R:R",
        "calc_live": "LIVE",

        # AI Analytics View
        "analytics_select_label": "Post-Mortem Trade:",
        "analytics_no_trades": "No Saved Trades Found",
        "btn_run_review": "Run Trade Review",
        "btn_reviewing": "⏳ Reviewing...",
        "btn_paste_rate": "📋 Paste & Rate Chart",
        "btn_analyzing": "⏳ Analyzing...",
        "analytics_intro": "💡 Select any trade from your journal above and click 'Run Trade Review', or click 'Paste & Rate Chart' (Ctrl+V) to evaluate an un-saved chart setup.",
        "analytics_loading_review": "⏳ AI is conducting post-mortem evaluation ({lang}) for Trade {id} ({pair})...\n\n",
        "analytics_loading_paste": "⏳ AI is analyzing and rating pasted chart setup ({lang})...\n\n",

        # Checklist View
        "checklist_title": "☑️ Pre-Trade Execution Checklist",
        "checklist_strategy": "Setup Strategy:",
        "btn_apply_trade": "✓ Apply to Trade",
        "btn_reset_checklist": "Reset Checklist",
        "checklist_readiness": "Readiness: {pct}% ({checked}/{total} items checked)",
        "checklist_validated": "Readiness: 100% ({checked}/{total}) — Fully Validated Entry! 🚀",

        # Settings View
        "settings_title": "⚙️  Settings",
        "settings_signed_in": "Signed in as {user}  •  {role}",
        "btn_back_dashboard": "← Back to Dashboard",
        "settings_appearance_title": "🎨  Appearance",
        "settings_appearance_desc": "Customize theme mode and display language.\nYour preferences are saved automatically.",
        "settings_theme_dark": "Current Theme: Dark Mode",
        "settings_theme_light": "Current Theme: Light Mode",
        "btn_switch_light": "☀️  Switch to Light Mode",
        "btn_switch_dark": "🌙  Switch to Dark Mode",
        "settings_lang_status": "Current Language: {lang}",
        "btn_lang_en": "English",
        "btn_lang_nl": "Nederlands (Dutch)",
        "settings_updates_title": "🚀  App Info & Updates",
        "settings_installed_ver": "Installed Version: v{ver} • Live Sync",
        "upd_idle": "Check GitHub releases for the latest version.",
        "upd_checking": "Checking GitHub releases...",
        "upd_available": "🚀 Update v{tag} is available!",
        "upd_up_to_date": "✓ Up to date (v{ver})",
        "upd_http_err": "Unable to check updates (HTTP {code}).",
        "upd_check_fail": "Update check failed: {err}",
        "upd_downloading": "⏳ Downloading latest update...",
        "upd_downloading_pct": "⏳ Downloading update... {pct}%",
        "upd_downloading_prog": "⏳ Downloading update...",
        "upd_verified": "✓ Update verified. Restarting app...",
        "upd_dl_fail": "Update download failed: {err}",
        "btn_dl_install": "⬇  Download & Install Update",
        "btn_dl_install_ver": "⬇  Download & Install v{tag}",
        "btn_check_updates": "🔄  Check for Updates",
        "btn_checking_updates": "⏳  Checking for Updates...",
        "btn_downloading_updates": "⏳  Downloading Update...",
        "settings_tz_title": "🕒  Timezone",
        "settings_tz_auto_prefix": "Auto (Windows / System",
        "settings_tz_status": "{abbr} ({offset}) • System: {detected}",
        "settings_pw_title": "🔑  Change Password",
        "settings_pw_desc": "Update your account password below.\nOther active sessions will be signed out for security.",
        "pw_curr_ph": "Current Password",
        "pw_new_ph": "New Password (min 6 chars)",
        "pw_conf_ph": "Confirm New Password",
        "btn_update_pw": "Update Password",
        "btn_updating": "⏳ Updating...",
        "pw_err_fill": "Please fill in all password fields.",
        "pw_err_len": "New password must be at least 6 characters.",
        "pw_err_match": "New passwords do not match.",
        "pw_updating": "Updating password...",
        "pw_updated": "✓ Password updated successfully.",
        "pw_failed": "Failed to update password.",

        # Admin Manage Users View
        "admin_title": "👥  Member & Access Management",
        "admin_subtitle": "Create accounts, modify roles/passwords, or remove users.",
        "admin_new_user_ph": "New Username",
        "admin_new_pass_ph": "New Password",
        "btn_create_account": "+ Create Account",
        "admin_table_hint": "Select a user row to edit details on the right.",
        "btn_remove_user": "🗑️ Remove Selected User",
        "admin_edit_title": "✏️  Modify Selected User",
        "admin_edit_prompt": "Select a user from the table to edit.",
        "admin_editing_account": "Editing Account #{id} ({user})",
        "admin_lbl_username": "Username",
        "admin_lbl_new_pw": "New Password (optional)",
        "admin_keep_pw_ph": "Leave blank to keep current",
        "admin_lbl_role": "Role",
        "btn_save_changes": "Save Changes",
        "admin_err_select_first": "Please select a user from the table first.",
        "admin_saving": "Saving changes...",
        "admin_err_update": "Failed to update user.",

        # Trade Preview View
        "preview_title_default": "📸 Trade Chart Preview",
        "preview_title_fmt": "📸 Trade #{num} Chart Preview — {pair}",
        "btn_back": "← Back",
        "btn_back_trades": "← Back to Trades",
        "btn_back_day": "← Back to Day Trades",
        "btn_back_preview": "← Back to Chart Preview",
        "btn_edit_trade": "✏️ Edit Trade",
        "btn_prev": "‹ Prev",
        "btn_next": "Next ›",
        "btn_tab_before": "📸 Before",
        "btn_tab_after": "🏁 After",
        "preview_status_lbl": "Status:",
        "btn_zoom_fit": "Fit",
        "btn_paste_after": "📋 Paste After",
        "btn_upload": "📁 Upload",
        "btn_remove": "🗑️ Remove",
        "preview_loading": "⏳ Loading trade screenshots...",
        "preview_empty_after": "No After-Trade screenshot attached yet.\nUse '📋 Paste After' or '📁 Upload' above to attach the outcome chart.",
        "preview_empty_before": "No Before-Trade screenshot found for this trade record.",

        # Edit Trade View
        "edit_title_default": "✏️  Edit Trade",
        "edit_title_fmt": "✏️  Edit Trade #{num} — {pair}",
        "edit_subtitle": "Update setup parameters, outcome status, notes, or After-Trade screenshot.",
        "edit_params_title": "📊  Trade Parameters",
        "edit_after_title": "🏁  After-Trade Chart",
        "edit_after_none": "None",
        "edit_after_attached": "Attached",
        "edit_after_loading": "⏳ Loading After-Trade chart...",
        "edit_after_new_ready": "✓ New Ready",
        "edit_after_will_remove": "Will Remove",
        "btn_paste_after_ctrlv": "📋 Paste After (Ctrl+V)",
        "btn_upload_after": "📁 Upload After",
        "edit_after_empty": "No After-Trade screenshot attached.\nUse 'Paste After' or 'Upload After' above.",
        "edit_after_removed_msg": "After-Trade screenshot marked for removal.\nClick 'Save Changes' to apply.",
        "edit_failed": "Failed to update trade.",
        "edit_lbl_pair": "Pair:",
        "edit_ph_pair": "e.g. EUR/USD",
        "edit_lbl_tf": "Timeframe:",
        "edit_ph_tf": "e.g. 15m",
        "edit_lbl_dir": "Direction:",
        "edit_ph_dir": "BUY or SELL",
        "edit_lbl_entry": "Entry:",
        "edit_ph_entry": "Entry price",
        "edit_lbl_sl": "SL:",
        "edit_ph_sl": "Stop loss",
        "edit_lbl_tp": "TP:",
        "edit_ph_tp": "Take profit",
        "edit_lbl_rr": "R:R:",
        "edit_ph_rr": "Auto-calculated (e.g. 1:2.00)",
        "edit_lbl_checklist": "Checklist:",
        "edit_ph_checklist": "e.g. 8/8 (100%)",
        "edit_lbl_date": "Date:",
        "edit_ph_date": "YYYY-MM-DD or YYYY-MM-DD HH:MM",
        "edit_err_date": "Invalid date format. Use YYYY-MM-DD or YYYY-MM-DD HH:MM.",
        "edit_lbl_notes": "Notes:",
        "edit_ph_notes": "Execution notes...",
        "edit_lbl_status": "Status:",
        "edit_lbl_type": "Setup Type:",

        # Dialogs & Messageboxes
        "dlg_logout_title": "Log Out",
        "dlg_logout_msg": "Are you sure you want to log out?",
        "dlg_session_title": "Session Terminated",
        "dlg_session_msg": "Your account has been logged in from another location or device.",
        "dlg_confirm_del_title": "Confirm Delete",
        "dlg_confirm_del_multi": "Are you sure you want to delete these {n} trades from your journal?",
        "dlg_confirm_del_one": "Are you sure you want to delete this trade from your journal?",
        "dlg_del_user_msg": "Are you sure you want to delete login for '{user}'?",
        "dlg_select_user_title": "Select User",
        "dlg_select_user_msg": "Please select a user from the list to delete.",
        "dlg_success": "Success",
        "dlg_error": "Error",
        "dlg_deleted": "Deleted",
        "dlg_user_created": "User '{user}' created successfully!",
        "dlg_validation_err": "Validation Error",
        "dlg_save_no_img": "Cannot save trade without an analyzed chart screenshot. Please paste and analyze a chart first.",
        "dlg_db_err": "Database Error",
        "dlg_db_save_fail": "Failed to save trade: {err}",
        "dlg_analysis_err": "Analysis Error",
        "dlg_extract_err": "Screenshot Extraction Error",
        "dlg_analytics_warn": "Analytics Warning",
        "dlg_analytics_select": "Please select a valid trade from the dropdown list.",
        "dlg_analytics_err": "Analytics Error",
        "dlg_paste_review_err": "Paste Review Error",
        "dlg_paste_review_fail": "Failed to analyze pasted chart: {err}",
        "dlg_csv_title": "Export Trades to CSV",
        "dlg_csv_type": "CSV File",
        "dlg_csv_done_title": "CSV Export Complete",
        "dlg_csv_done_msg": "Exported {total} trade(s) to:\n{path}",
        "dlg_export_err_title": "Export Error",
        "dlg_csv_fail": "Failed to export CSV: {err}",
        "dlg_zip_export_title": "Export Trade Journal Backup",
        "dlg_zip_type": "Zip Archive",
        "dlg_export_done_title": "Export Complete",
        "dlg_export_done_msg": "Successfully exported {count} trade(s) and chart screenshots to:\n{path}",
        "dlg_export_fail": "Failed to export journal: {err}",
        "dlg_zip_import_title": "Import Trade Journal Backup",
        "dlg_import_err_title": "Import Error",
        "dlg_import_missing_json": "Invalid backup archive: missing 'journal_trades.json'",
        "dlg_import_done_title": "Import Complete",
        "dlg_import_done_msg": "Successfully imported {count} trade(s) into your journal database!",
        "dlg_import_skipped": "\nSkipped {skipped} duplicate trade(s).",
        "dlg_import_fail": "Failed to import journal: {err}",
        "dlg_upd_avail_title": "Update Available",
        "dlg_upd_whats_new": "\n\nWhat's New:\n{notes}",
        "dlg_upd_avail_msg": "A new version (v{tag}) is available!\nYour current version is v{cur}.{notes_block}\n\nWould you like to download and install the update now?",
        "dlg_auto_upd_title": "Auto Update",
        "dlg_auto_upd_latest": "You are running the latest version (v{ver}).",
        "dlg_auto_upd_exe_only": "Auto-updating in-place is only supported for packaged executable builds.",
        "dlg_upd_check_fail_title": "Update Check Failed",
        "dlg_upd_check_fail_msg": "Unable to connect to update server (Status {code}).",
        "dlg_upd_err_title": "Update Error",
        "dlg_upd_err_msg": "Failed to check for updates: {err}",
        "dlg_upd_untrusted": "Untrusted or missing update/checksum URL.",
        "dlg_upd_dl_fail_msg": "Failed to download update: {err}",
        "dlg_clip_warn_title": "Clipboard Warning",
        "dlg_clip_warn_msg": "No image found in clipboard. Please copy a chart screenshot first.",
        "dlg_clip_err_title": "Clipboard Error",
        "dlg_clip_err_msg": "Failed to grab image: {err}",
        "dlg_browse_title": "Select Chart Screenshot",
        "dlg_browse_type": "Image Files",
        "dlg_img_err_title": "Image Error",
        "dlg_img_err_msg": "Failed to open image: {err}",
        "cal_day_default_title": "📊 Trades",
        "cal_day_default_sub": "Double-click any row to inspect chart  •  Right-click to edit",
        "preview_meta_status": "Status: {status}",
        "analytics_trade_fmt": "#{id} | {pair} ({dir}) - Status: {status} [{date}]",
    },
    "Dutch": {
        "lang_name": "Nederlands",
        "guest": "GAST",
        "role_admin": "BEHEERDER",
        "role_pro": "PRO",
        "nav_dashboard": "📊  Dashboard",
        "nav_analytics": "🔍  AI Analytics",
        "nav_news": "📰  Economisch Nieuws",
        "nav_checklist": "📋  Pre-trade Checklist",
        "nav_trades": "📈  Trades Logboek",
        "nav_calendar": "📅  Kalender",
        "nav_calculator": "🧮  Lot Calculator",
        "btn_manage_users": "👥  Gebruikers Beheren",
        "btn_settings": "⚙️  Instellingen",
        "btn_logout": "🚪  Uitloggen",
        "sidebar_version": "Versie {ver} • Live Sync",

        # Login window
        "login_title": "K's Trade Journal - Inloggen",
        "login_subtitle": "Log in om toegang te krijgen tot je trading-werkruimte",
        "login_username": "Gebruikersnaam",
        "login_password": "Wachtwoord",
        "login_remember": "Ingelogd blijven",
        "login_btn": "Inloggen",
        "login_err_empty": "Vul zowel gebruikersnaam als wachtwoord in.",
        "login_err_lockout": "Te veel mislukte pogingen. Wacht 60 seconden.",
        "login_err_lockout_retry": "Te veel mislukte pogingen. Probeer opnieuw over 60 seconden.",
        "login_err_invalid": "Ongeldige gebruikersnaam of wachtwoord.",
        "login_admin_notice_title": "Eerste Beheerdersinstelling",
        "login_admin_notice_msg": (
            "Bij de eerste configuratie is het 'admin'-account aangemaakt met een gegenereerd wachtwoord:\n\n"
            "Gebruikersnaam: admin\nWachtwoord: {pw}\n\n"
            "Log in en wijzig dit wachtwoord bij Instellingen."
        ),

        # Dashboard KPIs & Analyzer
        "kpi_winrate": "WINPERCENTAGE",
        "kpi_winrate_sub": "{w}W · {l}L · {o} Open",
        "kpi_avg_rr": "GEM. R:R",
        "kpi_rr_sub": "Exp: {exp} · Netto {net}",
        "kpi_rr_sub_empty": "Exp: — · Netto: —",
        "kpi_best_setup": "BESTE SETUP & DISCIPLINE",
        "kpi_best_setup_sub": "Gem. Checklist: {val}",
        "kpi_upcoming_news": "KOMEND NIEUWS (VOLGENDE 3)",
        "kpi_clear_pairs": "VRIJE PAREN (GEEN NIEUWS)",
        "kpi_active_sessions": "ACTIEVE SESSIES",
        "no_news_today": "Geen nieuws vandaag",
        "calculating": "Berekenen...",
        "no_upcoming_news": "Geen komend nieuws",
        "no_clear_pairs_today": "Geen vrije paren vandaag",
        "btn_paste_chart": "📋 Chart Plakken (Ctrl+V)",
        "btn_analyzing_chart": "⏳ Chart Analyseren...",
        "paste_placeholder": "Plak screenshot (Ctrl+V) of klik om te uploaden",
        "lbl_pair_asset": "Paar / Instrument:",
        "lbl_timeframe": "Timeframe:",
        "lbl_direction": "Richting (BUY/SELL):",
        "lbl_entry_price": "Instapprijs (Entry):",
        "lbl_stop_loss": "Stop Loss:",
        "lbl_take_profit": "Take Profit:",
        "lbl_rr": "Risk / Reward:",
        "lbl_status": "Status (OPEN/TP/SL):",
        "lbl_setup_type": "Setup-type:",
        "notes_placeholder": "Optionele Trade Confluentie-notities...",
        "checklist_badge_none": "Checklist: --  ›",
        "checklist_badge_val": "Checklist: {score}  ›",
        "btn_save_trade": "Trade Opslaan in Journaal",
        "btn_saving": "⏳ Opslaan...",
        "msg_trade_saved": "✓ Trade opgeslagen in journaal",
        "recent_trades": "Recente Trades",
        "recent_trades_hint": "Dubbelklik op rij om chart te bekijken  •  Rechtermuisklik om te bewerken",

        # Table Column Headings
        "col_id": "ID",
        "col_user": "Gebruiker",
        "col_pair": "Paar",
        "col_tf": "TF",
        "col_direction": "Richting",
        "col_entry": "Entry",
        "col_sl": "SL",
        "col_tp": "TP",
        "col_rr": "R:R",
        "col_status": "Status",
        "col_type": "Type",
        "col_checklist": "Checklist",
        "col_date": "Datum",
        "col_impact": "Impact",
        "col_time": "Tijd",
        "col_currency": "Valuta",
        "col_event": "Gebeurtenis",
        "col_actual": "Actueel",
        "col_forecast": "Verwachting",
        "col_previous": "Vorig",
        "col_username": "Gebruikersnaam",
        "col_role": "Rol",
        "col_created": "Aangemaakt",

        # Context Menus & Popups
        "ctx_modify_trade": "✏️ Trade Bewerken",
        "ctx_delete_trades": "🗑️ Trade(s) Verwijderen",
        "popup_no_match": "Geen zoekresultaten gevonden",

        # Economic News View
        "news_title": "Forex Economische Kalender",
        "btn_refresh_news": "↻ Nieuws Vernieuwen",
        "news_range_today": "Vandaag",
        "news_range_this_week": "Deze Week",
        "news_range_next_week": "Volgende Week",
        "news_range_last_week": "Vorige Week",
        "news_all_currencies": "Alle Valuta's",
        "news_all_impacts": "Alle Impactniveaus",
        "news_high_impact": "Hoge Impact",
        "news_high_medium": "Hoog & Gemiddeld",
        "news_loading": "Economische kalendergegevens laden ({range})...",
        "news_empty": "Geen nieuwsitems gevonden voor het huidige filter.",

        # Calendar & CalendarDay View
        "day_mon": "Ma",
        "day_tue": "Di",
        "day_wed": "Wo",
        "day_thu": "Do",
        "day_fri": "Vr",
        "day_sat": "Za",
        "day_sun": "Zo",
        "months": [
            "", "Januari", "Februari", "Maart", "April", "Mei", "Juni",
            "Juli", "Augustus", "September", "Oktober", "November", "December"
        ],
        "cal_zero_trades": "0 trades",
        "cal_one_trade": "{count} trade",
        "cal_many_trades": "{count} trades",
        "cal_day_title": "📊 Trades op {date}",
        "cal_day_subtitle": "{count} trade(s) • Dubbelklik om chart te bekijken • Rechtermuisklik om te bewerken",
        "btn_back_calendar": "← Terug naar Kalender",

        # Trades Log View
        "trades_db_title": "Volledige Journaaldatabase",
        "trades_zero": "0 trades",
        "trades_total": "({total} totaal)",
        "trades_showing": "({shown} van {total} weergegeven)",
        "btn_delete_selected": "🗑️ Selectie Verwijderen",
        "btn_refresh": "↻ Vernieuwen",
        "btn_import_export": "⇅ Importeren / Exporteren",
        "io_export_zip": "Journaal Exporteren (.zip)",
        "io_export_csv": "Trades Exporteren (.csv)",
        "io_import_zip": "Journaal Importeren (.zip)",
        "trades_search_ph": "🔍 Zoek op paar, setup, datum of notities...",
        "filter_all_statuses": "Alle Statussen",
        "filter_all_directions": "Alle Richtingen",
        "date_all_time": "Alle Tijd",
        "date_today": "Vandaag",
        "date_last_7": "Laatste 7 Dagen",
        "date_this_month": "Deze Maand",
        "date_last_30": "Laatste 30 Dagen",
        "date_last_90": "Laatste 90 Dagen",
        "scope_my_trades": "Mijn Trades",
        "scope_all_users": "Alle Gebruikers",
        "scope_user_prefix": "Gebruiker: {user}",
        "trades_tip": "Dubbelklik voor chart  •  Rechtermuisklik voor opties",

        # Lot Calculator View
        "calc_inputs_title": "Account- & Trade-invoer",
        "btn_calc_paste": "📋 Screenshot Plakken (Ctrl+V)",
        "btn_calc_extracting": "⏳ Trade Uitlezen...",
        "calc_extracting_msg": "⏳ Paar, Entry, SL & TP uitlezen...",
        "calc_extracted_msg": "✓ Uitgelezen: {parts}",
        "calc_extract_warn": "⚠️ Kon geen trade-gegevens op de chart herkennen",
        "calc_balance": "Accountsaldo ($):",
        "calc_risk": "Risico:",
        "calc_risk_pct": "Risico %",
        "calc_risk_cash": "Bedrag $",
        "calc_pair": "Paar:",
        "calc_entry": "Instapprijs (Entry):",
        "calc_sl": "Stop Loss Prijs:",
        "calc_tp": "Take Profit (Optioneel):",
        "btn_calculate": "Bereken Lotgrootte",
        "calc_results_title": "Positieresultaten",
        "calc_res_lots_title": "AANBEVOLEN LOTGROOTTE",
        "calc_res_cash_title": "TOTALE GELDSRISICO",
        "calc_res_reward_title": "VERWACHTE WINST / R:R",
        "calc_live": "LIVE",

        # AI Analytics View
        "analytics_select_label": "Post-Mortem Trade:",
        "analytics_no_trades": "Geen Opgeslagen Trades Gevonden",
        "btn_run_review": "Trade Review Starten",
        "btn_reviewing": "⏳ Beoordelen...",
        "btn_paste_rate": "📋 Chart Plakken & Beoordelen",
        "btn_analyzing": "⏳ Analyseren...",
        "analytics_intro": "💡 Selecteer hierboven een trade uit je journaal en klik op 'Trade Review Starten', of klik op 'Chart Plakken & Beoordelen' (Ctrl+V) om een niet-opgeslagen chart setup te beoordelen.",
        "analytics_loading_review": "⏳ AI voert een post-mortem analyse ({lang}) uit voor Trade {id} ({pair})...\n\n",
        "analytics_loading_paste": "⏳ AI analyseert en beoordeelt geplakte chart setup ({lang})...\n\n",

        # Checklist View
        "checklist_title": "☑️ Pre-Trade Uitvoeringschecklist",
        "checklist_strategy": "Setup-strategie:",
        "btn_apply_trade": "✓ Toepassen op Trade",
        "btn_reset_checklist": "Checklist Resetten",
        "checklist_readiness": "Gereedheid: {pct}% ({checked}/{total} items aangevinkt)",
        "checklist_validated": "Gereedheid: 100% ({checked}/{total}) — Volledig Gevalideerde Entry! 🚀",

        # Settings View
        "settings_title": "⚙️  Instellingen",
        "settings_signed_in": "Ingelogd als {user}  •  {role}",
        "btn_back_dashboard": "← Terug naar Dashboard",
        "settings_appearance_title": "🎨  Weergave",
        "settings_appearance_desc": "Pas het thema en de weergavetaal aan.\nJe voorkeuren worden automatisch opgeslagen.",
        "settings_theme_dark": "Huidig Thema: Donkere Modus",
        "settings_theme_light": "Huidig Thema: Lichte Modus",
        "btn_switch_light": "☀️  Schakel naar Lichte Modus",
        "btn_switch_dark": "🌙  Schakel naar Donkere Modus",
        "settings_lang_status": "Huidige Taal: {lang}",
        "btn_lang_en": "English",
        "btn_lang_nl": "Nederlands (Dutch)",
        "settings_updates_title": "🚀  App-info & Updates",
        "settings_installed_ver": "Geïnstalleerde Versie: v{ver} • Live Sync",
        "upd_idle": "Controleer GitHub-releases op de nieuwste versie.",
        "upd_checking": "Controleren op GitHub-releases...",
        "upd_available": "🚀 Update v{tag} is beschikbaar!",
        "upd_up_to_date": "✓ Up-to-date (v{ver})",
        "upd_http_err": "Kan updates niet controleren (HTTP {code}).",
        "upd_check_fail": "Updatecontrole mislukt: {err}",
        "upd_downloading": "⏳ Nieuwste update downloaden...",
        "upd_downloading_pct": "⏳ Update downloaden... {pct}%",
        "upd_downloading_prog": "⏳ Update downloaden...",
        "upd_verified": "✓ Update geverifieerd. App wordt herstart...",
        "upd_dl_fail": "Downloaden van update mislukt: {err}",
        "btn_dl_install": "⬇  Update Downloaden & Installeren",
        "btn_dl_install_ver": "⬇  Download & Installeer v{tag}",
        "btn_check_updates": "🔄  Controleren op Updates",
        "btn_checking_updates": "⏳  Controleren op Updates...",
        "btn_downloading_updates": "⏳  Update Downloaden...",
        "settings_tz_title": "🕒  Tijdzone",
        "settings_tz_auto_prefix": "Auto (Windows / Systeem",
        "settings_tz_status": "{abbr} ({offset}) • Systeem: {detected}",
        "settings_pw_title": "🔑  Wachtwoord Wijzigen",
        "settings_pw_desc": "Werk hieronder het wachtwoord van je account bij.\nAndere actieve sessies worden voor de veiligheid uitgelogd.",
        "pw_curr_ph": "Huidig Wachtwoord",
        "pw_new_ph": "Nieuw Wachtwoord (min. 6 tekens)",
        "pw_conf_ph": "Bevestig Nieuw Wachtwoord",
        "btn_update_pw": "Wachtwoord Bijwerken",
        "btn_updating": "⏳ Bijwerken...",
        "pw_err_fill": "Vul alle wachtwoordvelden in.",
        "pw_err_len": "Nieuw wachtwoord moet minimaal 6 tekens bevatten.",
        "pw_err_match": "Nieuwe wachtwoorden komen niet overeen.",
        "pw_updating": "Wachtwoord bijwerken...",
        "pw_updated": "✓ Wachtwoord succesvol bijgewerkt.",
        "pw_failed": "Wachtwoord bijwerken mislukt.",

        # Admin Manage Users View
        "admin_title": "👥  Leden- & Toegangsbeheer",
        "admin_subtitle": "Maak accounts aan, wijzig rollen/wachtwoorden of verwijder gebruikers.",
        "admin_new_user_ph": "Nieuwe Gebruikersnaam",
        "admin_new_pass_ph": "Nieuw Wachtwoord",
        "btn_create_account": "+ Account Aanmaken",
        "admin_table_hint": "Selecteer een rij om rechts de gegevens te bewerken.",
        "btn_remove_user": "🗑️ Gebruiker Verwijderen",
        "admin_edit_title": "✏️  Geselecteerde Gebruiker Bewerken",
        "admin_edit_prompt": "Selecteer een gebruiker uit de tabel om te bewerken.",
        "admin_editing_account": "Account #{id} ({user}) Bewerken",
        "admin_lbl_username": "Gebruikersnaam",
        "admin_lbl_new_pw": "Nieuw Wachtwoord (optioneel)",
        "admin_keep_pw_ph": "Leeg laten om huidige te behouden",
        "admin_lbl_role": "Rol",
        "btn_save_changes": "Wijzigingen Opslaan",
        "admin_err_select_first": "Selecteer eerst een gebruiker uit de tabel.",
        "admin_saving": "Wijzigingen opslaan...",
        "admin_err_update": "Gebruiker bijwerken mislukt.",

        # Trade Preview View
        "preview_title_default": "📸 Trade Chart Voorbeeld",
        "preview_title_fmt": "📸 Trade #{num} Chart Voorbeeld — {pair}",
        "btn_back": "← Terug",
        "btn_back_trades": "← Terug naar Trades",
        "btn_back_day": "← Terug naar Dagtrades",
        "btn_back_preview": "← Terug naar Chart Voorbeeld",
        "btn_edit_trade": "✏️ Trade Bewerken",
        "btn_prev": "‹ Vorige",
        "btn_next": "Volgende ›",
        "btn_tab_before": "📸 Voor",
        "btn_tab_after": "🏁 Na",
        "preview_status_lbl": "Status:",
        "btn_zoom_fit": "Passend",
        "btn_paste_after": "📋 Plak Na",
        "btn_upload": "📁 Uploaden",
        "btn_remove": "🗑️ Verwijder",
        "preview_loading": "⏳ Trade-screenshots laden...",
        "preview_empty_after": "Nog geen Na-Trade screenshot toegevoegd.\nGebruik hierboven '📋 Plak Na' of '📁 Uploaden' om de resultaat-chart toe te voegen.",
        "preview_empty_before": "Geen Voor-Trade screenshot gevonden voor deze trade.",

        # Edit Trade View
        "edit_title_default": "✏️  Trade Bewerken",
        "edit_title_fmt": "✏️  Trade #{num} Bewerken — {pair}",
        "edit_subtitle": "Werk setup-parameters, status, notities of Na-Trade screenshot bij.",
        "edit_params_title": "📊  Trade Parameters",
        "edit_after_title": "🏁  Na-Trade Chart",
        "edit_after_none": "Geen",
        "edit_after_attached": "Toegevoegd",
        "edit_after_loading": "⏳ Na-Trade chart laden...",
        "edit_after_new_ready": "✓ Nieuw Gereed",
        "edit_after_will_remove": "Wordt Verwijderd",
        "btn_paste_after_ctrlv": "📋 Plak Na (Ctrl+V)",
        "btn_upload_after": "📁 Na Uploaden",
        "edit_after_empty": "Geen Na-Trade screenshot toegevoegd.\nGebruik 'Plak Na' of 'Na Uploaden' hierboven.",
        "edit_after_removed_msg": "Na-Trade screenshot gemarkeerd voor verwijdering.\nKlik op 'Wijzigingen Opslaan' om toe te passen.",
        "edit_failed": "Trade bijwerken mislukt.",
        "edit_lbl_pair": "Paar:",
        "edit_ph_pair": "bijv. EUR/USD",
        "edit_lbl_tf": "Timeframe:",
        "edit_ph_tf": "bijv. 15m",
        "edit_lbl_dir": "Richting:",
        "edit_ph_dir": "BUY of SELL",
        "edit_lbl_entry": "Entry:",
        "edit_ph_entry": "Instapprijs",
        "edit_lbl_sl": "SL:",
        "edit_ph_sl": "Stop loss",
        "edit_lbl_tp": "TP:",
        "edit_ph_tp": "Take profit",
        "edit_lbl_rr": "R:R:",
        "edit_ph_rr": "Automatisch (bijv. 1:2.00)",
        "edit_lbl_checklist": "Checklist:",
        "edit_ph_checklist": "bijv. 8/8 (100%)",
        "edit_lbl_date": "Datum:",
        "edit_ph_date": "JJJJ-MM-DD of JJJJ-MM-DD UU:MM",
        "edit_err_date": "Ongeldig datumformaat. Gebruik JJJJ-MM-DD of JJJJ-MM-DD UU:MM.",
        "edit_lbl_notes": "Notities:",
        "edit_ph_notes": "Uitvoeringsnotities...",
        "edit_lbl_status": "Status:",
        "edit_lbl_type": "Setup-type:",

        # Dialogs & Messageboxes
        "dlg_logout_title": "Uitloggen",
        "dlg_logout_msg": "Weet je zeker dat je wilt uitloggen?",
        "dlg_session_title": "Sessie Beëindigd",
        "dlg_session_msg": "Je account is vanaf een andere locatie of apparaat ingelogd.",
        "dlg_confirm_del_title": "Verwijderen Bevestigen",
        "dlg_confirm_del_multi": "Weet je zeker dat je deze {n} trades uit je journaal wilt verwijderen?",
        "dlg_confirm_del_one": "Weet je zeker dat je deze trade uit je journaal wilt verwijderen?",
        "dlg_del_user_msg": "Weet je zeker dat je het account '{user}' wilt verwijderen?",
        "dlg_select_user_title": "Gebruiker Selecteren",
        "dlg_select_user_msg": "Selecteer eerst een gebruiker uit de lijst om te verwijderen.",
        "dlg_success": "Succes",
        "dlg_error": "Fout",
        "dlg_deleted": "Verwijderd",
        "dlg_user_created": "Gebruiker '{user}' succesvol aangemaakt!",
        "dlg_validation_err": "Validatiefout",
        "dlg_save_no_img": "Kan trade niet opslaan zonder geanalyseerde chart-screenshot. Plak en analyseer eerst een chart.",
        "dlg_db_err": "Databasefout",
        "dlg_db_save_fail": "Opslaan van trade mislukt: {err}",
        "dlg_analysis_err": "Analysefout",
        "dlg_extract_err": "Screenshot Uitleesfout",
        "dlg_analytics_warn": "Analytics Waarschuwing",
        "dlg_analytics_select": "Selecteer een geldige trade uit de keuzelijst.",
        "dlg_analytics_err": "Analytics Fout",
        "dlg_paste_review_err": "Plak-Review Fout",
        "dlg_paste_review_fail": "Analyseren van geplakte chart mislukt: {err}",
        "dlg_csv_title": "Trades Exporteren naar CSV",
        "dlg_csv_type": "CSV-bestand",
        "dlg_csv_done_title": "CSV-export Voltooid",
        "dlg_csv_done_msg": "{total} trade(s) geëxporteerd naar:\n{path}",
        "dlg_export_err_title": "Exportfout",
        "dlg_csv_fail": "CSV exporteren mislukt: {err}",
        "dlg_zip_export_title": "Trade Journaal Back-up Exporteren",
        "dlg_zip_type": "Zip-archief",
        "dlg_export_done_title": "Export Voltooid",
        "dlg_export_done_msg": "{count} trade(s) en chart-screenshots succesvol geëxporteerd naar:\n{path}",
        "dlg_export_fail": "Journaal exporteren mislukt: {err}",
        "dlg_zip_import_title": "Trade Journaal Back-up Importeren",
        "dlg_import_err_title": "Importfout",
        "dlg_import_missing_json": "Ongeldig back-uparchief: 'journal_trades.json' ontbreekt",
        "dlg_import_done_title": "Import Voltooid",
        "dlg_import_done_msg": "{count} trade(s) succesvol geïmporteerd in je journaaldatabase!",
        "dlg_import_skipped": "\n{skipped} dubbele trade(s) overgeslagen.",
        "dlg_import_fail": "Journaal importeren mislukt: {err}",
        "dlg_upd_avail_title": "Update Beschikbaar",
        "dlg_upd_whats_new": "\n\nNieuw in deze versie:\n{notes}",
        "dlg_upd_avail_msg": "Er is een nieuwe versie (v{tag}) beschikbaar!\nJe huidige versie is v{cur}.{notes_block}\n\nWil je de update nu downloaden en installeren?",
        "dlg_auto_upd_title": "Automatisch Updaten",
        "dlg_auto_upd_latest": "Je gebruikt de nieuwste versie (v{ver}).",
        "dlg_auto_upd_exe_only": "Automatisch bijwerken wordt alleen ondersteund in de verpakte applicatieversie.",
        "dlg_upd_check_fail_title": "Updatecontrole Mislukt",
        "dlg_upd_check_fail_msg": "Kan geen verbinding maken met de updateserver (Status {code}).",
        "dlg_upd_err_title": "Updatefout",
        "dlg_upd_err_msg": "Controleren op updates mislukt: {err}",
        "dlg_upd_untrusted": "Onbetrouwbare of ontbrekende update-/checksum-URL.",
        "dlg_upd_dl_fail_msg": "Downloaden van update mislukt: {err}",
        "dlg_clip_warn_title": "Klembord Waarschuwing",
        "dlg_clip_warn_msg": "Geen afbeelding op het klembord gevonden. Kopieer eerst een chart-screenshot.",
        "dlg_clip_err_title": "Klembord Fout",
        "dlg_clip_err_msg": "Kan afbeelding niet ophalen: {err}",
        "dlg_browse_title": "Chart Screenshot Selecteren",
        "dlg_browse_type": "Afbeeldingsbestanden",
        "dlg_img_err_title": "Afbeeldingsfout",
        "dlg_img_err_msg": "Kan afbeelding niet openen: {err}",
        "cal_day_default_title": "📊 Trades",
        "cal_day_default_sub": "Dubbelklik op een rij om de chart te bekijken  •  Rechtermuisklik om te bewerken",
        "preview_meta_status": "Status: {status}",
        "analytics_trade_fmt": "#{id} | {pair} ({dir}) - Status: {status} [{date}]",
    },
}


def tr(lang, key, /, **kwargs):
    lang_key = "Dutch" if str(lang).strip().lower() == "dutch" else "English"
    text = TRANSLATIONS.get(lang_key, TRANSLATIONS["English"]).get(
        key,
        TRANSLATIONS["English"].get(key, key),
    )
    if kwargs and isinstance(text, str):
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


@dataclass
class TradeRecord:
    id: int
    pair: str
    timeframe: str
    direction: str
    entry_price: str
    stop_loss: str
    take_profit: str
    rr_ratio: str
    status: str
    date: str
    notes: str = ""
    image_path: str = ""
    trade_type: str = "HnS"
    username: str = "admin"
    checklist_score: str = ""
    after_image_path: str = ""
    pnl_pips: str = ""
    _cached_short_date_key: tuple = field(default=(), init=False, repr=False, compare=False)
    _cached_short_date: str = field(default="", init=False, repr=False, compare=False)

    @classmethod
    def from_row(cls, row):
        return cls(
            id=int(row[0]),
            pair=str(row[1] or ""),
            timeframe=str(row[2] or ""),
            direction=str(row[3] or ""),
            entry_price=str(row[4] or ""),
            stop_loss=str(row[5] or ""),
            take_profit=str(row[6] or ""),
            rr_ratio=str(row[7] or ""),
            status=str(row[8] or "OPEN"),
            date=str(row[9] or ""),
            notes=str(row[10]) if len(row) > 10 and row[10] else "",
            image_path=str(row[11]) if len(row) > 11 and row[11] else "",
            trade_type=str(row[12]) if len(row) > 12 and row[12] else "HnS",
            username=str(row[13]) if len(row) > 13 and row[13] else "admin",
            checklist_score=str(row[14]) if len(row) > 14 and row[14] else "",
            after_image_path=str(row[15]) if len(row) > 15 and row[15] else "",
            pnl_pips=str(row[16]) if len(row) > 16 and row[16] else "",
        )

    @property
    def short_date(self):
        cache_key = (self.date, get_app_timezone_pref(), get_tz_cache_epoch())
        if self._cached_short_date_key != cache_key:
            self._cached_short_date = utc_timestamp_to_local_short_date(self.date)
            self._cached_short_date_key = cache_key
        return self._cached_short_date


def encode_chart_thumbnail_b64(image_path, max_dim=1100, quality=78):
    """Encodes a compressed JPEG base64 thumbnail of a chart screenshot for cross-device DB sync."""
    if not image_path or not os.path.exists(image_path):
        return ""
    try:
        with Image.open(image_path) as raw_img:
            img = raw_img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def decode_chart_thumbnail_b64(b64_str, cache_dir=None, trade_id=None, filename_prefix="synced_chart_"):
    """Decodes a base64 JPEG chart thumbnail into a PIL Image (and optionally caches it locally)."""
    if not b64_str or len(b64_str) > MAX_DECODED_IMAGE_B64_LEN:
        return None
    try:
        raw_bytes = base64.b64decode(b64_str, validate=True)
        with Image.open(io.BytesIO(raw_bytes)) as img:
            result = img.copy()

        if cache_dir and trade_id is not None:
            safe_id = int(trade_id)
            safe_prefix = re.sub(r"[^a-zA-Z0-9_-]", "", str(filename_prefix or "synced_chart_"))
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            cached_path = os.path.join(cache_dir, f"{safe_prefix}{safe_id}.jpg")
            with open(cached_path, "wb") as f:
                f.write(raw_bytes)
        return result
    except Exception:
        return None


def clean_pair(pair_str):
    if not pair_str:
        return ""
    s = str(pair_str).strip().upper()
    if len(s) == 6 and s.isalpha() and "/" not in s:
        return f"{s[:3]}/{s[3:]}"
    return s


def clean_direction(direction_str):
    if not direction_str:
        return "BUY"
    val = str(direction_str).strip().upper()
    if val in ["LONG", "BUY"]:
        return "BUY"
    elif val in ["SHORT", "SELL"]:
        return "SELL"
    return val


def clean_status(status_str):
    if not status_str:
        return "OPEN"
    val = str(status_str).strip().upper()
    if val in ["WIN", "TP"]:
        return "TP"
    elif val in ["LOSS", "SL"]:
        return "SL"
    return "OPEN" if val not in ["TP", "SL", "OPEN"] else val


def format_timeframe(tf_str):
    return normalize_timeframe_str(tf_str, map_hours=False)


def compute_rr_string(entry_val, sl_val, tp_val):
    try:
        e = float(str(entry_val).replace(",", "").strip())
        sl = float(str(sl_val).replace(",", "").strip())
        tp = float(str(tp_val).replace(",", "").strip())
        risk = abs(e - sl)
        reward = abs(tp - e)
        if risk > 0 and reward > 0:
            return f"1:{reward / risk:.2f}"
    except (ValueError, TypeError):
        pass
    return ""


def strip_markdown_links(text):
    return RE_MARKDOWN_LINK.sub(r'\1', str(text))


def capture_image_to_preview(pil_img, prefix, thumb_size, preview_label):
    """Shared helper to save a PIL image to temp storage and render its CTkImage thumbnail."""
    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, f"{prefix}_{int(time.time())}_{secrets.token_hex(3)}.png")
    pil_img.save(temp_path, format="PNG", compress_level=1)

    preview_img = pil_img.copy()
    preview_img.thumbnail(thumb_size, Image.Resampling.BILINEAR)
    ctk_img = ctk.CTkImage(light_image=preview_img, dark_image=preview_img, size=preview_img.size)
    preview_label.configure(image=ctk_img, text="")
    return temp_path, ctk_img


def grab_clipboard_pil_image(lang="English"):
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            return img
        messagebox.showwarning(tr(lang, "dlg_clip_warn_title"), tr(lang, "dlg_clip_warn_msg"))
    except Exception as e:
        messagebox.showerror(tr(lang, "dlg_clip_err_title"), tr(lang, "dlg_clip_err_msg", err=e))
    return None


def browse_pil_image(lang="English"):
    file_path = filedialog.askopenfilename(
        title=tr(lang, "dlg_browse_title"),
        filetypes=[(tr(lang, "dlg_browse_type"), "*.png *.jpg *.jpeg *.webp *.bmp")]
    )
    if not file_path:
        return None
    try:
        with Image.open(file_path) as img:
            return img.copy()
    except Exception as e:
        messagebox.showerror(tr(lang, "dlg_img_err_title"), tr(lang, "dlg_img_err_msg", err=e))
    return None


def parse_news_event(ev, app_tz=None):
    title = ev.get("title", ev.get("name", ev.get("event", "")))
    if not title:
        return None

    raw_date_str = str(ev.get("date", ev.get("dateUtc", "")))
    country = str(ev.get("country", ev.get("currency", ev.get("countryCode", "")))).upper()
    impact = str(ev.get("impact", ev.get("volatility", "")))
    actual = str(ev.get("actual", ""))
    forecast = str(ev.get("forecast", ""))
    previous = str(ev.get("previous", ""))

    if app_tz is None:
        app_tz = get_app_tzinfo()
    ev_dt = None
    if raw_date_str:
        try:
            iso_str = raw_date_str.replace("Z", "+00:00")
            ev_dt = datetime.fromisoformat(iso_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            ev_dt = ev_dt.astimezone(app_tz)
        except Exception:
            clean_str = raw_date_str.split("+")[0].split(".")[0].replace("Z", "")
            if len(clean_str) > 19:
                clean_str = clean_str[:19]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    ev_dt = datetime.strptime(clean_str, fmt).replace(tzinfo=timezone.utc).astimezone(app_tz)
                    break
                except ValueError:
                    pass

    time_formatted = "N/A"
    if ev_dt:
        raw_time = ev_dt.strftime("%I:%M%p").lower()
        time_formatted = raw_time.lstrip("0") if raw_time.startswith("0") else raw_time

    return {
        "date": ev_dt.strftime("%a %b %d").replace(" 0", " ") if ev_dt else "N/A",
        "time": time_formatted,
        "currency": country,
        "impact": impact,
        "title": title,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "dt": ev_dt
    }


def compute_dashboard_kpis(records):
    """Computes dashboard KPI aggregates (winrate, R:R, expectancy, best setup, avg checklist) from TradeRecord items."""
    total_wins = 0
    total_closed = 0
    total_open = 0
    total_rr = 0.0
    realized_r = 0.0
    realized_r_count = 0
    checklist_pct_sum = 0.0
    checklist_count = 0

    setup_wins = defaultdict(int)
    setup_total = defaultdict(int)

    for r in records:
        tf_val = format_timeframe(r.timeframe)
        status = clean_status(r.status)

        chk_raw = str(getattr(r, "checklist_score", "") or "").strip()
        if chk_raw:
            m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", chk_raw)
            if m_pct:
                checklist_pct_sum += min(100.0, max(0.0, float(m_pct.group(1))))
                checklist_count += 1
            else:
                m_frac = re.search(r"(\d+)\s*/\s*(\d+)", chk_raw)
                if m_frac and int(m_frac.group(2)) > 0:
                    checklist_pct_sum += min(100.0, max(0.0, (int(m_frac.group(1)) / int(m_frac.group(2))) * 100.0))
                    checklist_count += 1

        if status == "OPEN":
            total_open += 1
        elif status in ("TP", "SL"):
            total_closed += 1
            setup_key = f"{r.trade_type} • {tf_val}" if tf_val else r.trade_type
            setup_total[setup_key] += 1

            if status == "TP":
                total_wins += 1
                setup_wins[setup_key] += 1

            parsed_rr = None
            try:
                parsed_rr = float(str(r.rr_ratio).replace("1:", "").strip())
                total_rr += parsed_rr
            except ValueError:
                pass

            if status == "SL":
                realized_r -= 1.0
                realized_r_count += 1
            elif parsed_rr is not None and parsed_rr > 0:
                realized_r += parsed_rr
                realized_r_count += 1

    winrate = (total_wins / total_closed * 100) if total_closed > 0 else 0.0
    avg_rr = (total_rr / total_closed) if total_closed > 0 else 0.0
    total_losses = total_closed - total_wins

    best_setup_str = "N/A"
    best_rate = -1.0
    max_tp_count = -1
    for setup, total_count in setup_total.items():
        if total_count > 0:
            wins = setup_wins[setup]
            rate = (wins / total_count) * 100.0
            if rate > best_rate or (rate == best_rate and wins > max_tp_count):
                best_rate = rate
                max_tp_count = wins
                best_setup_str = f"{setup} / {rate:.1f}%"

    avg_chk_str = f"{checklist_pct_sum / checklist_count:.0f}%" if checklist_count > 0 else "—"
    exp_r = (realized_r / realized_r_count) if realized_r_count > 0 else 0.0

    return {
        "total_trades": len(records),
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_open": total_open,
        "total_closed": total_closed,
        "winrate": winrate,
        "avg_rr": avg_rr,
        "wr_txt": f"{winrate:.1f}%",
        "rr_txt": f"1:{avg_rr:.2f}",
        "best_setup_str": best_setup_str,
        "avg_chk_str": avg_chk_str,
        "realized_r": realized_r,
        "realized_r_count": realized_r_count,
        "exp_r": exp_r,
    }


TRADES_COL_INDEX_MAP = {
    "ID": 0, "User": 1, "Pair": 2, "TF": 3, "Direction": 4,
    "Entry": 5, "SL": 6, "TP": 7, "R:R": 8, "Status": 9,
    "Type": 10, "Checklist": 11, "Date": 12,
}


def filter_and_sort_trades(
    records,
    query="",
    status_filter="All Statuses",
    dir_filter="All Directions",
    date_filter="All Time",
    sort_col=None,
    sort_reverse=False,
    today_date=None,
):
    """Pure filtering and sorting pipeline for TradeRecord collections."""
    clean_query = str(query or "").strip().lower()
    today_dt = today_date if today_date is not None else datetime.now(get_app_tzinfo()).date()
    month_prefix = today_dt.strftime("%Y-%m")
    today_str = today_dt.strftime("%Y-%m-%d")

    cutoff_dt = None
    if date_filter == "Last 7 Days":
        cutoff_dt = today_dt - timedelta(days=6)
    elif date_filter == "Last 30 Days":
        cutoff_dt = today_dt - timedelta(days=29)
    elif date_filter == "Last 90 Days":
        cutoff_dt = today_dt - timedelta(days=89)

    total_trades = len(records)
    rows_to_render = []

    for idx, r in enumerate(records):
        display_id = total_trades - idx
        user_val = r.username or ""
        pair_val = clean_pair(r.pair)
        tf_val = format_timeframe(r.timeframe)
        dir_val = clean_direction(r.direction)
        status_val = clean_status(r.status)
        trade_type_val = r.trade_type
        checklist_val = r.checklist_score or "—"
        date_val = r.short_date

        if status_filter != "All Statuses" and status_val != status_filter:
            continue
        if dir_filter != "All Directions" and dir_val != dir_filter:
            continue
        if date_filter == "Today" and date_val != today_str:
            continue
        elif date_filter == "This Month" and not date_val.startswith(month_prefix):
            continue
        elif cutoff_dt is not None:
            try:
                rec_d = datetime.strptime(date_val, "%Y-%m-%d").date()
            except ValueError:
                continue
            if rec_d < cutoff_dt:
                continue
        if clean_query:
            searchable = f"{display_id} {user_val} {pair_val} {tf_val} {dir_val} {status_val} {trade_type_val} {checklist_val} {date_val} {r.notes}".lower()
            if clean_query not in searchable:
                continue

        display_row = [
            display_id, user_val, pair_val, tf_val, dir_val, r.entry_price, r.stop_loss,
            r.take_profit, r.rr_ratio, status_val, trade_type_val, checklist_val, date_val
        ]
        rows_to_render.append((r, status_val, display_row))

    if sort_col in TRADES_COL_INDEX_MAP:
        col_idx = TRADES_COL_INDEX_MAP[sort_col]

        def _sort_key(item):
            val = item[2][col_idx]
            if sort_col == "ID":
                return (0, int(val))
            if sort_col in ("Entry", "SL", "TP"):
                try:
                    return (0, float(str(val).replace(",", "")))
                except (ValueError, TypeError):
                    return (1, str(val).lower())
            if sort_col == "R:R":
                try:
                    return (0, float(str(val).replace("1:", "").strip()))
                except (ValueError, TypeError):
                    return (1, str(val).lower())
            return (0, str(val or "").lower())

        rows_to_render.sort(key=_sort_key, reverse=bool(sort_reverse))

    return rows_to_render, total_trades


def csv_safe_cell(val):
    """Neutralizes spreadsheet formula prefixes in exported CSV cells."""
    s = str(val if val is not None else "")
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def trade_to_csv_row(display_id, r):
    """Serializes a TradeRecord into a sanitized CSV export row."""
    return [
        display_id,
        csv_safe_cell(r.username or ""),
        csv_safe_cell(clean_pair(r.pair)),
        csv_safe_cell(format_timeframe(r.timeframe)),
        csv_safe_cell(clean_direction(r.direction)),
        csv_safe_cell(r.entry_price),
        csv_safe_cell(r.stop_loss),
        csv_safe_cell(r.take_profit),
        csv_safe_cell(r.rr_ratio),
        csv_safe_cell(clean_status(r.status)),
        csv_safe_cell(r.trade_type or ""),
        csv_safe_cell(r.checklist_score or ""),
        csv_safe_cell(r.short_date),
        csv_safe_cell(r.notes or ""),
    ]


class CustomDropdownButton(ctk.CTkFrame):
    _instances = weakref.WeakSet()

    def __init__(
        self,
        parent,
        options,
        default_val,
        app_instance,
        width=None,
        height=30,
        command_callback=None,
        is_searchable=False,
        enable_favorites=False,
        custom_max_h=None,
        bg_color=None
    ):
        resolved_bg = bg_color if bg_color is not None else _resolve_parent_bg(parent)
        super().__init__(
            parent,
            fg_color=pair_color("bg_input"),
            bg_color=resolved_bg,
            border_color=pair_color("border_strong"),
            border_width=1,
            corner_radius=7,
            height=height,
            cursor="hand2"
        )
        if width:
            self.configure(width=width)
        self.pack_propagate(False)
        self.grid_propagate(False)

        CustomDropdownButton._instances.add(self)

        self.app_instance = app_instance
        self.options = list(options)
        self.selected_value = default_val
        self.command_callback = command_callback
        self.is_searchable = is_searchable
        self.enable_favorites = enable_favorites
        self.custom_max_h = custom_max_h
        self._hovering = False

        self.grid_rowconfigure(0, minsize=2)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, minsize=2)
        self.grid_columnconfigure(0, minsize=2)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)
        self.grid_columnconfigure(3, minsize=2)

        self.lbl_text = ctk.CTkLabel(
            self,
            text=default_val,
            height=18,
            fg_color="transparent",
            text_color=pair_color("text_primary"),
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w"
        )
        self.lbl_text.grid(row=1, column=1, sticky="w", padx=(10, 4), pady=2)

        self.lbl_arrow = ctk.CTkLabel(
            self,
            text="▾",
            height=18,
            width=16,
            fg_color="transparent",
            text_color=pair_color("text_secondary"),
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="e"
        )
        self.lbl_arrow.grid(row=1, column=2, sticky="e", padx=(0, 8), pady=2)

        self._bind_click_events(self)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _bind_click_events(self, widget):
        widget.bind("<Button-1>", self.toggle)
        for child in widget.winfo_children():
            self._bind_click_events(child)

    def _on_enter(self, event=None):
        if not self._hovering:
            self.on_hover(True)

    def _on_leave(self, event=None):
        try:
            if event is not None:
                under = self.winfo_containing(event.x_root, event.y_root)
                if under is self or (under is not None and str(under).startswith(str(self) + ".")):
                    return
        except Exception:
            pass
        if self._hovering:
            self.on_hover(False)

    def on_hover(self, hovering):
        self._hovering = hovering
        color_key = "bg_hover" if hovering else "bg_input"
        border_key = "accent" if hovering else "border_strong"
        self.configure(fg_color=pair_color(color_key), border_color=pair_color(border_key))
        self.lbl_arrow.configure(
            text_color=pair_color("accent") if hovering else pair_color("text_secondary")
        )

    def refresh_theme(self):
        self.on_hover(False)

    @classmethod
    def refresh_all_themes(cls):
        for inst in list(cls._instances):
            try:
                if inst.winfo_exists():
                    inst.refresh_theme()
            except Exception:
                pass

    def toggle(self, event=None):
        self.app_instance.show_custom_popup_menu(
            self,
            self.options,
            self.command_callback,
            is_searchable=self.is_searchable,
            enable_favorites=self.enable_favorites,
            custom_max_h=self.custom_max_h
        )
        return "break"

    def set(self, val):
        self.selected_value = val
        self.lbl_text.configure(text=val)

    def get(self):
        return self.selected_value

    def update_options(self, new_options):
        self.options = list(new_options)