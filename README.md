# Bambu Direct Monitor

Small always-on-top Windows monitor for Bambu Lab printers.

The app connects directly to a Bambu printer through either Bambu Cloud or LAN
MQTT and renders a compact round gauge inspired by small ESP32 status displays.

## Features

- Bambu Cloud sign-in and printer picker
- Optional LAN mode with printer IP, serial number, and LAN access code
- Small resizable graphical gauge
- Always-on-top mode
- Progress, state, file, ETA, layer, nozzle temp, bed temp, and AMS humidity
- Calculated local finish clock time under the ETA duration
- Chamber light on/off commands when supported

## Run

Download or build the app, then run:

```text
dist\Run Bambu Direct Monitor.bat
```

The first run opens settings. For Cloud mode, use **Cloud sign in / pick
printer**. Your Bambu password is used for the login request and is not stored.
The resulting access token is saved locally in:

```text
BambuDirectMonitor-config.json
```

Do not commit or share that config file.

## Build

Requirements:

- Windows
- Python 3.11+ recommended
- PyInstaller
- paho-mqtt
- curl_cffi

Install dependencies:

```powershell
python -m pip install pyinstaller "paho-mqtt<3" curl_cffi
```

Build:

```powershell
.\build.ps1
```

The executable is written to:

```text
dist\BambuDirectMonitor.exe
```

## Controls

- Resize it like a normal Windows window.
- Drag the title bar to move it.
- Double-click the gauge to open settings.
- Right-click for settings, reconnect, light controls, always-on-top, and exit.

## Attribution

This project includes and adapts Bambu cloud/printer protocol code from
[Keralots/BambuHelper](https://github.com/Keralots/BambuHelper), which is MIT
licensed.
