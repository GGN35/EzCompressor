"""EzCompressor - sidechain ducker for Windows.

Reads the peak level of a source (input device, output device or app) and
lowers the Windows volume of a target (output device or app) like a
compressor: threshold, ratio, attack, release, max reduction.
"""
import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

# Use the multithreaded COM apartment everywhere: COM objects made in the
# engine thread may be released by the garbage collector in the UI thread.
sys.coinit_flags = 0  # COINIT_MULTITHREADED, must be set before comtypes import
import comtypes
from comtypes import CLSCTX_ALL

from pycaw.pycaw import (
    IAudioClient,
    IAudioEndpointVolume,
    IAudioMeterInformation,
    IAudioSessionControl2,
    IAudioSessionManager2,
    ISimpleAudioVolume,
)
from pycaw.utils import AudioUtilities, AudioDeviceState

import pystray
from PIL import Image, ImageDraw

APP_NAME = "EzCompressor"
# Settings live next to the .exe (or the script when run from source).
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                          else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "EzCompressor.json")
OLD_CONFIG_PATH = os.path.join(os.environ.get("APPDATA", "."), APP_NAME, "config.json")
TICK = 0.01  # control loop period (s)
MIN_DB = -90.0

DEFAULTS = {
    "source": None,
    "target": None,
    "threshold": -30.0,
    "ratio": 4.0,
    "attack": 20.0,
    "release": 400.0,
    "range": 30.0,
    "level": 100.0,
    "static": False,
    "sensitivity": 100.0,
    "enabled": False,
}


# --------------------------------------------------------------------------
# Audio endpoints
# --------------------------------------------------------------------------
# Channel keys: "in:<device id>", "out:<device id>", "app:<process name>"

def list_channels():
    """Return (sources, targets) as lists of (key, label)."""
    sources, targets = [], []
    apps = set()
    for dev in AudioUtilities.GetAllDevices():
        if dev.state != AudioDeviceState.Active or not dev.id:
            continue
        is_capture = dev.id.startswith("{0.0.1.")
        if is_capture:
            sources.append((f"in:{dev.id}", f"🎤 {dev.FriendlyName}"))
        else:
            sources.append((f"out:{dev.id}", f"🔊 {dev.FriendlyName}"))
            targets.append((f"out:{dev.id}", f"🔊 {dev.FriendlyName}"))
            for name in _device_app_names(dev.id):
                apps.add(name)
    for name in sorted(apps, key=str.lower):
        sources.append((f"app:{name}", f"▶ {name}"))
        targets.append((f"app:{name}", f"▶ {name}"))
    return sources, targets


def _imm_device(dev_id):
    return AudioUtilities.GetDeviceEnumerator().GetDevice(dev_id)


def _activate(dev, iface):
    # QueryInterface keeps COM refcounts right; ctypes.cast would not
    # AddRef and the object gets released twice by the garbage collector.
    return dev.Activate(iface._iid_, CLSCTX_ALL, None).QueryInterface(iface)


def _device_sessions(dev_id):
    """Yield (process name, IAudioSessionControl2) for a render device."""
    try:
        mgr = _activate(_imm_device(dev_id), IAudioSessionManager2)
        enum = mgr.GetSessionEnumerator()
        for i in range(enum.GetCount()):
            ctl = enum.GetSession(i).QueryInterface(IAudioSessionControl2)
            pid = ctl.GetProcessId()
            if not pid:
                continue
            try:
                import psutil
                name = psutil.Process(pid).name()
            except Exception:
                continue
            yield name, ctl
    except Exception:
        return


def _device_app_names(dev_id):
    return {name for name, _ in _device_sessions(dev_id)}


def _render_device_ids():
    return [d.id for d in AudioUtilities.GetAllDevices()
            if d.state == AudioDeviceState.Active and d.id and d.id.startswith("{0.0.0.")]


class Source:
    """Peak meter for a device or for all sessions of an app."""

    def __init__(self, key):
        kind, ident = key.split(":", 1)
        self.kind, self.ident = kind, ident
        self.meters = []
        self.client = None
        self.last_refresh = 0.0
        if kind in ("in", "out"):
            dev = _imm_device(ident)
            self.meters = [_activate(dev, IAudioMeterInformation)]
            if kind == "in":
                # A capture endpoint only meters while a stream is open,
                # so keep a silent shared-mode stream running.
                c = _activate(dev, IAudioClient)
                fmt = c.GetMixFormat()
                c.Initialize(0, 0, 2_000_000, 0, fmt, None)
                c.Start()
                self.client = c
        else:
            self._refresh_sessions()

    def _refresh_sessions(self):
        self.meters = []
        for dev_id in _render_device_ids():
            for name, ctl in _device_sessions(dev_id):
                if name.lower() == self.ident.lower():
                    self.meters.append(ctl.QueryInterface(IAudioMeterInformation))
        self.last_refresh = time.monotonic()

    def peak(self):
        if self.kind == "app" and time.monotonic() - self.last_refresh > 3.0:
            self._refresh_sessions()
        p = 0.0
        for m in self.meters:
            try:
                p = max(p, m.GetPeakValue())
            except Exception:
                pass
        return p

    def close(self):
        if self.client is not None:
            try:
                self.client.Stop()
            except Exception:
                pass
            self.client = None


class Target:
    """Volume control for a device or all sessions of an app.

    The normal (un-ducked) volume is the fixed 'level' setting, not the
    volume read at start: a volume left lowered by a crash or by another
    instance would otherwise become the new normal.
    """

    def __init__(self, key, level):
        kind, ident = key.split(":", 1)
        self.kind, self.ident = kind, ident
        self.vols = {}  # id -> interface
        self.last_refresh = 0.0
        self.last_gr = None
        if kind == "out":
            ev = _activate(_imm_device(ident), IAudioEndpointVolume)
            self.min_db, self.max_db, _ = ev.GetVolumeRange()
            self.vols["dev"] = ev
        self.set_level(level)
        if kind == "app":
            self._refresh_sessions()

    def set_level(self, level):
        self.level = level
        if self.kind == "out":
            # map the Windows volume-slider scalar to dB once
            ev = self.vols["dev"]
            ev.SetMasterVolumeLevelScalar(level, None)
            self.base_db = ev.GetMasterVolumeLevel()
        self.last_gr = None

    def _refresh_sessions(self):
        seen = set()
        for dev_id in _render_device_ids():
            for name, ctl in _device_sessions(dev_id):
                if name.lower() != self.ident.lower():
                    continue
                sid = ctl.GetSessionInstanceIdentifier()
                seen.add(sid)
                if sid not in self.vols:
                    self.vols[sid] = ctl.QueryInterface(ISimpleAudioVolume)
                    self.last_gr = None  # force apply to the new session
        for sid in list(self.vols):
            if sid not in seen:
                self.vols.pop(sid)
        self.last_refresh = time.monotonic()

    def apply(self, gr_db):
        if self.kind == "app" and time.monotonic() - self.last_refresh > 3.0:
            self._refresh_sessions()
        if self.last_gr is not None and abs(gr_db - self.last_gr) < 0.05:
            return
        self.last_gr = gr_db
        for k, vol in list(self.vols.items()):
            try:
                if self.kind == "out":
                    db = max(self.min_db, self.base_db - gr_db)
                    vol.SetMasterVolumeLevel(db, None)
                else:
                    vol.SetMasterVolume(self.level * 10 ** (-gr_db / 20), None)
            except Exception:
                pass

    def close(self):
        self.last_gr = None
        for vol in self.vols.values():
            try:
                if self.kind == "out":
                    vol.SetMasterVolumeLevel(self.base_db, None)
                else:
                    vol.SetMasterVolume(self.level, None)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Compressor engine
# --------------------------------------------------------------------------

class Engine(threading.Thread):
    def __init__(self, settings):
        super().__init__(daemon=True)
        self.settings = settings
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.rebuild = threading.Event()
        self.level_db = MIN_DB
        self.gr_db = 0.0
        self.error = ""

    def request_rebuild(self):
        self.rebuild.set()

    def run(self):
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        src = tgt = None
        env = 0.0
        power = 0.0  # smoothed squared peak for the level detector
        try:
            while not self.stop_evt.is_set():
                if self.rebuild.is_set():
                    self.rebuild.clear()
                    for o in (src, tgt):
                        if o:
                            o.close()
                    src = tgt = None
                    env = 0.0
                    self.gr_db = 0.0
                    s = self.settings
                    self.error = ""
                    try:
                        if s["source"]:
                            src = Source(s["source"])
                        if s["enabled"] and s["target"]:
                            if s["target"] == s["source"]:
                                self.error = "Kaynak ve hedef aynı olamaz"
                            else:
                                tgt = Target(s["target"], s["level"] / 100)
                    except Exception as e:
                        self.error = f"Hata: {e}"

                s = self.settings
                peak = src.peak() if src else 0.0
                # Sensitivity 100% follows peaks (~5 ms), 0% averages the
                # signal power over ~1 s for a calmer, mean-based level.
                tau = 1000 * 0.005 ** (s["sensitivity"] / 100)
                coef = math.exp(-TICK * 1000 / tau)
                power = peak * peak + (power - peak * peak) * coef
                level = 10 * math.log10(power) if power > 1e-10 else MIN_DB
                self.level_db = level

                over = level - s["threshold"]
                if s["static"]:
                    # fixed reduction while the source is above threshold
                    target_gr = s["range"] if over > 0 else 0.0
                else:
                    target_gr = over * (1 - 1 / s["ratio"]) if over > 0 else 0.0
                    target_gr = min(target_gr, s["range"])
                tc = s["attack"] if target_gr > env else s["release"]
                coef = math.exp(-TICK * 1000 / max(tc, 1.0))
                env = target_gr + (env - target_gr) * coef
                self.gr_db = env if tgt else 0.0

                if tgt:
                    if tgt.level != s["level"] / 100:
                        tgt.set_level(s["level"] / 100)
                    tgt.apply(env)
                time.sleep(TICK)
        finally:
            for o in (src, tgt):
                if o:
                    o.close()
            comtypes.CoUninitialize()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def load_settings():
    s = dict(DEFAULTS)
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else OLD_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def make_icon(on):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (46, 204, 113) if on else (127, 140, 141)
    d.rounded_rectangle((4, 4, 60, 60), 12, fill=color)
    for i, h in enumerate((18, 34, 26, 12)):
        x = 14 + i * 10
        d.rectangle((x, 48 - h, x + 6, 48), fill="white")
    return img


class App:
    def __init__(self, start_hidden=False):
        self.s = load_settings()
        save_settings(self.s)  # create/migrate the file next to the exe
        self.engine = Engine(self.s)
        self.engine.request_rebuild()
        self.engine.start()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self.sources, self.targets = [], []
        self._build_ui()
        self.refresh_channels()
        self._setup_tray()
        self._tick()
        if start_hidden:
            self.root.withdraw()

    # ---- layout ----
    def _build_ui(self):
        f = ttk.Frame(self.root, padding=12)
        f.grid(sticky="nsew")
        f.columnconfigure(1, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.enabled_var = tk.BooleanVar(value=self.s["enabled"])
        self.toggle_btn = tk.Button(top, width=8, font=("Segoe UI", 11, "bold"),
                                    relief="flat", command=self.toggle)
        self.toggle_btn.pack(side="left")
        ttk.Button(top, text="↻ Yenile", command=self.refresh_channels).pack(side="right")
        self.static_var = tk.BooleanVar(value=self.s["static"])
        ttk.Checkbutton(top, text="Statik kısma", variable=self.static_var,
                        command=self._on_static).pack(side="left", padx=12)
        self._update_toggle()

        ttk.Label(f, text="Kaynak (sidechain)").grid(row=1, column=0, sticky="w")
        self.src_cb = ttk.Combobox(f, state="readonly", width=48)
        self.src_cb.grid(row=1, column=1, columnspan=2, sticky="ew", pady=2)
        self.src_cb.bind("<<ComboboxSelected>>", lambda e: self._on_channel())

        ttk.Label(f, text="Hedef (kısılacak)").grid(row=2, column=0, sticky="w")
        self.tgt_cb = ttk.Combobox(f, state="readonly", width=48)
        self.tgt_cb.grid(row=2, column=1, columnspan=2, sticky="ew", pady=2)
        self.tgt_cb.bind("<<ComboboxSelected>>", lambda e: self._on_channel())

        ttk.Separator(f).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)

        self.param_vars = {}
        self.param_labels, self.param_scales = {}, {}
        params = [
            ("threshold", "Threshold", -80, 0, "dB"),
            ("sensitivity", "Hassasiyet", 0, 100, "%"),
            ("ratio", "Ratio", 1, 20, ":1"),
            ("attack", "Attack", 1, 1000, "ms"),
            ("release", "Release", 10, 5000, "ms"),
            ("range", "Max kısma", 1, 60, "dB"),
            ("level", "Hedef seviyesi", 0, 100, "%"),
        ]
        for i, (key, label, lo, hi, unit) in enumerate(params, start=4):
            name_lbl = ttk.Label(f, text=label)
            name_lbl.grid(row=i, column=0, sticky="w")
            self.param_labels[key] = name_lbl
            var = tk.DoubleVar(value=self.s[key])
            val_lbl = ttk.Label(f, width=9, anchor="e")
            scale = ttk.Scale(f, from_=lo, to=hi, variable=var, length=300,
                              command=lambda v, k=key, l=val_lbl, u=unit: self._on_param(k, l, u))
            scale.grid(row=i, column=1, sticky="ew", pady=2)
            val_lbl.grid(row=i, column=2, sticky="e")
            self.param_vars[key] = var
            self.param_scales[key] = scale
            self._on_param(key, val_lbl, unit, save=False)
        self._on_static(save=False)

        ttk.Separator(f).grid(row=11, column=0, columnspan=3, sticky="ew", pady=8)

        ttk.Label(f, text="Giriş seviyesi").grid(row=12, column=0, sticky="w")
        self.level_cv = tk.Canvas(f, height=14, bg="#222", highlightthickness=0)
        self.level_cv.grid(row=12, column=1, sticky="ew", pady=2)
        self.level_lbl = ttk.Label(f, width=9, anchor="e")
        self.level_lbl.grid(row=12, column=2, sticky="e")

        ttk.Label(f, text="Kısma (GR)").grid(row=13, column=0, sticky="w")
        self.gr_cv = tk.Canvas(f, height=14, bg="#222", highlightthickness=0)
        self.gr_cv.grid(row=13, column=1, sticky="ew", pady=2)
        self.gr_lbl = ttk.Label(f, width=9, anchor="e")
        self.gr_lbl.grid(row=13, column=2, sticky="e")

        self.status = ttk.Label(f, foreground="#c0392b")
        self.status.grid(row=14, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(f, text="Pencereyi kapatınca tepsi simgesinde çalışmaya devam eder.",
                  foreground="#777").grid(row=15, column=0, columnspan=3, sticky="w")

    def _on_static(self, save=True):
        static = self.static_var.get()
        self.s["static"] = static
        # static mode: ratio is unused, 'range' is the fixed reduction amount
        self.param_scales["ratio"].state(["disabled"] if static else ["!disabled"])
        self.param_labels["ratio"].state(["disabled"] if static else ["!disabled"])
        self.param_labels["range"].config(text="Kısma miktarı" if static else "Max kısma")
        if save:
            save_settings(self.s)

    def _on_param(self, key, label, unit, save=True):
        v = self.param_vars[key].get()
        if key == "ratio":
            v = round(v, 1)
            label.config(text=f"{v:g}{unit}" if v < 20 else "∞:1")
            if v >= 20:
                v = 1000.0
        elif key in ("attack", "release", "level", "sensitivity"):
            v = round(v)
            label.config(text=f"{v:g} {unit}")
        else:
            v = round(v, 1)
            label.config(text=f"{v:g} {unit}")
        self.s[key] = float(v)
        if save:
            save_settings(self.s)

    # ---- channels ----
    def refresh_channels(self):
        self.sources, self.targets = list_channels()
        for cb, items, key in ((self.src_cb, self.sources, "source"),
                               (self.tgt_cb, self.targets, "target")):
            keys = [k for k, _ in items]
            cur = self.s.get(key)
            if cur and cur not in keys and cur.startswith("app:"):
                # keep app selection even if it is not playing right now
                items.append((cur, f"▶ {cur[4:]} (kapalı)"))
                keys.append(cur)
            cb["values"] = [l for _, l in items]
            if cur in keys:
                cb.current(keys.index(cur))
            else:
                cb.set("")
        self.engine.request_rebuild()

    def _on_channel(self):
        i, j = self.src_cb.current(), self.tgt_cb.current()
        self.s["source"] = self.sources[i][0] if i >= 0 else None
        self.s["target"] = self.targets[j][0] if j >= 0 else None
        save_settings(self.s)
        self.engine.request_rebuild()

    # ---- on/off ----
    def toggle(self):
        self.s["enabled"] = not self.s["enabled"]
        save_settings(self.s)
        self._update_toggle()
        self.engine.request_rebuild()
        if hasattr(self, "tray"):
            self.tray.icon = make_icon(self.s["enabled"])
            self.tray.update_menu()

    def _update_toggle(self):
        on = self.s["enabled"]
        self.toggle_btn.config(text="ON" if on else "OFF",
                               bg="#2ecc71" if on else "#7f8c8d",
                               fg="white", activebackground="#27ae60" if on else "#95a5a6")

    # ---- meters ----
    def _tick(self):
        lvl, gr = self.engine.level_db, self.engine.gr_db
        self._bar(self.level_cv, (lvl - MIN_DB) / -MIN_DB, "#3498db",
                  marker=(self.s["threshold"] - MIN_DB) / -MIN_DB)
        self._bar(self.gr_cv, gr / max(self.s["range"], 1), "#e67e22")
        self.level_lbl.config(text=f"{lvl:.0f} dB" if lvl > MIN_DB else "-∞ dB")
        self.gr_lbl.config(text=f"-{gr:.1f} dB")
        self.status.config(text=self.engine.error)
        self.root.after(33, self._tick)

    @staticmethod
    def _bar(cv, frac, color, marker=None):
        w, h = cv.winfo_width(), cv.winfo_height()
        cv.delete("all")
        cv.create_rectangle(0, 0, w * max(0.0, min(1.0, frac)), h, fill=color, width=0)
        if marker is not None:
            x = w * marker
            cv.create_line(x, 0, x, h, fill="white", width=2)

    # ---- tray ----
    def _setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Göster", lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem("Açık", lambda: self.root.after(0, self.toggle),
                             checked=lambda item: self.s["enabled"]),
            pystray.MenuItem("Çıkış", lambda: self.root.after(0, self.quit)),
        )
        self.tray = pystray.Icon(APP_NAME, make_icon(self.s["enabled"]), APP_NAME, menu)
        self.tray.run_detached()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()

    def quit(self):
        save_settings(self.s)
        self.engine.stop_evt.set()
        self.engine.join(timeout=2)  # restores target volume
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def already_running():
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    already_running.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "EzCompressor.SingleInstance")
    return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if already_running():
        # two instances would fight over the same volume
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "EzCompressor zaten çalışıyor (tepsi simgesine bakın).", APP_NAME, 0x40)
        sys.exit(0)
    App(start_hidden="--hidden" in sys.argv).run()
