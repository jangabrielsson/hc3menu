# HC3 Menu

A macOS menu bar app (Python + [`rumps`](https://github.com/jaredks/rumps)) to control Fibaro HC3 devices.

## Features (v1)

- Three browse modes: **Favorites**, **By Room**, **By Type**
- Supports binary switches, dimmers/shutters, sensors (read-only), thermostats
- Live state via `/refreshStates` long-polling in a background thread
- macOS notifications on configurable device-property changes
- PyObjC Preferences window (Connection + Favorites/Notifications tabs)
- Connects directly to HC3 via REST + Basic Auth

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (dev)

```bash
python -m hc3menu
```

On first launch you'll be prompted to open Preferences and enter HC3 host/user/password.
Credentials are stored in `~/.hc3menu/.env`, favorites/rules in `~/.hc3menu/config.json`.

## Build a standalone .app (later)

```bash
pip install -r requirements-dev.txt
python setup.py py2app -A      # alias build (fast, dev)
# or
python setup.py py2app         # full standalone bundle in dist/
```

The bundle uses `LSUIElement: True` so it appears only in the menu bar (no Dock icon).
macOS notifications work reliably only from the bundled `.app`.

## Layout

```
hc3menu/
  app.py            # rumps app, glue
  hc3_client.py     # HC3 REST wrapper
  state.py          # StateStore + RefreshPoller
  menu_builder.py   # per-device-type rumps.MenuItem factories
  prefs_window.py   # PyObjC NSWindow Preferences
  notifications.py  # rule matching + dispatch
  config.py         # .env + ~/.hc3menu/config.json
```

## Testing against plua

You can point the app at a local plua emulator instead of a real HC3:

```bash
plua --fibaro --run-for 0 some_qa.lua
```

Then in Preferences set host=`127.0.0.1`, port=`<plua API port>`.
