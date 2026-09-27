#!/usr/bin/env python3
"""Klik-snapshot: maakt bij elke muisklik een PNG-screenshot.

Modi: actieve applicatie (venster waarop geklikt wordt), scherm waarop
geklikt wordt, alle schermen, of een vooraf geselecteerd gebied.
Werkt volledig offline op X11 (Tk + Pillow + python-xlib).
"""

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageGrab
from Xlib import X, display
from Xlib.ext import randr, record
from Xlib.protocol import rq

APP_NAME = "klik-snapshot"
CONFIG_PATH = os.path.expanduser(f"~/.config/{APP_NAME}/config.json")

MODES = [
    ("window", "Actieve applicatie (venster waarop geklikt wordt)"),
    ("monitor", "Scherm waarop geklikt wordt"),
    ("all", "Alle schermen"),
    ("region", "Geselecteerd gebied"),
]

DEFAULTS = {
    "mode": "window",
    "folder": os.path.expanduser("~/Afbeeldingen/klik-snapshots"),
    "region": None,  # [x, y, w, h]
    "buttons": {"1": True, "2": False, "3": False},
    "delay_ms": 0,
    "mark_click": True,
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------
# X11 helpers
# --------------------------------------------------------------------------

class XGeometry:
    """Geometrie-vragen aan de X-server (eigen verbinding per thread)."""

    def __init__(self):
        self.d = display.Display()
        self.root = self.d.screen().root
        self.WM_STATE = self.d.intern_atom("WM_STATE")
        self.NET_FRAME = self.d.intern_atom("_NET_FRAME_EXTENTS")
        self.GTK_FRAME = self.d.intern_atom("_GTK_FRAME_EXTENTS")

    def root_size(self):
        g = self.root.get_geometry()
        return g.width, g.height

    def monitors(self):
        try:
            mons = randr.get_monitors(self.root, True).monitors
            return [(m.x, m.y, m.width_in_pixels, m.height_in_pixels) for m in mons]
        except Exception:
            w, h = self.root_size()
            return [(0, 0, w, h)]

    def monitor_at(self, x, y):
        mons = self.monitors()
        for m in mons:
            if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]:
                return m
        return mons[0]

    def toplevel_at(self, x, y):
        """Top-level (frame)venster van de window manager op positie x, y."""
        child = self.root.translate_coords(self.root, x, y).child
        return child if child else None

    def toplevel_of(self, win_id):
        """Loop de ouderketen af tot het venster direct onder root."""
        win = self.d.create_resource_object("window", win_id)
        while True:
            parent = win.query_tree().parent
            if not parent or parent.id == self.root.id:
                return win.id
            win = parent

    def _find_client(self, win, depth=0):
        if win.get_full_property(self.WM_STATE, X.AnyPropertyType):
            return win
        if depth > 4:
            return None
        for c in win.query_tree().children:
            found = self._find_client(c, depth + 1)
            if found:
                return found
        return None

    def _extents(self, win, atom):
        p = win.get_full_property(atom, X.AnyPropertyType)
        if p and len(p.value) == 4:
            return list(p.value)  # left, right, top, bottom
        return None

    def window_bbox(self, frame):
        """Zichtbare rechthoek van het venster, incl. titelbalk, zonder schaduw."""
        client = self._find_client(frame)
        if client is None:
            g = frame.get_geometry()
            return g.x, g.y, g.width, g.height
        g = client.get_geometry()
        pos = client.translate_coords(self.root, 0, 0)
        x, y, w, h = -pos.x, -pos.y, g.width, g.height
        net = self._extents(client, self.NET_FRAME)
        if net:  # decoraties van de window manager erbij
            l, r, t, b = net
            x, y, w, h = x - l, y - t, w + l + r, h + t + b
        gtk = self._extents(client, self.GTK_FRAME)
        if gtk:  # onzichtbare schaduwrand van client-side decoraties eraf
            l, r, t, b = gtk
            x, y, w, h = x + l, y + t, w - l - r, h - t - b
        return x, y, w, h

    def clip(self, bbox):
        rw, rh = self.root_size()
        x, y, w, h = bbox
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(rw, x + w), min(rh, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1 - x0, y1 - y0


class ClickListener(threading.Thread):
    """Luistert globaal naar muisklikken via de X RECORD-extensie."""

    def __init__(self, on_click):
        super().__init__(daemon=True)
        self.on_click = on_click
        self.ctrl = display.Display()
        self.rec = display.Display()
        self.ctx = self.ctrl.record_create_context(
            0,
            [record.AllClients],
            [{
                "core_requests": (0, 0),
                "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0),
                "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.ButtonPress, X.ButtonPress),
                "errors": (0, 0),
                "client_started": False,
                "client_died": False,
            }],
        )
        self.ctrl.sync()  # context moet bestaan voordat rec hem gebruikt

    def run(self):
        self.rec.record_enable_context(self.ctx, self._handle)
        self.rec.record_free_context(self.ctx)
        self.rec.close()

    def _handle(self, reply):
        if reply.category != record.FromServer or reply.client_swapped:
            return
        data = reply.data
        while data:
            event, data = rq.EventField(None).parse_binary_value(
                data, self.rec.display, None, None)
            if event.type == X.ButtonPress:
                self.on_click(event.detail, event.root_x, event.root_y)

    def stop(self):
        self.ctrl.record_disable_context(self.ctx)
        self.ctrl.flush()
        self.ctrl.close()


# --------------------------------------------------------------------------
# Opnemen
# --------------------------------------------------------------------------

class Capturer(threading.Thread):
    """Verwerkt klikken uit een wachtrij en slaat screenshots op."""

    def __init__(self, settings, ignore_toplevel, on_saved, on_error):
        super().__init__(daemon=True)
        self.s = settings
        self.ignore_toplevel = ignore_toplevel
        self.on_saved = on_saved
        self.on_error = on_error
        self.q = queue.Queue()

    def click(self, button, x, y):
        if self.s["buttons"].get(str(button)):
            self.q.put((button, x, y, time.time()))

    def stop(self):
        self.q.put(None)

    def run(self):
        xg = XGeometry()
        while True:
            item = self.q.get()
            if item is None:
                break
            _, x, y, t = item
            try:
                self._capture(xg, x, y, t)
            except Exception as e:  # blijf draaien bij een mislukte snap
                self.on_error(str(e))
        xg.d.close()

    def _capture(self, xg, x, y, t):
        frame = xg.toplevel_at(x, y)
        if frame is not None and frame.id == self.ignore_toplevel:
            return  # klik in de eigen GUI
        mode = self.s["mode"]
        if mode == "window":
            bbox = xg.window_bbox(frame) if frame is not None else xg.monitor_at(x, y)
        elif mode == "monitor":
            bbox = xg.monitor_at(x, y)
        elif mode == "region":
            bbox = tuple(self.s["region"])
        else:
            bbox = (0, 0) + xg.root_size()
        bbox = xg.clip(bbox)
        if bbox is None:
            return

        delay = self.s["delay_ms"] / 1000
        if delay:
            time.sleep(delay)

        bx, by, bw, bh = bbox
        img = ImageGrab.grab(bbox=(bx, by, bx + bw, by + bh))
        if self.s["mark_click"]:
            self._mark(img, x - bx, y - by)

        folder = self.s["folder"]
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = os.path.join(folder, f"snap_{stamp}.png")
        img.save(path, "PNG")
        self.on_saved(path)

    @staticmethod
    def _mark(img, cx, cy):
        if not (0 <= cx < img.width and 0 <= cy < img.height):
            return
        d = ImageDraw.Draw(img)
        for r, col, wdt in ((16, "white", 6), (16, "red", 3)):
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=wdt)
        d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill="red")


# --------------------------------------------------------------------------
# Gebied selecteren
# --------------------------------------------------------------------------

class RegionSelector:
    """Schermvullende bevroren screenshot waarop je een rechthoek sleept."""

    def __init__(self, master, on_done):
        self.master = master
        self.on_done = on_done
        shot = ImageGrab.grab()
        dimmed = Image.blend(shot.convert("RGB"), Image.new("RGB", shot.size, "black"), 0.35)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".ppm", delete=False)
        dimmed.save(self.tmp.name, "PPM")
        self.tmp.close()

        self.top = tk.Toplevel(master)
        self.top.overrideredirect(True)
        self.top.geometry(f"{shot.width}x{shot.height}+0+0")
        self.top.attributes("-topmost", True)
        self.photo = tk.PhotoImage(file=self.tmp.name)
        c = self.canvas = tk.Canvas(self.top, width=shot.width, height=shot.height,
                                    highlightthickness=0, cursor="crosshair")
        c.pack()
        c.create_image(0, 0, image=self.photo, anchor="nw")
        xg = XGeometry()
        for mx, my, mw, mh in xg.monitors():
            c.create_text(mx + mw // 2, my + 40, fill="white",
                          font=("Sans", 16, "bold"),
                          text="Sleep een gebied  •  Esc = annuleren")
        xg.d.close()
        self.rect = None
        self.start = None
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        self.top.bind("<Escape>", lambda e: self._finish(None))
        self.top.after(50, self._grab)

    def _grab(self):
        self.top.focus_force()
        try:
            self.top.grab_set_global()
        except tk.TclError:
            self.top.after(50, self._grab)

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline="red", width=2, dash=(6, 3))

    def _drag(self, e):
        if self.start:
            self.canvas.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x, y = min(x0, e.x), min(y0, e.y)
        w, h = abs(e.x - x0), abs(e.y - y0)
        self._finish([x, y, w, h] if w > 4 and h > 4 else None)

    def _finish(self, region):
        self.top.grab_release()
        self.top.destroy()
        os.unlink(self.tmp.name)
        self.on_done(region)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App:
    def __init__(self):
        self.cfg = load_config()
        self.root = tk.Tk(className=APP_NAME)
        self.root.title("Klik-snapshot")
        self.root.resizable(False, False)
        self.listener = None
        self.capturer = None
        self.count = 0

        pad = {"padx": 10, "pady": 4}
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both", expand=True)

        # Modus
        lf = ttk.LabelFrame(f, text="Wat vastleggen bij elke klik?", padding=8)
        lf.pack(fill="x", **pad)
        self.mode = tk.StringVar(value=self.cfg["mode"])
        self.inputs = []
        for key, label in MODES:
            rb = ttk.Radiobutton(lf, text=label, value=key, variable=self.mode,
                                 command=self._changed)
            rb.pack(anchor="w")
            self.inputs.append(rb)
        rrow = ttk.Frame(lf)
        rrow.pack(fill="x", padx=(22, 0), pady=(2, 0))
        b = ttk.Button(rrow, text="Selecteer gebied…", command=self.select_region)
        b.pack(side="left")
        self.inputs.append(b)
        self.region_lbl = ttk.Label(rrow, foreground="gray")
        self.region_lbl.pack(side="left", padx=8)

        # Map
        lf = ttk.LabelFrame(f, text="Opslaglocatie (PNG)", padding=8)
        lf.pack(fill="x", **pad)
        self.folder = tk.StringVar(value=self.cfg["folder"])
        e = ttk.Entry(lf, textvariable=self.folder, width=45)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<FocusOut>", lambda _e: self._changed())
        self.inputs.append(e)
        b = ttk.Button(lf, text="Bladeren…", command=self.browse)
        b.pack(side="left", padx=(6, 0))
        self.inputs.append(b)
        ttk.Button(lf, text="Open map", command=self.open_folder).pack(side="left", padx=(6, 0))

        # Opties
        lf = ttk.LabelFrame(f, text="Opties", padding=8)
        lf.pack(fill="x", **pad)
        row = ttk.Frame(lf)
        row.pack(fill="x")
        ttk.Label(row, text="Muisknoppen:").pack(side="left")
        self.btn_vars = {}
        for num, name in (("1", "Links"), ("2", "Midden"), ("3", "Rechts")):
            v = tk.BooleanVar(value=self.cfg["buttons"].get(num, False))
            cb = ttk.Checkbutton(row, text=name, variable=v, command=self._changed)
            cb.pack(side="left", padx=4)
            self.btn_vars[num] = v
            self.inputs.append(cb)
        row = ttk.Frame(lf)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Vertraging na klik (ms):").pack(side="left")
        self.delay = tk.IntVar(value=self.cfg["delay_ms"])
        sb = ttk.Spinbox(row, from_=0, to=5000, increment=50, width=6,
                         textvariable=self.delay, command=self._changed)
        sb.pack(side="left", padx=4)
        sb.bind("<FocusOut>", lambda _e: self._changed())
        self.inputs.append(sb)
        self.mark = tk.BooleanVar(value=self.cfg["mark_click"])
        cb = ttk.Checkbutton(lf, text="Klikpositie markeren met rode cirkel",
                             variable=self.mark, command=self._changed)
        cb.pack(anchor="w", pady=(6, 0))
        self.inputs.append(cb)

        # Start/stop
        row = ttk.Frame(f)
        row.pack(fill="x", **pad)
        self.start_btn = ttk.Button(row, text="▶ Start", command=self.toggle)
        self.start_btn.pack(side="left")
        self.status = ttk.Label(row, text="Gestopt")
        self.status.pack(side="left", padx=10)
        self.last = ttk.Label(f, text="", foreground="gray")
        self.last.pack(fill="x", padx=10)
        ttk.Label(f, text="Klikken in dit venster worden genegeerd.",
                  foreground="gray").pack(fill="x", padx=10, pady=(4, 0))

        self._update_region_label()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # -- instellingen ------------------------------------------------------
    def _changed(self):
        self.cfg["mode"] = self.mode.get()
        self.cfg["folder"] = os.path.expanduser(self.folder.get().strip())
        self.cfg["buttons"] = {k: v.get() for k, v in self.btn_vars.items()}
        try:
            self.cfg["delay_ms"] = max(0, int(self.delay.get()))
        except (tk.TclError, ValueError):
            self.cfg["delay_ms"] = 0
        self.cfg["mark_click"] = self.mark.get()
        save_config(self.cfg)

    def _update_region_label(self):
        r = self.cfg.get("region")
        self.region_lbl.config(
            text=f"{r[2]}×{r[3]} op ({r[0]}, {r[1]})" if r else "nog geen gebied gekozen")

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or os.path.expanduser("~"),
                                    title="Kies map voor snapshots")
        if d:
            self.folder.set(d)
            self._changed()

    def open_folder(self):
        folder = os.path.expanduser(self.folder.get())
        os.makedirs(folder, exist_ok=True)
        subprocess.Popen(["xdg-open", folder])

    def select_region(self):
        self.root.withdraw()
        self.root.after(300, lambda: RegionSelector(self.root, self._region_done))

    def _region_done(self, region):
        self.root.deiconify()
        if region:
            self.cfg["region"] = region
            self.mode.set("region")
            self._changed()
            self._update_region_label()

    # -- starten/stoppen ---------------------------------------------------
    def toggle(self):
        if self.listener:
            self.stop()
        else:
            self.start()

    def start(self):
        self._changed()
        if not any(self.cfg["buttons"].values()):
            messagebox.showwarning("Klik-snapshot", "Kies minstens één muisknop.")
            return
        if self.cfg["mode"] == "region" and not self.cfg.get("region"):
            messagebox.showwarning("Klik-snapshot", "Selecteer eerst een gebied.")
            return
        try:
            os.makedirs(self.cfg["folder"], exist_ok=True)
        except OSError as e:
            messagebox.showerror("Klik-snapshot", f"Kan map niet aanmaken:\n{e}")
            return

        self.root.update_idletasks()
        xg = XGeometry()
        own = xg.toplevel_of(int(self.root.wm_frame(), 16))
        xg.d.close()

        self.capturer = Capturer(dict(self.cfg), own,
                                 on_saved=lambda p: self.root.after(0, self._saved, p),
                                 on_error=lambda m: self.root.after(0, self._error, m))
        self.capturer.start()
        try:
            self.listener = ClickListener(self.capturer.click)
        except Exception as e:
            self.capturer.stop()
            self.capturer = None
            messagebox.showerror("Klik-snapshot", f"Kan muisklikken niet volgen:\n{e}")
            return
        self.listener.start()
        self.count = 0
        for w in self.inputs:
            w.state(["disabled"])
        self.start_btn.config(text="■ Stop")
        self.status.config(text="Actief – 0 snaps", foreground="green")

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        if self.capturer:
            self.capturer.stop()
            self.capturer = None
        for w in self.inputs:
            w.state(["!disabled"])
        self.start_btn.config(text="▶ Start")
        self.status.config(text=f"Gestopt – {self.count} snaps gemaakt", foreground="")

    def _saved(self, path):
        self.count += 1
        if self.listener:
            self.status.config(text=f"Actief – {self.count} snaps")
        self.last.config(text=f"Laatste: {os.path.basename(path)}")

    def _error(self, msg):
        self.last.config(text=f"Fout: {msg}")

    def quit(self):
        self.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
