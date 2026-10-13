"""A local orgs server: set up, run, update, configure and its users.

orgs/localserver.py does the work (no sublime in it); this is the sublime
side - the commands, the questions, and when to start, update or renew.
The same capabilities as the vscode plugin's local server, with Sublime's
own login (=sublimetext=) in place of vscode's.
"""
import os
import sys
import threading
import time
import webbrowser

import sublime
import sublime_plugin

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgdb as db
import OrgEverywhere.orgs.session as orgs
from OrgEverywhere.orgs import client as C
from OrgEverywhere.orgs import localserver as L

SETTINGS = "OrgEverywhere.sublime-settings"


r"""
/* SDOC: Reference::Settings::Connection
*** Local orgs server settings
    For the server *Org: Set Up a Local orgs Server* downloads and runs.

    | Setting                     | Default | Effect                                                                                                         |
    |-----------------------------+---------+----------------------------------------------------------------------------------------------------------------|
    | =orgsServerAutoStart=       | =true=  | Start the local server when Sublime starts, if it is not already running.                                      |
    | =orgsServerAutoLogin=       | =true=  | Log in to the local server as =sublimetext= with the password Sublime keeps for it, so you are never asked. =false= asks, like any other server. |
    | =orgsServerCheckForUpdates= | =true=  | Once a day, ask github whether there is a newer orgs release and offer it.                                     |

    The server is the local one when =orgsUrl= is its address (set up writes
    it). Its files live in your user data folder, not in Packages: on macOS
    =~/Library/Application Support/OrgEverywhere/orgs=, on Windows
    =%LOCALAPPDATA%\OrgEverywhere\orgs=, on Linux
    =~/.local/share/OrgEverywhere/orgs=.
EDOC */
"""


def _server():
    return orgs.local_server()


def _main(fn, *args):
    sublime.set_timeout(lambda: fn(*args), 0)


def _thread(fn, name="orgs-server"):
    threading.Thread(target=fn, name=name, daemon=True).start()


def _say(msg):
    _main(sublime.status_message, "orgs: " + msg)


def _fail(e, what):
    msg = "{0}:\n\n{1}".format(what, getattr(e, "msg", None) or e)
    srv = _server()
    if os.path.exists(srv.log_file):
        _main(lambda: sublime.ok_cancel_dialog(msg, "Show Its Log") and _show_log(sublime.active_window()))
    else:
        _main(sublime.error_message, msg)


def _set_user_setting(name, value):
    """Write name to the user's OrgEverywhere settings (ui thread)."""
    s = sublime.load_settings(SETTINGS)
    s.set(name, value)
    sublime.save_settings(SETTINGS)


def _window():
    return sublime.active_window()


def _quick(window, items, on_done, placeholder=None):
    kw = {}
    if placeholder and int(sublime.version()) >= 4081:
        kw["placeholder"] = placeholder
    window.show_quick_panel(items, on_done, **kw)


def _is_managed():
    return _server().is_managed(sets.Get("orgsUrl", None) or "")


def _reconnect(interactive=False):
    """Connect to the local server afresh: a new client, so a renewed
    certificate is the one trusted."""
    cl = orgs.client()
    if cl is not None:
        cl.close()
    orgs.connect(interactive=interactive, on_done=lambda ok: ok and db.Get().RebuildDb())


def _keep_password(user, password):
    orgs._store().set(L.PASSWORD_KEY, {"user": user, "password": password})


def _kept_password():
    try:
        return orgs._store().get(L.PASSWORD_KEY) or {}
    except Exception:
        return {}


def _show_log(window):
    srv = _server()
    if os.path.exists(srv.log_file):
        window.open_file(srv.log_file)
    else:
        sublime.status_message("orgs: the local server has not been started from here, so there is no log")


def _ask_password(window, who, on_done):
    """A password, typed masked, twice. on_done(password) only when both
    match and it is not empty."""
    def first(pw):
        if not pw:
            sublime.status_message("orgs: an empty password is not one")
            return
        orgs.MaskedInput(window, "Password for {0}, again:".format(who),
                         lambda again: on_done(pw) if again == pw else sublime.error_message("Those passwords do not match."),
                         lambda: None)
    orgs.MaskedInput(window, "Password for {0}:".format(who), first, lambda: None)


def _ask_new_user(window, taken, on_done, caption="New orgs user name:"):
    """A user name orgs would take and nobody has, then its password."""
    def got(name):
        name = name.strip()
        why = L.username_problem(name) or ("there is already a user " + name if name in taken else None)
        if why:
            sublime.error_message("orgs: " + why + ".")
            return
        _ask_password(window, name, lambda pw: on_done(name, pw))
    window.show_input_panel(caption, "", got, None, None)


# ----------------------------------------------------------------------
# Set up

r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Set Up a Local orgs Server
    Command: =orgs_set_up_server=  Keys: none

    Downloads the latest orgs release from github for this machine, checks
    it against the release's =checksums.txt=, and runs it in the
    background, reading a folder of org files you pick (one of the
    project's folders, or any other). It writes an =orgs.yaml=, a keystore
    and a self-signed certificate beside it, serves https only and answers
    this machine only. =orgsUrl= is pointed at it.

    It then asks how you want to log in:

    - *Log me in automatically*: the user =sublimetext= gets a random
      password, kept in your OS keychain, and Sublime logs in with it. You
      are never asked (=orgsServerAutoLogin=).
    - *Ask me to log in*: you pick a user name and password of your own (an
      administrator). Sublime asks for them like any other server's login.

    Once set up, the command opens the local server's menu instead (*Org:
    Local orgs Server*).
EDOC */
"""

class OrgsSetUpServerCommand(sublime_plugin.WindowCommand):
    def run(self):
        if _server().installed():
            self.window.run_command("orgs_server_menu")
            return
        _say("looking up the latest orgs release")
        _thread(self.lookup)

    def lookup(self):
        try:
            release = L.latest_release()
        except Exception as e:
            _fail(e, "Could not find an orgs release")
            return
        if not L.asset_name(release.tag):
            _main(sublime.error_message,
                  "orgs has no build for this machine ({0}). Build it from github.com/{1} and set orgsUrl.".format(
                      sys.platform, L.ORGS_REPO))
            return
        _main(self.confirm, release)

    def confirm(self, release):
        if not sublime.ok_cancel_dialog(
                "Download orgs {0} from github.com/{1} and run it on this machine?\n\n"
                "It reads the org files in a folder you choose and serves the agenda, clocking, refile, "
                "export and the rest. It runs in the background, answers this machine only, and does not "
                "run code blocks unless you turn that on in its config.".format(release.tag, L.ORGS_REPO),
                "Download and Set Up"):
            return
        self.pick_folder(lambda d: self.pick_login(release, d))

    def pick_folder(self, on_done):
        folders = self.window.folders()
        items = [[os.path.basename(f) or f, f] for f in folders] + [
            ["Type a folder path...", "enter accepts it"],
            ["Browse for a folder...", "pick it, then click Open"] if hasattr(sublime, "select_folder_dialog") else None]
        items = [i for i in items if i]

        def done(i):
            if i < 0:
                return
            if i < len(folders):
                on_done(folders[i])
            elif i == len(folders):
                self.type_folder(on_done, folders[0] if folders else os.path.expanduser("~/org"))
            else:
                sublime.select_folder_dialog(lambda d: d and on_done(d[0] if isinstance(d, list) else d))
        _quick(self.window, items, done, "Which folder of org files should orgs read?")

    def type_folder(self, on_done, initial, caption="Folder of org files for orgs to read:"):
        """A folder typed by hand; asks again, keeping what was typed, when
        it is not a folder."""
        def got(d):
            d = os.path.abspath(os.path.expanduser(d.strip()))
            if os.path.isdir(d):
                on_done(d)
                return
            sublime.set_timeout(lambda: self.type_folder(on_done, d, "Not a folder - folder of org files:"), 0)
        self.window.show_input_panel(caption, initial, got, None, None)

    def pick_login(self, release, org_dir):
        items = [
            ["Log me in automatically",
             "Sublime keeps a random password for the user {0} in your keychain; you are never asked".format(L.ORGS_USER)],
            ["Ask me to log in",
             "Make me a user of my own; Sublime asks for its password like any server's login"],
        ]

        def done(i):
            if i == 0:
                self.install(release, org_dir, None)
            elif i == 1:
                _ask_new_user(self.window, [L.ORGS_USER],
                              lambda name, pw: self.install(release, org_dir, (name, pw)),
                              "Your orgs user name:")
        _quick(self.window, items, done, "How should Sublime log in to it?")

    def install(self, release, org_dir, own):
        _thread(lambda: self.work(release, org_dir, own), "orgs-setup")

    def work(self, release, org_dir, own):
        srv = _server()
        try:
            os.makedirs(srv.dir, exist_ok=True)
            srv.place_binary(srv.fetch_binary(release, _say), release.tag)
            _say("writing its config")
            password = srv.configure(org_dir)
            _keep_password(L.ORGS_USER, password)
            _say("starting - reading " + org_dir)
            srv.start()
            if own:
                # Added over the wire: the password never reaches a command
                # line, only a hash of it does.
                cl = C.OrgsClient(srv.url, cafile=srv.cafile())
                cl.login(L.ORGS_USER, password)
                cl.add_user(L.add_user_body(own[0], own[1], admin=True))
                cl.close()
        except Exception as e:
            # A half set up server is not one: the next try starts over.
            try:
                os.remove(srv.config)
            except OSError:
                pass
            _fail(e, "Setting up orgs failed")
            return
        if own:
            # A login some earlier server on the same address left behind.
            try:
                orgs._store().delete("orgs:" + srv.url)
            except Exception:
                pass
        orgs.run_on_main_and_wait(lambda: self.point_at(srv, own))
        _reconnect(interactive=bool(own))
        _main(self.done, release, org_dir)

    def point_at(self, srv, own):
        _set_user_setting("orgsUrl", srv.url)
        _set_user_setting("orgsServerAutoLogin", not own)
        if own:
            _set_user_setting("orgsUser", own[0])

    def done(self, release, org_dir):
        srv = _server()
        items = [["Open the agenda", ""], ["Edit its config", srv.config], ["Done", ""]]

        def pick(i):
            if i == 0:
                v = self.window.active_view()
                if v is not None:
                    v.run_command("org_agenda_day_view")
            elif i == 1:
                self.window.open_file(srv.config)
        _quick(self.window, items, pick,
               "orgs {0} is running at {1}, reading {2}".format(release.tag, srv.url, org_dir) +
               ("" if srv.https else " (on http: this release cannot make a certificate)"))


# ----------------------------------------------------------------------
# Start, stop, restart

def _start(then_connect=True):
    srv = _server()

    def work():
        try:
            _say("starting the local server")
            srv.start()
        except Exception as e:
            _fail(e, "orgs did not start")
            return
        _say("local server running at " + srv.url)
        if then_connect:
            _reconnect()
    _thread(work)


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Start / Stop / Restart Local orgs Server
    Commands: =orgs_start_server=, =orgs_stop_server=, =orgs_restart_server=
    Keys: none

    The server runs detached, so it outlives the Sublime that started it and
    every window shares it. Sublime starts it by itself when it opens
    (=orgsServerAutoStart=). Stop only stops a server this set up; anything
    else answering on its port is left alone.
EDOC */
"""

class OrgsStartServerCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not _server().installed():
            self.window.run_command("orgs_set_up_server")
            return
        _start()


class OrgsStopServerCommand(sublime_plugin.WindowCommand):
    def run(self):
        srv = _server()

        def work():
            if srv.stop():
                _say("local server stopped")
                if _is_managed():
                    _reconnect()
            else:
                _main(sublime.message_dialog,
                      "Something still answers on {0}, and it is not an orgs this set up.".format(srv.url))
        _thread(work)

    def is_enabled(self):
        return _server().installed()


class OrgsRestartServerCommand(sublime_plugin.WindowCommand):
    def run(self):
        srv = _server()

        def work():
            srv.stop()
            try:
                srv.start()
            except Exception as e:
                _fail(e, "orgs did not start")
                return
            _say("local server restarted")
            if _is_managed():
                _reconnect()
        _thread(work)

    def is_enabled(self):
        return _server().installed()


# ----------------------------------------------------------------------
# Updates

def _update(release=None):
    """(worker) Update to release (default: the latest)."""
    srv = _server()
    try:
        release = release or L.latest_release()
    except Exception as e:
        _fail(e, "Could not look for an orgs update")
        return
    have = srv.version
    if not L.is_newer(release.tag, have):
        _main(sublime.message_dialog, "orgs {0} is the latest release.".format(have))
        return
    try:
        staged = srv.fetch_binary(release, _say)
        _say("restarting")
        was_up = srv.is_up()
        was_managed = _is_managed()
        if was_up and not srv.stop():
            raise OSError("the running orgs would not stop")
        srv.place_binary(staged, release.tag)
        # A release that can make a certificate takes the server to https.
        moved = srv.upgrade_to_https()
        if moved and was_managed:
            orgs.run_on_main_and_wait(lambda: _set_user_setting("orgsUrl", srv.url))
        if was_up:
            srv.start()
    except Exception as e:
        _fail(e, "Updating orgs failed")
        return
    if _is_managed():
        _reconnect()

    def tell():
        if sublime.ok_cancel_dialog("orgs is now {0}.".format(release.tag), "Release Notes"):
            webbrowser.open(release.page)
    _main(tell)


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Update Local orgs Server
    Command: =orgs_update_server=  Keys: none

    Looks for a newer orgs release on github. When there is one it is
    downloaded and checked like at set up, the server is stopped, the binary
    replaced and the server started again. Once a day Sublime also looks by
    itself and offers a new release (=orgsServerCheckForUpdates=): update,
    read the release notes, skip that version, or not now.
EDOC */
"""

class OrgsUpdateServerCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not _server().installed():
            self.window.run_command("orgs_set_up_server")
            return
        _say("looking for an orgs update")
        _thread(_update)


def _check_for_update():
    """(worker) Once a day: is there a newer release? Offer it."""
    srv = _server()
    st = srv.state()
    if not sets.Get("orgsServerCheckForUpdates", True) or time.time() - (st.get("lastCheck") or 0) < 24 * 3600:
        return
    srv.set_state(lastCheck=time.time())
    try:
        release = L.latest_release()
    except Exception:
        return  # Offline, or github is busy: tomorrow.
    if not L.is_newer(release.tag, st.get("version")) or release.tag == st.get("skipped"):
        return

    def offer():
        items = [["Update to orgs " + release.tag, "you have " + (st.get("version") or "?")],
                 ["Read the release notes", release.page or ""],
                 ["Skip this version", "and do not offer it again"],
                 ["Not now", ""]]

        def done(i):
            if i == 0:
                _thread(lambda: _update(release))
            elif i == 1:
                webbrowser.open(release.page)
            elif i == 2:
                srv.set_state(skipped=release.tag)
        _quick(_window(), items, done, "orgs {0} is out".format(release.tag))
    _main(offer)


# ----------------------------------------------------------------------
# Config and log

r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Edit Local orgs Server Config
    Command: =orgs_edit_server_config=  Keys: none

    Opens the local server's =orgs.yaml=. Saving it has the server reload
    it at once; when something you changed needs a restart (the port,
    orgDirs, the keystore) Sublime says so and offers to restart it.
EDOC */
"""

class OrgsEditServerConfigCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not _server().installed():
            self.window.run_command("orgs_set_up_server")
            return
        self.window.open_file(_server().config)


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Show Local orgs Server Log
    Command: =orgs_show_server_log=  Keys: none

    Opens what the local server printed since it was last started from
    Sublime.
EDOC */
"""

class OrgsShowServerLogCommand(sublime_plugin.WindowCommand):
    def run(self):
        _show_log(self.window)


class OrgsServerConfigListener(sublime_plugin.EventListener):
    """The local server's orgs.yaml was saved: orgs takes most of it without
    a restart."""
    def on_post_save_async(self, view):
        srv = _server()
        fn = view.file_name()
        if not fn or not srv.installed() or not _is_managed():
            return
        if os.path.normcase(os.path.abspath(fn)) != os.path.normcase(os.path.abspath(srv.config)):
            return

        def done(res):
            needs = (res.get("needsRestart") or []) if isinstance(res, dict) else []
            if needs:
                if sublime.ok_cancel_dialog("orgs needs a restart for: " + ", ".join(needs) + ".", "Restart"):
                    _window().run_command("orgs_restart_server")
            else:
                sublime.status_message("orgs: reloaded its config")
        orgs.run(lambda c: c.reload_config(), done, what="reload config")


# ----------------------------------------------------------------------
# Users

r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Manage orgs Users
    Command: =orgs_manage_users=  Keys: none

    Gives you a login of your own, for worg in a browser or =orgs login=
    somewhere else. For the local server it lists its users (administrator,
    last login) and offers:

    - *Add a user*: name, password (typed masked, twice), ordinary user or
      administrator. Added over the wire (=POST /users/add=), so it works
      at once; the password does not travel, a salted hash of it does.
    - On a user: *Change the password* or *Remove*. These edit the keystore
      with the =orgs user= command line, so the server is stopped for the
      moment it takes.

    =sublimetext= is Sublime's own login and is left alone. For a server
    that is not the local one, adding a user is all there is, and needs
    your login to be an administrator there.
EDOC */
"""

class OrgsManageUsersCommand(sublime_plugin.WindowCommand):
    def run(self):
        if not _is_managed():
            self.add([])
            return
        _thread(self.load)

    def load(self):
        users = _server().users()
        _main(self.show, users)

    def show(self, users):
        items = [["Add a user...", ""]]
        for u in users:
            bits = []
            if u.get("Admin"):
                bits.append("admin")
            if u.get("Name") == L.ORGS_USER:
                bits.append("Sublime's own login")
            last = u.get("LastLogin")
            if last:
                bits.append("never logged in" if last == "never" else "last login " + last)
            items.append([u.get("Name", ""), " - ".join(bits)])

        def done(i):
            if i == 0:
                self.add([u.get("Name") for u in users])
            elif i > 0:
                self.user(users[i - 1])
        _quick(self.window, items, done, "Users of the local orgs server")

    def add(self, taken):
        def got(name, pw):
            def role(i):
                if i < 0:
                    return
                admin = i == 1

                def work(c):
                    return c.add_user(L.add_user_body(name, pw, admin))

                def done(res):
                    if sublime.ok_cancel_dialog(
                            "{0} can log in now - to worg in a browser, or with  orgs login -user {0}.".format(name),
                            "Open worg"):
                        self.window.run_command("org_orgs_open_web")

                def err(e):
                    sublime.error_message("Could not add {0}: {1}".format(name, getattr(e, "msg", None) or e))
                orgs.run(work, done, on_error=err)
            _quick(self.window, [["Ordinary user", ""], ["Administrator", "may add users of their own"]],
                   role, "What is {0}?".format(name))
        _ask_new_user(self.window, taken, got)

    def user(self, u):
        name = u.get("Name")
        if name == L.ORGS_USER:
            sublime.message_dialog("{0} is how Sublime logs in, with a password it keeps; it is left alone here. "
                                   "Add a user of your own to log in from a browser.".format(name))
            return

        def done(i):
            if i == 0:
                _ask_password(self.window, name,
                              lambda pw: self.keystore("new password for " + name,
                                                       ["user", "passwd", name, "-password", pw]))
            elif i == 1:
                if sublime.ok_cancel_dialog("Remove {0}? They will not be able to log in.".format(name), "Remove"):
                    self.keystore("removing " + name, ["user", "rm", name, "-force"])
        _quick(self.window, [["Change the password", ""], ["Remove", ""]], done,
               name + " (the server restarts for a moment to take the change)")

    def keystore(self, what, args):
        srv = _server()

        def work():
            _say(what)
            try:
                # On orgs' command line for the moment it takes to hash it,
                # as at set up: orgs user has no other way in for a password.
                _, restarted = srv.with_stopped(lambda: srv.cli(args))
            except Exception as e:
                _fail(e, "orgs: " + what + " failed")
                return
            _say(what + " - done")
            if restarted:
                _reconnect()
        _thread(work)


# ----------------------------------------------------------------------
# The rest of the menu

r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Copy Local orgs Server Password
    Command: =orgs_copy_server_password=  Keys: none

    Copies the password Sublime keeps for =sublimetext=, to log in by hand.
    Adding a user of your own (*Org: Manage orgs Users*) is the better way.
EDOC */
"""

class OrgsCopyServerPasswordCommand(sublime_plugin.WindowCommand):
    def run(self):
        d = _kept_password()
        if not d.get("password"):
            sublime.message_dialog("There is no password kept for the local orgs server. "
                                   "Org: Set Up a Local orgs Server makes one when it sets it up.")
            return
        sublime.set_clipboard(d["password"])
        sublime.status_message("orgs: copied the password for " + (d.get("user") or L.ORGS_USER))


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Use the Local orgs Server from a Terminal
    Command: =orgs_server_terminal=  Keys: none

    The orgs command line beside the server reads its =orgs.yaml=, keys and
    all, so it makes its own token and needs no login. This shows where it
    is and copies an alias for your shell: =alias orgs='<path>'=.
EDOC */
"""

class OrgsServerTerminalCommand(sublime_plugin.WindowCommand):
    def run(self):
        b = _server().bin
        quoted = '"' + b + '"' if " " in b else b
        alias = "alias orgs='{0}'".format(b) if sys.platform != "win32" else quoted
        if sublime.ok_cancel_dialog(
                "Run the local server's own orgs by its path, and it needs no login (it reads the orgs.yaml "
                "beside it):\n\n{0}\n\nOr alias it in your shell:\n\n{1}".format(quoted, alias),
                "Copy the Alias" if sys.platform != "win32" else "Copy Its Path"):
            sublime.set_clipboard(alias)
            sublime.status_message("orgs: copied")

    def is_enabled(self):
        return _server().installed()


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Trust Local orgs Server Certificate in Browsers
    Command: =orgs_trust_server_certificate=  Keys: none

    Browsers warn about the local server's self-signed certificate until it
    is trusted. On macOS this adds it to your login keychain, on Windows to
    your user's root store; each asks you first. On Linux each browser
    keeps its own list, so it tells you where to import it. Do it again
    after the certificate is renewed (every couple of years).
EDOC */
"""

class OrgsTrustServerCertificateCommand(sublime_plugin.WindowCommand):
    def run(self):
        srv = _server()
        crt = srv.crt
        if sys.platform.startswith("linux"):
            if sublime.ok_cancel_dialog(
                    "On Linux each browser keeps its own list: import this certificate as an authority in its "
                    "settings (Chrome: Privacy and security > Security > Manage certificates; Firefox: "
                    "Settings > Certificates).\n\n" + crt, "Copy Its Path"):
                sublime.set_clipboard(crt)
            return

        def work():
            try:
                if sys.platform == "darwin":
                    L._run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-k",
                            os.path.expanduser("~/Library/Keychains/login.keychain-db"), crt], timeout=300)
                else:
                    L._run(["certutil", "-user", "-addstore", "Root", crt], timeout=300)
            except Exception as e:
                _fail(e, "Could not add the certificate")
                return
            _main(sublime.message_dialog, "Browsers on this machine now trust {0} "
                                          "(Firefox keeps its own list, and may still ask).".format(srv.url))
        _thread(work)

    def is_enabled(self):
        return _server().installed() and _server().https


r"""
/* SDOC: Reference::Commands::Local orgs Server
*** Org: Local orgs Server
    Command: =orgs_server_menu=  Keys: none

    The local server's menu, headed by its version, address and whether it
    is running: start or restart, stop, check for an update, edit its
    config, show its log, open worg in the browser (logged in for you),
    trust its certificate in browsers, use it from a terminal, users and
    passwords, copy its password, and connect to it when =orgsUrl= points
    elsewhere. Sets one up when there is none (*Org: Set Up a Local orgs
    Server*).
EDOC */
"""

class OrgsServerMenuCommand(sublime_plugin.WindowCommand):
    def run(self):
        srv = _server()
        if not srv.installed():
            self.window.run_command("orgs_set_up_server")
            return
        _thread(lambda: _main(self.show, srv.is_up()))

    def show(self, up):
        srv = _server()
        w = self.window
        entries = [
            ("Restart", "", lambda: w.run_command("orgs_restart_server")) if up else
            ("Start", "", lambda: w.run_command("orgs_start_server")),
            ("Stop", "", lambda: w.run_command("orgs_stop_server")) if up else None,
            ("Check for an update", srv.version or "", lambda: w.run_command("orgs_update_server")),
            ("Edit its config", "orgs.yaml", lambda: w.run_command("orgs_edit_server_config")),
            ("Show its log", "", lambda: w.run_command("orgs_show_server_log")),
            ("Open worg in the browser", "logged in for you", lambda: w.run_command("org_orgs_open_web")) if up else None,
            ("Trust its certificate in browsers", "so they stop warning about it",
             lambda: w.run_command("orgs_trust_server_certificate")) if srv.https else None,
            ("Use it from a terminal", "the orgs command line, no login", lambda: w.run_command("orgs_server_terminal")),
            ("Users and passwords", "add a login of your own for worg or the command line",
             lambda: w.run_command("orgs_manage_users")),
            ("Copy its password", "to log in by hand - user " + L.ORGS_USER,
             lambda: w.run_command("orgs_copy_server_password")),
            None if _is_managed() else ("Connect to it", srv.url, self.connect),
        ]
        entries = [e for e in entries if e]
        _quick(w, [[e[0], e[1]] for e in entries], lambda i: i >= 0 and entries[i][2](),
               "Local orgs server {0} at {1}: {2}".format(srv.version or "", srv.url, "running" if up else "stopped"))

    def connect(self):
        _set_user_setting("orgsUrl", _server().url)
        _reconnect(interactive=True)


r"""
/* SDOC: Reference::Commands::Connection
*** Org: orgs
    Command: =orgs_menu=  Keys: none

    Everything about the orgs connection in one quick panel: log in or out,
    check again, the connection status, open worg, change the server url,
    connect to the local server Org Everywhere for VS Code runs (when there
    is one), and the local orgs server (or set one up).
EDOC */
"""

class OrgsMenuCommand(sublime_plugin.WindowCommand):
    def run(self):
        w = self.window
        cl = orgs.client()
        logged_in = cl is not None and orgs.connected()
        entries = [
            None if cl is None else
            (("Log out", "", lambda: w.run_command("orgs_logout")) if logged_in and not cl.auth_disabled else
             ("Log in", "", lambda: w.run_command("orgs_login")) if not logged_in else None),
            ("Check again", "", lambda: w.run_command("orgs_connect")),
            ("Connection status", orgs.describe(), lambda: w.run_command("orgs_status")),
            ("Open worg", "the orgs web ui", lambda: w.run_command("org_orgs_open_web")) if cl is not None else None,
            ("Change the server url...", sets.Get("orgsUrl", None) or "", lambda: w.run_command("orgs_set_url")),
        ] + [
            ("Connect to VS Code's orgs server", srv.url, lambda srv=srv: self.connect(srv.url))
            for srv in orgs.vscode_servers() if not srv.is_managed(sets.Get("orgsUrl", None) or "")
        ] + [
            ("Local orgs server...", _server().url, lambda: w.run_command("orgs_server_menu"))
            if _server().installed() else
            ("Set up a local orgs server...", "download orgs and run it on this machine",
             lambda: w.run_command("orgs_set_up_server")),
        ]
        entries = [e for e in entries if e]
        _quick(w, [[e[0], e[1]] for e in entries], lambda i: i >= 0 and entries[i][2](),
               "orgs server: " + (cl.url if cl else sets.Get("orgsUrl", None) or "none found"))

    def connect(self, url):
        _set_user_setting("orgsUrl", url)
        _reconnect(interactive=True)


# ----------------------------------------------------------------------
# When Sublime starts

def _certificate_upkeep(srv):
    """(worker) Move an http server to https once its orgs can, and replace
    a certificate with under a month left. Restarts it when it was up.
    True when something changed."""
    changed = False
    was_managed = _is_managed()
    if srv.upgrade_to_https():
        changed = True
        if was_managed:
            orgs.run_on_main_and_wait(lambda: _set_user_setting("orgsUrl", srv.url))
    elif srv.https:
        exp = srv.certificate_expires()
        if exp is None or (exp - C.now_utc()).total_seconds() < 30 * 24 * 3600:
            changed = srv.make_certificate(force=True)
    if changed and srv.is_up():
        srv.stop()
        srv.start()
    return changed


def _offer_set_up():
    """(worker) Once ever: no server found and none set up - offer one."""
    srv = _server()
    if srv.state().get("offered") or sets.Get("orgsUrl", None):
        return
    for _ in range(60):
        st, _msg = orgs.state()
        if st != orgs.SEARCHING:
            break
        time.sleep(0.5)
    if orgs.state()[0] != orgs.UNAVAILABLE:
        return
    srv.set_state(offered=True)

    def ask():
        if sublime.ok_cancel_dialog(
                "OrgEverywhere could not find an orgs server. The agenda, capture, refile, clocking and the rest "
                "need one.\n\nDownload orgs and set one up on this machine now? (Org: Set Up a Local orgs "
                "Server does it any time.)", "Set Up..."):
            _window().run_command("orgs_set_up_server")
    _main(ask)


def _startup():
    srv = _server()
    if not orgs.enabled():
        return
    if not srv.installed():
        _offer_set_up()
        return
    try:
        changed = _certificate_upkeep(srv)
    except Exception as e:
        changed = False
        _fail(e, "The local orgs server's certificate")
    started = False
    if _is_managed() and sets.Get("orgsServerAutoStart", True) and not srv.is_up():
        try:
            srv.start()
            started = True
        except Exception as e:
            _fail(e, "The local orgs server did not start")
    if (changed or started) and _is_managed():
        _reconnect()
    _check_for_update()


def plugin_loaded():
    # After orgeverywhere's plugin_loaded has begun connecting: a server
    # started here is picked up by the reconnect that follows.
    sublime.set_timeout(lambda: _thread(_startup, "orgs-startup"), 500)
