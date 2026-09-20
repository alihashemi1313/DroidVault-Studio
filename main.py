import os
import sys
if sys.platform.startswith("win"):
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("droidvault.studio.app.2.5")
    except Exception:
        pass
import json
import math
import re
import threading
import time
from collections import OrderedDict
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import tkinter.messagebox as mbox

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageTk

import adb_utils as adbu
import backup_manager
import restore_manager

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

ACCENT = "#6366F1"
ACCENT_HOVER = "#4F46E5"
DANGER = "#EF4444"
SUCCESS = "#10B981"
WARNING = "#F59E0B"
GHOST_HOVER = ("#F1F5F9", "#1B2130")
BORDER = ("#E2E8F0", "#212836")
TEXT_PRIMARY = ("#0F172A", "#F8FAFC")
TEXT_SECONDARY = ("#64748B", "#94A3B8")

FONT_BRAND = ("Segoe UI", 13, "bold")
FONT_H1 = ("Segoe UI", 11, "bold")
FONT_H2 = ("Segoe UI", 10, "bold")
FONT_BODY = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO = ("Consolas", 10)

PALETTE = [
    (99, 102, 241), (16, 185, 129), (245, 158, 11),
    (239, 68, 68), (14, 165, 233), (168, 85, 247),
    (236, 72, 153), (20, 184, 166), (79, 70, 229)
]

_ICON_CACHE = OrderedDict()
MAX_ICON_CACHE = 6000
_ICON_LAYER_CACHE = {}
_FONT_CACHE = {}

_LEVEL_RANK = {"Safe": 0, "Recommended": 1, "Caution": 1, "Advanced": 2}
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def _parse_size_like(s):
    s = s.strip()
    if s in ("—", "-", ""):
        return -1.0
    parts = s.split()
    if len(parts) == 2 and parts[1].upper() in _SIZE_UNITS:
        try:
            return float(parts[0]) * _SIZE_UNITS[parts[1].upper()]
        except ValueError:
            return None
    return None


def _leading_number(s):
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(data):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            if os.path.exists(CONFIG_PATH + ".tmp"):
                os.remove(CONFIG_PATH + ".tmp")
        except Exception:
            pass


def make_ui_icon(kind, size=(20, 20)):
    scale = 3
    design = 20
    w = h = design * scale
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if kind == "backup":
        d.rounded_rectangle([3*scale, 4*scale, 19*scale, 18*scale], radius=3*scale, fill=(99, 102, 241, 255))
        d.rectangle([7*scale, 2*scale, 15*scale, 4*scale], fill=(79, 70, 229, 255))
        d.ellipse([9*scale, 9*scale, 13*scale, 13*scale], fill=(255, 255, 255, 230))
    elif kind == "restore":
        d.ellipse([3*scale, 3*scale, 19*scale, 19*scale], outline=(16, 185, 129, 255), width=2*scale)
        d.polygon([(11*scale, 1*scale), (16*scale, 4*scale), (11*scale, 7*scale)], fill=(16, 185, 129, 255))
    elif kind == "debloater":
        d.rounded_rectangle([3*scale, 3*scale, 19*scale, 19*scale], radius=4*scale, fill=(245, 158, 11, 255))
        d.ellipse([7*scale, 7*scale, 15*scale, 15*scale], outline=(255, 255, 255, 255), width=2*scale)
    elif kind == "modder":
        points = [(12*scale, 2*scale), (5*scale, 11*scale), (11*scale, 11*scale),
                  (10*scale, 19*scale), (17*scale, 9*scale), (11*scale, 9*scale)]
        d.polygon(points, fill=(168, 85, 247, 255))
    elif kind == "mirror":
        d.rounded_rectangle([3*scale, 3*scale, 19*scale, 15*scale], radius=2*scale, fill=(14, 165, 233, 255))
        d.rectangle([9*scale, 15*scale, 13*scale, 18*scale], fill=(14, 165, 233, 255))
        d.rectangle([6*scale, 18*scale, 16*scale, 19*scale], fill=(14, 165, 233, 255))
    elif kind == "wireless":
        d.ellipse([9*scale, 14*scale, 13*scale, 18*scale], fill=(16, 185, 129, 255))
        d.arc([5*scale, 8*scale, 15*scale, 18*scale], start=210, end=330, fill=(16, 185, 129, 255), width=2*scale)
        d.arc([2*scale, 3*scale, 18*scale, 19*scale], start=210, end=330, fill=(16, 185, 129, 255), width=2*scale)
    elif kind == "sun":
        d.ellipse([6*scale, 6*scale, 14*scale, 14*scale], fill=(245, 158, 11, 255))
        for ang in (0, 45, 90, 135, 180, 225, 270, 315):
            rad = math.radians(ang)
            x1, y1 = 10*scale + 7*scale*math.cos(rad), 10*scale + 7*scale*math.sin(rad)
            x2, y2 = 10*scale + 9.5*scale*math.cos(rad), 10*scale + 9.5*scale*math.sin(rad)
            d.line([(x1, y1), (x2, y2)], fill=(245, 158, 11, 255), width=int(1.4*scale))
    elif kind == "moon":
        d.ellipse([4*scale, 3*scale, 18*scale, 17*scale], fill=(99, 102, 241, 255))
        d.ellipse([7*scale, 1.5*scale, 19*scale, 15.5*scale], fill=(17, 20, 29, 255))

    resized = img.resize(size, Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=resized, dark_image=resized, size=size)


def make_card(parent, **kwargs):
    defaults = dict(fg_color=("#FFFFFF", "#151922"), corner_radius=10, border_width=1, border_color=BORDER)
    defaults.update(kwargs)
    return ctk.CTkFrame(parent, **defaults)


def _get_font(size, bold=True):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    names = ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
    font = None
    for name in names:
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _get_clean_monogram(pkg):
    try:
        parts = [p for p in str(pkg).split(".") if p and p.lower() not in ("com", "org", "net", "io", "app", "android")]
        if not parts:
            return str(pkg)[:2].upper() if len(str(pkg)) >= 2 else "AP"
        if len(parts) >= 2:
            c1 = parts[0][0] if len(parts[0]) > 0 else "A"
            c2 = parts[1][0] if len(parts[1]) > 0 else "P"
            return (c1 + c2).upper()
        return parts[0][:2].upper()
    except Exception:
        return "AP"


def _build_icon_layer(pkg, is_system):
    isz = 22
    layer = Image.new("RGBA", (isz, isz), (0, 0, 0, 0))
    cached = os.path.join(adbu.ICON_CACHE_DIR, f"{pkg}.png")
    if os.path.exists(cached) and os.path.getsize(cached) > 200:
        try:
            ico = Image.open(cached).convert("RGBA").resize((isz, isz), Image.Resampling.LANCZOS)
            layer.paste(ico, (0, 0), ico)
            return layer
        except Exception:
            pass

    draw = ImageDraw.Draw(layer)
    color = (100, 116, 139) if is_system else PALETTE[abs(hash(pkg)) % len(PALETTE)]
    draw.rounded_rectangle([0, 0, isz, isz], radius=5, fill=color)
    monogram = _get_clean_monogram(pkg)
    font = _get_font(10, bold=True)
    try:
        bbox = draw.textbbox((0, 0), monogram, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = isz / 2 - tw / 2 - bbox[0]
        ty = isz / 2 - th / 2 - bbox[1]
        draw.text((tx, ty), monogram, fill="white", font=font)
    except Exception:
        draw.text((4, 5), monogram, fill="white", font=font)
    return layer


def get_icon_layer(pkg, is_system):
    layer = _ICON_LAYER_CACHE.get(pkg)
    if layer is None:
        layer = _build_icon_layer(pkg, is_system)
        _ICON_LAYER_CACHE[pkg] = layer
    return layer


def invalidate_icon_layer(pkg):
    _ICON_LAYER_CACHE.pop(pkg, None)


def get_styled_row_icon(pkg, checked, is_system, mode):
    key = (pkg, checked, mode)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    is_dark = (mode.lower() == "dark")
    cb_bg = (30, 41, 59) if is_dark else (248, 250, 252)
    cb_border = (51, 65, 85) if is_dark else (203, 213, 225)

    w, h = 56, 26
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy, cw, ch = 2, 4, 18, 18
    if checked:
        draw.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=4, fill=(99, 102, 241, 255))
        draw.line([(cx + 4, cy + 9), (cx + 8, cy + 13), (cx + 14, cy + 5)], fill="white", width=2)
    else:
        draw.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=4, fill=cb_bg, outline=cb_border, width=1)

    ix, iy = 26, 2
    layer = get_icon_layer(pkg, is_system)
    img.paste(layer, (ix, iy), layer)

    tk_img = ImageTk.PhotoImage(img)
    _ICON_CACHE[key] = tk_img
    _ICON_CACHE.move_to_end(key)
    while len(_ICON_CACHE) > MAX_ICON_CACHE:
        _ICON_CACHE.popitem(last=False)
    return tk_img


class StudioTable(ctk.CTkFrame):
    def __init__(self, master, columns, name_column_title="  Package Name", **kwargs):
        super().__init__(master, fg_color=("#FFFFFF", "#151922"), border_color=("#E2E8F0", "#212836"), **kwargs)
        self.columns = columns
        self.checked_keys = set()
        self.items = {}
        self.visible_keys = []
        self.current_mode = "Dark"
        self._tree_images = {}
        self._name_col_title = name_column_title
        self._sort_col = None
        self._sort_reverse = False

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="tree headings", selectmode="extended")
        self.tree.heading("#0", text=self._name_col_title, command=lambda: self.sort_by("#0"))
        self.tree.column("#0", width=290, anchor="w")

        for cid, text, width, anchor in columns:
            self.tree.heading(cid, text=text, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor)

        self.scrollbar = ctk.CTkScrollbar(self, command=self.tree.yview, width=12)
        self.tree.configure(yscrollcommand=self.scrollbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 1), pady=1)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._on_space())

    def apply_style(self, mode):
        self.current_mode = mode
        is_dark = (mode.lower() == "dark")

        tree_bg = "#151922" if is_dark else "#FFFFFF"
        tree_fg = "#F8FAFC" if is_dark else "#0F172A"
        head_bg = "#1A202C" if is_dark else "#F1F5F9"
        head_fg = "#94A3B8" if is_dark else "#64748B"
        selected_bg = "#28344E" if is_dark else "#E0E7FF"

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=tree_bg,
            foreground=tree_fg,
            fieldbackground=tree_bg,
            rowheight=32,
            font=("Segoe UI", 10),
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=head_bg,
            foreground=head_fg,
            font=("Segoe UI", 9, "bold"),
            relief="flat",
        )
        style.map("Treeview", background=[("selected", selected_bg)], foreground=[("selected", tree_fg)])
        style.map("Treeview.Heading", background=[("active", head_bg)])

        for k in self.visible_keys:
            if self.tree.exists(k):
                it = self.items.get(k, {})
                img = get_styled_row_icon(k, k in self.checked_keys, it.get("is_system", False), self.current_mode)
                self._tree_images[k] = img
                self.tree.item(k, image=img)

    def _sort_value(self, it, col):
        if col == "#0":
            return (0, str(it.get("name", "")).lower())
        raw = it.get(col, "")
        s = str(raw)
        if col == "level" and s in _LEVEL_RANK:
            return (0, _LEVEL_RANK[s])
        size_v = _parse_size_like(s)
        if size_v is not None:
            return (0, size_v)
        num_v = _leading_number(s)
        if num_v is not None:
            return (0, num_v)
        try:
            return (0, float(s))
        except ValueError:
            return (1, s.lower())

    def sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        sorted_keys = sorted(self.visible_keys, key=lambda k: self._sort_value(self.items.get(k, {}), col),
                              reverse=self._sort_reverse)
        self._refresh_header_labels()
        self.display(sorted_keys)

    def _refresh_header_labels(self):
        arrow = (" ▼" if self._sort_reverse else " ▲") if self._sort_col == "#0" else ""
        self.tree.heading("#0", text=self._name_col_title + arrow)
        for cid, text, width, anchor in self.columns:
            arrow = (" ▼" if self._sort_reverse else " ▲") if self._sort_col == cid else ""
            self.tree.heading(cid, text=text + arrow)

    def set_items(self, items):
        self.items = {it["key"]: it for it in items}
        keys = list(self.items.keys())
        if self._sort_col:
            keys = sorted(keys, key=lambda k: self._sort_value(self.items.get(k, {}), self._sort_col),
                          reverse=self._sort_reverse)
        self.display(keys)

    def display(self, keys):
        self.visible_keys = keys
        self.tree.delete(*self.tree.get_children())
        self._tree_images.clear()
        for k in keys:
            it = self.items.get(k)
            if not it:
                continue
            vals = [it.get(col[0], "") for col in self.columns]
            img = get_styled_row_icon(k, k in self.checked_keys, it.get("is_system", False), self.current_mode)
            self._tree_images[k] = img
            self.tree.insert("", "end", iid=k, text=f"  {it.get('name', k)}", image=img, values=vals)

    def _on_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.toggle(row_id)

    def _on_space(self):
        for s in self.tree.selection():
            self.toggle(s)

    def toggle(self, key):
        if key in self.checked_keys:
            self.checked_keys.remove(key)
        else:
            self.checked_keys.add(key)
        self.refresh_row_icon(key)

    def refresh_row_icon(self, key):
        if self.tree.exists(key):
            it = self.items.get(key, {})
            img = get_styled_row_icon(key, key in self.checked_keys, it.get("is_system", False), self.current_mode)
            self._tree_images[key] = img
            self.tree.item(key, image=img)

    def on_icon_updated(self, key):
        invalidate_icon_layer(key)
        for mode in ("Dark", "Light"):
            _ICON_CACHE.pop((key, False, mode), None)
            _ICON_CACHE.pop((key, True, mode), None)
        self.refresh_row_icon(key)

    def select_all(self):
        for k in self.visible_keys:
            self.checked_keys.add(k)
            self.refresh_row_icon(k)

    def select_none(self):
        for k in self.visible_keys:
            self.checked_keys.discard(k)
            self.refresh_row_icon(k)

    def update_col(self, key, idx, val):
        if self.tree.exists(key):
            vals = list(self.tree.item(key, "values"))
            if idx < len(vals):
                vals[idx] = val
                img = self._tree_images.get(key)
                if not img:
                    it = self.items.get(key, {})
                    img = get_styled_row_icon(key, key in self.checked_keys, it.get("is_system", False), self.current_mode)
                    self._tree_images[key] = img
                self.tree.item(key, values=vals, image=img)

# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        
        def get_asset_path(relative_path):
            if hasattr(sys, '_MEIPASS'):
                return os.path.join(sys._MEIPASS, relative_path)
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)
        
        super().__init__()

        ico_file = get_asset_path("app_icon.ico")
        png_file = get_asset_path("app_icon.png")
        
        if sys.platform.startswith("win") and os.path.exists(ico_file):
            self.iconbitmap(ico_file)
        elif os.path.exists(png_file):
            self._window_icon = ImageTk.PhotoImage(file=png_file)
            self.iconphoto(True, self._window_icon)
        self.config_data = load_config()
        self.current_mode = self.config_data.get("theme", "Dark")
        ctk.set_appearance_mode(self.current_mode)

        self.title("DroidVault Studio • Universal Android Management Suite")
        self.geometry("1280x840")
        self.minsize(1100, 720)
        self.configure(fg_color=("#F8FAFC", "#0B0E14"))

        self.serial = None
        self.device_info = {}
        self.is_rooted = False
        self.external_sd_path = None
        self.packages = []
        self.package_by_name = {}
        self.package_apk_sizes = {}
        self.package_data_sizes = {}

        self.operation_lock = threading.Lock()
        self.operation_state = "IDLE"
        self.operation_cancel_event = None
        self.operation_serial = None
        self.size_scan_generation = 0
        self.size_scan_cancel_event = None
        self.icon_loader_generation = 0
        self._closing = False

        self.sessions = []
        self.current_session = None

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._load_vector_icons()
        self._build_header_hub()
        self._build_sidebar()
        self._build_content_studios()
        self._switch_studio("backup")
        self._apply_global_theme(self.current_mode)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
    
    def _load_vector_icons(self):
        self.icon_backup = make_ui_icon("backup", (20, 20))
        self.icon_restore = make_ui_icon("restore", (20, 20))
        self.icon_debloat = make_ui_icon("debloater", (20, 20))
        self.icon_modder = make_ui_icon("modder", (20, 20))
        self.icon_mirror = make_ui_icon("mirror", (16, 16))
        self.icon_wireless = make_ui_icon("wireless", (16, 16))
        self.icon_sun = make_ui_icon("sun", (18, 18))
        self.icon_moon = make_ui_icon("moon", (18, 18))

    def run_async(self, target, on_done=None):
        def worker():
            res, err = None, None
            try:
                res = target()
            except Exception as e:
                err = e
            if on_done:
                try:
                    self.after(0, lambda: on_done(res, err))
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _begin_operation(self, state):
        if not self.operation_lock.acquire(blocking=False):
            messagebox.showwarning("Busy", f"Another operation is already running: {self.operation_state}")
            return None
        if self.size_scan_cancel_event:
            self.size_scan_cancel_event.set()
        self.operation_state = state
        self.operation_cancel_event = threading.Event()
        self.operation_serial = self.serial
        self.size_scan_generation += 1
        return self.operation_cancel_event

    def _end_operation(self):
        self.operation_state = "IDLE"
        self.operation_cancel_event = None
        self.operation_serial = None
        if self.operation_lock.locked():
            try:
                self.operation_lock.release()
            except RuntimeError:
                pass

    def _cancel_current_operation(self):
        if self.operation_cancel_event:
            self.operation_cancel_event.set()
        if self.operation_serial:
            adbu.terminate_active_processes(self.operation_serial)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        if self.size_scan_cancel_event:
            self.size_scan_cancel_event.set()
        if self.operation_cancel_event:
            self.operation_cancel_event.set()
            # Interrupt the currently blocked ADB process, but keep the Tk loop
            # alive long enough for the worker to execute its rollback/finally.
            adbu.terminate_active_processes(self.operation_serial or self.serial)
            self.after(100, self._finish_close_when_safe)
        else:
            adbu.terminate_active_processes(self.serial)
            self.destroy()

    def _finish_close_when_safe(self):
        if self.operation_state == "IDLE":
            self.destroy()
            return
        self.after(100, self._finish_close_when_safe)


    def _build_header_hub(self):
        self.header = ctk.CTkFrame(self, height=58, corner_radius=0, fg_color=("#FFFFFF", "#151922"))
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.header.grid_propagate(False)

        left = ctk.CTkFrame(self.header, fg_color="transparent")
        left.pack(side="left", padx=18)
        ctk.CTkLabel(left, text="⚡ DroidVault Studio", font=FONT_BRAND, text_color=TEXT_PRIMARY).pack(side="left")
        ctk.CTkLabel(left, text=" v2.5", font=FONT_SMALL, text_color=TEXT_SECONDARY).pack(side="left", padx=(2, 0))

        right = ctk.CTkFrame(self.header, fg_color="transparent")
        right.pack(side="right", padx=14)

        self.device_badge = ctk.CTkLabel(right, text="No Device Connected", font=FONT_BODY, text_color=TEXT_SECONDARY)
        self.device_badge.pack(side="left", padx=(0, 12))

        ghost = dict(fg_color="transparent", border_width=1, border_color=BORDER,
                     text_color=TEXT_PRIMARY, hover_color=GHOST_HOVER, corner_radius=8)

        ctk.CTkButton(right, text="Mirror Screen", image=self.icon_mirror, compound="left", width=118, height=32,
                      command=self._launch_mirror, **ghost).pack(side="left", padx=(0, 6))

        ctk.CTkButton(right, text="Wireless ADB", image=self.icon_wireless, compound="left", width=118, height=32,
                      command=self._open_wireless_dialog, **ghost).pack(side="left", padx=(0, 6))

        self.recheck_btn = ctk.CTkButton(right, text="Check Root", width=98, height=32,
                                         command=self.recheck_root, state="disabled", **ghost)
        self.recheck_btn.pack(side="left", padx=(0, 6))

        self.detect_btn = ctk.CTkButton(right, text="Detect Device", width=118, height=32, fg_color=ACCENT,
                                        hover_color=ACCENT_HOVER, text_color="white", corner_radius=8,
                                        command=self.detect_device)
        self.detect_btn.pack(side="left", padx=(0, 12))

        self.theme_btn = ctk.CTkButton(right, text="", width=40, height=32, corner_radius=16,
                                       fg_color="transparent", border_width=1, border_color=BORDER,
                                       hover_color=GHOST_HOVER, command=self._toggle_theme)
        self.theme_btn.pack(side="left")

    def _toggle_theme(self):
        self.current_mode = "Light" if self.current_mode == "Dark" else "Dark"
        ctk.set_appearance_mode(self.current_mode)
        self.config_data["theme"] = self.current_mode
        save_config(self.config_data)
        self._apply_global_theme(self.current_mode)

    def _apply_global_theme(self, mode):
        self.theme_btn.configure(image=self.icon_sun if mode == "Dark" else self.icon_moon)
        self.backup_table.apply_style(mode)
        self.restore_table.apply_style(mode)
        self.debloater_table.apply_style(mode)

    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0, fg_color=("#F1F5F9", "#11141D"))
        self.sidebar.grid(row=1, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        box = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        box.pack(fill="x", padx=10, pady=16)

        self.nav_buttons = {}
        studios = [
            ("backup", "Backup Studio", self.icon_backup),
            ("restore", "Restore & Inspect", self.icon_restore),
            ("debloater", "Debloat Suite", self.icon_debloat),
            ("modder", "Modder's Toolkit", self.icon_modder),
        ]
        for key, text, icon in studios:
            b = ctk.CTkButton(box, text=f"  {text}", image=icon, compound="left", height=40, anchor="w",
                              corner_radius=8, fg_color="transparent", text_color=TEXT_PRIMARY,
                              hover_color=GHOST_HOVER, font=FONT_BODY,
                              command=lambda k=key: self._switch_studio(k))
            b.pack(fill="x", pady=4)
            self.nav_buttons[key] = b

    def _switch_studio(self, key):
        for k, p in self.studios.items():
            p.grid_remove()
        self.studios[key].grid(row=0, column=0, sticky="nsew")
        for k, btn in self.nav_buttons.items():
            if k == key:
                btn.configure(fg_color=ACCENT, text_color="white")
            else:
                btn.configure(fg_color="transparent", text_color=TEXT_PRIMARY)

    def _build_content_studios(self):
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.grid(row=1, column=1, sticky="nsew", padx=14, pady=12)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        self.studios = {
            "backup": self._build_backup_studio(self.container),
            "restore": self._build_restore_studio(self.container),
            "debloater": self._build_debloater_studio(self.container),
            "modder": self._build_modder_studio(self.container),
        }

    # ------------------------------------------------------------------
    # 1. Backup Studio
    # ------------------------------------------------------------------
    def _build_backup_studio(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=6)
        page.grid_columnconfigure(1, weight=4)
        page.grid_rowconfigure(0, weight=1)

        left = make_card(page)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(left, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=8)

        self.filter_seg = ctk.CTkSegmentedButton(bar, values=["User", "System", "All"], height=28,
                                                 command=lambda m: self.apply_backup_filter())
        self.filter_seg.set("User")
        self.filter_seg.pack(side="left", padx=(0, 8))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.apply_backup_filter())
        ctk.CTkEntry(bar, textvariable=self.search_var, placeholder_text="Filter packages...", height=28).pack(side="left", fill="x", expand=True, padx=(0, 8))

        ctk.CTkButton(bar, text="All", width=42, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"),
                      command=lambda: self.backup_table.select_all()).pack(side="right", padx=2)
        ctk.CTkButton(bar, text="None", width=42, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"),
                      command=lambda: self.backup_table.select_none()).pack(side="right")

        cols = [
            ("apk_size", "APK Size", 75, "e"),
            ("data_size", "Data Size", 80, "e"),
            ("type", "Type", 60, "center"),
        ]
        self.backup_table = StudioTable(left, cols)
        self.backup_table.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        right = ctk.CTkFrame(page, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)

        # Card 1: Application Scope
        c1 = make_card(right)
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        c1_b = ctk.CTkFrame(c1, fg_color="transparent")
        c1_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c1_b, text="Application Scope", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        r1 = ctk.CTkFrame(c1_b, fg_color="transparent")
        r1.pack(fill="x")
        self.inc_apk_var = tk.BooleanVar(value=True)
        self.inc_data_var = tk.BooleanVar(value=True)
        self.inc_ext_var = tk.BooleanVar(value=False)
        self.inc_obb_var = tk.BooleanVar(value=False)
        self.inc_perms_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(r1, text="APK", variable=self.inc_apk_var, font=FONT_BODY).pack(side="left", padx=(0, 10))
        self.chk_data = ctk.CTkCheckBox(r1, text="Data", variable=self.inc_data_var, font=FONT_BODY)
        self.chk_data.pack(side="left", padx=(0, 10))
        self.chk_ext = ctk.CTkCheckBox(r1, text="ExtData", variable=self.inc_ext_var, font=FONT_BODY)
        self.chk_ext.pack(side="left", padx=(0, 10))
        self.chk_obb = ctk.CTkCheckBox(r1, text="OBB", variable=self.inc_obb_var, font=FONT_BODY)
        self.chk_obb.pack(side="left")

        r2 = ctk.CTkFrame(c1_b, fg_color="transparent")
        r2.pack(fill="x", pady=(8, 0))
        ctk.CTkCheckBox(r2, text="Backup Runtime Permissions (Auto-Grant upon Restore)",
                        variable=self.inc_perms_var, font=FONT_BODY).pack(side="left")

        # Card 2: Personal Databases
        c2 = make_card(right)
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        c2_b = ctk.CTkFrame(c2, fg_color="transparent")
        c2_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c2_b, text="Personal Databases & Engine", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        self.use_zstd_var = tk.BooleanVar(value=False)
        self.inc_wifi_var = tk.BooleanVar(value=True)
        self.inc_sms_var = tk.BooleanVar(value=True)
        self.inc_contacts_var = tk.BooleanVar(value=True)

        r3 = ctk.CTkFrame(c2_b, fg_color="transparent")
        r3.pack(fill="x")
        self.chk_wifi = ctk.CTkCheckBox(r3, text="Wi-Fi Keys", variable=self.inc_wifi_var, font=FONT_BODY)
        self.chk_wifi.pack(side="left", padx=(0, 10))
        self.chk_sms = ctk.CTkCheckBox(r3, text="SMS", variable=self.inc_sms_var, font=FONT_BODY)
        self.chk_sms.pack(side="left", padx=(0, 10))
        self.chk_contacts = ctk.CTkCheckBox(r3, text="Contacts & Calls", variable=self.inc_contacts_var, font=FONT_BODY)
        self.chk_contacts.pack(side="left")

        r4 = ctk.CTkFrame(c2_b, fg_color="transparent")
        r4.pack(fill="x", pady=(6, 0))
        ctk.CTkCheckBox(r4, text="Use Zstandard (.tar.zst) Fast Multi-core Compression", variable=self.use_zstd_var, font=FONT_BODY).pack(side="left")

        # Card 3: Storage
        c3 = make_card(right)
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        c3_b = ctk.CTkFrame(c3, fg_color="transparent")
        c3_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c3_b, text="Device Storage Scope", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        self.inc_storage_var = tk.BooleanVar(value=False)
        self.inc_ext_sd_var = tk.BooleanVar(value=False)
        self.exclude_dup_var = tk.BooleanVar(value=True)

        ctk.CTkCheckBox(c3_b, text="Backup Full Internal Storage (/sdcard)", variable=self.inc_storage_var, font=FONT_BODY).pack(anchor="w", pady=(0, 2))
        ctk.CTkCheckBox(c3_b, text="Skip Android/data & Android/obb inside storage", variable=self.exclude_dup_var, font=FONT_SMALL, text_color=("#64748B", "#94A3B8")).pack(anchor="w", pady=(0, 4))

        self.chk_backup_ext_sd = ctk.CTkCheckBox(c3_b, text="MicroSD Card (Not Inserted)", variable=self.inc_ext_sd_var, font=FONT_BODY, state="disabled")
        self.chk_backup_ext_sd.pack(anchor="w")

        # Card 4: Run & Destination
        c4 = make_card(right)
        c4.grid(row=3, column=0, sticky="nsew")
        c4_b = ctk.CTkFrame(c4, fg_color="transparent")
        c4_b.pack(fill="both", expand=True, padx=12, pady=10)

        p_row = ctk.CTkFrame(c4_b, fg_color="transparent")
        p_row.pack(fill="x", pady=(0, 6))
        self.dest_var = tk.StringVar(value=self.config_data.get("last_dest", ""))
        ctk.CTkEntry(p_row, textvariable=self.dest_var, placeholder_text="Backup directory...", height=28).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(p_row, text="Browse", width=65, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"), command=self._choose_dest).pack(side="left")

        act = ctk.CTkFrame(c4_b, fg_color="transparent")
        act.pack(fill="x", pady=(0, 4))
        self.start_btn = ctk.CTkButton(act, text="Start Backup", height=30, width=115, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.start_backup)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.cancel_btn = ctk.CTkButton(act, text="Cancel", height=30, width=65, fg_color=DANGER, state="disabled", command=self._cancel_current_operation)
        self.cancel_btn.pack(side="left")

        self.backup_pbar_frame = ctk.CTkFrame(c4_b, fg_color="transparent")
        self.backup_pbar = ctk.CTkProgressBar(self.backup_pbar_frame, height=8, corner_radius=4)
        self.backup_pbar.set(0)
        self.backup_lbl = ctk.CTkLabel(self.backup_pbar_frame, text="", font=FONT_SMALL, text_color=("#64748B", "#94A3B8"))

        self.backup_log = ctk.CTkTextbox(c4_b, font=FONT_MONO, fg_color=("#0F172A", "#080B10"), text_color="#38BDF8", wrap="word", height=120)
        self.backup_log.pack(fill="both", expand=True, pady=(6, 0))
        self.backup_log.configure(state="disabled")

        return page

    # ------------------------------------------------------------------
    # 2. Restore Studio
    # ------------------------------------------------------------------
    def _build_restore_studio(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=6)
        page.grid_columnconfigure(1, weight=4)
        page.grid_rowconfigure(0, weight=1)

        left = make_card(page)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(left, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        ctk.CTkLabel(bar, text="Apps in Session", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(side="left")
        ctk.CTkButton(bar, text="All", width=42, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"), command=lambda: self.restore_table.select_all()).pack(side="right", padx=2)
        ctk.CTkButton(bar, text="None", width=42, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"), command=lambda: self.restore_table.select_none()).pack(side="right")

        cols = [
            ("version", "Version", 80, "center"),
            ("perms", "Permissions", 85, "center"),
        ]
        self.restore_table = StudioTable(left, cols)
        self.restore_table.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))

        right = ctk.CTkFrame(page, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)

        # Card 1: Application Scope
        c1 = make_card(right)
        c1.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        c1_b = ctk.CTkFrame(c1, fg_color="transparent")
        c1_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c1_b, text="Application Scope", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))

        self.rest_apk_var = tk.BooleanVar(value=True)
        self.rest_data_var = tk.BooleanVar(value=True)
        self.rest_ext_var = tk.BooleanVar(value=True)
        self.rest_obb_var = tk.BooleanVar(value=True)
        self.rest_perms_var = tk.BooleanVar(value=True)

        r1 = ctk.CTkFrame(c1_b, fg_color="transparent")
        r1.pack(fill="x")
        self.chk_rest_apk = ctk.CTkCheckBox(r1, text="APK", variable=self.rest_apk_var, font=FONT_BODY)
        self.chk_rest_apk.pack(side="left", padx=(0, 10))
        self.chk_rest_data = ctk.CTkCheckBox(r1, text="Data", variable=self.rest_data_var, font=FONT_BODY)
        self.chk_rest_data.pack(side="left", padx=(0, 10))
        self.chk_rest_ext = ctk.CTkCheckBox(r1, text="ExtData", variable=self.rest_ext_var, font=FONT_BODY)
        self.chk_rest_ext.pack(side="left", padx=(0, 10))
        self.chk_rest_obb = ctk.CTkCheckBox(r1, text="OBB", variable=self.rest_obb_var, font=FONT_BODY)
        self.chk_rest_obb.pack(side="left")

        r2 = ctk.CTkFrame(c1_b, fg_color="transparent")
        r2.pack(fill="x", pady=(8, 0))
        self.chk_rest_perms = ctk.CTkCheckBox(r2, text="Auto-Grant Permissions (pm grant)", variable=self.rest_perms_var, font=FONT_BODY)
        self.chk_rest_perms.pack(side="left")

        # Card 2: Personal Databases
        c2 = make_card(right)
        c2.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        c2_b = ctk.CTkFrame(c2, fg_color="transparent")
        c2_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c2_b, text="Personal Databases", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))

        self.rest_wifi_var = tk.BooleanVar(value=True)
        self.rest_sms_var = tk.BooleanVar(value=True)
        self.rest_contacts_var = tk.BooleanVar(value=True)

        r3 = ctk.CTkFrame(c2_b, fg_color="transparent")
        r3.pack(fill="x")
        self.chk_rest_wifi = ctk.CTkCheckBox(r3, text="Wi-Fi Keys", variable=self.rest_wifi_var, font=FONT_BODY)
        self.chk_rest_wifi.pack(side="left", padx=(0, 10))
        self.chk_rest_sms = ctk.CTkCheckBox(r3, text="SMS", variable=self.rest_sms_var, font=FONT_BODY)
        self.chk_rest_sms.pack(side="left", padx=(0, 10))
        self.chk_rest_contacts = ctk.CTkCheckBox(r3, text="Contacts & Calls", variable=self.rest_contacts_var, font=FONT_BODY)
        self.chk_rest_contacts.pack(side="left")

        # Card 3: Storage
        c3 = make_card(right)
        c3.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        c3_b = ctk.CTkFrame(c3, fg_color="transparent")
        c3_b.pack(fill="x", padx=12, pady=10)
        ctk.CTkLabel(c3_b, text="Device Storage Scope", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))

        self.rest_storage_var = tk.BooleanVar(value=False)
        self.rest_ext_sd_var = tk.BooleanVar(value=False)

        self.chk_rest_internal = ctk.CTkCheckBox(c3_b, text="Restore Internal Storage (/sdcard)", variable=self.rest_storage_var, font=FONT_BODY, state="disabled")
        self.chk_rest_internal.pack(anchor="w", pady=(0, 4))
        self.chk_rest_ext_sd = ctk.CTkCheckBox(c3_b, text="MicroSD Card (Not in backup)", variable=self.rest_ext_sd_var, font=FONT_BODY, state="disabled")
        self.chk_rest_ext_sd.pack(anchor="w")

        # Card 4: Session Selector & Actions
        c4 = make_card(right)
        c4.grid(row=3, column=0, sticky="nsew")
        c4_b = ctk.CTkFrame(c4, fg_color="transparent")
        c4_b.pack(fill="both", expand=True, padx=12, pady=10)

        s_row = ctk.CTkFrame(c4_b, fg_color="transparent")
        s_row.pack(fill="x", pady=(0, 6))
        self.restore_src_var = tk.StringVar(value=self.config_data.get("last_restore_root", self.config_data.get("last_dest", "")))
        ctk.CTkEntry(s_row, textvariable=self.restore_src_var, placeholder_text="Sessions folder...", height=28).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(s_row, text="Browse", width=65, height=28, fg_color="transparent", border_width=1,
                      border_color=("#E2E8F0", "#212836"), hover_color=("#F1F5F9", "#1B2130"), text_color=("#0F172A", "#F8FAFC"), command=self._choose_restore_src).pack(side="left", padx=(0, 4))
        ctk.CTkButton(s_row, text="Scan", width=60, height=28, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._load_sessions).pack(side="left")

        self.session_cb_var = tk.StringVar()
        self.session_combo = ctk.CTkComboBox(c4_b, variable=self.session_cb_var, state="readonly", height=28, command=lambda _=None: self._on_session_picked())
        self.session_combo.pack(fill="x", pady=(0, 6))

        act = ctk.CTkFrame(c4_b, fg_color="transparent")
        act.pack(fill="x", pady=(0, 4))
        self.rest_start_btn = ctk.CTkButton(act, text="Start Restore", height=30, width=115, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.start_restore)
        self.rest_start_btn.pack(side="left", padx=(0, 6))
        self.rest_cancel_btn = ctk.CTkButton(act, text="Cancel", height=30, width=65, fg_color=DANGER, state="disabled", command=self._cancel_current_operation)
        self.rest_cancel_btn.pack(side="left")

        self.restore_log = ctk.CTkTextbox(c4_b, font=FONT_MONO, fg_color=("#0F172A", "#080B10"), text_color="#38BDF8", wrap="word", height=110)
        self.restore_log.pack(fill="both", expand=True, pady=(6, 0))
        self.restore_log.configure(state="disabled")

        return page

    # ------------------------------------------------------------------
    # 3. Debloat Suite Studio
    # ------------------------------------------------------------------
    def _build_debloater_studio(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        c = make_card(page)
        c.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        box = ctk.CTkFrame(c, fg_color="transparent")
        box.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(box, text="Package Control", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(side="left", padx=(0, 14))
        ctk.CTkButton(box, text="Select Safe Bloat", fg_color=SUCCESS, width=130, height=28, command=self._select_safe_bloat).pack(side="left", padx=(0, 6))
        ctk.CTkButton(box, text="Freeze", fg_color=WARNING, width=80, height=28, command=self._debloat_freeze).pack(side="left", padx=(0, 6))
        ctk.CTkButton(box, text="Unfreeze", fg_color=ACCENT, width=85, height=28, command=self._debloat_unfreeze).pack(side="left", padx=(0, 6))
        ctk.CTkButton(box, text="Uninstall (User 0)", fg_color=DANGER, width=120, height=28, command=self._debloat_uninstall).pack(side="left")

        left = make_card(page)
        left.grid(row=1, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)

        cols = [
            ("vendor", "Vendor / Source", 110, "w"),
            ("level", "Safety Level", 100, "center"),
            ("desc", "Description", 280, "w"),
            ("status", "Status", 75, "center"),
        ]
        self.debloater_table = StudioTable(left, cols)
        self.debloater_table.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        return page

    # ------------------------------------------------------------------
    # 4. Modder's Toolkit Studio
    # ------------------------------------------------------------------
    def _build_modder_studio(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)

        c1 = make_card(page)
        c1.pack(fill="x", pady=(0, 10))
        b1 = ctk.CTkFrame(c1, fg_color="transparent")
        b1.pack(fill="x", padx=16, pady=14)
        ctk.CTkLabel(b1, text="Boot Partition Dumper (Anti-Bootloop Protection)", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(b1, text="Extract live kernel/boot image (boot.img) before installing updates.", font=FONT_SMALL, text_color=("#64748B", "#94A3B8")).pack(anchor="w", pady=(0, 10))
        ctk.CTkButton(b1, text="Dump boot.img to PC", width=180, height=32, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._dump_boot).pack(anchor="w")

        c2 = make_card(page)
        c2.pack(fill="x", pady=(0, 10))
        b2 = ctk.CTkFrame(c2, fg_color="transparent")
        b2.pack(fill="x", padx=16, pady=14)
        ctk.CTkLabel(b2, text="Root Modules Backup (Magisk & KernelSU)", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(b2, text="Archive all active modules from /data/adb/modules into a portable tar archive.", font=FONT_SMALL, text_color=("#64748B", "#94A3B8")).pack(anchor="w", pady=(0, 10))
        ctk.CTkButton(b2, text="Backup Modules Archive", width=180, height=32, fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self._backup_modules).pack(anchor="w")
        return page

    # ------------------------------------------------------------------
    # Handlers & Actions
    # ------------------------------------------------------------------
    def _append_log(self, widget, msg, max_lines=2500):
        widget.configure(state="normal")
        widget.insert("end", str(msg) + "\n")
        try:
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > max_lines:
                widget.delete("1.0", f"{line_count - max_lines + 1}.0")
        except Exception:
            pass
        widget.see("end")
        widget.configure(state="disabled")

    def log_backup(self, msg):
        self._append_log(self.backup_log, msg)

    def log_restore(self, msg):
        self._append_log(self.restore_log, msg)

    def detect_device(self):
        cancel_event = self._begin_operation("SCANNING")
        if cancel_event is None:
            return
        self.device_badge.configure(text="Connecting to device...")

        def task():
            if not adbu.check_adb_available():
                raise RuntimeError("ADB executable not found.")
            devs = adbu.list_devices()
            if not devs:
                raise RuntimeError("No phone detected over ADB. Check USB debugging.")
            if len(devs) > 1:
                if self.serial and self.serial in devs:
                    serial = self.serial
                else:
                    # انتخاب اولین دستگاه با اولویت اتصال USB
                    usb_devs = [d for d in devs if not ":" in d]
                    serial = usb_devs[0] if usb_devs else devs[0]
            else:
                serial = devs[0]
            info = adbu.get_device_info(serial)
            rooted = adbu.check_root(serial)
            ext_sd = adbu.get_external_sdcard_path(serial)
            recovery_lock = f"scan-{os.getpid()}-{threading.get_ident()}"
            if adbu.acquire_device_operation_lock(serial, recovery_lock, stale_seconds=600):
                try:
                    # Recover the durable PC-side APK snapshot first, while any
                    # device group-commit marker still exists. Then reconcile
                    # root data journals.
                    restore_manager.recover_local_apk_transactions(serial)
                    if rooted:
                        restore_manager.recover_incomplete_transactions(serial)
                        adbu.cleanup_stale_temp(serial)
                    adbu.cleanup_stale_storage_temp(serial, "/sdcard", older_than_seconds=0)
                    if ext_sd:
                        adbu.cleanup_stale_storage_temp(serial, ext_sd, older_than_seconds=0)
                finally:
                    adbu.release_device_operation_lock(serial, recovery_lock)
            free_mb = adbu.get_free_storage_mb(serial)
            pkgs = adbu.list_packages_fast(serial)
            return serial, info, rooted, pkgs, free_mb, ext_sd

        def done(res, err):
            self._end_operation()
            if self._closing:
                return
            if err:
                self.device_badge.configure(text="No Device Connected", text_color=("#64748B", "#94A3B8"))
                messagebox.showerror("Error", str(err))
                return
            serial, info, rooted, pkgs, free_mb, ext_sd = res
            device_changed = self.serial is not None and self.serial != serial
            self.serial = serial
            self.device_info = info
            self.is_rooted = rooted
            self.external_sd_path = ext_sd
            self.packages = pkgs
            self.package_by_name = {p["package"]: p for p in pkgs}
            valid_packages = set(self.package_by_name)
            # Keep selections across search/filter, but never keep a package that
            # disappeared from the freshly detected device inventory.
            self.backup_table.checked_keys.intersection_update(valid_packages)
            self.debloater_table.checked_keys.intersection_update(valid_packages)
            if device_changed:
                self.package_apk_sizes.clear()
                self.package_data_sizes.clear()
                self.backup_table.checked_keys.clear()
                self.debloater_table.checked_keys.clear()
                self.restore_table.checked_keys.clear()

            free_gb = free_mb / 1024
            storage_str = f"{free_gb:.1f} GB Free" if free_mb > 0 else "Storage: Unknown"
            root_txt = "Root: Granted ✅" if rooted else "Non-Root (Shell) ⚠️"
            self.device_badge.configure(text=f"{info['model']} | {storage_str} | {root_txt}", text_color=SUCCESS if rooted else WARNING)
            self.recheck_btn.configure(state="normal")
            self._update_sdcard_hardware_state()
            self.apply_backup_filter()
            self._populate_debloater()
            if rooted:
                self._fetch_sizes()

        self.run_async(task, done)

    def _update_sdcard_hardware_state(self):
        if self.external_sd_path:
            sd_name = os.path.basename(self.external_sd_path)
            self.chk_backup_ext_sd.configure(state="normal", text=f"MicroSD Card ({sd_name})")
        else:
            self.chk_backup_ext_sd.configure(state="disabled", text="MicroSD Card (Not Inserted)")
            self.inc_ext_sd_var.set(False)

        if self.current_session:
            self._on_session_picked()

    def _fetch_sizes(self):
        if not self.serial:
            return
        serial = self.serial
        generation = self.size_scan_generation
        scan_cancel = threading.Event()
        if self.size_scan_cancel_event:
            self.size_scan_cancel_event.set()
        self.size_scan_cancel_event = scan_cancel

        def still_current():
            return (
                not self._closing
                and not scan_cancel.is_set()
                and generation == self.size_scan_generation
                and serial == self.serial
            )

        def apk_task():
            return adbu.get_batch_apk_sizes(serial, should_cancel=scan_cancel.is_set)

        def apk_done(apk_sizes, err):
            if not still_current():
                return
            if not err and apk_sizes is not None:
                self.package_apk_sizes = apk_sizes
                for pkg in self.backup_table.items:
                    sa = adbu.format_size(self.package_apk_sizes.get(pkg, 0))
                    self.backup_table.items[pkg]["apk_size"] = sa
                    self.backup_table.update_col(pkg, 0, sa)

            # Data Size remains a first-class feature, but its heavier /data walk
            # runs after the cheap APK scan, at low device I/O priority, and can be
            # cancelled immediately when a real operation starts.
            def data_task():
                return adbu.get_batch_data_sizes(serial, should_cancel=scan_cancel.is_set)

            def data_done(data_sizes, data_err):
                if self.size_scan_cancel_event is scan_cancel:
                    self.size_scan_cancel_event = None
                if not still_current() or data_err or data_sizes is None:
                    return
                self.package_data_sizes = data_sizes
                for pkg in self.backup_table.items:
                    sd = adbu.format_size(self.package_data_sizes.get(pkg, 0))
                    self.backup_table.items[pkg]["data_size"] = sd
                    self.backup_table.update_col(pkg, 1, sd)

            self.run_async(data_task, data_done)

        self.run_async(apk_task, apk_done)

    def recheck_root(self):
        if not self.serial:
            return
        cancel_event = self._begin_operation("SCANNING")
        if cancel_event is None:
            return
        serial = self.serial

        def task():
            return adbu.check_root(serial)

        def done(res, err):
            self._end_operation()
            if self._closing:
                return
            if err:
                messagebox.showerror("Root", str(err))
                return
            self.is_rooted = bool(res)
            if res:
                messagebox.showinfo("Root", "Root permissions verified! ✅")
            else:
                messagebox.showwarning("Root", "Root access is not available.")
            self.detect_device()

        self.run_async(task, done)

    def apply_backup_filter(self):
        query = self.search_var.get().strip().lower()
        mode = self.filter_seg.get().lower()

        items = []
        for p in self.packages:
            pkg = p["package"]
            is_sys = p.get("is_system", False)
            if mode == "user" and is_sys:
                continue
            if mode == "system" and not is_sys:
                continue
            if query and query not in pkg.lower():
                continue

            sa = adbu.format_size(self.package_apk_sizes.get(pkg, 0))
            sd = adbu.format_size(self.package_data_sizes.get(pkg, 0))

            items.append({
                "key": pkg,
                "name": pkg,
                "apk_size": sa,
                "data_size": sd,
                "type": "System" if is_sys else "User",
                "is_system": is_sys,
            })
        self.backup_table.set_items(items)

    def _choose_dest(self):
        p = filedialog.askdirectory()
        if p:
            self.dest_var.set(p)

    def _choose_restore_src(self):
        p = filedialog.askdirectory()
        if p:
            self.restore_src_var.set(p)
            self.config_data["last_restore_root"] = p
            save_config(self.config_data)
            self._load_sessions()

    def start_backup(self):
        if not self.serial:
            messagebox.showwarning("Notice", "Detect device first.")
            return
        if not self.package_by_name and self.packages:
            self.package_by_name = {p["package"]: p for p in self.packages}

        keys = [k for k in self.backup_table.checked_keys if k in self.package_by_name]
        selected = []
        for k in keys:
            info = dict(self.package_by_name[k])
            if self.package_data_sizes.get(k, 0) > 0:
                info["_known_data_size"] = int(self.package_data_sizes[k])
            if self.package_apk_sizes.get(k, 0) > 0:
                info["_known_apk_size"] = int(self.package_apk_sizes[k])
            selected.append(info)
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showerror("Error", "Please select a backup destination directory.")
            return

        any_personal = self.inc_wifi_var.get() or self.inc_sms_var.get() or self.inc_contacts_var.get()
        any_storage = self.inc_storage_var.get() or self.inc_ext_sd_var.get()
        if not selected and not any_personal and not any_storage:
            messagebox.showwarning("Notice", "No applications or data components selected for backup.")
            return

        root_needed = bool(
            any_personal or (selected and (self.inc_data_var.get() or self.inc_ext_var.get() or self.inc_obb_var.get()))
        )
        if root_needed and not self.is_rooted:
            messagebox.showerror("Root Required", "The selected app-data/system components require root access. APK-only and normal storage backup can still be used without root.")
            return

        os.makedirs(dest, exist_ok=True)
        self.config_data["last_dest"] = dest
        save_config(self.config_data)
        opts = {
            "include_apk": self.inc_apk_var.get(),
            "include_data": self.inc_data_var.get(),
            "include_extdata": self.inc_ext_var.get(),
            "include_obb": self.inc_obb_var.get(),
            "include_permissions": self.inc_perms_var.get(),
            "include_wifi": self.inc_wifi_var.get(),
            "include_sms": self.inc_sms_var.get(),
            "include_contacts": self.inc_contacts_var.get(),
            "include_internal_storage": self.inc_storage_var.get(),
            "include_external_storage": self.inc_ext_sd_var.get(),
            "external_sd_path": self.external_sd_path,
            "exclude_app_data_in_storage": self.exclude_dup_var.get(),
            "use_zstd": self.use_zstd_var.get(),
        }

        cancel_event = self._begin_operation("BACKUP")
        if cancel_event is None:
            return
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.backup_pbar_frame.pack(fill="x", pady=(4, 4))
        self.backup_pbar.pack(fill="x")
        self.backup_lbl.pack(anchor="w")

        serial = self.serial
        def task():
            return backup_manager.run_backup(
                serial, selected, dest, opts,
                log=lambda m: self.after(0, lambda msg=m: self.log_backup(msg)),
                progress=lambda i, tot, sp: self.after(0, lambda ii=i, tt=tot, ss=sp: self._update_p(self.backup_pbar, self.backup_lbl, ii, tt, ss)),
                should_cancel=cancel_event.is_set,
            )

        def done(res, err):
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._end_operation()
            if self._closing:
                return
            if isinstance(err, adbu.OperationCancelled):
                messagebox.showinfo("Cancelled", "Backup was cancelled safely. The incomplete session was not committed.")
            elif err:
                messagebox.showerror("Error", str(err))
            elif res:
                result_type = res[1].get("result", "success")
                if result_type == "partial":
                    messagebox.showwarning("Backup Completed with Warnings", f"Backup session was committed, but some selected components failed. Review the log/manifest before relying on them.\n{res[0]}")
                else:
                    messagebox.showinfo("Success", f"Backup completed successfully!\n{res[0]}")

        self.run_async(task, done)

    def _update_p(self, pbar, lbl, i, total, speed):
        t = max(total, 1)
        pbar.set(i / t)
        lbl.configure(text=f"Progress: {i}/{t} ({int(i/t*100)}%) • {speed}")

    def _load_sessions(self):
        p = self.restore_src_var.get().strip()
        if not p:
            return
        self.sessions = restore_manager.list_sessions(p)
        if not self.sessions:
            messagebox.showinfo("Info", "No backup sessions found in selected folder.")
            self.session_combo.configure(values=[])
            self.session_cb_var.set("")
            self.restore_table.checked_keys.clear()
            self.restore_table.set_items([])
            self.current_session = None
            return
        names = [f"{s['name']} ({len(s['manifest'].get('apps', []))} apps)" for s in self.sessions]
        self.session_combo.configure(values=names)
        self.session_combo.set(names[0])
        self._on_session_picked()

    def _on_session_picked(self):
        val = self.session_cb_var.get()
        if not self.sessions or not val:
            return
        try:
            idx = [f"{s['name']} ({len(s['manifest'].get('apps', []))} apps)" for s in self.sessions].index(val)
        except ValueError:
            return
        self.current_session = self.sessions[idx]
        manifest = self.current_session["manifest"]
        session_root = self.current_session["path"]

        # 1. Populate apps table
        apps_data = list(manifest.get("apps", []))
        items = []
        has_any_apk = False
        has_any_data = False
        has_any_ext = False
        has_any_obb = False
        has_any_perms = False

        for a in apps_data:
            perms_count = len(a.get("permissions", []))
            if a.get("apk_files"):
                has_any_apk = True
            if a.get("has_data"):
                has_any_data = True
            if a.get("has_extdata"):
                has_any_ext = True
            if a.get("has_obb"):
                has_any_obb = True
            if perms_count > 0:
                has_any_perms = True

            items.append({
                "key": a["package"],
                "name": a["package"],
                "version": a.get("versionName", "—"),
                "perms": f"{perms_count} perms" if perms_count else "—",
            })
        self.restore_table.checked_keys.clear()
        self.restore_table.set_items(items)

        # 2. Dynamic Gray-Out for Application Scope
        self.chk_rest_apk.configure(state="normal" if has_any_apk else "disabled")
        self.rest_apk_var.set(has_any_apk)

        self.chk_rest_data.configure(state="normal" if has_any_data else "disabled")
        self.rest_data_var.set(has_any_data)

        self.chk_rest_ext.configure(state="normal" if has_any_ext else "disabled")
        self.rest_ext_var.set(has_any_ext)

        self.chk_rest_obb.configure(state="normal" if has_any_obb else "disabled")
        self.rest_obb_var.set(has_any_obb)

        self.chk_rest_perms.configure(state="normal" if has_any_perms else "disabled")
        self.rest_perms_var.set(has_any_perms)

        # 3. Dynamic Gray-Out for Personal Databases (new metadata + legacy files)
        sys_dir = os.path.join(session_root, "system_data")
        system_meta = manifest.get("system_data", {}) if isinstance(manifest.get("system_data"), dict) else {}
        wifi_meta = system_meta.get("wifi")
        has_wifi = bool(isinstance(wifi_meta, dict) and wifi_meta.get("included")) or any(
            os.path.exists(os.path.join(sys_dir, name)) for name in ("WifiConfigStore.xml", "wpa_supplicant.conf")
        )
        sms_meta = system_meta.get("sms")
        has_sms = bool(isinstance(sms_meta, dict) and sms_meta.get("included")) or os.path.exists(os.path.join(sys_dir, "sms_databases.tar.gz"))
        cc_meta = system_meta.get("contacts_and_calls")
        has_contacts = bool(
            isinstance(cc_meta, dict) and any(isinstance(v, dict) and v.get("included") for v in cc_meta.values())
        ) or os.path.exists(os.path.join(sys_dir, "contacts_databases.tar.gz")) or os.path.exists(os.path.join(sys_dir, "samsung_calllogs.tar.gz"))

        self.chk_rest_wifi.configure(state="normal" if has_wifi else "disabled")
        self.rest_wifi_var.set(has_wifi)
        self.chk_rest_sms.configure(state="normal" if has_sms else "disabled")
        self.rest_sms_var.set(has_sms)
        self.chk_rest_contacts.configure(state="normal" if has_contacts else "disabled")
        self.rest_contacts_var.set(has_contacts)

        # 4. Dynamic Gray-Out for Storage Scope
        if int(manifest.get("schema_version", 0) or 0) >= 3:
            has_internal_storage = bool(manifest.get("internal_storage_included"))
        else:
            has_internal_storage = manifest.get("internal_storage_included") or os.path.isdir(os.path.join(session_root, "internal_storage"))
        self.chk_rest_internal.configure(state="normal" if has_internal_storage else "disabled")
        self.rest_storage_var.set(bool(has_internal_storage))

        if int(manifest.get("schema_version", 0) or 0) >= 3:
            has_ext_sd_backup = bool(manifest.get("external_storage_included"))
        else:
            has_ext_sd_backup = manifest.get("external_storage_included") or os.path.isdir(os.path.join(session_root, "external_storage"))
        if has_ext_sd_backup:
            if self.external_sd_path:
                sd_name = os.path.basename(self.external_sd_path)
                self.chk_rest_ext_sd.configure(state="normal", text=f"MicroSD Card ({sd_name})")
                self.rest_ext_sd_var.set(True)
            else:
                self.chk_rest_ext_sd.configure(state="disabled", text="MicroSD Card (Phone has no SD Card)")
                self.rest_ext_sd_var.set(False)
        else:
            self.chk_rest_ext_sd.configure(state="disabled", text="MicroSD Card (Not in backup)")
            self.rest_ext_sd_var.set(False)

        # 5. Background thread extracts launcher icons; old session workers self-cancel.
        self.icon_loader_generation += 1
        icon_generation = self.icon_loader_generation
        def background_icon_loader():
            for a in apps_data:
                if icon_generation != self.icon_loader_generation:
                    return
                pkg = a["package"]
                cached = os.path.join(adbu.ICON_CACHE_DIR, f"{pkg}.png")
                if not os.path.exists(cached):
                    pkg_apk_dir = os.path.join(session_root, "apps", pkg, "apk")
                    target_apk = os.path.join(pkg_apk_dir, "base.apk")
                    if not os.path.isfile(target_apk):
                        apk_files = a.get("apk_files", [])
                        base_match = next((f for f in apk_files if "base" in f.lower()), None)
                        chosen = base_match or (apk_files[0] if apk_files else None)
                        if chosen:
                            target_apk = os.path.join(pkg_apk_dir, chosen)

                    if os.path.isfile(target_apk):
                        adbu.extract_icon_from_local_apk(target_apk, pkg)

                if os.path.exists(cached):
                    self.after(0, lambda p=pkg: self.restore_table.on_icon_updated(p))
                time.sleep(0.01)

        threading.Thread(target=background_icon_loader, daemon=True).start()

    def start_restore(self):
        if not self.serial or not self.current_session:
            return
        keys = list(self.restore_table.checked_keys)
        restore_storage = self.rest_storage_var.get()
        restore_ext_sd = self.rest_ext_sd_var.get()
        restore_any_personal = self.rest_wifi_var.get() or self.rest_sms_var.get() or self.rest_contacts_var.get()
        if not keys and not restore_storage and not restore_ext_sd and not restore_any_personal:
            messagebox.showwarning("Notice", "No components selected for restore.")
            return

        opts = {
            "restore_apk": self.rest_apk_var.get(),
            "restore_data": self.rest_data_var.get(),
            "restore_extdata": self.rest_ext_var.get(),
            "restore_obb": self.rest_obb_var.get(),
            "restore_permissions": self.rest_perms_var.get(),
            "restore_wifi": self.rest_wifi_var.get(),
            "restore_sms": self.rest_sms_var.get(),
            "restore_contacts": self.rest_contacts_var.get(),
        }
        root_needed = bool(
            restore_any_personal or (keys and (opts["restore_data"] or opts["restore_extdata"] or opts["restore_obb"] or opts["restore_permissions"]))
        )
        if root_needed and not self.is_rooted:
            messagebox.showerror("Root Required", "The selected restore components require root access. APK-only and normal storage restore can run without root.")
            return

        cancel_event = self._begin_operation("RESTORE")
        if cancel_event is None:
            return
        self.rest_start_btn.configure(state="disabled")
        self.rest_cancel_btn.configure(state="normal")

        session_dir = self.current_session["path"]
        manifest = self.current_session["manifest"]
        serial = self.serial

        def task():
            return restore_manager.run_restore(
                serial, session_dir, manifest, keys, opts,
                restore_internal=restore_storage,
                restore_external=restore_ext_sd,
                external_sd_path=self.external_sd_path,
                log=lambda m: self.after(0, lambda msg=m: self.log_restore(msg)),
                progress=lambda i, total: self.after(0, lambda ii=i, tt=total: self.log_restore(f"Progress: {ii}/{tt}")),
                should_cancel=cancel_event.is_set,
            )

        def done(res, err):
            self.rest_start_btn.configure(state="normal")
            self.rest_cancel_btn.configure(state="disabled")
            self._end_operation()
            if self._closing:
                return
            if isinstance(err, adbu.OperationCancelled):
                messagebox.showinfo("Cancelled", "Restore was cancelled. Any active transactional component was rolled back when possible.")
                return
            if err:
                messagebox.showerror("Restore Error", str(err))
                return
            summary = res.summary() if res else {}
            if res and res.failed:
                messagebox.showwarning(
                    "Restore Completed with Failures",
                    f"Succeeded: {summary.get('succeeded', 0)}\nFailed: {summary.get('failed', 0)}\nNot restored: {summary.get('unrestored', 0)}\nSkipped: {summary.get('skipped', 0)}\nRolled back: {summary.get('rolled_back', 0)}\n\nReview the restore log before using affected apps."
                )
            elif res and getattr(res, "unrestored", None):
                messagebox.showwarning(
                    "Restore Completed Partially",
                    f"Succeeded: {summary.get('succeeded', 0)}\nNot restored for compatibility/safety: {summary.get('unrestored', 0)}\nSkipped: {summary.get('skipped', 0)}\n\nNo incompatible component was overwritten; review the log for details."
                )
            else:
                messagebox.showinfo("Done", f"Restore finished safely.\nSucceeded: {summary.get('succeeded', 0)}\nSkipped: {summary.get('skipped', 0)}")

        self.run_async(task, done)

    # ------------------------------------------------------------------
    # Debloater Handlers
    # ------------------------------------------------------------------
    def _populate_debloater(self):
        items = []
        for p in self.packages:
            pkg = p["package"]
            preset = adbu.BLOATWARE_PRESETS.get(pkg)
            is_sys = p.get("is_system", False)

            if preset:
                vendor = preset.get("vendor", "OEM / Known")
                level = preset.get("level", "Safe")
                desc = preset.get("desc", "Known pre-installed package")
                is_known_bloat = True
            else:
                vendor = "System" if is_sys else "User App"
                level = "Caution" if is_sys else "User App"
                desc = "Unverified system component" if is_sys else "User-installed application"
                is_known_bloat = False

            st = "Frozen" if p.get("is_disabled") else "Active"
            items.append({
                "key": pkg,
                "name": pkg,
                "vendor": vendor,
                "level": level,
                "desc": desc,
                "status": st,
                "is_system": is_sys,
                "is_known_bloat": is_known_bloat,
            })
        valid_keys = {it["key"] for it in items}
        self.debloater_table.checked_keys.intersection_update(valid_keys)
        self.debloater_table.set_items(items)

    def _select_safe_bloat(self):
        count = 0
        for k, it in self.debloater_table.items.items():
            if it.get("is_known_bloat") and it.get("level") in ("Safe", "Recommended"):
                self.debloater_table.checked_keys.add(k)
                self.debloater_table.refresh_row_icon(k)
                count += 1
        messagebox.showinfo("Selected", f"Selected {count} verified bloatware packages.")

    def _run_debloat_action(self, label, func, success_status=None, refresh_after=False):
        if not self.serial:
            messagebox.showwarning("Notice", "Detect device first.")
            return
        keys = list(self.debloater_table.checked_keys)
        if not keys:
            messagebox.showwarning("Notice", "No packages selected.")
            return
        cancel_event = self._begin_operation("DEBLOAT")
        if cancel_event is None:
            return
        serial = self.serial
        lock_id = f"debloat-{int(time.time())}-{threading.get_ident()}"

        def task():
            if not adbu.acquire_device_operation_lock(serial, lock_id):
                raise RuntimeError("Another DroidVault operation is already active on this device.")
            heartbeat = adbu.start_device_lock_heartbeat(serial, lock_id)
            ok, failed = [], []
            try:
                for pkg in keys:
                    if cancel_event.is_set():
                        raise adbu.OperationCancelled("Debloat operation cancelled.")
                    try:
                        if func(serial, pkg):
                            ok.append(pkg)
                        else:
                            failed.append(pkg)
                    except Exception:
                        failed.append(pkg)
                return ok, failed
            finally:
                adbu.stop_device_lock_heartbeat(heartbeat)
                adbu.release_device_operation_lock(serial, lock_id)

        def done(res, err):
            self._end_operation()
            if self._closing:
                return
            if isinstance(err, adbu.OperationCancelled):
                messagebox.showinfo("Cancelled", f"{label} was cancelled.")
                return
            if err:
                messagebox.showerror("Error", str(err))
                return
            ok, failed = res
            if success_status:
                for pkg in ok:
                    self.debloater_table.update_col(pkg, 3, success_status)
            if failed:
                messagebox.showwarning(label, f"Succeeded: {len(ok)}\nFailed: {len(failed)}\n\n" + "\n".join(failed[:20]))
            else:
                messagebox.showinfo(label, f"Succeeded for {len(ok)} package(s).")
            if refresh_after:
                self.detect_device()

        self.run_async(task, done)

    def _debloat_freeze(self):
        self._run_debloat_action("Freeze", adbu.freeze_package, success_status="Frozen")

    def _debloat_unfreeze(self):
        self._run_debloat_action("Unfreeze", adbu.unfreeze_package, success_status="Active")

    def confirm_dangerous_action(self, action_name, target_name, risk_warning):
        """Display an explicit warning dialog before executing sensitive and hazardous operations."""
        title = f"⚠️ High-Risk Action: {action_name}"
        msg = (
            f"You are about to execute a privileged operation on:\n\n"
            f"Target: {target_name}\n\n"
            f"Risk:\n{risk_warning}\n\n"
            f"Are you sure you want to proceed?"
        )
        return mbox.askyesno(title, msg, icon="warning", parent=self)
    
    def _debloat_uninstall(self):
        keys = list(self.debloater_table.checked_keys)
        if not keys:
            messagebox.showwarning("Notice", "No packages selected.")
            return

        preview_pkgs = ", ".join(keys[:4]) + (f" ... and {len(keys) - 4} more" if len(keys) > 4 else "")
        risk_text = (
            "Removing system packages (user 0) can break core system features, "
            "trigger UI crashes, or lead to soft bootloops on vendor ROMs.\n"
            "Keep in mind that some packages cannot be safely re-installed without a factory reset."
        )

        if not self.confirm_dangerous_action("Uninstall System Packages (User 0)", f"{len(keys)} package(s):\n{preview_pkgs}", risk_text):
            return

        self._run_debloat_action("Uninstall", adbu.uninstall_package_user0, refresh_after=True)

    # ------------------------------------------------------------------
    # Modder's Toolkit Handlers
    # ------------------------------------------------------------------
    def _dump_boot(self):
        if not self.serial:
            return
        if not self.is_rooted:
            messagebox.showerror("Root Required", "Dumping boot.img requires root access.")
            return

        warn = (
            "Direct block-level dumping reads raw kernel flash storage.\n"
            "Ensure target drive has at least 128 MB free."
        )
        if not self.confirm_dangerous_action("Boot Partition Dump", "Kernel Boot Block (/dev/block/.../boot)", warn):
            return

        dest = filedialog.asksaveasfilename(defaultextension=".img", filetypes=[("Boot Image", "*.img")])
        if not dest:
            return
        cancel_event = self._begin_operation("BOOT_DUMP")
        if cancel_event is None:
            return
        serial = self.serial

        def task():
            lock_id = f"boot-{int(time.time())}"
            if not adbu.acquire_device_operation_lock(serial, lock_id):
                raise RuntimeError("Another DroidVault operation is already active on this device.")
            heartbeat = adbu.start_device_lock_heartbeat(serial, lock_id)
            try:
                return adbu.dump_boot_partition(serial, dest, should_cancel=cancel_event.is_set)
            finally:
                adbu.stop_device_lock_heartbeat(heartbeat)
                adbu.release_device_operation_lock(serial, lock_id)

        def done(res, err):
            self._end_operation()
            if self._closing:
                return
            if isinstance(err, adbu.OperationCancelled):
                messagebox.showinfo("Cancelled", "Boot dump cancelled; partial output was removed.")
            elif err:
                messagebox.showerror("Error", str(err))
            elif res:
                messagebox.showinfo("Success", f"Saved boot.img to:\n{dest}")
            else:
                messagebox.showerror("Error", "Could not dump boot partition.")

        self.run_async(task, done)

    def _backup_modules(self):
        if not self.serial:
            return
        if not self.is_rooted:
            messagebox.showerror("Root Required", "Backing up root modules requires root access.")
            return
        dest = filedialog.asksaveasfilename(defaultextension=".tar.gz", filetypes=[("Archive", "*.tar.gz")])
        if not dest:
            return
        cancel_event = self._begin_operation("MODULE_BACKUP")
        if cancel_event is None:
            return
        serial = self.serial

        def task():
            lock_id = f"modules-{int(time.time())}"
            if not adbu.acquire_device_operation_lock(serial, lock_id):
                raise RuntimeError("Another DroidVault operation is already active on this device.")
            heartbeat = adbu.start_device_lock_heartbeat(serial, lock_id)
            try:
                return adbu.backup_root_modules(serial, dest, should_cancel=cancel_event.is_set)
            finally:
                adbu.stop_device_lock_heartbeat(heartbeat)
                adbu.release_device_operation_lock(serial, lock_id)

        def done(res, err):
            self._end_operation()
            if self._closing:
                return
            if isinstance(err, adbu.OperationCancelled):
                messagebox.showinfo("Cancelled", "Modules backup cancelled; partial archive was removed.")
            elif err:
                messagebox.showerror("Error", str(err))
            elif res:
                messagebox.showinfo("Success", f"Saved modules to:\n{dest}")
            else:
                messagebox.showerror("Error", "Could not archive modules.")

        self.run_async(task, done)

    # ------------------------------------------------------------------
    # Scrcpy & Wireless Hub Handlers
    # ------------------------------------------------------------------
    def _launch_mirror(self):
        if not self.serial:
            messagebox.showwarning("Notice", "Detect device first.")
            return

        saved_path = self.config_data.get("scrcpy_path", "")
        ok, msg = adbu.launch_scrcpy(self.serial, custom_path=saved_path)

        if not ok and msg == "NOT_FOUND":
            choice = messagebox.askyesno(
                "Scrcpy Not Found",
                "scrcpy.exe was not detected automatically.\n\n"
                "Would you like to select scrcpy.exe manually?",
            )
            if choice:
                exe_path = filedialog.askopenfilename(
                    title="Select scrcpy.exe",
                    filetypes=[("Executable", "scrcpy.exe"), ("All Files", "*.*")]
                )
                if exe_path:
                    self.config_data["scrcpy_path"] = exe_path
                    save_config(self.config_data)
                    ok2, msg2 = adbu.launch_scrcpy(self.serial, custom_path=exe_path)
                    if not ok2:
                        messagebox.showerror("Error", msg2)
        elif not ok:
            messagebox.showerror("Scrcpy Error", msg)

    def _open_wireless_dialog(self):
        w = ctk.CTkToplevel(self)
        w.title("Wireless ADB")
        w.geometry("420x330")
        w.resizable(False, False)
        w.transient(self)
        w.grab_set()

        b = ctk.CTkFrame(w, fg_color="transparent")
        b.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(b, text="Wireless Debugging", font=FONT_H1, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(b, text="Enable 'Wireless debugging' on your phone.", font=FONT_SMALL, text_color=("#64748B", "#94A3B8")).pack(anchor="w", pady=(0, 10))

        p_card = make_card(b)
        p_card.pack(fill="x", pady=(0, 8))
        pb = ctk.CTkFrame(p_card, fg_color="transparent")
        pb.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(pb, text="1. Pair with pairing code", font=FONT_H2, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        r1 = ctk.CTkFrame(pb, fg_color="transparent")
        r1.pack(fill="x")
        ip_pair = ctk.CTkEntry(r1, placeholder_text="IP:Port", width=140, height=28)
        ip_pair.pack(side="left", padx=(0, 6))
        code_pair = ctk.CTkEntry(r1, placeholder_text="Code", width=90, height=28)
        code_pair.pack(side="left", padx=(0, 6))
        ctk.CTkButton(r1, text="Pair", width=60, height=28, fg_color=ACCENT,
                      command=lambda: self._do_pair(ip_pair.get().strip(), code_pair.get().strip())).pack(side="left")

        c_card = make_card(b)
        c_card.pack(fill="x", pady=(0, 10))
        cb = ctk.CTkFrame(c_card, fg_color="transparent")
        cb.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(cb, text="2. Connect to Device IP", font=FONT_H2, text_color=("#0F172A", "#F8FAFC")).pack(anchor="w", pady=(0, 4))
        r2 = ctk.CTkFrame(cb, fg_color="transparent")
        r2.pack(fill="x")
        ip_conn = ctk.CTkEntry(r2, placeholder_text="192.168.1.X:5555", height=28)
        ip_conn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(r2, text="Connect", width=70, height=28, fg_color=SUCCESS,
                      command=lambda: self._do_connect(ip_conn.get().strip(), w)).pack(side="left")

    def _do_pair(self, ip, code):
        if not ip or not code:
            return
        ok, msg = adbu.adb_pair(ip, code)
        if ok:
            messagebox.showinfo("Success", "Device paired! Now enter IP in Connect box.")
        else:
            messagebox.showerror("Error", msg)

    def _do_connect(self, ip, win):
        if not ip:
            return
        ok, msg = adbu.adb_connect(ip)
        if ok:
            messagebox.showinfo("Success", f"Connected to {ip}!")
            self.serial = ip
            self.detect_device()
            win.destroy()
        else:
            messagebox.showerror("Error", msg)


if __name__ == "__main__":
    app = App()
    app.mainloop()
