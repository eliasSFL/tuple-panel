# Tuple Panel

A clean, native **GTK4 / libadwaita** control panel for the **Tuple Linux CLI**.

Tuple for Linux has no GUI — every action goes through the `tuple` command-line
client talking to a background daemon. This app is a thin native front-end: each
button shells out to `tuple …`, and the header status pill reflects live state
derived from Tuple's log.

## Requirements

- The `tuple` CLI installed and on your `PATH` (`which tuple`), and logged in.
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

This copies the app to `~/.local/bin/tuple-panel` (executable, on your PATH) and a
launcher to `~/.local/share/applications/`. Afterwards run it from a terminal with
`tuple-panel`, or launch **Tuple Panel** from your app menu. Remove it with
`./uninstall.sh`.

## What it does

| Area      | Controls                                                                 |
|-----------|--------------------------------------------------------------------------|
| Header    | Live status pill (disconnected / connection / in-call), Refresh, account & daemon menu |
| Call      | New call, Join by URL, End call, Mute mic, Share screen                   |
| Contacts  | Availability dot, favorite ⭐ toggle, per-contact Call, search box        |
| Menu      | Daemon on/off, Log in / Auth code / Log out, **Settings**, About          |
| Settings  | Opened from the menu — `overlay`, `capture`, `guest-mode`, `transcription-model` (auto-detected) |

Every command result is shown as a toast (success or the CLI's error text).

### Reactive flow

The UI shows only the controls that make sense for your current state:

- **Call group** — when you're *not* in a call it shows **New call** and **Join URL**.
  Once a call starts (you press New/Join/Call, or the log shows a call) those are
  replaced by an **In a call** row plus **Mute**, **Share screen**, and **End call**.
- **Account menu** — shows **Log in** / **Enter auth code** only when logged out, and
  **Log out** only when logged in (detected from `…/tuple/0/.auth_token`).
- **Daemon menu** — shows **Start daemon (on)** when the daemon is stopped, and
  **Stop daemon (off)** when it's running (detected by finding the persistent
  `tuple on` process via `/proc`).

State is re-evaluated on launch, when you hit **Refresh**, every few seconds while
running (so changes made outside the app show up too), and continuously from the log.

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

