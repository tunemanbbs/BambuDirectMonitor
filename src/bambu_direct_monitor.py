import json
import queue
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import paho.mqtt.client as mqtt

import bambu_core


PORT = 8883
USERNAME = "bblp"
PUSHALL_SECONDS = 30
CLOUD_PUSHALL_SECONDS = 300

DEFAULT_CONFIG = {
    "mode": "cloud",
    "region": "us",
    "printer_name": "Bambu Printer",
    "printer_ip": "",
    "serial": "",
    "access_code": "",
    "cloud_user_id": "",
    "cloud_token": "",
    "always_on_top": True,
    "frameless": False,
    "window_width": 260,
    "window_height": 260,
}


STATUS_LABELS = {
    "RUNNING": "Printing",
    "PAUSE": "Paused",
    "PAUSED": "Paused",
    "FINISH": "Finished",
    "FAILED": "Failed",
    "IDLE": "Idle",
    "PREPARE": "Preparing",
    "SLICING": "Slicing",
}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path():
    return app_dir() / "BambuDirectMonitor-config.json"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    if cfg.get("window_width", 260) >= 330 and cfg.get("window_height", 260) >= 330:
        cfg["window_width"] = 260
        cfg["window_height"] = 260
    cfg["frameless"] = bool(cfg.get("frameless", False))
    return cfg


def save_config(cfg):
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def as_text(value, default="--"):
    if value is None or value == "":
        return default
    return str(value)


def as_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def as_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def format_minutes(minutes):
    mins = as_int(minutes, 0)
    if mins <= 0:
        return "--"
    hours, rem = divmod(mins, 60)
    if hours:
        return f"{hours}h {rem}m"
    return f"{rem}m"


def summarize_ams_humidity(print_status):
    ams_obj = print_status.get("ams")
    if not isinstance(ams_obj, dict):
        return "--"
    units = ams_obj.get("ams")
    if not isinstance(units, list) or not units:
        return "--"

    readings = []
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        unit_id = as_text(unit.get("id"), str(index))
        raw = unit.get("humidity_raw")
        level = unit.get("humidity")
        label = f"A{unit_id}"
        if raw not in (None, "", "0", 0):
            readings.append(f"{label} {as_int(raw)}%")
        elif level not in (None, ""):
            readings.append(f"{label} L{as_int(level)}")
    if not readings:
        return "--"
    if len(readings) == 1:
        value = readings[0]
        if value.startswith("A0 "):
            return "AMS " + value[3:]
        return "AMS " + value
    return " ".join(readings[:2])


def make_client(client_id):
    if hasattr(mqtt, "CallbackAPIVersion"):
        try:
            return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        except Exception:
            pass
    return mqtt.Client(client_id=client_id)


class BambuConnection(threading.Thread):
    def __init__(self, cfg, events, stop_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.events = events
        self.stop_event = stop_event
        self.client = None
        self.connected = False
        self.last_pushall = 0
        self.sequence_id = 1

    def emit(self, kind, payload):
        self.events.put((kind, payload))

    def request_topic(self):
        return f"device/{self.cfg['serial']}/request"

    def report_topic(self):
        return f"device/{self.cfg['serial']}/report"

    def publish_json(self, payload):
        if not self.client or not self.connected:
            return
        self.client.publish(self.request_topic(), json.dumps(payload), qos=0)

    def request_pushall(self):
        payload = {
            "pushing": {
                "sequence_id": str(self.sequence_id),
                "command": "pushall",
                "version": 1,
                "push_target": 1,
            }
        }
        self.sequence_id += 1
        self.publish_json(payload)
        self.last_pushall = time.time()
        self.emit("log", "Requested full printer status")

    def set_chamber_light(self, on):
        for node in ("chamber_light", "chamber_light2"):
            payload = {
                "system": {
                    "sequence_id": str(self.sequence_id),
                    "command": "ledctrl",
                    "led_node": node,
                    "led_mode": "on" if on else "off",
                    "led_on_time": 500,
                    "led_off_time": 500,
                    "loop_times": 0,
                    "interval_time": 0,
                }
            }
            self.sequence_id += 1
            self.publish_json(payload)

    def on_connect(self, client, userdata, flags, rc, *extra):
        self.connected = rc == 0
        if rc == 0:
            self.emit("connection", "Connected")
            client.subscribe(self.report_topic(), qos=0)
            self.emit("log", f"Subscribed to {self.report_topic()}")
            self.request_pushall()
        else:
            self.emit("connection", f"MQTT auth failed: rc={rc}")

    def on_disconnect(self, client, userdata, rc, *extra):
        self.connected = False
        if not self.stop_event.is_set():
            self.emit("connection", f"Disconnected: rc={rc}")

    def on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except Exception:
            return
        print_obj = data.get("print")
        if isinstance(print_obj, dict):
            self.emit("status", print_obj)

    def run(self):
        mode = self.cfg.get("mode", "cloud")
        broker = self.cfg["printer_ip"].strip() if mode == "lan" else bambu_core.broker_for_region(self.cfg.get("region", "us"))
        serial = self.cfg["serial"].strip().upper()
        username = USERNAME if mode == "lan" else self.cfg.get("cloud_user_id", "").strip()
        password = self.cfg["access_code"].strip() if mode == "lan" else self.cfg.get("cloud_token", "").strip()
        if not broker or not serial or not username or not password:
            self.emit("connection", "Missing printer settings")
            return

        while not self.stop_event.is_set():
            try:
                client_id = f"bambu_direct_{uuid.uuid4().hex[:10]}"
                self.client = make_client(client_id)
                self.client.username_pw_set(username, password)
                if mode == "lan":
                    self.client.tls_set(cert_reqs=ssl.CERT_NONE)
                    self.client.tls_insecure_set(True)
                else:
                    self.client.tls_set()
                self.client.on_connect = self.on_connect
                self.client.on_disconnect = self.on_disconnect
                self.client.on_message = self.on_message

                self.emit("connection", f"Connecting to {broker}:{PORT}")
                self.client.connect(broker, PORT, keepalive=30 if mode == "cloud" else 60)

                while not self.stop_event.is_set():
                    self.client.loop(timeout=1.0)
                    interval = CLOUD_PUSHALL_SECONDS if mode == "cloud" else PUSHALL_SECONDS
                    if self.connected and time.time() - self.last_pushall >= interval:
                        self.request_pushall()
            except Exception as exc:
                self.connected = False
                self.emit("connection", f"Connection error: {exc}")
                time.sleep(5)
            finally:
                try:
                    if self.client:
                        self.client.disconnect()
                except Exception:
                    pass


def choose_device(parent, devices):
    if len(devices) == 1:
        return devices[0]

    dialog = tk.Toplevel(parent)
    dialog.title("Pick Bambu Printer")
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.grab_set()
    result = {"device": None}

    body = ttk.Frame(dialog, padding=16)
    body.grid(row=0, column=0, sticky="nsew")
    ttk.Label(body, text="Select the printer to monitor:").grid(row=0, column=0, sticky="w")
    listbox = tk.Listbox(body, width=72, height=min(10, len(devices)), activestyle="dotbox")
    listbox.grid(row=1, column=0, sticky="ew", pady=(8, 10))

    for dev in devices:
        listbox.insert("end", bambu_core.device_label(dev))
    listbox.selection_set(0)

    buttons = ttk.Frame(body)
    buttons.grid(row=2, column=0, sticky="e")

    def pick():
        selection = listbox.curselection()
        if selection:
            result["device"] = devices[selection[0]]
            dialog.destroy()

    def cancel():
        dialog.destroy()

    ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(8, 0))
    ttk.Button(buttons, text="Use Selected", command=pick).pack(side="right")
    listbox.bind("<Double-Button-1>", lambda _event: pick())
    dialog.bind("<Return>", lambda _event: pick())
    dialog.bind("<Escape>", lambda _event: cancel())
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    parent.wait_window(dialog)
    return result["device"]


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, cfg):
        super().__init__(master)
        self.title("Bambu Printer Settings")
        self.resizable(False, False)
        self.result = None
        self.transient(master)
        self.grab_set()

        self.vars = {
            "mode": tk.StringVar(value=cfg.get("mode", "cloud")),
            "region": tk.StringVar(value=cfg.get("region", "us")),
            "printer_name": tk.StringVar(value=cfg.get("printer_name", "")),
            "printer_ip": tk.StringVar(value=cfg.get("printer_ip", "")),
            "serial": tk.StringVar(value=cfg.get("serial", "")),
            "access_code": tk.StringVar(value=cfg.get("access_code", "")),
            "cloud_user_id": tk.StringVar(value=cfg.get("cloud_user_id", "")),
            "cloud_token": tk.StringVar(value=cfg.get("cloud_token", "")),
            "always_on_top": tk.BooleanVar(value=bool(cfg.get("always_on_top", True))),
        }

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="Connection").grid(row=0, column=0, sticky="w", pady=5)
        mode = ttk.Combobox(body, textvariable=self.vars["mode"], values=("cloud", "lan"), width=33, state="readonly")
        mode.grid(row=0, column=1, sticky="ew", pady=5, padx=(12, 0))

        ttk.Label(body, text="Region").grid(row=1, column=0, sticky="w", pady=5)
        region = ttk.Combobox(body, textvariable=self.vars["region"], values=("us", "cn"), width=33, state="readonly")
        region.grid(row=1, column=1, sticky="ew", pady=5, padx=(12, 0))

        cloud_button = ttk.Button(body, text="Cloud sign in / pick printer", command=self.cloud_setup)
        cloud_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 10))

        fields = [
            ("Printer name", "printer_name", False),
            ("Printer IP (LAN only)", "printer_ip", False),
            ("Serial number", "serial", False),
            ("LAN access code", "access_code", True),
            ("Cloud user ID", "cloud_user_id", False),
            ("Cloud token", "cloud_token", True),
        ]
        for idx, (label, key, secret) in enumerate(fields):
            row = idx + 3
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(body, textvariable=self.vars[key], width=36, show="*" if secret else "")
            entry.grid(row=row, column=1, sticky="ew", pady=5, padx=(12, 0))

        ttk.Checkbutton(
            body,
            text="Keep monitor always on top",
            variable=self.vars["always_on_top"],
        ).grid(row=len(fields) + 3, column=0, columnspan=2, sticky="w", pady=(10, 4))

        hint = ttk.Label(
            body,
            text="Cloud mode signs in through Bambu's website APIs and stores an access token. LAN mode needs printer LAN mode enabled.",
            wraplength=420,
            foreground="#555555",
        )
        hint.grid(row=len(fields) + 4, column=0, columnspan=2, sticky="w", pady=(4, 10))

        buttons = ttk.Frame(body)
        buttons.grid(row=len(fields) + 5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")

        self.bind("<Return>", lambda _event: self.save())
        self.bind("<Escape>", lambda _event: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(50, self.focus_first)

    def cloud_setup(self):
        region = self.vars["region"].get() or "us"
        email = simpledialog.askstring("Bambu Cloud", "Bambu Lab email:", parent=self)
        if not email:
            return
        password = simpledialog.askstring("Bambu Cloud", "Bambu Lab password:", show="*", parent=self)
        if not password:
            return
        try:
            data = bambu_core.cloud_login(email, password, region)
            token = bambu_core.cloud_extract_token(data)
            if not token:
                login_type = data.get("loginType", "")
                tfa_key = data.get("tfaKey") or ""
                if login_type == "tfa" or (not login_type and tfa_key):
                    code = simpledialog.askstring("Bambu Cloud", "Authenticator code:", parent=self)
                    if not code:
                        return
                    token = bambu_core.cloud_verify_totp(code.strip(), tfa_key)
                elif login_type == "verifyCode":
                    code = simpledialog.askstring("Bambu Cloud", "Email verification code:", parent=self)
                    if not code:
                        return
                    data = bambu_core.cloud_verify_email(email, code.strip(), region)
                    token = bambu_core.cloud_extract_token(data)
                else:
                    raise RuntimeError(f"Unknown login response: {json.dumps(data)[:300]}")
            if not token:
                raise RuntimeError("No access token came back from Bambu.")

            user_id = bambu_core.resolve_user_id(token, region)
            devices = bambu_core.cloud_fetch_devices(token, region)
            if not devices:
                raise RuntimeError("No printers were found on this Bambu account.")
            dev = choose_device(self, devices)
            if not dev:
                return
            self.vars["mode"].set("cloud")
            self.vars["printer_name"].set(dev.get("name") or "Bambu Printer")
            self.vars["serial"].set((dev.get("dev_id") or "").upper())
            self.vars["cloud_user_id"].set(user_id)
            self.vars["cloud_token"].set(token)
            messagebox.showinfo("Bambu Cloud", "Cloud setup complete. Click Save to connect.", parent=self)
        except Exception as exc:
            messagebox.showerror("Bambu Cloud", str(exc), parent=self)

    def focus_first(self):
        for child in self.winfo_children():
            child.focus_set()
            break

    def save(self):
        mode = self.vars["mode"].get()
        cfg = {
            "mode": mode,
            "region": self.vars["region"].get() or "us",
            "printer_name": self.vars["printer_name"].get().strip() or "Bambu Printer",
            "printer_ip": self.vars["printer_ip"].get().strip(),
            "serial": self.vars["serial"].get().strip().upper(),
            "access_code": self.vars["access_code"].get().strip(),
            "cloud_user_id": self.vars["cloud_user_id"].get().strip(),
            "cloud_token": self.vars["cloud_token"].get().strip(),
            "always_on_top": bool(self.vars["always_on_top"].get()),
        }
        if mode == "lan" and (not cfg["printer_ip"] or not cfg["serial"] or not cfg["access_code"]):
            messagebox.showerror("Missing settings", "LAN mode requires printer IP, serial, and LAN access code.", parent=self)
            return
        if mode == "cloud" and (not cfg["serial"] or not cfg["cloud_user_id"] or not cfg["cloud_token"]):
            messagebox.showerror("Missing settings", "Cloud mode requires Cloud sign in / pick printer.", parent=self)
            return
        self.result = cfg
        self.destroy()

    def cancel(self):
        self.destroy()


class MonitorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Bambu Direct Monitor")
        self.cfg = load_config()
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.status = {}
        self.last_update = None
        self.connection_text = "Not connected"
        self.display = {
            "printer": self.cfg.get("printer_name", "Bambu Printer"),
            "state": "--",
            "job": "--",
            "progress": 0,
            "remaining": "--",
            "nozzle": "--",
            "bed": "--",
            "chamber": "--",
            "layer": "--",
            "wifi": "--",
            "ams": "--",
            "errors": "",
        }
        self.drag_origin = None

        self.build_ui()
        self.root.geometry(f"{self.cfg.get('window_width', 260)}x{self.cfg.get('window_height', 260)}")
        self.root.minsize(210, 210)
        self.root.resizable(True, True)
        if self.cfg.get("frameless", True):
            self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        if not self.config_ready():
            self.root.after(200, self.open_settings)
        else:
            self.start_connection()
        self.root.after(200, self.process_events)
        self.root.after(1000, self.tick)

    def build_ui(self):
        self.root.configure(bg="#05070a")
        self.canvas = tk.Canvas(self.root, bg="#05070a", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.draw_face())
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag_window)
        self.canvas.bind("<Double-Button-1>", lambda _event: self.open_settings())
        self.canvas.bind("<Button-3>", self.show_menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Settings", command=self.open_settings)
        self.menu.add_command(label="Reconnect", command=self.reconnect)
        self.menu.add_separator()
        self.menu.add_command(label="Light On", command=lambda: self.send_light(True))
        self.menu.add_command(label="Light Off", command=lambda: self.send_light(False))
        self.menu.add_separator()
        self.menu.add_command(label="Toggle Always On Top", command=self.toggle_topmost)
        self.menu.add_command(label="Exit", command=self.close)
        self.draw_face()

    def config_ready(self):
        mode = self.cfg.get("mode", "cloud")
        if mode == "lan":
            return bool(self.cfg.get("printer_ip") and self.cfg.get("serial") and self.cfg.get("access_code"))
        return bool(self.cfg.get("serial") and self.cfg.get("cloud_user_id") and self.cfg.get("cloud_token"))

    def start_drag(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag_window(self, event):
        if not self.drag_origin:
            return
        start_x, start_y, win_x, win_y = self.drag_origin
        self.root.geometry(f"+{win_x + event.x_root - start_x}+{win_y + event.y_root - start_y}")

    def show_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def toggle_topmost(self):
        value = not bool(self.cfg.get("always_on_top", True))
        self.cfg["always_on_top"] = value
        save_config(self.cfg)
        self.root.attributes("-topmost", value)

    def fit_text(self, text, max_chars):
        text = as_text(text)
        if len(text) <= max_chars:
            return text
        return text[:max(1, max_chars - 1)] + "..."

    def draw_face(self):
        c = self.canvas
        c.delete("all")
        w = max(210, c.winfo_width())
        h = max(210, c.winfo_height())
        size = min(w, h) - 14
        cx, cy = w / 2, h / 2
        r = size / 2
        bbox = (cx - r, cy - r, cx + r, cy + r)
        progress = max(0, min(100, int(self.display.get("progress", 0))))
        connected = self.connection_text.lower().startswith(("connected", "requested", "subscribed"))
        alert = bool(self.display.get("errors"))
        accent = "#ff4d4d" if alert else "#32e649"
        muted = "#8fa2b5"

        font_scale = size / 300.0
        arc_width = max(7, int(size * 0.04))
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#06080d", outline="#1a2432", width=max(3, int(size * 0.015)))
        c.create_oval(cx - r + 13, cy - r + 13, cx + r - 13, cy + r - 13, outline="#111a25", width=1)

        arc_pad = max(16, int(size * 0.06))
        arc_box = (bbox[0] + arc_pad, bbox[1] + arc_pad, bbox[2] - arc_pad, bbox[3] - arc_pad)
        c.create_arc(arc_box, start=112, extent=-300, style="arc", outline="#1f2937", width=arc_width)
        c.create_arc(arc_box, start=112, extent=-(300 * progress / 100), style="arc", outline=accent, width=arc_width)

        dot_color = "#32e649" if connected else "#64748b"
        dot_y = cy - r + size * 0.145
        c.create_oval(cx - 3, dot_y - 3, cx + 3, dot_y + 3, fill=dot_color, outline="")
        pct_font = max(12, int(22 * font_scale))
        state_font = max(9, int(13 * font_scale))
        c.create_text(cx, cy - r + size * 0.235, text=f"{progress}%", fill=accent, font=("Segoe UI", pct_font, "bold"))
        c.create_text(cx, cy - r + size * 0.315, text=self.fit_text(self.display.get("state"), 14).lower(),
                      fill="#66f59a", font=("Segoe UI", state_font, "italic"))

        printer_w = size * 0.12
        printer_h = size * 0.12
        px0 = cx - printer_w / 2
        py0 = cy - printer_h / 2 + size * 0.025
        c.create_rectangle(px0, py0, px0 + printer_w, py0 + printer_h, outline=accent, width=2)
        c.create_rectangle(px0 + printer_w * 0.22, py0 + printer_h * 0.18,
                           px0 + printer_w * 0.78, py0 + printer_h * 0.54, fill=accent, outline="")
        c.create_line(px0 + printer_w * 0.5, py0 + printer_h * 0.54,
                      px0 + printer_w * 0.5, py0 + printer_h * 0.86, fill=accent, width=2)

        metric_font = max(9, int(12 * font_scale))
        label_font = max(7, int(8 * font_scale))
        c.create_text(cx - r * 0.56, cy - r * 0.01, text=self.display.get("nozzle", "--"),
                      fill="#f8fafc", font=("Segoe UI", metric_font, "bold"), anchor="center")
        c.create_text(cx + r * 0.56, cy - r * 0.01, text=self.display.get("bed", "--"),
                      fill="#f8fafc", font=("Segoe UI", metric_font, "bold"), anchor="center")
        c.create_text(cx - r * 0.56, cy + r * 0.11, text="nozzle", fill=muted, font=("Segoe UI", label_font))
        c.create_text(cx + r * 0.56, cy + r * 0.11, text="bed", fill=muted, font=("Segoe UI", label_font))

        c.create_text(cx, cy + r * 0.24, text=self.display.get("ams", "--"),
                      fill="#d5f9ff", font=("Segoe UI", max(8, int(10 * font_scale)), "bold"))
        c.create_text(cx, cy + r * 0.39, text=f"Layer: {self.display.get('layer', '--')}",
                      fill="#f8fafc", font=("Segoe UI", max(9, int(11 * font_scale)), "bold"))
        c.create_text(cx, cy + r * 0.52, text=self.fit_text(self.display.get("job"), 24),
                      fill="#cbd5e1", font=("Segoe UI", max(7, int(8 * font_scale))))
        c.create_text(cx, cy + r * 0.70, text=f"ETA  {self.display.get('remaining', '--')}",
                      fill="#b9dcff", font=("Segoe UI", max(11, int(16 * font_scale)), "bold"))

        footer = self.fit_text(self.connection_text, 36)
        c.create_text(cx, cy + r - 13, text=footer, fill="#64748b", font=("Segoe UI", max(7, int(8 * font_scale))))
        c.create_text(cx + r - 26, cy - r + 27, text="...", fill="#64748b", font=("Segoe UI", max(10, int(15 * font_scale)), "bold"))

    def start_connection(self):
        self.stop_worker()
        self.stop_event = threading.Event()
        self.worker = BambuConnection(self.cfg, self.events, self.stop_event)
        self.worker.start()

    def stop_worker(self):
        if self.worker:
            self.stop_event.set()
            self.worker = None

    def reconnect(self):
        self.connection_text = "Reconnecting"
        self.draw_face()
        self.start_connection()

    def send_light(self, on):
        if self.worker and self.worker.connected:
            self.worker.set_chamber_light(on)
            self.connection_text = "Sent chamber light command"
            self.draw_face()
        else:
            messagebox.showwarning("Not connected", "The printer is not connected yet.", parent=self.root)

    def open_settings(self):
        dialog = SettingsDialog(self.root, self.cfg)
        self.root.wait_window(dialog)
        if dialog.result:
            self.cfg.update(dialog.result)
            save_config(self.cfg)
            self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
            self.display["printer"] = self.cfg.get("printer_name", "Bambu Printer")
            self.draw_face()
            self.start_connection()

    def process_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "connection":
                    self.connection_text = payload
                    self.draw_face()
                elif kind == "status":
                    self.apply_status(payload)
                elif kind == "log":
                    self.connection_text = payload
                    self.draw_face()
        except queue.Empty:
            pass
        self.root.after(200, self.process_events)

    def tick(self):
        self.draw_face()
        self.root.after(1000, self.tick)

    def apply_status(self, payload):
        self.status.update(payload)
        state_raw = as_text(self.status.get("gcode_state"), "UNKNOWN").upper()
        state = STATUS_LABELS.get(state_raw, state_raw.title())
        progress = max(0, min(100, as_int(self.status.get("mc_percent"), 0)))
        job = self.status.get("subtask_name") or self.status.get("gcode_file") or "--"

        nozzle = as_float(self.status.get("nozzle_temper"), 0)
        nozzle_target = as_float(self.status.get("nozzle_target_temper"), 0)
        bed = as_float(self.status.get("bed_temper"), 0)
        bed_target = as_float(self.status.get("bed_target_temper"), 0)
        chamber = self.status.get("chamber_temper")
        layer = as_int(self.status.get("layer_num"), 0)
        layers = as_int(self.status.get("total_layer_num"), 0)
        wifi = self.status.get("wifi_signal")

        self.display["state"] = state
        self.display["job"] = as_text(job)
        self.display["progress"] = progress
        self.display["remaining"] = format_minutes(self.status.get("mc_remaining_time"))
        self.display["nozzle"] = f"{nozzle:.0f}/{nozzle_target:.0f}C" if nozzle or nozzle_target else "--"
        self.display["bed"] = f"{bed:.0f}/{bed_target:.0f}C" if bed or bed_target else "--"
        self.display["chamber"] = f"{as_float(chamber):.0f}C" if chamber not in (None, "") else "--"
        self.display["layer"] = f"{layer} / {layers}" if layer or layers else "--"
        self.display["wifi"] = f"{wifi} dBm" if wifi not in (None, "") else "--"
        self.display["ams"] = summarize_ams_humidity(self.status)

        error_bits = []
        if self.status.get("print_error"):
            error_bits.append(f"Print error: {self.status.get('print_error')}")
        if self.status.get("hms"):
            try:
                error_bits.append(f"HMS: {len(self.status.get('hms'))} code(s)")
            except Exception:
                error_bits.append("HMS codes reported")
        self.display["errors"] = " | ".join(error_bits)

        self.last_update = time.strftime("%I:%M:%S %p").lstrip("0")
        self.draw_face()

    def close(self):
        try:
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            if width > 240 and height > 240:
                self.cfg["window_width"] = width
                self.cfg["window_height"] = height
                save_config(self.cfg)
        except Exception:
            pass
        self.stop_worker()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    MonitorApp().run()
