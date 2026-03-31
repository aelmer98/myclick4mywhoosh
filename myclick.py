import asyncio
import threading
import json
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import keyboard
import pystray
from pystray import MenuItem as item
from PIL import Image, ImageDraw
import winreg
import webbrowser
from bleak import BleakClient, BleakScanner

config_dir = os.path.join(os.path.expanduser("~"), ".myclick")
os.makedirs(config_dir, exist_ok=True)
CONFIG_FILE = os.path.join(config_dir, ".myclick_settings.json")
APP_NAME = "MyClick"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_DIR = getattr(sys, "_MEIPASS", APP_DIR) if getattr(sys, "frozen", False) else APP_DIR
ICON_FILE = os.path.join(RESOURCE_DIR, "myclick.ico")
ICON_PNG = os.path.join(RESOURCE_DIR, "myclick.png")

NOTIFY_CHAR   = "00000002-19ca-4651-86e5-fa29dcdd09d1"
WRITE_CHAR    = "00000003-19ca-4651-86e5-fa29dcdd09d1"
INDICATE_CHAR = "00000004-19ca-4651-86e5-fa29dcdd09d1"
RIDE_ON       = b"RideOn"

# Colours
BG        = "#141414"
BG2       = "#1e1e1e"
BG3       = "#2a2a2a"
ACCENT    = "#00c853"
ACCENT2   = "#00897b"
TEXT      = "#f0f0f0"
TEXT_DIM  = "#888888"
DANGER    = "#ff5252"
WARNING   = "#ffaa00"

# MyWhoosh actions
MYWHOOSH_ACTIONS = [
    ("— None —",        None),
    ("Gear Up",         "k"),
    ("Gear Down",       "i"),
    ("Steer Left",      "j"),
    ("Steer Right",     "l"),
    ("Aero Tuck",       "t"),
    ("Hide/Show HUD",   "h"),
    ("Toggle Map",      "m"),
    ("Leave Ride",      "Escape"),
    ("Emote 1",         "1"),
    ("Emote 2",         "2"),
    ("Emote 3",         "3"),
    ("Emote 4",         "4"),
    ("Emote 5",         "5"),
    ("Emote 6",         "6"),
    ("Emote 7",         "7"),
    ("Emote 8",         "8"),
    ("Emote 9",         "9"),
    ("Rider List",      "Tab"),
]

ACTION_LABELS = [a[0] for a in MYWHOOSH_ACTIONS]
ACTION_MAP    = {a[0]: a[1] for a in MYWHOOSH_ACTIONS}
KEY_TO_LABEL  = {a[1]: a[0] for a in MYWHOOSH_ACTIONS if a[1]}

CLICK_BUTTON_NAMES = ["+", "A", "B", "Y", "Z", "–", "Left", "Right", "Up", "Down"]

# --- Config ---

def load_config():
    defaults = {
        "click_up": {"address": None, "name": None, "patterns": {}},
        "click_down": {"address": None, "name": None, "patterns": {}},
        "autostart": False,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            # Migrate old format
            for side in ["click_up", "click_down"]:
                if side in saved and "pattern" in saved[side]:
                    pat = saved[side].pop("pattern")
                    saved[side]["patterns"] = {"main": {"pattern": pat, "action": "Gear Up" if side == "click_up" else "Gear Down"}}
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# --- Autostart ---

def set_autostart(enabled):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            exe = sys.executable
            script = os.path.abspath(__file__)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" "{script}"')
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Autostart error: {e}")

# --- Tray icon ---

def make_icon_image(color="green"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c = (0, 200, 83) if color == "green" else (255, 82, 82)
    draw.ellipse([4, 4, 60, 60], fill=c)
    return img

# Ensure icons exist (prefer custom ICO, do not overwrite it with fallback by default)
if not os.path.exists(ICON_PNG):
    if os.path.exists(ICON_FILE):
        try:
            Image.open(ICON_FILE).save(ICON_PNG)
        except Exception:
            make_icon_image().save(ICON_PNG)
    else:
        make_icon_image().save(ICON_PNG)

# Do not auto-generate myclick.ico so your custom file is authoritative.
if not os.path.exists(ICON_FILE):
    pass  # Custom ICO should be provided

# --- BLE Shifter ---

class Shifter:
    def __init__(self, config, on_status):
        self.config = config
        self.on_status = on_status
        self.running = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        tasks = []
        for side in ["click_up", "click_down"]:
            cfg = self.config.get(side, {})
            if cfg.get("address") and cfg.get("patterns"):
                # Build pattern → key map
                pattern_map = {}
                for btn_data in cfg["patterns"].values():
                    pat = btn_data.get("pattern")
                    action = btn_data.get("action")
                    key = ACTION_MAP.get(action)
                    if pat and key:
                        pattern_map[bytes.fromhex(pat)] = (action, key)
                if pattern_map:
                    tasks.append(self._connect_with_retry(cfg["address"], side, pattern_map))
        if tasks:
            await asyncio.gather(*tasks)

    async def _connect_with_retry(self, address, label, pattern_map):
        while self.running:
            try:
                self.on_status(f"Connecting {label}...")
                async with BleakClient(address) as client:
                    await client.start_notify(INDICATE_CHAR, lambda s, d: None)
                    await client.start_notify(NOTIFY_CHAR, self._make_handler(label, pattern_map))
                    await client.write_gatt_char(WRITE_CHAR, RIDE_ON, response=False)
                    self.on_status(f"{label} connected ✓")
                    while self.running and client.is_connected:
                        await asyncio.sleep(1)
            except Exception as e:
                self.on_status(f"Reconnecting {label}...")
                await asyncio.sleep(3)

    def _make_handler(self, label, pattern_map):
        last = {"pattern": None}
        def handler(sender, data):
            if len(data) != 7 or data[0] != 0x23 or data[1] != 0x08:
                return
            p = bytes(data[2:6])
            if p == last["pattern"]:
                return
            last["pattern"] = p
            if p in pattern_map:
                action, key = pattern_map[p]
                keyboard.press_and_release(key)
                print(f"[{label}] {action} → '{key}'")
            else:
                last["pattern"] = None
        return handler

# --- UI Helpers ---

def styled_label(parent, text, size=10, color=TEXT, bold=False, dim=False):
    fg = TEXT_DIM if dim else color
    weight = "bold" if bold else "normal"
    return tk.Label(parent, text=text, bg=BG, fg=fg,
                    font=("Segoe UI", size, weight))

def styled_button(parent, text, command, accent=False, small=False):
    bg = ACCENT if accent else BG3
    fg = "#000000" if accent else TEXT
    size = 9 if small else 10
    btn = tk.Button(parent, text=text, command=command,
                    bg=bg, fg=fg, activebackground=ACCENT2,
                    activeforeground=TEXT, font=("Segoe UI", size),
                    relief="flat", bd=0, cursor="hand2",
                    padx=12, pady=6)
    return btn

def separator(parent):
    return tk.Frame(parent, bg=BG3, height=1)

# --- Click Config Panel ---

class ClickPanel(tk.Frame):
    def __init__(self, parent, title, side_key, config, **kwargs):
        super().__init__(parent, bg=BG2, **kwargs)
        self.side_key = side_key
        self.config = config
        self.button_rows = []
        self._build(title)

    def _build(self, title):
        # Header
        header = tk.Frame(self, bg=BG2)
        header.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(header, text=title, bg=BG2, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        self.device_label = tk.Label(header, text="No device selected",
                                     bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9))
        self.device_label.pack(side="right")

        # Device selector row
        sel_frame = tk.Frame(self, bg=BG2)
        sel_frame.pack(fill="x", padx=16, pady=4)

        self.device_var = tk.StringVar()
        self.device_cb = ttk.Combobox(sel_frame, textvariable=self.device_var,
                                      width=30, state="readonly",
                                      font=("Segoe UI", 9))
        self.device_cb.pack(side="left", padx=(0, 8))

        saved = self.config.get(self.side_key, {})
        if saved.get("address"):
            display = f"{saved['name']} ({saved['address']})"
            self.device_cb["values"] = [display]
            self.device_var.set(display)
            self.device_label.config(text=saved["address"][-8:])

        # Button mappings header
        sep = tk.Frame(self, bg=BG3, height=1)
        sep.pack(fill="x", padx=16, pady=(10, 6))

        tk.Label(self, text="BUTTON MAPPINGS", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=16)

        # Scrollable button list
        self.btn_frame = tk.Frame(self, bg=BG2)
        self.btn_frame.pack(fill="x", padx=16, pady=4)

        self._build_button_rows()

        # Add button
        add_btn = tk.Button(self, text="+ Detect new button", bg=BG2, fg=ACCENT,
                            font=("Segoe UI", 9), relief="flat", bd=0,
                            cursor="hand2", activebackground=BG2,
                            activeforeground=ACCENT2,
                            command=self._detect_new_button)
        add_btn.pack(anchor="w", padx=14, pady=(4, 12))

    def _build_button_rows(self):
        for w in self.btn_frame.winfo_children():
            w.destroy()
        self.button_rows = []

        saved = self.config.get(self.side_key, {})
        patterns = saved.get("patterns", {})

        for btn_id, btn_data in patterns.items():
            self._add_button_row(btn_id, btn_data)

        if not patterns:
            tk.Label(self.btn_frame, text="No buttons detected yet",
                     bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=4)

    def _add_button_row(self, btn_id, btn_data):
        row = tk.Frame(self.btn_frame, bg=BG3)
        row.pack(fill="x", pady=2)

        # Pattern label
        tk.Label(row, text=f"Button {btn_id[:6]}",
                 bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 8),
                 width=10).pack(side="left", padx=8, pady=6)

        # Action dropdown
        action_var = tk.StringVar(value=btn_data.get("action", "— None —"))
        cb = ttk.Combobox(row, textvariable=action_var, values=ACTION_LABELS,
                          state="readonly", width=18, font=("Segoe UI", 9))
        cb.pack(side="left", padx=4, pady=6)

        def on_action_change(var=action_var, bid=btn_id):
            self.config[self.side_key]["patterns"][bid]["action"] = var.get()

        action_var.trace_add("write", lambda *a, v=action_var, b=btn_id: on_action_change(v, b))

        # Delete button
        def delete(bid=btn_id):
            del self.config[self.side_key]["patterns"][bid]
            self._build_button_rows()

        tk.Button(row, text="✕", bg=BG3, fg=TEXT_DIM,
                  font=("Segoe UI", 9), relief="flat", bd=0,
                  cursor="hand2", command=delete,
                  activebackground=BG3, activeforeground=DANGER).pack(side="right", padx=8)

        self.button_rows.append((btn_id, action_var))

    def _detect_new_button(self):
        val = self.device_var.get()
        if not val:
            messagebox.showwarning("No device", "Please select a device first, then scan.")
            return
        address = val.split("(")[-1].strip(")")

        win = tk.Toplevel(self)
        win.title("Detect Button")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.geometry("300x160")

        tk.Label(win, text="Press a button on your Click...",
                 bg=BG, fg=TEXT, font=("Segoe UI", 11)).pack(pady=(24, 8))
        status = tk.Label(win, text="Waiting...", bg=BG, fg=WARNING,
                          font=("Segoe UI", 9))
        status.pack()
        tk.Button(win, text="Cancel", bg=BG3, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", bd=0,
                  command=win.destroy).pack(pady=16)

        detected = {"pattern": None}

        def run():
            async def _ble():
                async with BleakClient(address) as client:
                    await client.start_notify(INDICATE_CHAR, lambda s, d: None)
                    got_it = asyncio.Event()

                    def handler(sender, data):
                        if len(data) != 7 or data[0] != 0x23 or data[1] != 0x08:
                            return
                        p = bytes(data[2:6])
                        idle = bytes([0xff, 0xff, 0xff, 0xff])
                        if p != idle and not detected["pattern"]:
                            detected["pattern"] = p.hex()
                            asyncio.get_event_loop().call_soon_threadsafe(got_it.set)

                    await client.start_notify(NOTIFY_CHAR, handler)
                    await client.write_gatt_char(WRITE_CHAR, RIDE_ON, response=False)
                    try:
                        await asyncio.wait_for(got_it.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        pass

            try:
                asyncio.run(_ble())
            except Exception as e:
                self.after(0, lambda: status.config(text=f"Error: {e}", fg=DANGER))
                return

            if detected["pattern"]:
                pat = detected["pattern"]
                # Check if already mapped
                existing = self.config.get(self.side_key, {}).get("patterns", {})
                for bid, bd in existing.items():
                    if bd.get("pattern") == pat:
                        self.after(0, lambda: status.config(
                            text="Already mapped!", fg=WARNING))
                        return
                btn_id = pat[:8]
                if self.side_key not in self.config:
                    self.config[self.side_key] = {"address": None, "name": None, "patterns": {}}
                if "patterns" not in self.config[self.side_key]:
                    self.config[self.side_key]["patterns"] = {}
                self.config[self.side_key]["patterns"][btn_id] = {
                    "pattern": pat,
                    "action": "— None —"
                }
                self.after(0, lambda: [
                    status.config(text="✓ Button detected! Assign an action below.", fg=ACCENT),
                    self._build_button_rows(),
                    win.after(1500, win.destroy)
                ])
            else:
                self.after(0, lambda: status.config(text="Timed out — try again", fg=DANGER))

        threading.Thread(target=run, daemon=True).start()

    def update_devices(self, choices):
        self.device_cb["values"] = choices
        saved = self.config.get(self.side_key, {})
        if saved.get("address"):
            for c in choices:
                if saved["address"] in c:
                    self.device_var.set(c)
                    break

    def save_device(self):
        val = self.device_var.get()
        if val:
            address = val.split("(")[-1].strip(")")
            name = val.split("(")[0].strip()
            if self.side_key not in self.config:
                self.config[self.side_key] = {"patterns": {}}
            self.config[self.side_key]["address"] = address
            self.config[self.side_key]["name"] = name
            self.device_label.config(text=address[-8:])

# --- Settings Window ---

class SettingsWindow:
    def __init__(self, config, on_save):
        self.config = config
        self.on_save = on_save
        self.root = tk.Tk()
        self.root.title("MyClick")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # set Windows taskbar icon explicitly
        try:
            self.root.iconbitmap(ICON_FILE)
        except Exception:
            pass

        # set Tk icon for window and cross-platform
        try:
            self.root.iconphoto(False, tk.PhotoImage(file=ICON_PNG))
        except Exception:
            pass

        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)
        self._build()

    def _build(self):
        root = self.root

        # Configure ttk style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=BG3,
                        background=BG3,
                        foreground=TEXT,
                        selectbackground=ACCENT,
                        selectforeground="#000000",
                        borderwidth=0)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG3)],
                  foreground=[("readonly", TEXT)])

        # Title bar
        title_bar = tk.Frame(root, bg=BG, pady=0)
        title_bar.pack(fill="x")

        tk.Label(title_bar, text="  ●  MyClick",
                 bg=BG, fg=ACCENT, font=("Segoe UI", 13, "bold")).pack(side="left", padx=16, pady=16)
        tk.Label(title_bar, text="for MyWhoosh",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 10)).pack(side="left")

        # Scan row
        scan_frame = tk.Frame(root, bg=BG)
        scan_frame.pack(fill="x", padx=16, pady=(0, 8))

        styled_button(scan_frame, "Scan for Clicks", self._scan, accent=True).pack(side="left")
        self.scan_status = tk.Label(scan_frame, text="",
                                    bg=BG, fg=TEXT_DIM, font=("Segoe UI", 9))
        self.scan_status.pack(side="left", padx=12)

        # Two-column click panels
        panels = tk.Frame(root, bg=BG)
        panels.pack(fill="x", padx=16, pady=4)

        self.panel_up = ClickPanel(panels, "⬆  Gear UP Click", "click_up",
                                   self.config, width=280)
        self.panel_up.pack(side="left", fill="both", expand=True, padx=(0, 6))

        self.panel_down = ClickPanel(panels, "⬇  Gear DOWN Click", "click_down",
                                     self.config, width=280)
        self.panel_down.pack(side="left", fill="both", expand=True, padx=(6, 0))

        # Bottom bar
        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", padx=16, pady=(8, 16))

        self.autostart_var = tk.BooleanVar(value=self.config.get("autostart", False))
        cb = tk.Checkbutton(bottom, text="Start with Windows",
                            variable=self.autostart_var,
                            bg=BG, fg=TEXT_DIM, selectcolor=BG3,
                            activebackground=BG, activeforeground=TEXT,
                            font=("Segoe UI", 9))
        cb.pack(side="left")

        self.status_label = tk.Label(bottom, text="",
                                     bg=BG, fg=ACCENT, font=("Segoe UI", 9))
        self.status_label.pack(side="left", padx=16)

        styled_button(bottom, "Donate", self._donate).pack(side="right", padx=(0, 8))
        styled_button(bottom, "Save & Start", self._save, accent=True).pack(side="right")

    def _scan(self):
        self.scan_status.config(text="Scanning... (5s)", fg=WARNING)
        self.root.update()

        def run():
            async def _ble():
                results = await BleakScanner.discover(timeout=5.0, return_adv=True)
                return [(d.name or "Unknown", d.address)
                        for d, adv in results.values()
                        if d.name and "zwift" in d.name.lower()]
            try:
                devices = asyncio.run(_ble())
                choices = [f"{n} ({a})" for n, a in devices]
                self.root.after(0, lambda: self._on_scan_done(choices))
            except Exception as e:
                self.root.after(0, lambda: self.scan_status.config(
                    text=f"Scan error: {e}", fg=DANGER))

        threading.Thread(target=run, daemon=True).start()

    def _on_scan_done(self, choices):
        self.panel_up.update_devices(choices)
        self.panel_down.update_devices(choices)
        self.scan_status.config(
            text=f"Found {len(choices)} Zwift device(s)", fg=ACCENT)

    def _donate(self):
        webbrowser.open("paypal.me/aelmer1")  # Replace with your link

    def _save(self):
        self.panel_up.save_device()
        self.panel_down.save_device()
        self.config["autostart"] = self.autostart_var.get()
        save_config(self.config)
        set_autostart(self.config["autostart"])
        self.status_label.config(text="Saved!")
        self.root.update()
        self.on_save(self.config)
        self.root.after(1500, self.root.withdraw)

    def show(self):
        self.root.deiconify()
        self.root.mainloop()

# --- Main ---

def main():
    config = load_config()
    shifter = {"instance": None}
    tray = {"instance": None}
    settings_win = {"instance": None}

    def update_status(msg):
        print(msg)

    def start_shifter(cfg):
        if shifter["instance"]:
            shifter["instance"].stop()
        s = Shifter(cfg, update_status)
        s.start()
        shifter["instance"] = s

    def open_settings():
        if settings_win["instance"] is None:
            win = SettingsWindow(config, start_shifter)
            settings_win["instance"] = win
            win.show()
        else:
            settings_win["instance"].root.after(
                0, settings_win["instance"].root.deiconify)

    def quit_app(icon, itm):
        if shifter["instance"]:
            shifter["instance"].stop()
        icon.stop()
        os._exit(0)

    def donate():
        webbrowser.open("paypal.me/aelmer1")  # Replace with your PayPal link

    if config["click_up"].get("address") or config["click_down"].get("address"):
        start_shifter(config)

    if os.path.exists(ICON_FILE):
        icon_img = Image.open(ICON_FILE)
    else:
        icon_img = make_icon_image("green")

    menu = pystray.Menu(
        item("Settings", lambda i, m: threading.Thread(
            target=open_settings, daemon=True).start()),
        item("Donate", lambda i, m: donate()),
        item("Quit", quit_app)
    )
    tray_icon = pystray.Icon(APP_NAME, icon_img, "MyClick", menu)
    tray["instance"] = tray_icon

    if not config["click_up"].get("address") and not config["click_down"].get("address"):
        threading.Thread(target=open_settings, daemon=True).start()

    tray_icon.run()

if __name__ == "__main__":
    main()