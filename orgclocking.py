import sublime
import sublime_plugin
import datetime
import os
import time
# from OrgEverywhere.orgparse.sublimenode import *
import logging
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgproperties as props
from OrgEverywhere.orgparse.node import *
import copy
import yaml
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as oui
from OrgEverywhere.orgs.client import parse_time
from OrgEverywhere.orgs.paths import PathMap

log = logging.getLogger(__name__)

STATUS_KEY = "orgclock"

# When orgs is connected the server's /clock is the running clock: clock in
# and out go to the server, which writes the CLOCK line into the heading's
# drawer on clock out. ClockManager.Clock then mirrors /clock and is never
# written to the local yaml file. Without a server the old local clock
# (Packages/User/OrgEverywhere_Clocks.yaml + buffer edits) is used.


class ClockManager:
    Clock = None
    FromServer = False   # Clock mirrors the server's /clock
    Fetched = 0.0        # time.time() of the last /clock answer
    Refreshing = 0.0     # time.time() an EnsureServer refresh was started

    @staticmethod
    def ClockInRecord(file, onode, dt):
        # parentHeading = ""
        # if (onode.parent and type(onode.parent) != node.OrgRootNode):
        #     parentHeading = onode.parent.heading
        ClockManager.Clock = {
            'file': file.filename,
            'start': dt,
            'heading': onode.get_locator()
        }
        ClockManager.SaveClock()

    @staticmethod
    def ClockRunning():
        if orgs.connected():
            ClockManager.EnsureServer()
        return ClockManager.Clock is not None

    @staticmethod
    def ReadLocalClock():
        """The offline clock in the yaml file, or None."""
        cpath = ClockManager.ClockPath()
        if not os.path.isfile(cpath):
            return None
        try:
            with open(cpath, 'r') as stream:
                res = yaml.load(stream, Loader=yaml.SafeLoader)
        except Exception as e:
            log.warning("could not read the offline clock: %s", e)
            return None
        return res if isinstance(res, dict) and res.get("file") else None

    @staticmethod
    def FormatClock(now):
        return now.strftime("[%Y-%m-%d %a %H:%M]")

    @staticmethod
    def FormatDuration(d):
        hours = d.seconds / 3600
        minutes = (d.seconds / 60) % 60
        return "{0:02d}:{1:02d}".format(int(hours), int(minutes))

    @staticmethod
    def ClockPath():
        return os.path.join(sublime.packages_path(), "User", "OrgEverywhere_Clocks.yaml")

    @staticmethod
    def SaveClock():
        f = open(ClockManager.ClockPath(), "w")
        yaml.dump(ClockManager.Clock, f)
        f.close()

    @staticmethod
    def LoadClock():
        if ClockManager.FromServer and orgs.connected():
            # The server owns the clock; the yaml file is only the offline clock.
            return
        ClockManager.FromServer = False
        ClockManager.Clock = None
        cpath = ClockManager.ClockPath()
        if (os.path.isfile(cpath)):
            stream = open(cpath, 'r')
            res = yaml.load(stream, Loader=yaml.SafeLoader)
            if res is not None and isinstance(res, dict):
                ClockManager.Clock = res
            stream.close()

    # --- The server clock ---------------------------------------------------

    @staticmethod
    def ApplyServer(res):
        """Take a /clock answer as the running clock."""
        ClockManager.Clock = ServerClockRecord(res)
        ClockManager.FromServer = True
        ClockManager.Fetched = time.time()
        ClockManager.Refreshing = 0.0
        UpdateStatus()

    @staticmethod
    def RefreshServer(on_done=None):
        """Ask the server for the running clock (async)."""
        if not orgs.connected():
            return

        def done(res):
            ClockManager.ApplyServer(res)
            if on_done:
                on_done()

        def failed(e):
            ClockManager.Refreshing = 0.0
            log.debug("orgs: could not read the clock: %s", e)
        orgs.run(lambda c: c.clock(), done, on_error=failed, what="clock")

    @staticmethod
    def EnsureServer():
        """Refresh a stale server clock in the background. Callers get the
        cached clock now (clock events keep it current)."""
        if ClockManager.FromServer and (time.time() - ClockManager.Fetched) < PollSeconds():
            return
        if ClockManager.Refreshing and (time.time() - ClockManager.Refreshing) < 30:
            return
        ClockManager.Refreshing = time.time()
        ClockManager.RefreshServer()

    # --- Clocking ------------------------------------------------------------

    @staticmethod
    def ClockIn(view, onDone=None):
        if orgs.connected():
            # A clock left open from offline is closed first, as offline does.
            ClockManager.CloseLocalClock(view)
            ServerClockIn(view, onDone)
            return
        if (ClockManager.ClockRunning()):
            ClockManager.ClockOut(view)
        # Handle clock already running
        node = db.Get().AtInView(view)
        if (node):
            file = db.Get().FindInfo(view)
            if (file):
                dt = datetime.datetime.now()
                ClockManager.ClockInRecord(file, node, dt)
                if (sets.Get("clockInPropertyBlock", False)):
                    props.AddProperty(view, node, "CLOCK", ClockManager.FormatClock(dt) + "--")
                else:
                    props.AddLogbook(view, node, "CLOCK:", ClockManager.FormatClock(dt) + "--")
        UpdateStatus()
        evt.EmitIf(onDone)

    @staticmethod
    def UpdateClockStart(view):
        if (not ClockManager.ClockRunning()):
            log.error("Clock not running, nothing to update")
            return
        # Eventually we want to navigate to this node
        # rather than doing this.
        if ClockManager.Clock is not None:
            node = db.Get().FindNode(ClockManager.Clock["file"], ClockManager.Clock["heading"])
            newStart = None
            if (node):
                tview = view.window().open_file(ClockManager.Clock["file"], sublime.ENCODED_POSITION)
                if (sets.Get("clockInPropertyBlock", False)):
                    val = props.GetProperty(tview, node, "CLOCK")
                else:
                    val = props.GetLogbook(tview, node, r"CLOCK:")
                if val:
                    clock = OrgDateClock.from_str(val)
                    if clock:
                        newStart = clock.start
            if newStart:
                file = db.Get().ParseFile(ClockManager.Clock["file"])
                ClockManager.ClockInRecord(file, node, newStart)

    @staticmethod
    def ClockOut(view, onDone=None):
        if orgs.connected():
            # A clock started offline (yaml file + open CLOCK line) is closed
            # the local way; the server does not know about it.
            if ClockManager.CloseLocalClock(view):
                sublime.status_message("Closed the clock started while offline")
            ServerClockOut(view, onDone)
            return
        if (not ClockManager.ClockRunning()):
            evt.EmitIf(onDone)
            return
        if ClockManager.Clock is not None:
            if ClockManager.CloseClockLine(view, ClockManager.Clock):
                ClockManager.ClearClock()
                view.run_command("save")
        UpdateStatus()
        evt.EmitIf(onDone)

    @staticmethod
    def CloseLocalClock(view):
        """(connected) Close an offline clock if the yaml file has one."""
        clock = ClockManager.ReadLocalClock()
        if clock is None:
            return False
        if not ClockManager.CloseClockLine(view, clock):
            return False
        ClockManager.ClearLocalClock()
        return True

    @staticmethod
    def CloseClockLine(view, clock):
        """Close the open CLOCK line of a local clock record in its buffer."""
        # Eventually we want to navigate to this node
        # rather than doing this.
        node = db.Get().FindNode(clock["file"], clock["heading"])
        if not node:
            log.error("Failed to clock out, couldn't find node")
            return False
        end = datetime.datetime.now()
        start = clock["start"]
        duration = end - start
        # Should we keep clocking entries less than a minute?
        shouldKeep = sets.Get("clockingSubMinuteClocks", True)
        tview = view.window().open_file(clock["file"], sublime.ENCODED_POSITION)
        if (not shouldKeep and duration.seconds < 60):
            if (sets.Get("clockInPropertyBlock", False)):
                props.RemoveProperty(tview, node, "CLOCK")
            else:
                props.RemoveLogbook(tview, node, r"CLOCK:")
        else:
            if (sets.Get("clockInPropertyBlock", False)):
                props.UpdateProperty(tview, node, "CLOCK", ClockManager.FormatClock(start) + "--" + ClockManager.FormatClock(end) + " => " + ClockManager.FormatDuration(duration))
            else:
                props.UpdateLogbook(tview, node, "CLOCK:", ClockManager.FormatClock(start) + "--" + ClockManager.FormatClock(end) + " => " + ClockManager.FormatDuration(duration))
        view.window().focus_view(view)
        tview.run_command("save")
        return True

    @staticmethod
    def ClearLocalClock():
        cpath = ClockManager.ClockPath()
        if (os.path.isfile(cpath)):
            os.remove(cpath)

    @staticmethod
    def ClearClock():
        ClockManager.Clock = None
        ClockManager.FromServer = False
        ClockManager.ClearLocalClock()
        UpdateStatus()

    @staticmethod
    def GetActiveClockFile():
        if (not ClockManager.ClockRunning()):
            return None
        return ClockManager.Clock["file"] if ClockManager.Clock is not None else None

    @staticmethod
    def GetActiveClockAt():
        if (not ClockManager.ClockRunning()):
            return None
        c = ClockManager.Clock
        if c is not None:
            node = None
            if c.get("olp"):
                node = FindByOlp(db.Get().ParseFile(c["file"]), c["olp"])
            if node is None and c.get("heading"):
                node = db.Get().FindNode(c["file"], c["heading"])
            if (node):
                return node.start_row
        return None


# --- Server helpers -----------------------------------------------------------

def PollSeconds():
    return max(15, int(sets.Get("orgsClockPollSeconds", 120) or 120))


def ServerClockRecord(res):
    """Our clock record for a /clock answer, None when no clock runs."""
    if not res or not res.get("Active"):
        return None
    tgt = res.get("Target") or {}
    start = parse_time((res.get("Time") or {}).get("Start"))
    if start is not None and start.tzinfo is not None:
        # Local naive time, like the offline clock.
        start = start.astimezone().replace(tzinfo=None)
    olp = []
    if tgt.get("Type") == "file+olp":
        olp = [p for p in (tgt.get("Id") or "").split("::") if p]
    return {
        'file': tgt.get("Filename"),
        'start': start,
        'heading': ":".join(olp),
        'olp': olp,
        'target': tgt,
    }


def FindByOlp(fi, olp):
    """The node of a parsed file at an outline path."""
    if fi is None or not olp:
        return None
    cur = fi.org[0]
    for name in olp:
        nxt = None
        for c in cur.children:
            if c.heading.strip() == name.strip():
                nxt = c
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def ServerTargetLine(cl, tgt):
    """(worker) 0 based line of a server Target, or 0 when it cannot be found."""
    typ = tgt.get("Type")
    if typ == "hash":
        t = cl.by_hash(tgt.get("Id"))
        return t.get("LineNum", 0) if t else 0
    if typ in ("id", "customid"):
        t = cl.by_id(tgt.get("Id"))
        return t.get("LineNum", 0) if t else 0
    if typ == "file+olp":
        olp = [p.strip() for p in (tgt.get("Id") or "").split("::") if p]
        stack = []
        for t in cl.headings(tgt.get("Filename")):
            lvl = max(1, int(t.get("Level") or 1))
            stack = stack[:lvl - 1] + [(t.get("Headline") or "").strip()]
            if stack == olp:
                return t.get("LineNum", 0)
    return 0


def FindOpenView(filename):
    if not filename:
        return None
    for w in sublime.windows():
        v = w.find_open_file(filename)
        if v is not None:
            return v
    return None


def ServerClockIn(view, onDone=None):
    def work(cl):
        h, tgt, node = orgs.heading_target(view)
        # A running clock is clocked out first: the server writes its file.
        cur = cl.clock()
        if cur.get("Active"):
            orgs.sync_path(cl, (cur.get("Target") or {}).get("Filename"))
        cl.clock_in(tgt)
        return (node.heading, cl.clock())

    def done(r):
        heading, res = r
        ClockManager.ApplyServer(res)
        sublime.status_message("orgs: clocked in to " + heading)
        evt.EmitIf(onDone)

    def failed(e):
        orgs.report(e, "clock in")
        evt.EmitIf(onDone)
    orgs.run(work, done, on_error=failed, what="clock in")


def ServerClockOut(view, onDone=None):
    def work(cl):
        cur = cl.clock()
        if not cur.get("Active"):
            return (False, cur)
        # The server writes the CLOCK line into that file: get unsaved edits
        # to it on disk first.
        orgs.sync_path(cl, (cur.get("Target") or {}).get("Filename"))
        cl.clock_out()
        return (True, cl.clock())

    def done(r):
        was, res = r
        ClockManager.ApplyServer(res)
        sublime.status_message("orgs: clocked out" if was else "orgs: no clock is running")
        evt.EmitIf(onDone)

    def failed(e):
        orgs.report(e, "clock out")
        evt.EmitIf(onDone)
    orgs.run(work, done, on_error=failed, what="clock out")


# --- Status bar -----------------------------------------------------------------

def FormatElapsed(d):
    mins = max(0, int(d.total_seconds() // 60))
    return "{0}:{1:02d}".format(mins // 60, mins % 60)


def StatusText():
    c = ClockManager.Clock
    if not c:
        return ""
    if c.get("olp"):
        name = c["olp"][-1]
    else:
        name = (c.get("heading") or "").split(":")[-1]
    start = c.get("start")
    if isinstance(start, datetime.datetime):
        return "Clock: {0} ({1})".format(name, FormatElapsed(datetime.datetime.now() - start))
    return "Clock: " + name


def UpdateStatus():
    if not sets.Get("clockShowInStatus", True):
        return
    text = StatusText()
    for w in sublime.windows():
        for v in w.views():
            fn = v.file_name() or ""
            if fn.endswith(".org") or "OrgEverywhere" in (v.settings().get("syntax") or ""):
                if text:
                    v.set_status(STATUS_KEY, text)
                else:
                    v.erase_status(STATUS_KEY)


# Bumped on unload: a timer loop of an older load of this module stops.
_tickGen = 0


def _Tick(gen):
    if gen != _tickGen:
        return
    if orgs.connected() and (time.time() - ClockManager.Fetched) >= PollSeconds():
        # The server can stop the clock by itself (autoclockout) without an
        # event, so look now and then.
        ClockManager.RefreshServer()
    else:
        UpdateStatus()
    sublime.set_timeout(lambda: _Tick(gen), 30000)


def OnServerEvent(ev):
    kind = ev.get("kind")
    if kind in ("clockin", "clockout", "hello"):
        ClockManager.RefreshServer()
    elif kind == "reload" and ClockManager.Clock and ev.get("file"):
        # A clock out writes the clocked file; so does autoclockout.
        if PathMap.same(ev.get("file"), ClockManager.Clock.get("file") or ""):
            ClockManager.RefreshServer()


def OnConnectionChanged(state):
    if state == orgs.CONNECTED:
        ClockManager.RefreshServer()
    elif ClockManager.FromServer:
        ClockManager.FromServer = False
        ClockManager.LoadClock()
        UpdateStatus()


# Load the clock cache.
def Load():
    global _tickGen
    ClockManager.LoadClock()
    orgs.on_event(OnServerEvent)
    orgs.on_state(OnConnectionChanged)
    _tickGen += 1
    gen = _tickGen
    sublime.set_timeout(lambda: _Tick(gen), 30000)


def plugin_unloaded():
    global _tickGen
    _tickGen += 1
    orgs.off_event(OnServerEvent)
    orgs.off_state(OnConnectionChanged)

# Clock in a task


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Clock In
    Command: =org_clock_in=  Keys: =alt+o c i=; NeoVintageous normal mode: =space c i=  Args: =onDone=

    Starts the clock on the heading at the cursor, clocking out a running
    clock first. With the orgs server the clock lives on the server, shared
    with every other orgs client (worg, the cli, other editors); the server
    writes the CLOCK line on clock out (open, modified buffers of the files
    involved are saved first). A clock left running from offline is closed
    first.

    Without a server the clock is local: an open =CLOCK: [start]--= line is
    added to the heading's =LOGBOOK= drawer (or its property drawer with
    =clockInPropertyBlock=) and the clock is remembered in
    =Packages/User/OrgEverywhere_Clocks.yaml=.

    The running clock and its elapsed time show in the status bar of org
    views (=clockShowInStatus=).
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Clocking
*** Clocking settings
    | Setting                   | Default | Effect                                                                                                  |
    |---------------------------+---------+---------------------------------------------------------------------------------------------------------|
    | =clockShowInStatus=       | =true=  | Show the running clock ("Clock: heading (h:mm)") in the status bar of org views.                       |
    | =orgsClockPollSeconds=    | =120=   | How often (seconds, at least 15) to ask the orgs server for its clock, which it can stop by itself (e.g. its autoclockout plugin). Clock events update it in between. |
    | =clockingSubMinuteClocks= | =true=  | Offline clock only: =false= removes a clock shorter than a minute on clock out instead of writing it.  |
    | =clockInPropertyBlock=    | =false= | Offline clock and Org Recalculate Clock: keep CLOCK lines in the property drawer instead of =LOGBOOK=. |
EDOC */
"""

class OrgClockInCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        ClockManager.LoadClock()
        ClockManager.ClockIn(self.view, onDone)

# Clock out a task


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Clock Out
    Command: =org_clock_out=  Keys: =alt+o c o=; NeoVintageous normal mode: =space c o=  Args: =onDone=

    Stops the running clock, wherever it is. With the orgs server the
    server closes the clock and writes =CLOCK: [start]--[end] => h:mm= in
    the heading's drawer; a clock started while offline is closed locally
    first. Says so when no clock is running.

    Offline the open CLOCK line is closed in its file (opened if needed) and
    the file saved; with =clockingSubMinuteClocks= =false= a clock shorter
    than a minute is removed instead.
EDOC */
"""

class OrgClockOutCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        ClockManager.LoadClock()
        ClockManager.ClockOut(self.view, onDone)

# Jump to an active clock


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Jump To Clock
    Command: =org_jump_to_clock=  Keys: none  Args: =onDone=

    Opens the heading the running clock is on (asking the server for it
    when connected). Says "No active clock" when none runs.
EDOC */
"""

class OrgJumpToClockCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        window = self.view.window()
        if orgs.connected():
            def work(cl):
                res = cl.clock()
                if not res.get("Active"):
                    return (None, 0, res)
                tgt = res.get("Target") or {}
                return (tgt.get("Filename"), ServerTargetLine(cl, tgt), res)

            def done(r):
                filename, line, res = r
                ClockManager.ApplyServer(res)
                if filename:
                    oui.goto(window, filename, line)
                else:
                    sublime.status_message("No active clock")
                evt.EmitIf(onDone)
            orgs.run(work, done, what="jump to clock")
            return
        ClockManager.LoadClock()
        at = ClockManager.GetActiveClockAt()
        filename = ClockManager.GetActiveClockFile()
        if at is not None and filename:
            path = "{0}:{1}".format(filename, at + 1)
            window.open_file(path, sublime.ENCODED_POSITION)
        else:
            sublime.status_message("No active clock")
        evt.EmitIf(onDone)

# Clear the currently running clock (if there is one)


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Clear Clock
    Command: =org_clear_clock=  Keys: none

    Offline only: forgets the running local clock without writing an end
    time (the open CLOCK line stays in the file). With the orgs server it
    refuses: the server clock is stopped with Org Clock Out.
EDOC */
"""

class OrgClearClockCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if orgs.connected():
            sublime.status_message("orgs: the server clock can only be stopped with Clock Out")
            return
        ClockManager.ClearClock()

# Update the currently running command based on an open clock manual update


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Update Active Clock
    Command: =org_update_clock=  Keys: none

    Offline only: after you edit the start time of the open CLOCK line by
    hand, reads it back as the running clock's start. With the orgs server
    it refuses (the server keeps the start; there is no open line).
EDOC */
"""

class OrgUpdateClockCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if orgs.connected():
            # The server keeps the start time itself; there is no open CLOCK
            # line in the file to read it back from.
            sublime.status_message("orgs: the server clock has no open CLOCK line to update from")
            return
        ClockManager.UpdateClockStart(self.view)

# Recalculate all the clock values in a node (Crtl-c Ctrl-c on a clock entry)


r"""
/* SDOC: Reference::Commands::Clocking
*** Org Recalculate Clock
    Command: =org_recalculate_clock=  Keys: none

    Rewrites every CLOCK line of the heading at the cursor, recomputing the
    =h:mm= duration of each from its start and end (after you edit a time
    by hand). Works on the buffer; no server needed. Uses the drawer chosen
    by =clockInPropertyBlock=. The cursor must be in a heading's section.
EDOC */
"""

class OrgRecalculateClockCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        node = db.Get().AtInView(self.view)
        clockList = copy.copy(node.clock)
        log.debug(str(clockList))
        log.debug(str(len(clockList)))
        if (sets.Get("clockInPropertyBlock", False)):
            props.RemoveAllInstances(self.view, node, "CLOCK")
        else:
            props.RemoveAllLogbookInstances(self.view, node, r"^\s*CLOCK:")
        log.debug(str(clockList))
        log.debug(str(len(clockList)))
        for clock in clockList:
            # File is reloaded have to regrab node
            node = db.Get().At(self.view, node.start_row)
            if (sets.Get("clockInPropertyBlock", False)):
                props.AddProperty(self.view, node, "CLOCK", clock.format_clock_str())
            else:
                props.AddLogbook(self.view, node, "CLOCK:", clock.format_clock_str())
