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
import select  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

TUPLE_BIN = "tuple"
DATA_HOME = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
LOG_PATH = os.path.join(DATA_HOME, "tuple", "0", "log.txt")
AUTH_TOKEN_PATH = os.path.join(DATA_HOME, "tuple", "0", ".auth_token")
APP_ID = "app.tuple.Panel"
CALL_URL_BASE = "https://tuple.app/c/"
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
AUTOSTART_PATH = os.path.join(CONFIG_HOME, "autostart", "tuple-panel.desktop")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# `tuple call`'s picker prints one "  N) Name <email>" line per available contact.
PICKER_RE = re.compile(r"^\s*(\d+)\)\s+.*?<([^>]+)>", re.M)


def ensure_local_bin_on_path():
    """App-menu launchers (GNOME etc.) start with a PATH that often omits
    ~/.local/bin — where `tuple` and `update-tuple` now live. Prepend it so
    shutil.which / subprocess can find them, regardless of how we were launched."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in (os.path.expanduser("~/.local/bin"), os.environ.get("XDG_BIN_HOME", "")):
        if d and d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(p for p in parts if p)


def is_daemon_running():
    """Detect the Tuple daemon by finding the `tuple` process that holds the log
    file open. The daemon keeps the argv of whichever command first started it
    (`tuple on`, `tuple ls`, ...), so we can't match on argv — but only the daemon
    holds log.txt open continuously, and `tuple off` leaves no such process.
    Linux-only, which is fine."""
    try:
        log_real = os.path.realpath(LOG_PATH)
    except OSError:
        return False
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    argv0 = f.read().split(b"\0")[0]
            except OSError:
                continue
            if os.path.basename(argv0.decode("utf-8", "replace")) != "tuple":
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        if os.path.realpath(os.path.join(fd_dir, fd)) == log_real:
                            return True
                    except OSError:
                        continue
            except OSError:
                continue
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

    def version(self):
        """Tuple's CLI prints its version in the usage banner (no `--version`)."""
        ok, out, err = self.run([])
        m = re.search(r"v\d[\w.]*", f"{out}\n{err}")
        return m.group(0) if m else None

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
    USER_RE = re.compile(r"\buser (\d+) color \w+(\s*\(local\))?")
    INCOMING_RE = re.compile(r"received incoming call: (.*)")
    END_MARKERS = ("invalidating call", "sfu closed", "call is no longer valid")

    def __init__(self, path, on_status):
        self.path = path
        self.on_status = on_status
        self.status = {
            "connection": "unknown",
            "in_call": False,
            "last": "",
            "participants": [],  # remote user ids in the current call
            "incoming": None,    # raw payload of an unanswered incoming call
        }
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

    def _set_in_call(self, value):
        """Set call state, resetting per-call data on each transition."""
        if self.status["in_call"] == value:
            return False
        self.status["in_call"] = value
        self.status["participants"] = []
        if value:
            self.status["incoming"] = None  # an answered call clears the prompt
        return True

    def _process(self, line):
        changed = False
        m = self.CONN_RE.search(line)
        if m and self.status["connection"] != m.group(1):
            self.status["connection"] = m.group(1)
            changed = True

        # Call state from stale history is unreliable, so only track it live.
        if not self._scanning:
            m = self.CLI_RE.search(line)
            if m:
                cmd = m.group(1)
                self.status["last"] = cmd
                if cmd in ("call", "new", "join"):
                    changed |= self._set_in_call(True)
                elif cmd == "end":
                    changed |= self._set_in_call(False)
                else:
                    changed = True
            if "call connected" in line:
                changed |= self._set_in_call(True)
            if any(k in line for k in self.END_MARKERS):
                changed |= self._set_in_call(False)

            # Participants joining the active call: "user 123 color Green (local)"
            um = self.USER_RE.search(line)
            if um and self.status["in_call"] and not um.group(2):  # skip local user
                uid = um.group(1)
                if uid not in self.status["participants"]:
                    self.status["participants"].append(uid)
                    changed = True

            im = self.INCOMING_RE.search(line)
            if im:
                self.status["incoming"] = im.group(1).strip()
                changed = True
        else:
            m = self.CLI_RE.search(line)
            if m:
                self.status["last"] = m.group(1)

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
            snapshot = dict(self.status)
            snapshot["participants"] = list(self.status["participants"])
            self.on_status(snapshot)


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
        self._really_quit = False
        self._bg_enabled = os.path.exists(AUTOSTART_PATH)  # run-in-background
        self._bg_notified = False
        self.connect("close-request", self._on_close_request)

        self.set_default_size(520, 760)

        # status pill widgets (in header)
        self._conn_status = {"connection": "unknown", "in_call": False, "participants": []}
        self._call_started = None  # monotonic time when the current call connected
        self._call_timer_id = None
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

        # body: a stack that swaps between prompts (daemon stopped / logged out)
        # and the app content (call + contacts).
        self._current_view = None
        self.toasts = Adw.ToastOverlay()
        self.page = Adw.PreferencesPage()

        self.offline_page = Adw.StatusPage(
            icon_name="network-offline-symbolic",
            title="Tuple isn't running",
            description="Start the Tuple daemon to load your contacts and make calls.",
        )
        start_btn = Gtk.Button(label="Start Tuple")
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_halign(Gtk.Align.CENTER)
        start_btn.connect("clicked", lambda *_: self._daemon_cmd(True))
        self.offline_page.set_child(start_btn)

        self.login_page = Adw.StatusPage(
            icon_name="avatar-default-symbolic",
            title="Not logged in",
            description="Log in to your Tuple account to see contacts and make calls.",
        )
        login_btn = Gtk.Button(label="Log in")
        login_btn.add_css_class("suggested-action")
        login_btn.add_css_class("pill")
        login_btn.set_halign(Gtk.Align.CENTER)
        login_btn.connect("clicked", self._on_login)
        self.login_page.set_child(login_btn)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.offline_page, "offline")
        self.stack.add_named(self.login_page, "login")
        self.stack.add_named(self.page, "main")

        # incoming-call banner shown above the content
        self._incoming_seen = None
        self._incoming_url = None
        self.incoming_banner = Adw.Banner(button_label="Join")
        self.incoming_banner.set_revealed(False)
        self.incoming_banner.connect("button-clicked", self._on_join_incoming)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.incoming_banner)
        body.append(self.stack)
        self.stack.set_vexpand(True)
        self.toasts.set_child(body)

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
        # keep contact availability current while the daemon is running
        GLib.timeout_add_seconds(30, self._auto_refresh_contacts)

        # Pick the initial view from the real state (synchronous so we don't
        # flash the wrong screen). Login state is read first, then the daemon —
        # so contacts are only loaded once the daemon is up AND we're logged in
        # (listing contacts would otherwise start the daemon, and we don't want
        # that behind the "start Tuple" / "log in" prompts).
        self.detect_login()
        self.set_daemon(is_daemon_running())

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
            ("update", self._on_update),
            ("about", self._on_about),
            ("quit", self._on_quit),
        ]:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", handler)
            self.add_action(act)

        bg = Gio.SimpleAction.new_stateful(
            "background", None, GLib.Variant.new_boolean(self._bg_enabled)
        )
        bg.connect("change-state", self._on_toggle_background)
        self.add_action(bg)

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
        misc.append("Check for Tuple updates…", "win.update")
        misc.append("Run in background (start at login)", "win.background")
        misc.append("About", "win.about")
        menu.append_section(None, misc)

        quit_section = Gio.Menu()
        quit_section.append("Quit", "win.quit")
        menu.append_section(None, quit_section)

        self.menu_button.set_menu_model(menu)

    def _update_view(self):
        """Pick the page for the current state: start-daemon prompt -> log-in
        prompt -> app content. Loads contacts/settings when entering 'main'."""
        if self.daemon_on is not True:        # off or not yet known
            view = "offline"
        elif self.logged_in is not True:      # logged out or not yet known
            view = "login"
        else:
            view = "main"
        if view == self._current_view:
            return
        self._current_view = view
        self.stack.set_visible_child_name(view)
        if view == "main":
            self.refresh_all()  # daemon up + logged in — load contacts/settings

    def set_logged_in(self, value):
        if value != self.logged_in:
            self.logged_in = value
            self._rebuild_menu()
            self._update_view()

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
            self._update_view()

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

    def start_daemon_in_background(self):
        """Launched at login in background mode: bring the Tuple daemon up so
        incoming calls are actually received without the user opening the app.
        No-op if it's already running, so a relaunch doesn't disturb a live call."""
        if is_daemon_running():
            return
        self._daemon_cmd(True)

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
        if active:
            self._start_call_timer()
        else:  # reset toggles for the next call
            self._stop_call_timer()
            self._suppress = True
            self.mute_row.set_active(False)
            self.share_row.set_active(False)
            self._suppress = False

    # -- active-call timer / participants ---------------------------------- #
    def _start_call_timer(self):
        if self._call_timer_id is None:
            self._call_started = time.monotonic()
            self._update_incall_label()
            self._call_timer_id = GLib.timeout_add_seconds(1, self._tick_call)

    def _stop_call_timer(self):
        if self._call_timer_id is not None:
            GLib.source_remove(self._call_timer_id)
            self._call_timer_id = None
        self._call_started = None

    def _tick_call(self):
        self._update_incall_label()
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _fmt_duration(secs):
        h, rem = divmod(int(secs), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _participant_names(self):
        by_id = {c["id"]: c["name"] for c, _row in self._contact_rows}
        return [by_id.get(uid, f"user {uid}")
                for uid in self._conn_status.get("participants", [])]

    def _update_incall_label(self):
        parts = []
        if self._call_started is not None:
            parts.append(self._fmt_duration(time.monotonic() - self._call_started))
        names = self._participant_names()
        if names:
            parts.append("with " + ", ".join(names))
        self.incall_label.set_subtitle(" · ".join(parts) or "Connected")

    # -- incoming-call alerts --------------------------------------------- #
    def _incoming_caller_name(self, payload):
        """Best-effort: a contact id appearing in the incoming payload -> name."""
        for c, _row in self._contact_rows:
            if c["id"] and re.search(rf"\b{c['id']}\b", payload):
                return c["name"]
        return None

    def _handle_incoming(self, payload):
        caller = self._incoming_caller_name(payload)
        m = UUID_RE.search(payload)
        self._incoming_url = (CALL_URL_BASE + m.group(0)) if m else None
        title = f"Incoming call from {caller}" if caller else "Incoming Tuple call"
        self.incoming_banner.set_title(title)
        self.incoming_banner.set_revealed(True)
        self._notify(title, "Click Join in Tuple Panel to answer." if self._incoming_url
                     else "Open Tuple to answer.")

    def _hide_incoming(self):
        self.incoming_banner.set_revealed(False)

    def _on_join_incoming(self, _banner):
        if self._incoming_url:
            self.do_cmd(["join", self._incoming_url], "Join")
            self.set_in_call(True)
        else:
            self.toast("Couldn't determine the call link — join from Tuple.")
        self._hide_incoming()

    def _notify(self, title, body):
        """Desktop notification via notify-send (best-effort, non-blocking)."""
        notifier = shutil.which("notify-send")
        if not notifier:
            return
        try:
            subprocess.Popen(
                [notifier, "-a", "Tuple Panel", "-i", "tuple-panel", title, body]
            )
        except Exception:  # noqa: BLE001
            pass

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
        self._contacts_status_row = None
        self._show_contacts_status("spinner", "Loading contacts…")

    def _on_search(self, entry):
        q = entry.get_text().strip().lower()
        for data, row in self._contact_rows:
            hay = f"{data['name']} {data['email']}".lower()
            row.set_visible(q in hay)

    def _clear_contacts(self):
        for _data, row in self._contact_rows:
            self.contacts_group.remove(row)
        self._contact_rows = []
        if self._contacts_status_row is not None:
            self.contacts_group.remove(self._contacts_status_row)
            self._contacts_status_row = None

    def _show_contacts_status(self, kind, text):
        """Show a single non-interactive row: loading spinner / empty / error."""
        self._clear_contacts()
        row = Adw.ActionRow(title=text)
        row.set_activatable(False)
        if kind == "spinner":
            sp = Gtk.Spinner()
            sp.start()
            sp.set_valign(Gtk.Align.CENTER)
            row.add_prefix(sp)
        elif kind == "error":
            img = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            img.set_valign(Gtk.Align.CENTER)
            row.add_prefix(img)
        self.contacts_group.add(row)
        self._contacts_status_row = row

    def _populate_contacts(self, contacts, ok=True, err=""):
        self._clear_contacts()
        if not ok:
            self._show_contacts_status("error", f"Couldn't load contacts — {err or 'error'}")
            return
        if not contacts:
            self._show_contacts_status("empty", "No contacts")
            return

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
        # `tuple call` takes no USER_ID — it opens an interactive picker that
        # lists *available* contacts and reads a positional choice on stdin.
        # Drive that picker (off the main thread) to ring this specific contact.
        threading.Thread(target=self._dial_contact, args=(c,), daemon=True).start()

    def _dial_contact(self, c):
        """Ring a specific contact by answering `tuple call`'s picker prompt.
        The picker prints "  N) Name <email>" for each available contact, then
        "enter a number to call:". We match our contact by email and write back
        its number; if it isn't listed (unavailable), abort without calling."""
        try:
            proc = subprocess.Popen(
                [TUPLE_BIN, "call"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self.toast, f"Call failed: {exc}")
            return

        target = (c.get("email") or "").strip().lower()
        options = {}  # email -> picker number
        buf = b""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.3)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:  # EOF before any prompt
                    break
                buf += chunk
                for m in PICKER_RE.finditer(buf.decode("utf-8", "replace")):
                    options[m.group(2).strip().lower()] = m.group(1)
                if b"enter a number" in buf.lower():
                    break
            elif proc.poll() is not None:
                break

        number = options.get(target)
        if number is not None:
            try:
                proc.stdin.write(f"{number}\n".encode())
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
            GLib.idle_add(self.toast, f"Calling {c['name']}…")
            GLib.idle_add(self.set_in_call, True)
            # Drain so the client never blocks on a full pipe, then reap it.
            # `tuple call` hands the call to the daemon and exits.
            try:
                while os.read(proc.stdout.fileno(), 4096):
                    pass
            except OSError:
                pass
            proc.wait()
            return

        # Not offered by the picker (unavailable) or no prompt — don't call.
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        proc.wait()
        text = buf.decode("utf-8", "replace")
        if "already in a call" in text.lower():
            GLib.idle_add(self.toast, "Already in a call.")
        elif options:
            GLib.idle_add(self.toast, f"{c['name']} isn't available to call right now.")
        else:
            last = text.strip().splitlines()[-1] if text.strip() else "no response from picker"
            GLib.idle_add(self.toast, f"Couldn't open the call picker — {last}")

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
    def _copy_text(self, text):
        self.get_clipboard().set(text)
        self.toast("Copied to clipboard")

    def _open_uri(self, uri):
        try:
            Gtk.UriLauncher.new(uri).launch(self, None, None)
        except Exception:  # noqa: BLE001
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.Popen([opener, uri])

    def _on_login(self, *_):
        def cb(ok, out, err):
            text = (out or err or "").strip()
            m = re.search(r"https?://\S+", text)
            url = m.group(0).rstrip(".,") if m else None

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

            if url:
                url_entry = Gtk.Entry(text=url, editable=False, hexpand=True)
                url_entry.set_tooltip_text(url)
                copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy link")
                copy_btn.connect("clicked", lambda *_: self._copy_text(url))
                open_btn = Gtk.Button(icon_name="external-link-symbolic", tooltip_text="Open in browser")
                open_btn.connect("clicked", lambda *_: self._open_uri(url))
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                for w in (url_entry, copy_btn, open_btn):
                    w.set_valign(Gtk.Align.CENTER)
                row.append(url_entry)
                row.append(copy_btn)
                row.append(open_btn)
                box.append(row)
            elif text:  # no URL parsed — show the raw output, selectable
                lbl = Gtk.Label(label=text, wrap=True, selectable=True, xalign=0)
                box.append(lbl)

            code_entry = Gtk.Entry(placeholder_text="Paste auth code here")
            box.append(code_entry)

            body = ("Open the link, authorize, then paste the code below."
                    if url else ("Couldn't start login." if not ok else
                                 "Login started — paste the code below."))
            dlg = Adw.AlertDialog(heading="Log in to Tuple", body=body)
            dlg.set_extra_child(box)
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("ok", "Authorize")
            dlg.set_default_response("ok")
            dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

            def on_resp(_d, resp):
                code = code_entry.get_text().strip()
                if resp == "ok" and code:
                    def acb(aok, aout, aerr):
                        self.report("Authorize")(aok, aout, aerr)
                        self.detect_login()  # re-check token -> may switch to main

                    self.cli.run_async(["auth", code], acb)

            dlg.connect("response", on_resp)
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

    def _find_update_tuple(self):
        """Locate update-tuple without relying on PATH — when launched from the
        app menu the session PATH usually omits ~/.local/bin. It's installed next
        to this program, so look there (and a few known spots) too."""
        found = shutil.which("update-tuple")
        if found:
            return found
        dirs = [
            os.path.dirname(os.path.realpath(sys.argv[0])),
            os.environ.get("XDG_BIN_HOME", ""),
            os.path.expanduser("~/.local/bin"),
            "/usr/local/bin",
            "/usr/bin",
        ]
        for d in dirs:
            cand = os.path.join(d, "update-tuple") if d else ""
            if cand and os.access(cand, os.X_OK):
                return cand
        return None

    def _on_update(self, *_):
        """Run `update-tuple` in a terminal so its sudo prompt and progress show."""
        updater = self._find_update_tuple()
        if not updater:
            self.toast("update-tuple not found — install it, then run it in a terminal.")
            return
        # Keep the window open after it finishes so the user can read the result.
        line = f"{updater}; echo; read -n1 -rsp 'Done — press any key to close…'"
        run = ["bash", "-lc", line]
        # Cover terminals across desktops, with each one's correct exec flag.
        terminals = [
            ("ptyxis", ["--", *run]),                  # GNOME (Ubuntu default)
            ("kgx", ["-e", *run]),                     # GNOME Console
            ("gnome-terminal", ["--", *run]),          # GNOME
            ("konsole", ["-e", *run]),                 # KDE
            ("xfce4-terminal", ["-x", *run]),          # XFCE
            ("mate-terminal", ["-x", *run]),           # MATE
            ("x-terminal-emulator", ["-e", *run]),     # Debian alternatives
            ("alacritty", ["-e", *run]),
            ("kitty", run),
            ("foot", run),                             # Wayland
            ("wezterm", ["start", "--", *run]),
            ("xterm", ["-e", *run]),                   # universal fallback
        ]
        for term, term_args in terminals:
            if shutil.which(term):
                try:
                    subprocess.Popen([term, *term_args])
                    self.toast("Checking for Tuple updates in a terminal…")
                except Exception as exc:  # noqa: BLE001
                    self.toast(f"Couldn't launch updater: {exc}")
                return
        self.toast("No terminal found — run `update-tuple` manually to update.")

    # -- background mode / run-on-login ----------------------------------- #
    def _self_exec_path(self):
        for d in (
            os.path.dirname(os.path.realpath(sys.argv[0])),
            os.environ.get("XDG_BIN_HOME", ""),
            os.path.expanduser("~/.local/bin"),
        ):
            cand = os.path.join(d, "tuple-panel") if d else ""
            if cand and os.access(cand, os.X_OK):
                return cand
        return os.path.realpath(sys.argv[0])

    def _on_toggle_background(self, action, value):
        enabled = value.get_boolean()
        action.set_state(value)
        self._bg_enabled = enabled
        try:
            if enabled:
                os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
                with open(AUTOSTART_PATH, "w") as f:
                    f.write(
                        "[Desktop Entry]\n"
                        "Type=Application\n"
                        "Name=Tuple Panel (background)\n"
                        f"Exec={self._self_exec_path()} --background\n"
                        "Icon=tuple-panel\n"
                        "Terminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
                self.toast("Will start in the background at login. "
                           "Closing the window now keeps it running.")
            else:
                if os.path.exists(AUTOSTART_PATH):
                    os.remove(AUTOSTART_PATH)
                self.toast("Background mode off — closing the window will quit.")
        except OSError as exc:
            self.toast(f"Couldn't update autostart: {exc}")

    def _on_close_request(self, *_):
        # In background mode, closing the window hides it (keeps watching for
        # incoming calls) instead of quitting. Quit from the menu to exit.
        if self._really_quit or not self._bg_enabled:
            return False  # allow the window to close / app to quit
        self.set_visible(False)
        if not self._bg_notified:
            self._bg_notified = True
            self._notify("Tuple Panel is still running",
                         "You'll be alerted on incoming calls. Quit from the menu to exit.")
        return True  # prevent destroy

    def _on_quit(self, *_):
        self._really_quit = True
        self.get_application().quit()

    def _on_about(self, *_):
        base = ("Drives the `tuple` CLI and reflects live status from its log.\n"
                "Tuple for Linux is still in alpha, so some status is best-effort.")
        about = Adw.AboutDialog(
            application_name="Tuple Panel",
            application_icon="phone-symbolic",
            developer_name="A native GTK4 front-end for the Tuple Linux CLI",
            version="1.0",
            comments=f"{base}\n\nTuple CLI: checking…",
        )
        about.present(self)

        # Fetch the Tuple CLI version off the main thread, then fill it in.
        def worker():
            ver = self.cli.version()
            GLib.idle_add(
                about.set_comments, f"{base}\n\nTuple CLI: {ver or 'not found'}"
            )

        threading.Thread(target=worker, daemon=True).start()

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

    def refresh_contacts(self, show_loading=True):
        if show_loading:
            self._show_contacts_status("spinner", "Loading contacts…")

        def contacts_cb():
            ok, contacts, err = self.cli.list_contacts()
            GLib.idle_add(self._populate_contacts, contacts, ok, err)

        threading.Thread(target=contacts_cb, daemon=True).start()

    def _auto_refresh_contacts(self):
        # Keep availability dots current. Only while the daemon is up — listing
        # contacts would otherwise start it. Silent (no spinner) to avoid flicker.
        if self.daemon_on:
            self.refresh_contacts(show_loading=False)
        return GLib.SOURCE_CONTINUE

    def refresh_all(self):
        self.detect_login()
        self.detect_daemon()
        self.refresh_contacts(show_loading=True)
        self.refresh_settings()

    def on_status(self, status):
        self._conn_status = status
        self.render_pill()
        # keep the Call group's layout in sync with observed call state
        self.set_in_call(status["in_call"] and self.daemon_on is not False)
        if self.in_call:
            self._update_incall_label()  # refresh participants list live

        # incoming-call alert: surface a new pending call, clear it once answered
        incoming = status.get("incoming")
        if self.in_call or self.daemon_on is False or not incoming:
            self._hide_incoming()
            if not incoming:
                self._incoming_seen = None
        elif incoming != self._incoming_seen:
            self._incoming_seen = incoming
            self._handle_incoming(incoming)

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
        # X11 reads the window icon from here; on Wayland the icon comes from the
        # desktop file whose basename matches our application_id (app.tuple.Panel).
        Gtk.Window.set_default_icon_name("tuple-panel")

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        first_run = self.win is None
        if first_run:
            self.win = TuplePanel(self, self.cli)
        # Started at login with --background: stay running (window hidden) so we
        # can watch for incoming calls, but don't pop the window. Any later
        # activation (relaunch) shows it.
        if first_run and "--background" in sys.argv:
            self.win.start_daemon_in_background()
            return
        self.win.present()


def main():
    ensure_local_bin_on_path()
    app = App()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
