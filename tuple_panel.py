#!/usr/bin/env python3
"""
Tuple Panel — a native GTK4/libadwaita control panel for the Tuple Linux CLI.

Tuple for Linux has no GUI; everything is driven through the `tuple` command-line
client talking to a background daemon. This app is a thin, native front-end: every
button shells out to `tuple ...`, and live status is derived by tailing Tuple's log.

Run:  python3 tuple_panel.py     (no install step; uses system PyGObject)
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk  # noqa: E402

import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402

TUPLE_BIN = "tuple"
DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
LOG_PATH = os.path.join(DATA_HOME, "tuple", "0", "log.txt")
AUTH_TOKEN_PATH = os.path.join(DATA_HOME, "tuple", "0", ".auth_token")
APP_ID = "app.tuple.Panel"


def is_daemon_running():
    """The Tuple daemon runs as a persistent `tuple on` process. Detect it by
    scanning /proc for a process whose argv is exactly ['tuple', 'on'] (this
    excludes transient CLI commands and this panel). Linux-only, which is fine."""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    parts = [p for p in f.read().split(b"\0") if p]
            except OSError:
                continue
            if (
                len(parts) == 2
                and os.path.basename(parts[0].decode("utf-8", "replace")) == "tuple"
                and parts[1] == b"on"
            ):
                return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------- #
# CLI wrapper                                                                  #
# --------------------------------------------------------------------------- #
class TupleCLI:
    """Wraps the `tuple` binary. All blocking work runs off the GTK main thread."""

    CONTACT_RE = re.compile(
        r"^\s*(\d+)\s+(.*?)\s+<([^>]*)>\s+\[(\w+)\](?:\s+\((favorite)\))?\s*$"
    )
    SETTING_RE = re.compile(r"^(.*?)\s*\|\s*(.*?)\s*\|\s*(.*)$")

    def __init__(self):
        self.available = shutil.which(TUPLE_BIN) is not None

    def run(self, args):
        """Synchronous. Returns (ok: bool, stdout: str, stderr: str)."""
        try:
            p = subprocess.run(
                [TUPLE_BIN, *args],
                capture_output=True,
                text=True,
                timeout=25,
            )
            return (p.returncode == 0, p.stdout.strip(), p.stderr.strip())
        except FileNotFoundError:
            return (False, "", f"`{TUPLE_BIN}` not found on PATH")
        except subprocess.TimeoutExpired:
            return (False, "", f"`{TUPLE_BIN} {' '.join(args)}` timed out")
        except Exception as exc:  # noqa: BLE001
            return (False, "", str(exc))

    def run_async(self, args, callback=None):
        """Run in a background thread; marshal the result back to the main loop."""

        def worker():
            ok, out, err = self.run(args)
            if callback is not None:
                GLib.idle_add(callback, ok, out, err)

        threading.Thread(target=worker, daemon=True).start()

    # -- parsers ----------------------------------------------------------- #
    def list_contacts(self):
        ok, out, err = self.run(["ls"])
        contacts = []
        if ok:
            for line in out.splitlines():
                m = self.CONTACT_RE.match(line)
                if m:
                    contacts.append(
                        {
                            "id": m.group(1),
                            "name": m.group(2).strip(),
                            "email": m.group(3),
                            "available": m.group(4) == "available",
                            "favorite": bool(m.group(5)),
                        }
                    )
        return ok, contacts, err

    def get_settings(self):
        ok, out, err = self.run(["settings"])
        settings = []
        if ok:
            for line in out.splitlines():
                m = self.SETTING_RE.match(line)
                if not m:
                    continue
                name, value, desc = (g.strip() for g in m.groups())
                options = None
                om = re.search(r"\(([^)]*)\)\s*$", desc)
                if om and "|" in om.group(1):
                    options = [o.strip() for o in om.group(1).split("|")]
                settings.append(
                    {"name": name, "value": value, "desc": desc, "options": options}
                )
        return ok, settings, err


# --------------------------------------------------------------------------- #
# Log watcher — derive live status                                            #
# --------------------------------------------------------------------------- #
class LogWatcher:
    """Tails Tuple's log to infer connection/call state. Best-effort."""

    CONN_RE = re.compile(r"realtime connection state: \w+ -> (\w+)")
    CLI_RE = re.compile(r"cli: (\w+)")

    def __init__(self, path, on_status):
        self.path = path
        self.on_status = on_status
        self.status = {"connection": "unknown", "in_call": False, "last": ""}
        self._pos = 0
        self._monitor = None
        self._scanning = False

    def start(self):
        # Process whole history once to establish connection state, then follow.
        # Call state from stale history is unreliable, so ignore it during the
        # initial scan and only track in_call from events after launch.
        self._scanning = True
        self._pos = 0
        self._read_new()
        self._scanning = False
        try:
            gfile = Gio.File.new_for_path(self.path)
            self._monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *_: self._read_new())
        except Exception:  # noqa: BLE001
            self._monitor = None
        # Poll as a safety net (Wayland/fs monitors can be flaky).
        GLib.timeout_add_seconds(2, self._poll)

    def _poll(self):
        self._read_new()
        return GLib.SOURCE_CONTINUE

    def _process(self, line):
        changed = False
        m = self.CONN_RE.search(line)
        if m and self.status["connection"] != m.group(1):
            self.status["connection"] = m.group(1)
            changed = True
        m = self.CLI_RE.search(line)
        if m:
            cmd = m.group(1)
            self.status["last"] = cmd
            if not self._scanning:  # in_call from stale history is unreliable
                if cmd in ("call", "new", "join") and not self.status["in_call"]:
                    self.status["in_call"] = True
                elif cmd == "end" and self.status["in_call"]:
                    self.status["in_call"] = False
            changed = True
        if not self._scanning and "call is no longer valid" in line and self.status["in_call"]:
            self.status["in_call"] = False
            changed = True
        return changed

    def _read_new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self._pos:  # rotated/truncated
            self._pos = 0
        if size == self._pos:
            return
        try:
            with open(self.path, "r", errors="replace") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
        except OSError:
            return
        changed = False
        for line in data.splitlines():
            if self._process(line):
                changed = True
        if changed:
            self.on_status(dict(self.status))


# --------------------------------------------------------------------------- #
# Window                                                                       #
# --------------------------------------------------------------------------- #
CSS = """
.dot { min-width: 11px; min-height: 11px; border-radius: 999px; }
.dot.green  { background: #2ec27e; }
.dot.grey   { background: #9aa0a6; }
.dot.orange { background: #e5a50a; }
.dot.red    { background: #e01b24; }
.status-pill {
    padding: 2px 10px 2px 8px;
    border-radius: 999px;
    background: alpha(currentColor, 0.10);
    font-weight: 600;
    font-size: 0.85em;
}
.status-pill box.dot { margin-right: 6px; }
"""


def make_dot(color):
    box = Gtk.Box()
    box.add_css_class("dot")
    box.add_css_class(color)
    box.set_valign(Gtk.Align.CENTER)
    return box


class TuplePanel(Adw.ApplicationWindow):
    def __init__(self, app, cli):
        super().__init__(application=app, title="Tuple")
        self.cli = cli
        self._suppress = False  # guard programmatic switch/combo changes
        self._contact_rows = []  # (data, row) for search filtering

        self.set_default_size(520, 760)

        # status pill widgets (in header)
        self._conn_status = {"connection": "unknown", "in_call": False}
        self._pill_dot = make_dot("grey")
        self._pill_label = Gtk.Label(label="unknown")
        pill = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        pill.add_css_class("status-pill")
        pill.append(self._pill_dot)
        pill.append(self._pill_label)
        pill.set_valign(Gtk.Align.CENTER)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Tuple", subtitle="control panel"))

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh contacts & settings")
        refresh_btn.connect("clicked", lambda *_: self.refresh_all())
        header.pack_start(refresh_btn)

        header.pack_end(self._make_menu_button())
        header.pack_end(pill)

        # body
        self.toasts = Adw.ToastOverlay()
        self.page = Adw.PreferencesPage()
        self.toasts.set_child(self.page)

        self._build_call_group()
        self._build_contacts_group()
        self._build_settings_dialog()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(self.toasts)
        self.set_content(root)

        if not self.cli.available:
            self.toast("`tuple` CLI not found on PATH — actions will fail.")

        # live status
        self.watcher = LogWatcher(LOG_PATH, self.on_status)
        self.watcher.start()

        # keep daemon/login menu state fresh even if changed outside the app
        GLib.timeout_add_seconds(3, self._poll_account_state)

        self.refresh_all()

    def _poll_account_state(self):
        self.detect_daemon()
        self.detect_login()
        return GLib.SOURCE_CONTINUE

    # -- helpers ----------------------------------------------------------- #
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def report(self, label):
        """Return a run_async callback that toasts the outcome."""

        def cb(ok, out, err):
            if ok:
                self.toast(f"{label}: ok" + (f" — {out.splitlines()[0]}" if out else ""))
            else:
                self.toast(f"{label} failed: {err or out or 'unknown error'}")

        return cb

    def do_cmd(self, args, label):
        self.cli.run_async(args, self.report(label))

    # -- menu -------------------------------------------------------------- #
    def _make_menu_button(self):
        self.logged_in = None  # unknown until detected
        self.daemon_on = None  # unknown until detected
        for name, handler in [
            ("daemon_on", self._on_daemon_on),
            ("daemon_off", self._on_daemon_off),
            ("login", self._on_login),
            ("auth", self._on_auth),
            ("logout", self._on_logout),
            ("settings", self._on_settings),
            ("about", self._on_about),
        ]:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", handler)
            self.add_action(act)

        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_button.set_tooltip_text("Account & daemon")
        self._rebuild_menu()
        return self.menu_button

    def _rebuild_menu(self):
        """Show login/auth only when logged out, logout only when logged in."""
        menu = Gio.Menu()

        daemon = Gio.Menu()
        if self.daemon_on:
            daemon.append("Stop daemon (off)", "win.daemon_off")
        else:
            daemon.append("Start daemon (on)", "win.daemon_on")
        menu.append_section(None, daemon)

        account = Gio.Menu()
        if self.logged_in:
            account.append("Log out", "win.logout")
        else:
            account.append("Log in…", "win.login")
            account.append("Enter auth code…", "win.auth")
        menu.append_section(None, account)

        misc = Gio.Menu()
        misc.append("Settings", "win.settings")
        misc.append("About", "win.about")
        menu.append_section(None, misc)

        self.menu_button.set_menu_model(menu)

    def set_logged_in(self, value):
        if value != self.logged_in:
            self.logged_in = value
            self._rebuild_menu()

    def detect_login(self):
        try:
            self.set_logged_in(os.path.getsize(AUTH_TOKEN_PATH) > 0)
        except OSError:
            self.set_logged_in(False)

    def set_daemon(self, value):
        if value != self.daemon_on:
            self.daemon_on = value
            self._rebuild_menu()
            if value is False:
                self.set_in_call(False)  # no daemon => not in a call
            self.render_pill()

    def detect_daemon(self):
        """Check whether the daemon process is alive (off the main thread)."""
        threading.Thread(
            target=lambda: GLib.idle_add(self.set_daemon, is_daemon_running()),
            daemon=True,
        ).start()

    def _force_connection(self, conn):
        """Override the (possibly stale) connection state and re-render.
        Stopping the daemon logs no disconnect line, so without this the watcher
        keeps reporting a stale 'connected'."""
        self.watcher.status["connection"] = conn
        self._conn_status["connection"] = conn
        self.render_pill()

    def _daemon_cmd(self, on):
        # Optimistic UI, then reconcile *after* the command completes — `tuple on`
        # can take longer than a fixed timer to fork the daemon, so a timed
        # re-check would race it and momentarily report "disconnected".
        self.set_daemon(on)
        self._force_connection("connecting" if on else "disconnected")
        label = "Daemon on" if on else "Daemon off"

        def cb(ok, out, err):
            self.report(label)(ok, out, err)
            self.detect_daemon()

        self.cli.run_async(["on" if on else "off"], cb)

    def _on_daemon_on(self, *_):
        self._daemon_cmd(True)

    def _on_daemon_off(self, *_):
        self._daemon_cmd(False)

    # -- call group -------------------------------------------------------- #
    def _build_call_group(self):
        g = Adw.PreferencesGroup(title="Call")
        self.call_group = g
        self.in_call = None  # unknown until first status

        # --- shown when NOT in a call -------------------------------------- #
        self.new_row = Adw.ActionRow(
            title="New call", subtitle="Start and join a call with your personal URL"
        )
        new_btn = Gtk.Button(label="Start")
        new_btn.add_css_class("suggested-action")
        new_btn.set_valign(Gtk.Align.CENTER)
        new_btn.connect("clicked", self._on_new)
        self.new_row.add_suffix(new_btn)
        self.new_row.set_activatable_widget(new_btn)
        g.add(self.new_row)

        self.join_row = Adw.EntryRow(title="Join URL")
        self.join_row.set_show_apply_button(True)
        self.join_row.connect("apply", self._on_join)
        g.add(self.join_row)

        # --- shown only WHILE in a call ------------------------------------ #
        self.incall_label = Adw.ActionRow(title="In a call", subtitle="Connected")
        self.incall_label.add_prefix(make_dot("green"))
        g.add(self.incall_label)

        self.mute_row = Adw.SwitchRow(title="Mute microphone")
        self.mute_row.connect("notify::active", self._on_mute_toggle)
        g.add(self.mute_row)

        self.share_row = Adw.SwitchRow(
            title="Share screen", subtitle="On Wayland the portal picker appears"
        )
        self.share_row.connect("notify::active", self._on_share_toggle)
        g.add(self.share_row)

        self.end_row = Adw.ActionRow(title="End call", subtitle="Leave / end the current call")
        end_btn = Gtk.Button(label="End")
        end_btn.add_css_class("destructive-action")
        end_btn.set_valign(Gtk.Align.CENTER)
        end_btn.connect("clicked", self._on_end)
        self.end_row.add_suffix(end_btn)
        self.end_row.set_activatable_widget(end_btn)
        g.add(self.end_row)

        self.page.add(g)
        self.set_in_call(False)

    def set_in_call(self, active):
        """Show only the rows that make sense for the current call state."""
        if active == self.in_call:
            return
        self.in_call = active
        self.new_row.set_visible(not active)
        self.join_row.set_visible(not active)
        self.incall_label.set_visible(active)
        self.mute_row.set_visible(active)
        self.share_row.set_visible(active)
        self.end_row.set_visible(active)
        if not active:  # reset toggles for the next call
            self._suppress = True
            self.mute_row.set_active(False)
            self.share_row.set_active(False)
            self._suppress = False

    def _on_new(self, *_):
        self.do_cmd(["new"], "New call")
        self.set_in_call(True)  # optimistic; log watcher reconciles

    def _on_end(self, *_):
        self.do_cmd(["end"], "End call")
        self.set_in_call(False)

    def _on_join(self, row):
        url = row.get_text().strip()
        if url:
            self.do_cmd(["join", url], "Join")
            row.set_text("")
            self.set_in_call(True)

    def _on_mute_toggle(self, row, _param):
        if self._suppress:
            return
        self.do_cmd(["mute" if row.get_active() else "unmute"], "Mute" if row.get_active() else "Unmute")

    def _on_share_toggle(self, row, _param):
        if self._suppress:
            return
        self.do_cmd(["share" if row.get_active() else "unshare"], "Share" if row.get_active() else "Unshare")

    # -- contacts group ---------------------------------------------------- #
    def _build_contacts_group(self):
        self.contacts_group = Adw.PreferencesGroup(title="Contacts")
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Search contacts…")
        search.connect("search-changed", self._on_search)
        self.contacts_group.set_header_suffix(search)
        self.page.add(self.contacts_group)

    def _on_search(self, entry):
        q = entry.get_text().strip().lower()
        for data, row in self._contact_rows:
            hay = f"{data['name']} {data['email']}".lower()
            row.set_visible(q in hay)

    def _populate_contacts(self, contacts):
        for _data, row in self._contact_rows:
            self.contacts_group.remove(row)
        self._contact_rows = []

        contacts.sort(key=lambda c: (not c["favorite"], not c["available"], c["name"].lower()))
        for c in contacts:
            row = Adw.ActionRow(title=c["name"], subtitle=c["email"])
            row.add_prefix(make_dot("green" if c["available"] else "grey"))

            star = Gtk.ToggleButton()
            star.set_icon_name("starred-symbolic" if c["favorite"] else "non-starred-symbolic")
            star.set_active(c["favorite"])
            star.add_css_class("flat")
            star.set_valign(Gtk.Align.CENTER)
            star.set_tooltip_text("Favorite")
            star.connect("toggled", self._on_star, c, )
            row.add_suffix(star)

            call_btn = Gtk.Button(label="Call")
            call_btn.set_valign(Gtk.Align.CENTER)
            call_btn.add_css_class("suggested-action" if c["available"] else "flat")
            call_btn.set_tooltip_text(f"Call {c['name']}")
            call_btn.connect("clicked", self._on_call_contact, c)
            row.add_suffix(call_btn)

            self.contacts_group.add(row)
            self._contact_rows.append((c, row))

    def _on_star(self, btn, c):
        fav = btn.get_active()
        btn.set_icon_name("starred-symbolic" if fav else "non-starred-symbolic")
        self.do_cmd(["favorite" if fav else "unfavorite", c["id"]],
                    f"{'Favorite' if fav else 'Unfavorite'} {c['name']}")

    def _on_call_contact(self, _btn, c):
        def cb(ok, out, err):
            if ok:
                self.toast(f"Calling {c['name']}…")
                self.set_in_call(True)
            elif re.search(r"unexpected|unknown|usage", err, re.I):
                self.toast("CLI can't dial a specific contact — use “New call” / the Tuple picker.")
            else:
                self.toast(f"Call failed: {err or out or 'unknown error'}")

        self.cli.run_async(["call", c["id"]], cb)

    # -- settings group ---------------------------------------------------- #
    def _build_settings_dialog(self):
        """Settings live in a dialog opened from the menu, not the main page."""
        self.settings_group = Adw.PreferencesGroup(
            description="On Wayland, set “capture” to portal for screen sharing."
        )
        self._setting_rows = []
        page = Adw.PreferencesPage()
        page.add(self.settings_group)
        self.settings_dialog = Adw.PreferencesDialog()
        self.settings_dialog.set_title("Settings")
        self.settings_dialog.add(page)

    def _populate_settings(self, settings):
        for row in self._setting_rows:
            self.settings_group.remove(row)
        self._setting_rows = []

        self._suppress = True
        for s in settings:
            name, value, opts = s["name"], s["value"], s["options"]
            desc = re.sub(r"\s*\([^)]*\)\s*$", "", s["desc"])  # strip "(a|b|c)"

            if opts == ["0", "1"]:
                row = Adw.SwitchRow(title=name, subtitle=desc)
                row.set_active(value == "1")
                row.connect("notify::active", self._on_set_switch, name)
            elif opts:
                row = Adw.ComboRow(title=name, subtitle=desc)
                model = Gtk.StringList.new(opts)
                row.set_model(model)
                if value in opts:
                    row.set_selected(opts.index(value))
                row._opts = opts
                row.connect("notify::selected", self._on_set_combo, name)
            else:
                row = Adw.EntryRow(title=name)
                row.set_text(value)
                row.set_show_apply_button(True)
                row.connect("apply", self._on_set_entry, name)

            self.settings_group.add(row)
            self._setting_rows.append(row)
        self._suppress = False

    def _on_set_switch(self, row, _p, name):
        if self._suppress:
            return
        self.do_cmd(["set", name, "1" if row.get_active() else "0"], f"set {name}")

    def _on_set_combo(self, row, _p, name):
        if self._suppress:
            return
        val = row._opts[row.get_selected()]
        self.do_cmd(["set", name, val], f"set {name}")

    def _on_set_entry(self, row, name):
        self.do_cmd(["set", name, row.get_text().strip()], f"set {name}")

    # -- account dialogs --------------------------------------------------- #
    def _on_login(self, *_):
        def cb(ok, out, err):
            msg = out or err or ("Login started." if ok else "Login failed.")
            dlg = Adw.AlertDialog(heading="Log in", body=msg)
            dlg.add_response("ok", "OK")
            dlg.present(self)

        self.cli.run_async(["login"], cb)

    def _on_auth(self, *_):
        dlg = Adw.AlertDialog(heading="Enter auth code", body="Paste the code from `tuple login`.")
        entry = Gtk.Entry(placeholder_text="AUTH_CODE")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Authorize")
        dlg.set_default_response("ok")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def on_resp(_d, resp):
            code = entry.get_text().strip()
            if resp == "ok" and code:
                def cb(ok, out, err):
                    self.report("Authorize")(ok, out, err)
                    self.detect_login()  # re-check token after auth
                self.cli.run_async(["auth", code], cb)

        dlg.connect("response", on_resp)
        dlg.present(self)

    def _on_logout(self, *_):
        def cb(ok, out, err):
            self.report("Log out")(ok, out, err)
            self.detect_login()

        self.cli.run_async(["logout"], cb)

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Tuple Panel",
            application_icon="phone-symbolic",
            developer_name="A native GTK4 front-end for the Tuple Linux CLI",
            version="1.0",
            comments="Drives the `tuple` CLI and reflects live status from its log.",
        )
        about.present(self)

    # -- refresh & status -------------------------------------------------- #
    def refresh_settings(self):
        def settings_cb():
            ok, settings, err = self.cli.get_settings()
            GLib.idle_add(self._populate_settings, settings)
            if not ok:
                GLib.idle_add(self.toast, f"Couldn't read settings: {err or 'error'}")

        threading.Thread(target=settings_cb, daemon=True).start()

    def _on_settings(self, *_):
        self.refresh_settings()  # load current values, then open
        self.settings_dialog.present(self)

    def refresh_all(self):
        def contacts_cb():
            ok, contacts, err = self.cli.list_contacts()
            GLib.idle_add(self._populate_contacts, contacts)
            if not ok:
                GLib.idle_add(self.toast, f"Couldn't list contacts: {err or 'error'}")

        self.detect_login()
        self.detect_daemon()
        threading.Thread(target=contacts_cb, daemon=True).start()
        self.refresh_settings()

    def on_status(self, status):
        self._conn_status = status
        self.render_pill()
        # keep the Call group's layout in sync with observed call state
        self.set_in_call(status["in_call"] and self.daemon_on is not False)
        self.incall_label.set_subtitle(status["connection"].capitalize())

    def render_pill(self):
        """Render the header pill from daemon state + log-derived connection.
        The daemon dying doesn't always log a clean disconnect, so daemon state
        takes precedence: no daemon means no connection."""
        status = self._conn_status
        if self.daemon_on is False:
            color, label = "grey", "disconnected"
        elif status["in_call"]:
            color, label = "green", "in call"
        else:
            conn = status["connection"]
            color = {
                "connected": "green",
                "connecting": "orange",
                "synchronizing": "orange",
                "disconnected": "red",
            }.get(conn, "grey")
            label = conn
        for c in ("green", "grey", "orange", "red"):
            self._pill_dot.remove_css_class(c)
        self._pill_dot.add_css_class(color)
        self._pill_label.set_text(label)


# --------------------------------------------------------------------------- #
# Application                                                                  #
# --------------------------------------------------------------------------- #
class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.cli = TupleCLI()
        self.win = None

    def do_activate(self):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        if not self.win:
            self.win = TuplePanel(self, self.cli)
        self.win.present()


def main():
    app = App()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
