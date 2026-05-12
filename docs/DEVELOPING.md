# Developing HC3 Menu

Python 3.11+ on macOS (Apple Silicon). Uses [`rumps`](https://github.com/jaredks/rumps)
for the menu bar, PyObjC for the Preferences window, and `requests` for the HC3 REST API.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt   # py2app, pytest, pillow
```

## Run from source

```sh
.venv/bin/python -m hc3menu
```

On first launch, open **Preferences** and enter HC3 host/user/password.
Credentials are stored in `~/.hc3menu/.env`, favorites/rules in `~/.hc3menu/config.json`.

When run from source, the Dock icon is hidden via
`NSApplication.setActivationPolicy_(2)` (Accessory) in `hc3menu/app.py`.

## Testing against plua

You can point the app at a local [plua](https://pypi.org/project/plua/) emulator
instead of a real HC3:

```sh
plua --fibaro --run-for 0 some_qa.lua
```

Then in Preferences set host=`127.0.0.1`, port=`<plua API port>`.

## Layout

```
hc3menu/
  __version__.py    # single source of truth for version
  app.py            # rumps app, glue
  hc3_client.py     # HC3 REST wrapper
  state.py          # StateStore + RefreshPoller
  menu_builder.py   # per-device-type rumps.MenuItem factories
  prefs_window.py   # PyObjC NSWindow Preferences
  notifications.py  # rule matching + dispatch
  config.py         # .env + ~/.hc3menu/config.json
  updater.py        # GitHub Releases check
scripts/
  make_icon.py      # generate assets/icon.icns
  build_dmg.sh      # py2app + DMG packaging
  release.sh        # tag + gh release
assets/
  icon.icns         # menu/Dock icon
```

## Build a standalone .app

```sh
python setup.py py2app -A      # alias build (fast, dev) — broken refs outside venv
python setup.py py2app         # full standalone bundle in dist/HC3 Menu.app
```

The bundle uses `LSUIElement: True` (menu bar only, no Dock icon).
macOS notifications work reliably only from the bundled `.app`.

## Build a release DMG

```sh
./scripts/build_dmg.sh
# → dist/HC3-Menu-<version>-arm64.dmg
```

The script runs py2app, ad-hoc-codesigns the bundle (`codesign --force --deep --sign -`),
strips xattrs, and packs the .app + an Applications symlink + a README.txt into a
compressed UDZO DMG. Apple Silicon only.

## Cut a GitHub release

1. Bump `hc3menu/__version__.py`.
2. Commit and push.
3. Run:

   ```sh
   ./scripts/release.sh
   ```

   This tags `v<version>`, pushes the tag, and uploads the DMG via `gh release create`
   with auto-generated notes. Requires the [`gh` CLI](https://cli.github.com/) authenticated.

## Regenerating the icon

```sh
.venv/bin/python scripts/make_icon.py
```

Produces `assets/icon.iconset/*.png` and packs them into `assets/icon.icns` via `iconutil`.
Requires Pillow.

## Tests

```sh
pytest
```

---

## AppleScript Integration (future)

HC3 Menu currently has no AppleScript dictionary. Options evaluated below, ranked by effort/value.

### Option A — CLI companion (`hc3` command) ✅ Recommended first step

A thin Python CLI (`hc3menu/cli.py`) that queries the local HTTP API (port 34562) and
outputs JSON or plain text. Installed as a symlink at `/usr/local/bin/hc3`.

```applescript
-- List devices needing attention, plain text
set result to do shell script "/usr/local/bin/hc3 attention --format text"

-- Turn a device on/off
do shell script "/usr/local/bin/hc3 device 42 on"
do shell script "/usr/local/bin/hc3 device 42 off"

-- Set brightness
do shell script "/usr/local/bin/hc3 device 42 level 75"

-- Run a scene
do shell script "/usr/local/bin/hc3 scene 7 run"

-- Full device JSON
set json to do shell script "/usr/local/bin/hc3 devices --format json"
```

Requires the local API to be enabled in `~/.hc3menu/config.json`:
```json
{ "local_api_port": 34562 }
```

Also works from Terminal, Automator "Run Shell Script", and the macOS Shortcuts
"Run Shell Script" action with no additional setup.

---

### Option B — Native AppleScript dictionary (`NSScriptability`)

Implement a `.sdef` (Script Definition) XML file plus `NSScriptCommand` PyObjC subclasses
so HC3 Menu becomes a first-class AppleScript target, discoverable in Script Editor.

```applescript
tell application "HC3 Menu"
    turn on device id 42
    turn off device id 42
    run scene id 7
    get attention devices
    get every device
end tell
```

**Steps required:**

1. Write `HC3Menu.sdef` (XML) declaring classes (`device`, `scene`, …) and commands
   (`turn on`, `turn off`, `set level`, `run scene`, …).
2. Register in `Info.plist`:
   - `NSAppleScriptEnabled` = `YES`
   - `OSAScriptingDefinition` = `HC3Menu.sdef`
3. Subclass `NSScriptCommand` for each verb; implement `performDefaultImplementation`.
   Wire properties via `NSScriptClassDescription` / `copyScriptingValue:forKey:withProperties:`.
4. Wire into the app via bundle auto-discovery or `NSApplication.setScriptingDefinition_`.

**Pro:** First-class AppleScript citizen, no local API needed, fully discoverable.  
**Con:** Significant boilerplate; PyObjC `NSScriptability` binding is sparsely documented.

Reference: [Cocoa Scripting Guide](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/ScriptableCocoaApplications/)

---

### Option C — Raw Apple Events (`NSAppleEventManager`)

Register 4-char-code event/class pairs and dispatch them manually. Works, but call syntax
from AppleScript is opaque (`«event HC3Mdevl»`). Not recommended.
