# Tuple Panel

A clean, native **GTK4 / libadwaita** control panel for the **Tuple Linux CLI**.

Tuple for Linux has no GUI — every action goes through the `tuple` command-line
client talking to a background daemon. This app is a thin native front-end: each
button shells out to `tuple …`, and the header status pill reflects live state
derived from Tuple's log.

> **Note:** [Tuple for Linux](https://docs.tuple.app/getting-started/tuple-for-linux)
> is still in **alpha** — the CLI is barebones and its commands/output may change.
> Some of this app's live status is parsed from the CLI/log on a best-effort basis
> and could break with a future Tuple release.

> **Disclaimer:** Unofficial and not affiliated with, endorsed by, or supported
> by Tuple. It drives the official `tuple` CLI and the bundled `update-tuple`
> downloads Tuple's released binary from their public bucket. "Tuple" is a
> trademark of its respective owner. Use at your own risk (see LICENSE).

## Compatibility

Works on **any Linux desktop** with **GTK4 + libadwaita** — it is **not
GNOME-specific**. Tested on GNOME (Wayland); also runs on KDE, XFCE, MATE,
Cinnamon, etc., on Wayland or X11. It relies only on freedesktop standards
(`.desktop` launchers, hicolor icons, XDG autostart, `org.freedesktop.Notifications`).

Notes:

- On non-GNOME desktops you may need to install libadwaita + the GTK4/Adw
  introspection data (see Requirements). The UI uses Adwaita styling regardless
  of your desktop theme.
- Linux only (uses `/proc` and the `tuple` Linux CLI).
- No system-tray icon (libappindicator is GTK3-only and can't load in a GTK4
  app); background mode uses notifications + relaunch instead.
- x86_64 and arm64 are both supported (the bundled `update-tuple` auto-detects).

## Requirements

- The `tuple` CLI. You don't have to install it yourself — `./install.sh`
  bootstraps it via the bundled `update-tuple` script if it's missing. (Tuple
  ships as a single static binary in an S3 bucket, not an apt package.) You still
  need to be logged in (`tuple login`).
- GTK4 + libadwaita Python bindings (PyGObject). These ship with most modern
  GNOME-based distros — no install step needed if this works:

  ```sh
  python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); print('ok')"
  ```

  If it doesn't, install your distro's PyGObject + GTK4 + libadwaita packages
  (e.g. Debian/Ubuntu: `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`).

## Run

Without installing:

```sh
python3 tuple_panel.py
```

GTK4 runs natively on Wayland — no flags required.

## Install (user-local, no root)

```sh
./install.sh
```

This copies the app to `~/.local/bin/tuple-panel` (executable, on your PATH), a
launcher + icon to `~/.local/share/`, and the `update-tuple` helper to
`~/.local/bin/`. If the `tuple` CLI isn't installed yet, it bootstraps it into
`~/.local/bin/tuple` (no sudo). Afterwards run it with `tuple-panel`, or launch
**Tuple Panel** from your app menu. Remove the panel with `./uninstall.sh` (this
leaves the `tuple` CLI and your login in place).

## Updating the Tuple CLI

Tuple has no built-in updater, so the bundled **`update-tuple`** fetches the
latest release binary and installs it to `~/.local/bin/tuple` — no sudo
(`update-tuple --force` to reinstall, `update-tuple <version>` to pin). From the
app, use **menu → Check for Tuple updates…**, which runs it in a terminal so you
can watch the download progress.

## What it does

| Area      | Controls                                                                 |
|-----------|--------------------------------------------------------------------------|
| Header    | Live status pill (disconnected / connection / in-call), Refresh, account & daemon menu |
| Call      | New call, Join by URL, End call, Mute mic, Share screen, **live call timer + participants** |
| Incoming  | **Banner + desktop notification on incoming calls, with a Join button**   |
| Contacts  | Availability dot (**auto-refreshed**), favorite ⭐ toggle, per-contact Call, search, loading/empty/error states |
| Menu      | Daemon on/off, Log in / Auth code / Log out, **Settings**, **Check for Tuple updates**, **Run in background**, About, Quit |
| Settings  | Opened from the menu — `overlay`, `capture`, `guest-mode`, `transcription-model` (auto-detected) |

Every command result is shown as a toast (success or the CLI's error text).

### Reactive flow

The UI shows only the controls that make sense for your current state:

- **Full-screen prompts** — if the daemon is stopped you get a **Start Tuple**
  prompt; if it's running but you're logged out, a **Log in** prompt. The contacts
  and call view appears only once the daemon is up *and* you're logged in.
- **Call group** — when you're *not* in a call it shows **New call** and **Join URL**.
  Once a call starts (you press New/Join/Call, or the log shows a call) those are
  replaced by an **In a call** row plus **Mute**, **Share screen**, and **End call**.
- **Account menu** — shows **Log in** / **Enter auth code** only when logged out, and
  **Log out** only when logged in (detected from `…/tuple/0/.auth_token`).
- **Daemon menu** — shows **Start daemon (on)** when the daemon is stopped, and
  **Stop daemon (off)** when it's running (detected by finding the `tuple` process
  that holds the log file open — the daemon keeps whatever argv first started it).

State is re-evaluated on launch, when you hit **Refresh**, every few seconds while
running (so changes made outside the app show up too), and continuously from the log.

## Run in background

Enable **menu → Run in background (start at login)** to:

- add an autostart entry (`~/.config/autostart/tuple-panel.desktop`, launched with
  `--background` so it starts hidden), and
- make the window **close to background** instead of quitting — the app keeps
  watching the log so you still get incoming-call notifications with no window
  open. Reopen it by launching **Tuple Panel** again; fully exit via **menu → Quit**.

Note: there's no system-tray icon. `libappindicator` is GTK3-only and can't be
loaded into this GTK4 app, and stock GNOME has no tray anyway — so background mode
relies on notifications + relaunch instead of a tray menu.

## Wayland & screen sharing

On Wayland, screen capture goes through the desktop portal / PipeWire. In the
**Settings** group set **`capture`** to **`portal`**. When you toggle **Share
screen**, the portal's source picker appears — choose your screen/window there.

## Notes & limitations

- The CLI exposes no `status` command, so connection / in-call state is inferred
  best-effort by tailing `~/.local/share/tuple/0/log.txt`
  (`$XDG_DATA_HOME/tuple/0/log.txt` if set). Mute/Share switches are optimistic —
  they reflect what you clicked, not a queried device state.
- **Per-contact Call**: the app runs `tuple call <USER_ID>`. If your CLI version
  doesn't accept a contact id, it reports so via a toast — use **New call** or
  Tuple's own picker instead. (`tuple call` with no argument opens the daemon's
  picker window.)
- **`tuple` in `~/.local/bin`**: app-menu launchers often start with a PATH that
  omits `~/.local/bin`, so a `tuple` installed there can read as "not found".
  The app prepends `~/.local/bin` (and `$XDG_BIN_HOME`) to its PATH at startup to
  handle this; for using `tuple` in a terminal, make sure that directory is on
  your shell PATH (add `export PATH="$HOME/.local/bin:$PATH"` to `~/.profile`).


## Project layout

```
tuple_panel.py            the app (single file)
install.sh / uninstall.sh user-local installer
data/tuple-panel.desktop  app-menu launcher
data/tuple-panel.svg      app icon
scripts/update-tuple      installs/updates the tuple CLI binary
```

## License

MIT — see [LICENSE](LICENSE).
