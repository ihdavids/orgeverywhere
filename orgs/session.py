"""The sublime side of the orgs connection.

Everything a feature needs to talk to orgs:

    import OrgEverywhere.orgs.session as orgs

    orgs.connected()                 is a server up and logged in?
    orgs.Get()                       the OrgsClient, or None
    orgs.run(fn, on_done, ...)       run fn(client) on an orgs worker thread
                                     and hand the result back on the ui
                                     thread, reporting errors to the user
    orgs.heading_target(view, node)  (worker) save the buffer if it needs it,
                                     wait for the server to re-read it and
                                     look up the heading's hash
    orgs.sync_path(cl, filename)     (worker) save any open, modified buffer
                                     of a file the server is about to write

Connection life cycle: plugin_loaded calls start(). The url comes from the
orgsUrl setting or, when that is not set, from probing orgsDiscoverUrls. A
stored login (keychain) is reused; otherwise the user is asked to log in.
A later 401 logs back in silently with a remembered login, or asks the user
and fails the call that hit it (the user runs the command again once logged
in): nothing ever blocks waiting on a prompt.
"""
import concurrent.futures
import logging
import os
import re
import threading
import time

import sublime

import OrgEverywhere.asettings as sets
from . import client as C
from . import creds
from . import discovery
from . import events
from .paths import PathMap

log = logging.getLogger(__name__)

STATUS_KEY = "orgs"

# States
DISABLED = "disabled"
SEARCHING = "searching"
UNAVAILABLE = "unavailable"
LOGIN = "login"
CONNECTED = "connected"

_client = None
_listener = None
_state = DISABLED
_state_msg = ""
_ui_thread = None
_listeners = []       # callables(event dict) for server events
_state_listeners = [] # callables(state) on connect/disconnect

# orgs work runs here, not on sublime's one shared async thread: exports,
# reload waits and hash retries take seconds and would hold up every other
# plugin's async callbacks.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="orgs")


# ----------------------------------------------------------------------
# Settings

r"""
/* SDOC: Reference::Settings::Connection
*** orgs server connection
    Settings that decide whether and where OrgEverywhere finds its orgs server.

    | Setting              | Default                                             | Effect                                                                                                   |
    |----------------------+-----------------------------------------------------+----------------------------------------------------------------------------------------------------------|
    | =orgsEnabled=        | =true=                                              | =false= runs without a server: no connection is attempted and the cross file features have no database. |
    | =orgsUrl=            | not set                                             | The server url, no trailing slash. When set it is the only url tried.                                    |
    | =orgsDiscoverUrls=   | =["http://localhost:8010", "https://localhost:443"]= | Tried in order when =orgsUrl= is not set; the first orgs server that answers wins.                     |
    | =orgsNoVerify=       | =false=                                             | Turn off https certificate verification (self signed certificates).                                     |
    | =orgsCaFile=         | not set                                             | A certificate (e.g. the server's =server.crt=) to trust for https, instead of turning verification off. |
    | =orgsTimeout=        | =15=                                                | Seconds to wait for a server answer, and for the probe of =orgsUrl= (=orgsDiscoverUrls= probes wait 2s). |
    | =orgsRetrySeconds=   | =60=                                                | When no server is found (or it goes away), look again after this many seconds. =0= never retries.       |

    A server is recognised by its =/salt= route, the one route that answers
    without a login. Org: Set orgs server url writes =orgsUrl= for you.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Connection
*** orgs live updates and status bar
    | Setting          | Default | Effect                                                                                                                   |
    |------------------+---------+--------------------------------------------------------------------------------------------------------------------------|
    | =orgsEvents=     | =true=  | Listen to the server's change event stream, which keeps the agenda, ids, backlinks and the clock current when files change outside Sublime. |
    | =orgsShowStatus= | =true=  | Show the connection ("orgs: url (user)", "login needed", "connecting...", "offline") in the status bar of org views.        |
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Connection
*** orgsSaveBeforeServerEdits
    Default: =true=

    Commands that have the server edit a file (refile, copy, archive, agenda
    actions, clocking, updater plugins, exports that rewrite the file...)
    first make the server see the buffer: a modified buffer of the file is
    saved and OrgEverywhere waits for the server to re-read it. With =false=
    such a command refuses ("save the file first") instead of saving for you.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Connection
*** orgsPathMap
    Default: ={}=

    For a server on another machine or in a container, whose file paths
    differ from yours: a map from the server's path prefixes to the local
    prefixes holding the same files, e.g.
    ={ "/home/me/org": "~/Dropbox/org" }=. The longest matching prefix wins;
    =~= is expanded on the local side. A server on this machine needs no map.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Connection
*** orgsPreviewOnHighlight
    Default: =true=

    Quick panels that list headings from the server (query search, agenda
    query, contacts, birthdays, text search...) open the highlighted heading
    in a transient preview as you move through the list. =false= only jumps
    when you pick one.
EDOC */
"""

def enabled():
    return bool(sets.Get("orgsEnabled", True))


def _setting(name, default=None):
    return sets.Get(name, default)


def _store():
    prefer = _setting("orgsCredentialStore", "auto")
    return creds.best_store(os.path.join(sublime.cache_path(), "OrgEverywhere"), prefer)


# ----------------------------------------------------------------------
# State

def Get():
    """The client when the server is up and we are logged in, else None."""
    return _client if _state == CONNECTED else None


def client():
    """The client whatever the login state (None if there is no server)."""
    return _client


def connected():
    return _state == CONNECTED


def state():
    return _state, _state_msg


def listener():
    return _listener


def on_event(fn):
    """Register fn(event) for server events (reload, clockin, clockout).
    Remove it again with off_event in plugin_unloaded."""
    if fn not in _listeners:
        _listeners.append(fn)


def off_event(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def on_state(fn):
    """Register fn(state) for connection changes. Remove with off_state."""
    if fn not in _state_listeners:
        _state_listeners.append(fn)


def off_state(fn):
    if fn in _state_listeners:
        _state_listeners.remove(fn)


def _set_state(st, msg=""):
    global _state, _state_msg
    changed = st != _state
    _state, _state_msg = st, msg
    sublime.set_timeout(update_status, 0)
    if changed:
        for fn in list(_state_listeners):
            sublime.set_timeout(lambda fn=fn: _safe(fn, st), 0)


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception:
        log.exception("orgs: listener")


def describe():
    cl = _client
    url = cl.url if cl else (_setting("orgsUrl") or "")
    if _state == CONNECTED and cl:
        who = "no auth" if cl.auth_disabled else (cl.user or "")
        return "orgs: {0} ({1})".format(url, who)
    if _state == LOGIN:
        return "orgs: login needed ({0})".format(url)
    if _state == SEARCHING:
        return "orgs: connecting..."
    if _state == UNAVAILABLE:
        return "orgs: offline"
    return ""


def update_status(view=None):
    if not _setting("orgsShowStatus", True):
        return
    text = describe()
    views = [view] if view else [v for w in sublime.windows() for v in w.views()]
    for v in views:
        if v is None:
            continue
        sc = v.settings().get("syntax") or ""
        if "OrgEverywhere" in sc or (v.file_name() or "").endswith(".org"):
            if text:
                v.set_status(STATUS_KEY, text)
            else:
                v.erase_status(STATUS_KEY)


# ----------------------------------------------------------------------
# Threads

def on_ui_thread():
    return _ui_thread is not None and threading.get_ident() == _ui_thread


def _main(fn, *args):
    sublime.set_timeout(lambda: fn(*args), 0)


def run_on_main_and_wait(fn, timeout=30.0):
    """Run fn on the ui thread from a worker and return its result."""
    if on_ui_thread():
        return fn()
    box = {}
    done = threading.Event()

    def go():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e
        done.set()
    sublime.set_timeout(go, 0)
    if not done.wait(timeout):
        raise C.OrgsError("timed out waiting for the editor")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def report(e, what=None):
    """Show an orgs failure to the user."""
    prefix = (what + ": ") if what else ""
    if isinstance(e, C.Unavailable):
        sublime.status_message("orgs: " + prefix + str(e))
        _set_state(UNAVAILABLE, str(e))
        retry_later()
    elif isinstance(e, C.AuthRequired):
        sublime.status_message("orgs: " + prefix + "log in, then try again (Org: Log in to orgs)")
    elif isinstance(e, C.OrgsError):
        sublime.status_message("orgs: " + prefix + str(e).strip())
        log.warning("orgs: %s%s", prefix, e)
    else:
        sublime.status_message("orgs: " + prefix + "unexpected error, see console")
        log.error("orgs: %s%s", prefix, e, exc_info=e)


def run(fn, on_done=None, on_error=None, what=None, need_connection=True):
    """Run fn(client) on an orgs worker; on_done(result) back on the ui.

    Errors go to on_error(exc) on the ui thread if given, else are reported.
    Returns False (and tells the user) when there is no server to talk to.
    """
    cl = Get() if need_connection else _client
    if cl is None:
        if need_connection and _state == LOGIN:
            sublime.status_message("orgs: log in first (Org: Log in to orgs)")
            prompt_login()
        else:
            sublime.status_message("orgs: not connected" + (" - " + _state_msg if _state_msg else ""))
        if on_error:
            on_error(C.Unavailable("not connected"))
        return False

    def work():
        try:
            res = fn(cl)
        except Exception as e:
            if on_error:
                _main(on_error, e)
            else:
                _main(report, e, what)
            return
        if on_done:
            _main(on_done, res)
    _pool.submit(work)
    return True


def call(fn, default=None):
    """Synchronous call for code that cannot be made asynchronous (rare).

    Returns default when not connected or on error. Keep these fast: they
    block the ui while they run.
    """
    cl = Get()
    if cl is None:
        return default
    try:
        return fn(cl)
    except C.Unavailable as e:
        # Stop paying a connect timeout on every call; retry_later brings
        # the connection back when the server returns.
        _set_state(UNAVAILABLE, str(e))
        retry_later()
        return default
    except Exception as e:
        if not isinstance(e, C.OrgsError):
            log.error("orgs call: %s", e, exc_info=e)
        else:
            log.info("orgs call failed: %s", e)
        return default


# ----------------------------------------------------------------------
# Login

_prompt_open = False
_waiters = []           # on_done callbacks for the open prompt


class MaskedInput:
    """An input panel whose text shows as dots.

    Sublime has no password field. Each change is diffed against what we
    know and the panel text is replaced with bullets. Handles typing, delete
    and paste at the end, and replaces the whole value on anything fancier.
    """
    BULLET = "•"

    def __init__(self, window, caption, on_done, on_cancel):
        self.value = ""
        self.window = window
        self.on_done_cb = on_done
        self.on_cancel_cb = on_cancel
        self.updating = False
        self.panel = window.show_input_panel(caption, "", self.done, self.changed, self.cancel)

    def changed(self, text):
        if self.updating:
            return
        shown = self.BULLET * len(self.value)
        if text == shown:
            return
        # Keep the part that is still bullets, take the rest as typed.
        common = 0
        for a in text:
            if a == self.BULLET and common < len(self.value):
                common += 1
            else:
                break
        typed = text[common:].replace(self.BULLET, "")
        self.value = self.value[:common] + typed
        self.updating = True
        try:
            self.panel.run_command("select_all")
            self.panel.run_command("insert", {"characters": self.BULLET * len(self.value)})
        finally:
            self.updating = False

    def done(self, text):
        v, self.value = self.value, ""
        self.on_done_cb(v)

    def cancel(self):
        self.value = ""
        self.on_cancel_cb()


def _finish_prompt(ok):
    global _prompt_open
    _prompt_open = False
    cl = _client
    if not ok and cl is not None:
        cl.note_login_cancelled()
    waiters = list(_waiters)
    del _waiters[:]
    for w in waiters:
        _safe(w, ok)


def prompt_login(window=None, on_done=None, reason=None):
    """Ask for a user name and password (ui thread). on_done(ok)."""
    global _prompt_open
    cl = _client
    if cl is None:
        sublime.status_message("orgs: no server to log in to")
        if on_done:
            on_done(False)
        return
    if on_done:
        _waiters.append(on_done)
    if _prompt_open:
        # A prompt is already up; its answer goes to every waiter.
        return
    _prompt_open = True
    try:
        window = window or sublime.active_window()
        if window is None:
            raise C.OrgsError("no window to ask in")
        # Never touch the secret store here: it can block on a locked keychain.
        default_user = cl.cached_user() or _setting("orgsUser", "") or ""

        def got_user(user):
            user = user.strip()
            if not user:
                _finish_prompt(False)
                return
            MaskedInput(window, "orgs password for {0}@{1}:".format(user, cl.url),
                        lambda pw: got_password(user, pw), lambda: _finish_prompt(False))

        def got_password(user, pw):
            def work():
                try:
                    cl.login(user, pw)
                except C.AuthRequired:
                    _main(sublime.error_message, "orgs login failed: wrong user name or password.")
                    _main(_finish_prompt, False)
                    return
                except C.OrgsError as e:
                    _main(sublime.error_message, "orgs login failed:\n\n" + str(e))
                    _main(_finish_prompt, False)
                    return
                _set_state(CONNECTED)
                _start_listener()
                _main(sublime.status_message, "orgs: logged in as " + user)
                _main(_finish_prompt, True)
            # Its own thread: the request that needed the login may be
            # occupying a worker.
            threading.Thread(target=work, name="orgs-login", daemon=True).start()

        caption = "orgs user for {0}".format(cl.url)
        if reason:
            caption = reason + " - " + caption
        window.show_input_panel(caption + ":", default_user, got_user, None, lambda: _finish_prompt(False))
    except Exception as e:
        log.warning("orgs: could not show the login prompt: %s", e)
        _finish_prompt(False)


def _auth_needed(cl, reason):
    """OrgsClient.on_auth_needed. Never waits on the prompt: it shows it and
    lets the call fail, so no thread (and no lock) is held while the user
    types. A remembered login has already been tried by the client."""
    _set_state(LOGIN, reason)
    _main(prompt_login, None, None, reason)
    return False


def _unauthorized(cl):
    """The event stream got a 401: the session is gone server side."""
    if _client is cl and _state == CONNECTED:
        _set_state(LOGIN, "session expired")
        connect(interactive=False)


# ----------------------------------------------------------------------
# Connecting

_retry_timer = None


def retry_later(seconds=None):
    global _retry_timer
    if seconds is None:
        seconds = _setting("orgsRetrySeconds", 60)
    if not seconds or _retry_timer is not None:
        return

    def again():
        global _retry_timer
        _retry_timer = None
        if _state in (UNAVAILABLE, SEARCHING):
            connect(interactive=False)
    _retry_timer = threading.Timer(float(seconds), again)
    _retry_timer.daemon = True
    _retry_timer.start()


def _make_client(url):
    global _client
    if _client is not None:
        _client.close()
    _client = C.OrgsClient(
        url,
        verify=not _setting("orgsNoVerify", False),
        cafile=_setting("orgsCaFile", None) or None,
        timeout=float(_setting("orgsTimeout", 15)),
        store=_store(),
        remember=bool(_setting("orgsRememberLogin", True)),
        path_map=PathMap(_setting("orgsPathMap", {}) or {}),
        on_auth_needed=_auth_needed)
    return _client


def _start_listener():
    global _listener
    if _client is None or not _setting("orgsEvents", True):
        return
    if _listener is not None and _listener.client is _client:
        _listener.start()
        return
    if _listener is not None:
        _listener.stop()
    _listener = events.EventListener(_client, _dispatch_event, on_unauthorized=_unauthorized)
    _listener.start()


def _dispatch_event(ev):
    for fn in list(_listeners):
        sublime.set_timeout(lambda fn=fn: _safe(fn, ev), 0)


def connect(interactive=True, on_done=None):
    """Find the server, then make sure we are logged in. Async."""
    if not enabled():
        _set_state(DISABLED)
        return

    def done(ok):
        if on_done:
            _main(on_done, ok)

    def work():
        _set_state(SEARCHING)
        url = (_setting("orgsUrl", None) or "").strip()
        verify = not _setting("orgsNoVerify", False)
        cafile = _setting("orgsCaFile", None) or None
        if url:
            r = discovery.probe(url, verify, cafile, timeout=float(_setting("orgsTimeout", 15)))
            results = [r]
            url = r.url if r.ok else None
        else:
            url, results = discovery.discover(_setting("orgsDiscoverUrls", None), verify, cafile)
        if not url:
            why = "; ".join("{0}: {1}".format(r.url, r.msg) for r in results)
            _set_state(UNAVAILABLE, why)
            log.info("orgs: no server found (%s)", why)
            if interactive:
                _main(sublime.message_dialog, "Could not find an orgs server.\n\n" + why.replace("; ", "\n") +
                      "\n\nSet orgsUrl in the OrgEverywhere settings to point at your server.")
            retry_later()
            done(False)
            return
        cl = _client if (_client is not None and _client.url == url.rstrip('/') and not _client.closed) \
            else _make_client(url)
        try:
            st = cl.check_session()
        except C.OrgsError as e:
            _set_state(UNAVAILABLE, str(e))
            retry_later()
            done(False)
            return
        if st == "login":
            user, chash = cl.remembered_hash()
            if user and chash:
                try:
                    cl.login_hash(user, chash)
                    st = "ok"
                except C.OrgsError as e:
                    log.info("orgs: remembered login refused: %s", e)
        if st == "ok":
            _set_state(CONNECTED)
            _start_listener()
            log.info("orgs: connected to %s", cl.url)
            done(True)
            return
        _set_state(LOGIN, "login required")
        if interactive or _setting("orgsPromptLoginOnStartup", True):
            _main(prompt_login, None, on_done, None)
        else:
            done(False)

    def guarded():
        try:
            work()
        except Exception as e:
            # Never leave the state at SEARCHING with nothing coming back.
            log.error("orgs: connect failed: %s", e, exc_info=e)
            _set_state(UNAVAILABLE, str(e))
            retry_later()
            done(False)
    _pool.submit(guarded)


def start():
    """Called from plugin_loaded."""
    global _ui_thread
    _ui_thread = threading.get_ident()
    if not enabled():
        _set_state(DISABLED)
        return
    connect(interactive=False)


def stop():
    """Called from plugin_unloaded. This module is not reloaded with the
    plugin, so everything the old plugin modules registered goes too."""
    global _listener, _client, _retry_timer, _state, _state_msg
    if _listener is not None:
        _listener.stop()
        _listener = None
    if _client is not None:
        _client.close()
        _client = None
    if _retry_timer is not None:
        _retry_timer.cancel()
        _retry_timer = None
    del _listeners[:]
    del _state_listeners[:]
    _state, _state_msg = DISABLED, ""


def logout(forget=True):
    if _client is not None:
        _client.logout(forget=forget)
    if _listener is not None:
        _listener.stop()
    _set_state(LOGIN, "logged out")


# ----------------------------------------------------------------------
# Making sure the server sees what the editor sees.

def _views_for(filename):
    """Every open view of filename, in every window (ui thread)."""
    out = []
    for w in sublime.windows():
        for v in w.views():
            fn = v.file_name()
            if fn and PathMap.same(fn, filename):
                out.append(v)
    return out


def _save_and_wait(cl, filename, views, timeout):
    lst = _listener
    before = lst.reload_count(filename) if lst else 0
    saved_at = time.time()

    def save():
        for v in views:
            if v.is_dirty():
                v.run_command("save")
        return [v for v in views if v.is_dirty()]
    still_dirty = run_on_main_and_wait(save)
    if still_dirty:
        raise C.OrgsError("could not save {0}; save it and try again".format(os.path.basename(filename)))
    if not (lst and lst.wait_for_reload(filename, before, timeout, since=saved_at)):
        # No event stream: give the watcher a moment.
        time.sleep(0.6)


def _outline(fi):
    return [n.start_row for n in fi.org[1:]] if fi and fi.org else []


def _server_current(cl, filename, rows):
    """Does the server's parse of filename have headings at these rows?"""
    try:
        heads = cl.headings(filename)
    except C.OrgsError:
        return False
    return [h.get("LineNum") for h in heads or []] == rows


def _wait_current(cl, filename, rows, timeout=2.0):
    """The heading rows are a cheap fingerprint of the file. A buffer saved
    a moment ago (not dirty now) may not be re-read yet; poll until the
    server's outline matches ours."""
    end = time.time() + timeout
    while True:
        if _server_current(cl, filename, rows):
            return True
        if time.time() >= end:
            return False
        time.sleep(0.2)


def sync_file(view, timeout=2.0, cl=None):
    """(worker) Make sure the server has the buffer's current text.

    Saves a modified buffer and waits for the server to re-read it. Returns
    the file name, or None for a buffer that has no file.
    """
    import OrgEverywhere.orgdb as db
    fn = run_on_main_and_wait(lambda: view.file_name())
    if not fn:
        return None
    cl = cl or Get()
    dirty = run_on_main_and_wait(lambda: view.is_dirty())
    if dirty:
        if not _setting("orgsSaveBeforeServerEdits", True):
            raise C.OrgsError("save the file first (orgsSaveBeforeServerEdits is off)")
        _save_and_wait(cl, fn, [view], timeout)
    if cl is not None:
        rows = run_on_main_and_wait(lambda: _outline(db.Get().FindInfo(view)))
        if not _wait_current(cl, fn, rows, timeout):
            log.info("orgs: server outline of %s still differs from the buffer", fn)
    return fn


def sync_path(cl, filename, timeout=2.0):
    """(worker) Before the server writes filename: save any open modified
    buffer of it (in any window) and wait for the server to re-read it.
    Otherwise the server's write is not loaded into the dirty buffer and the
    next save of that buffer silently undoes it."""
    if not filename:
        return
    views = run_on_main_and_wait(lambda: _views_for(filename))
    if not views:
        return
    if run_on_main_and_wait(lambda: any(v.is_dirty() for v in views)):
        if not _setting("orgsSaveBeforeServerEdits", True):
            raise C.OrgsError("{0} has unsaved changes; save it first".format(os.path.basename(filename)))
        _save_and_wait(cl, filename, views, timeout)


def file_text_current(cl, view, timeout=2.0):
    """(worker) For whole file server writes (reformat, babel, dynamic blocks):
    sync, then require the file on disk to be exactly the buffer."""
    fn = sync_file(view, timeout, cl)
    if not fn:
        raise C.OrgsError("this buffer has no file; save it first")
    text = run_on_main_and_wait(lambda: view.substr(sublime.Region(0, view.size())))
    if cl.file_text(fn).replace("\r\n", "\n") != text.replace("\r\n", "\n"):
        raise C.OrgsError("the file on disk differs from the buffer; save and try again")
    return fn


# ----------------------------------------------------------------------
# Targets: getting from a place in a buffer to something the server knows.

_LINK = re.compile(r"\[\[([^\]]*)\](?:\[([^\]]*)\])?\]")


def _norm_headline(s):
    # orgparse shows links by description, go-org keeps them raw.
    s = _LINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), s or "")
    return " ".join(s.split())


class DuplicateHeading(C.OrgsError):
    pass


def heading_hash(cl, filename, row, heading=None, tries=6):
    """(worker) The hash of the heading at row.

    Checked by asking the server where that hash lives: it must be this
    heading's row in this file. A file the server is still re-reading
    retries for a little while. Two headings with the same outline path
    share a hash on the server, which would send the edit to the other one:
    that is refused.
    """
    last = None
    for i in range(tries):
        h = cl.lookup_hash(filename, row)
        if h:
            t = cl.by_hash(h)
            if t and PathMap.same(t.get("Filename"), filename):
                if t.get("LineNum") == row:
                    if heading is None or _norm_headline(t.get("Headline")) == _norm_headline(heading):
                        return h
                elif heading is not None and _norm_headline(t.get("Headline")) == _norm_headline(heading):
                    last = t
        time.sleep(0.25 * (i + 1))
    if last is not None:
        raise DuplicateHeading(
            "'{0}' has a twin with the same outline path at line {1}; give one of them an ID".format(
                heading, last.get("LineNum", 0) + 1))
    raise C.OrgsError("the server does not see heading '{0}' at line {1} of {2} (yet?)".format(
        heading or "", row + 1, os.path.basename(filename)))


def heading_target(view, node=None, row=None):
    """(worker) Sync the buffer and return (hash, Target dict, node).

    node: the orgparse node to target (default: the heading at the cursor).
    """
    import OrgEverywhere.orgdb as db
    cl = Get()
    if cl is None:
        raise C.Unavailable("not connected to orgs")
    fn = sync_file(view, cl=cl)
    if not fn:
        raise C.OrgsError("this buffer has no file; save it first")
    if node is None:
        node = run_on_main_and_wait(lambda: db.Get().AtInView(view) if row is None else db.Get().At(view, row))
    if node is None or node.is_root():
        raise C.OrgsError("not on a heading")
    h = heading_hash(cl, fn, node.start_row, node.heading)
    return h, C.target(cl.remote(fn), h, "hash"), node


def file_target(cl, filename):
    return C.target(cl.remote(filename), "", "file")


def olp_target(cl, filename, olp):
    return C.target(cl.remote(filename), "::".join(olp), "file+olp")
