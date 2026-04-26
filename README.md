# HC3 Menu

A macOS menu bar app to control your **Fibaro Home Center 3** from the top of your screen — toggle lights, dim, open shutters, set thermostats, see sensor values, and get notifications when things change.

> Requires **macOS 11+ on Apple Silicon (M1/M2/M3/M4)**.

---

## Install

1. Download the latest **`HC3-Menu-x.y.z-arm64.dmg`** from the
   [Releases page](https://github.com/jangabrielsson/hc3menu/releases).
2. Open the DMG and drag **HC3 Menu** into the **Applications** folder.
3. Launch **HC3 Menu** from Applications. A house icon appears in the menu bar.

> The app is signed with an Apple Developer ID and notarized by Apple, so
> macOS opens it without warnings. (If you have an older unsigned 0.1.x build
> installed, drag the new one over it — Gatekeeper will accept it.)

### Allow Local Network access

The first time HC3 Menu tries to reach your HC3, macOS should pop up:

> *"HC3 Menu" would like to find devices on your local network.*

Click **Allow**, then **quit HC3 Menu (⌘Q) and launch it again** — macOS only applies
the new permission to a freshly started process.

If you missed the prompt or connections fail with **"No route to host" (errno 65)**:

- Open **System Settings → Privacy & Security → Local Network** and toggle
  **HC3 Menu** on, then quit and relaunch.
- If HC3 Menu isn't in the list, reset its privacy state in Terminal:

  ```sh
  tccutil reset All com.jangabrielsson.hc3menu
  open "/Applications/HC3 Menu.app"
  ```

  Then click **Refresh** in the menu so it tries to connect again and re-triggers the prompt.

### Connect to your HC3

Open the menu and choose **Preferences…**, then on the **Connection** tab fill in:

- **Host** — your HC3's IP address (e.g. `192.168.1.50`)
- **User** / **Password** — an HC3 account with API access
- **PIN** *(optional)* — required only if your HC3 enforces a PIN for certain actions

Click **Save**. The menu will populate within a second or two.

---

## Using HC3 Menu

Click the house icon in the menu bar.

- **Favorites** — your starred devices, always at the top.
- **Rooms** — devices grouped by room, then by type within each room.
- Plus (when present on your HC3) **Alarm**, **Profiles**, **Scenes**, **Attention**, **Activity**, **Debug messages**, **Diagnostics**.

Inside each device submenu:

- **Switches** — click the row to toggle on/off.
- **Dimmers** — click the row to toggle on/off; **Set value…** opens a slider/input for 0–100 %.
- **Shutters** — *Open / Close / Stop*.
- **Thermostats** — set heating mode and target temperature.
- **Sensors** — read-only value (temperature, lux, humidity, motion…).
- **☆ Add to favorites / ★ Remove from favorites** — manage favorites per device.

### Notifications

In **Preferences → Notifications**, tick the **Notify** column for any device
whose state changes you want to see as macOS notifications (e.g. front door opened,
motion in garage). Notifications appear in Notification Center.

### Check for updates

The menu has **Check for updates…** which fetches the latest GitHub release and
opens the download page if a newer version is available.

---

## Where settings are stored

- `~/.hc3menu/.env` — host, user, password, PIN (plain text; `chmod 600` recommended).
- `~/.hc3menu/config.json` — favorites and notification rules.

To start fresh, quit HC3 Menu and `rm -rf ~/.hc3menu`.

---

## Troubleshooting

| Problem | Try |
|---|---|
| "No route to host" / errno 65 | Allow Local Network access (see above), then quit and relaunch. |
| Menu shows nothing / "HC3 connection failed" | Verify host/user/password in Preferences. From Terminal: `curl -u user:pass http://<host>/api/settings/info` |
| Want to see logs | `tail -f ~/Library/Logs/HC3\ Menu/*.log` |

---

## For developers

HC3 Menu is open source (MIT) — Python + [`rumps`](https://github.com/jaredks/rumps) + PyObjC.
If you want to run from source, hack on it, or build your own DMG, see
[docs/DEVELOPING.md](docs/DEVELOPING.md).

---

## License

MIT — see [LICENSE](LICENSE). Personal-use build, signed and notarized for
distribution. No warranty. Not affiliated with Fibaro.
