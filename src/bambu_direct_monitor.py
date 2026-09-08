import json
import queue
import ssl
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import paho.mqtt.client as mqtt

import bambu_core


PORT = 8883
USERNAME = "bblp"
PUSHALL_SECONDS = 30
CLOUD_PUSHALL_SECONDS = 300
COUNTED_PRINT_STATES = {"RUNNING"}
PRINT_TIMER_SAVE_SECONDS = 60

LOWER_FIELD_LAYOUT = {
    "eta": {"label": "ETA", "x": 50, "y": 43, "size": 19},
    "finish": {"label": "Finish", "x": 50, "y": 53, "size": 11},
    "layer": {"label": "Layer", "x": 50, "y": 62, "size": 12},
    "job": {"label": "Print Name", "x": 50, "y": 70, "size": 10},
    "ams": {"label": "AMS", "x": 50, "y": 82, "size": 10},
    "total": {"label": "Total Print Hours", "x": 50, "y": 90, "size": 8},
}
LOWER_FIELD_ORDER = ("eta", "finish", "layer", "job", "ams", "total")

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
    "total_print_seconds": 0,
    "print_timer_job_key": "",
    "print_timer_job_accounted_seconds": 0,
    "layout_fields": {},
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
    cfg["layout_fields"] = normalize_layout_fields(cfg.get("layout_fields"))
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


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def normalize_layout_fields(layout):
    incoming = layout if isinstance(layout, dict) else {}
    normalized = {}
    for key, defaults in LOWER_FIELD_LAYOUT.items():
        item = incoming.get(key) if isinstance(incoming.get(key), dict) else {}
        normalized[key] = {
            "x": clamp(as_float(item.get("x"), defaults["x"]), 5, 95),
            "y": clamp(as_float(item.get("y"), defaults["y"]), 35, 95),
            "size": clamp(as_int(item.get("size"), defaults["size"]), 6, 32),
        }
    return normalized


def format_minutes(minutes):
    mins = as_int(minutes, 0)
    if mins <= 0:
        return "--"
    hours, rem = divmod(mins, 60)
    if hours:
        return f"{hours}h {rem}m"
    return f"{rem}m"


def format_finish_time(minutes):
    mins = as_int(minutes, 0)
    if mins <= 0:
        return "--"
    finish = datetime.now() + timedelta(minutes=mins)
    return finish.strftime("%I:%M %p").lstrip("0")


def format_total_hours(seconds):
    total = max(0.0, as_float(seconds, 0.0))
    hours = total / 3600.0
    if hours >= 100:
        return f"{hours:.1f}h"
    return f"{hours:.2f}h"


def print_job_key(status):
    parts = []
    for key in ("project_id", "profile_id", "task_id", "subtask_id", "subtask_name", "gcode_file"):
        value = status.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return "|".join(parts)


def estimate_job_elapsed_seconds(status):
    progress = as_float(status.get("mc_percent"), 0.0)
    remaining_minutes = as_int(status.get("mc_remaining_time"), 0)
    if progress <= 0 or progress >= 100 or remaining_minutes <= 0:
        return None
    return (remaining_minutes * 60.0) * (progress / (100.0 - progress))


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


class LayoutDialog(tk.Toplevel):
    def __init__(self, master, cfg):
        super().__init__(master)
        self.title("Gauge Layout Editor")
        self.resizable(False, False)
        self.result = None
        self.transient(master)
        self.grab_set()

        self.vars = {}
        layout = normalize_layout_fields(cfg.get("layout_fields"))

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")

        ttk.Label(body, text="Field").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=(0, 6))
        ttk.Label(body, text="X %").grid(row=0, column=1, sticky="w", padx=4, pady=(0, 6))
        ttk.Label(body, text="Y %").grid(row=0, column=2, sticky="w", padx=4, pady=(0, 6))
        ttk.Label(body, text="Size").grid(row=0, column=3, sticky="w", padx=4, pady=(0, 6))

        for row, key in enumerate(LOWER_FIELD_ORDER, start=1):
            defaults = LOWER_FIELD_LAYOUT[key]
            values = layout[key]
            self.vars[key] = {
                "x": tk.DoubleVar(value=values["x"]),
                "y": tk.DoubleVar(value=values["y"]),
                "size": tk.IntVar(value=values["size"]),
            }
            ttk.Label(body, text=defaults["label"]).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            ttk.Spinbox(body, from_=5, to=95, increment=1, width=6, textvariable=self.vars[key]["x"]).grid(
                row=row, column=1, sticky="w", padx=4, pady=4
            )
            ttk.Spinbox(body, from_=35, to=95, increment=1, width=6, textvariable=self.vars[key]["y"]).grid(
                row=row, column=2, sticky="w", padx=4, pady=4
            )
            ttk.Spinbox(body, from_=6, to=32, increment=1, width=6, textvariable=self.vars[key]["size"]).grid(
                row=row, column=3, sticky="w", padx=4, pady=4
            )

        hint = ttk.Label(
            body,
            text="X and Y are percentages of the monitor window. Use Y values from about 35 to 95 for the lower display area.",
            wraplength=420,
            foreground="#555555",
        )
        hint.grid(row=len(LOWER_FIELD_ORDER) + 1, column=0, columnspan=4, sticky="w", pady=(10, 8))

        buttons = ttk.Frame(body)
        buttons.grid(row=len(LOWER_FIELD_ORDER) + 2, column=0, columnspan=4, sticky="e")
        ttk.Button(buttons, text="Reset Defaults", command=self.reset_defaults).pack(side="left", padx=(0, 12))
        ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right")

        self.bind("<Return>", lambda _event: self.save())
        self.bind("<Escape>", lambda _event: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def reset_defaults(self):
        for key, defaults in LOWER_FIELD_LAYOUT.items():
            self.vars[key]["x"].set(defaults["x"])
            self.vars[key]["y"].set(defaults["y"])
            self.vars[key]["size"].set(defaults["size"])

    def save(self):
        layout = {}
        for key in LOWER_FIELD_ORDER:
            layout[key] = {
                "x": self.vars[key]["x"].get(),
                "y": self.vars[key]["y"].get(),
                "size": self.vars[key]["size"].get(),
            }
        self.result = normalize_layout_fields(layout)
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
            "finish": "--",
            "nozzle": "--",
            "bed": "--",
            "chamber": "--",
            "layer": "--",
            "wifi": "--",
            "ams": "--",
            "total_hours": format_total_hours(self.cfg.get("total_print_seconds", 0)),
            "errors": "",
        }
        self.drag_origin = None
        self.print_timer_active = False
        self.print_timer_last_ts = None
        self.print_timer_last_save = time.time()

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
        self.menu.add_command(label="Layout Editor", command=self.open_layout_editor)
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

    def draw_edge_progress(self, x0, y0, x1, y1, progress, accent):
        c = self.canvas
        width = max(5, int(min(x1 - x0, y1 - y0) * 0.035))
        c.create_rectangle(x0, y0, x1, y1, outline="#1f2937", width=width)

        perimeter = 2 * ((x1 - x0) + (y1 - y0))
        remaining = perimeter * max(0, min(100, progress)) / 100.0
        segments = (
            (x0, y0, x1, y0),
            (x1, y0, x1, y1),
            (x1, y1, x0, y1),
            (x0, y1, x0, y0),
        )
        for sx0, sy0, sx1, sy1 in segments:
            length = ((sx1 - sx0) ** 2 + (sy1 - sy0) ** 2) ** 0.5
            if remaining <= 0:
                break
            draw_length = min(remaining, length)
            ratio = draw_length / length if length else 0
            ex = sx0 + (sx1 - sx0) * ratio
            ey = sy0 + (sy1 - sy0) * ratio
            c.create_line(sx0, sy0, ex, ey, fill=accent, width=width, capstyle=tk.PROJECTING)
            remaining -= draw_length

    def draw_layout_text(self, key, text, color, weight="bold", max_chars=None):
        layout = normalize_layout_fields(self.cfg.get("layout_fields")).get(key, LOWER_FIELD_LAYOUT[key])
        w = max(210, self.canvas.winfo_width())
        h = max(210, self.canvas.winfo_height())
        value = self.fit_text(text, max_chars) if max_chars else text
        self.canvas.create_text(
            w * (layout["x"] / 100.0),
            h * (layout["y"] / 100.0),
            text=value,
            fill=color,
            font=("Segoe UI", int(layout["size"]), weight),
        )

    def draw_face(self):
        c = self.canvas
        c.delete("all")
        w = max(210, c.winfo_width())
        h = max(210, c.winfo_height())
        size = min(w, h)
        cx, cy = w / 2, h / 2
        panel_pad = max(12, int(size * 0.055))
        panel = (panel_pad, panel_pad, w - panel_pad, h - panel_pad)
        content_top = panel_pad + 8
        content_h = max(160, h - (panel_pad * 2) - 30)
        y_at = lambda fraction: content_top + content_h * fraction
        progress = max(0, min(100, int(self.display.get("progress", 0))))
        connected = self.connection_text.lower().startswith(("connected", "requested", "subscribed"))
        alert = bool(self.display.get("errors"))
        accent = "#ff4d4d" if alert else "#32e649"
        muted = "#8fa2b5"

        font_scale = size / 300.0
        c.create_rectangle(0, 0, w, h, fill="#05070a", outline="")
        c.create_rectangle(panel, fill="#06080d", outline="#111a25", width=1)
        self.draw_edge_progress(panel_pad / 2, panel_pad / 2, w - panel_pad / 2, h - panel_pad / 2, progress, accent)

        dot_color = "#32e649" if connected else "#64748b"
        dot_y = y_at(0.04)
        c.create_oval(panel_pad + 4, dot_y - 3, panel_pad + 10, dot_y + 3, fill=dot_color, outline="")
        pct_font = max(12, int(22 * font_scale))
        state_font = max(9, int(13 * font_scale))
        c.create_text(cx, y_at(0.04), text=f"{progress}%", fill=accent, font=("Segoe UI", pct_font, "bold"))
        c.create_text(cx, y_at(0.13), text=self.fit_text(self.display.get("state"), 14).lower(),
                      fill="#66f59a", font=("Segoe UI", state_font, "italic"))

        printer_w = size * 0.12
        printer_h = size * 0.12
        px0 = cx - printer_w / 2
        py0 = y_at(0.20)
        c.create_rectangle(px0, py0, px0 + printer_w, py0 + printer_h, outline=accent, width=2)
        c.create_rectangle(px0 + printer_w * 0.22, py0 + printer_h * 0.18,
                           px0 + printer_w * 0.78, py0 + printer_h * 0.54, fill=accent, outline="")
        c.create_line(px0 + printer_w * 0.5, py0 + printer_h * 0.54,
                      px0 + printer_w * 0.5, py0 + printer_h * 0.86, fill=accent, width=2)

        metric_font = max(9, int(12 * font_scale))
        label_font = max(7, int(8 * font_scale))
        metric_y = y_at(0.27)
        left_x = panel_pad + size * 0.19
        right_x = w - panel_pad - size * 0.19
        c.create_text(left_x, metric_y, text=self.display.get("nozzle", "--"),
                      fill="#f8fafc", font=("Segoe UI", metric_font, "bold"), anchor="center")
        c.create_text(right_x, metric_y, text=self.display.get("bed", "--"),
                      fill="#f8fafc", font=("Segoe UI", metric_font, "bold"), anchor="center")
        c.create_text(left_x, y_at(0.33), text="nozzle", fill=muted, font=("Segoe UI", label_font))
        c.create_text(right_x, y_at(0.33), text="bed", fill=muted, font=("Segoe UI", label_font))

        self.draw_layout_text("eta", f"ETA  {self.display.get('remaining', '--')}", "#b9dcff")
        self.draw_layout_text("finish", f"Finish {self.display.get('finish', '--')}", "#dbeafe")
        self.draw_layout_text("layer", f"Layer: {self.display.get('layer', '--')}", "#f8fafc")
        self.draw_layout_text("job", self.display.get("job", "--"), "#cbd5e1", weight="normal", max_chars=28)
        self.draw_layout_text("ams", self.display.get("ams", "--"), "#d5f9ff")
        self.draw_layout_text("total", f"Total Print Hours {self.display.get('total_hours', '--')}", "#96f7c2")

        footer = self.fit_text(self.connection_text, 36)
        c.create_text(cx, h - panel_pad - 5, text=footer, fill="#64748b", font=("Segoe UI", max(7, int(8 * font_scale))))
        c.create_text(w - panel_pad - 15, panel_pad + 13, text="...", fill="#64748b", font=("Segoe UI", max(10, int(15 * font_scale)), "bold"))

    def start_connection(self):
        self.update_print_timer(active=False)
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
        self.update_print_timer(active=False)
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

    def open_layout_editor(self):
        dialog = LayoutDialog(self.root, self.cfg)
        self.root.wait_window(dialog)
        if dialog.result:
            self.cfg["layout_fields"] = dialog.result
            save_config(self.cfg)
            self.draw_face()

    def process_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "connection":
                    self.connection_text = payload
                    if not payload.lower().startswith("connected"):
                        self.update_print_timer(active=False)
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
        self.update_print_timer(time.time(), persist=True)
        self.draw_face()
        self.root.after(1000, self.tick)

    def update_print_timer(self, now=None, active=None, persist=True):
        now = now or time.time()
        if active is None:
            active = self.print_timer_active

        last_ts = self.print_timer_last_ts
        if last_ts is not None and self.print_timer_active:
            interval = CLOUD_PUSHALL_SECONDS if self.cfg.get("mode", "cloud") == "cloud" else PUSHALL_SECONDS
            max_elapsed = max(5, (interval * 2) + 10)
            elapsed = max(0.0, min(now - last_ts, max_elapsed))
            self.add_print_seconds(elapsed)

        self.print_timer_active = bool(active)
        self.print_timer_last_ts = now
        self.display["total_hours"] = format_total_hours(self.cfg.get("total_print_seconds", 0))

        if persist and now - self.print_timer_last_save >= PRINT_TIMER_SAVE_SECONDS:
            save_config(self.cfg)
            self.print_timer_last_save = now

    def add_print_seconds(self, seconds):
        seconds = max(0.0, as_float(seconds, 0.0))
        if seconds <= 0:
            return
        self.cfg["total_print_seconds"] = as_float(self.cfg.get("total_print_seconds"), 0.0) + seconds
        self.cfg["print_timer_job_accounted_seconds"] = (
            as_float(self.cfg.get("print_timer_job_accounted_seconds"), 0.0) + seconds
        )

    def reconcile_print_job(self):
        job_key = print_job_key(self.status)
        if not job_key:
            return
        if self.cfg.get("print_timer_job_key") != job_key:
            self.cfg["print_timer_job_key"] = job_key
            self.cfg["print_timer_job_accounted_seconds"] = 0

        estimated_elapsed = estimate_job_elapsed_seconds(self.status)
        if estimated_elapsed is None:
            return
        accounted = as_float(self.cfg.get("print_timer_job_accounted_seconds"), 0.0)
        if estimated_elapsed > accounted + 1:
            self.add_print_seconds(estimated_elapsed - accounted)

    def apply_status(self, payload):
        self.status.update(payload)
        state_raw = as_text(self.status.get("gcode_state"), "UNKNOWN").upper()
        self.update_print_timer(active=state_raw in COUNTED_PRINT_STATES)
        if state_raw in COUNTED_PRINT_STATES:
            self.reconcile_print_job()
            self.display["total_hours"] = format_total_hours(self.cfg.get("total_print_seconds", 0))
        elif state_raw in ("FINISH", "FAILED", "IDLE"):
            self.cfg["print_timer_job_key"] = ""
            self.cfg["print_timer_job_accounted_seconds"] = 0
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
        remaining_minutes = self.status.get("mc_remaining_time")
        self.display["remaining"] = format_minutes(remaining_minutes)
        self.display["finish"] = format_finish_time(remaining_minutes)
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
            self.update_print_timer(time.time(), persist=True)
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
