Bambu Direct Monitor
====================

Run:
  Run Bambu Direct Monitor.bat

This app connects directly to your Bambu printer. It does not need an ESP32
BambuHelper device and does not open a helper web page.

Cloud mode is the default. Use:
  - Cloud sign in / pick printer

That signs into Bambu's website/API, fetches your printer list, and stores an
access token beside the exe. Your password is not stored.

LAN mode is still available if you ever enable printer LAN mode. It requires:
  - Printer IP address
  - Printer serial number
  - LAN access code

Settings are saved beside the exe:
  BambuDirectMonitor-config.json

The monitor stays always on top by default.
It also shows AMS humidity when the printer/cloud MQTT payload includes it:
raw percent as "AMS 42%" or the unit level as "AMS L3".
ETA shows both remaining duration and the calculated local finish clock time.

Small display controls:
  - Resize it like a normal Windows window
  - Drag the title bar to move it
  - Double-click to open settings
  - Right-click for settings, reconnect, light controls, always-on-top, exit
