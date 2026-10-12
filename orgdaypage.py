import sublime
import sublime_plugin
import datetime
import re
import os
import logging
import getpass
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgextension as ext
import OrgEverywhere.orgparse.date as orgdate
import OrgEverywhere.orgdb as db
import OrgEverywhere.orgagendanode as agnode
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.client as orgsclient
from OrgEverywhere.orgs.paths import PathMap
import time

log = logging.getLogger(__name__)

# I think I will model this feature after org-roam dailies.
# olBc has not really gotten back to me on the subject and I think
# dailies makes a lot of sense.

def dayPageGetToday():
    dt     = datetime.datetime.now()
    change = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    if(sets.Get("dayPageMode","day") == "week"):
        firstDay = sets.Get("dayPageModeWeekDay","Monday").lower()
        startAt = [idx for idx, element in enumerate(change) if element.startswith(firstDay)]
        if(len(startAt) > 0):
            startAt = startAt[0]
        else:
            startAt = 0
        offset = (dt.weekday() - startAt)
        if(offset != 0):
            dt = dt - datetime.timedelta(days=offset)
    return dt

def dayPageGetPath():
    dpPath = sets.Get("dayPagePath",None)
    if(dpPath == None):
        return None
    try:
        if isinstance(dpPath, list):
            sublime.status_message("Day Page error. dayPagePath setting should be a string not a list! ABORT!")
            log.error(" Cannot create day page without propper dayPathPath in configuration. Expected string, found list")
            return None
        if isinstance(dpPath, str) and not dpPath.strip() == "":
            os.makedirs(dpPath, exist_ok=True)
    except Exception as e:
        sublime.status_message("Day Page error. dayPagePath setting is not valid could not create daypage! ABORT!")
        log.error("Cannot create day page without propper dayPathPath in configuration: \n" + str(e))
        return None
    return dpPath

def dayPageGetDateString(dt):
    formatStr = sets.Get("dayPageNameFormat","%a_%Y_%m_%d")
    return dt.strftime(formatStr)

def dayPageFilenameToDateTime(view):
    filename = view.file_name()
    if(not filename):
        return None
    formatStr = sets.Get("dayPageNameFormat","%a_%Y_%m_%d")
    filename = os.path.splitext(os.path.basename(filename))[0]
    try:
        return datetime.datetime.strptime(filename,formatStr)
    except ValueError:
        return None

def dayPageGetName(dt):
    path = dayPageGetPath()
    if path == None:
        return "DAY_PAGE_NOT_SET.org"
    else:
        return os.path.join(dayPageGetPath(),dayPageGetDateString(dt) + ".org")


def OnLoaded(view,dt):
    view.sel().clear()    
    view.sel().add(0)
    snippet  = sets.Get("dayPageSnippet","dayPageSnippet")
    snipName = ext.find_extension_file('orgsnippets',snippet,'.sublime-snippet')
    if(snipName == None):
        log.error(" Could not locate snippet file: " + str(snippet) + ".sublime-snippet using default")
        snipName = ext.find_extension_file('orgsnippets','dayPageSnippet.sublime-snippet')
    # NeoVintageous users probably prefern not to have to hit insert when editing things.
    view.run_command('_enter_insert_mode', {"count": 1, "mode": "mode_internal_normal"})
    now  = dt
    inow = orgdate.OrgDate.format_date(now, False)
    anow = orgdate.OrgDate.format_date(now, True)
    ai = view.settings().get('auto_indent')
    view.settings().set('auto_indent',False)
    # "Packages/OrgEverywhere/orgsnippets/"+snippet+".sublime-snippet"
    # OTHER VARIABLES:
    # TM_FULLNAME - Users full name
    # TM_FILENAME - File name of the file being edited
    # TM_CURRENT_WORD - Word under cursor when snippet was triggered
    # TM_SELECTED_TEXT - Selected text when snippet was triggered
    # TM_CURRENT_LINE - Line of snippet when snippet was triggered
    #insert_snippet {"name": "Packages/OrgEverywhere/orgsnippets/page.sublime-snippet"}
    view.run_command("insert_snippet",
        { "name" : snipName
        , "ORG_INACTIVE_DATE": inow
        , "ORG_ACTIVE_DATE":   anow
        , "ORG_DATE":          str(dt.date().today())
        , "ORG_TIME":          dt.strftime("%H:%M:%S")
        , "ORG_CLIPBOARD":     sublime.get_clipboard()
        , "ORG_SELECTION":     view.substr(view.sel()[0])
        , "ORG_LINENUM":       str(view.curRow())
        , "ORG_FILENAME":      dayPageGetDateString(dt)
        , "ORG_AUTHOR":        getpass.getuser()
        })
    view.settings().set('auto_indent',ai)

def LoadedCheck(view,dt):
    if(view.is_loading()):
        sublime.set_timeout(lambda: LoadedCheck(view,dt),1)
    else:
        OnLoaded(view,dt)

def LoadedCheck2(view,dt, onDone):
    if(view.is_loading()):
        sublime.set_timeout(lambda: LoadedCheck2(view,dt,onDone),1)
    else:
        onDone(view,dt)

def dayPageInsertSnippet(view,dt):
    window   = view.window()
    window.focus_view(view)
    LoadedCheck(view,dt)

def dayPageFindOldPage(dt):
    maxScan     = 90
    for i in range(maxScan):
        dt = dt - datetime.timedelta(days=1)
        fn = dayPageGetName(dt)
        if(os.path.exists(fn)):
            return fn
    return None

# The local (no server) day pages parse only day page files: the last page,
# to archive it and carry its open tasks over.
def dayPageArchiveOld(dt):
    fn = dayPageFindOldPage(dt)
    if(fn == None):
        return
    # A private parse: the tree is changed and written back.
    f = db.Get().ParseFile(fn, copy=True)
    if(f == None):
        return
    if(f.org[0].set_comment("FILETAGS","ARCHIVE")):
        f.Save()

def IsTodo(n):
    return n.todo and n.todo in n.env.todo_keys

def dayPageCopyOpenTasks(tview, dt):
    fn = dayPageFindOldPage(dt)
    if(fn == None):
        dayPageInsertSnippet(tview,dt)
        return
    f = db.Get().ParseFile(fn)
    if(f == None):
        dayPageInsertSnippet(tview,dt)
        return
    out = ""
    for h in f.org[0].children:
        if IsTodo(h):
            for line in h._lines:
                out += line + "\n"
    if(out != ""):
        LoadedCheck2(tview, dt, lambda a,b: tview.run_command("org_internal_insert", {"location": 0, "text": out, "onDone": evt.Make(lambda : dayPageInsertSnippet(tview, dt))}))
    else:
        dayPageInsertSnippet(tview,dt)
    pass

def dayPageCopyOpenPhase(tview,dt):
    if(sets.Get("dayPageCopyOpenTasks", True)):
        dayPageCopyOpenTasks(tview, dt)
    else:
        dayPageInsertSnippet(tview,dt)

# Today's tasks come from the orgs server's agenda for the day: the open
# tasks on it (scheduled, carried forward, deadlines due or warning, dated)
# from other files. Their text is cut out of the file text the server has
# (a heading line down to the next heading); nothing is parsed here.
RE_HEADING_LINE = re.compile(r"^\*+\s")
RE_HEADING_PARTS = re.compile(r"^\*+\s+(.*?)\s*(:[\w@#%:]+:)?\s*$")

def dayPageHeadingKey(line, keys):
    """A heading line without its stars, keyword, priority and tags."""
    m = RE_HEADING_PARTS.match(line.strip())
    if not m:
        return None
    words = m.group(1).split()
    if words and words[0] in keys:
        words = words[1:]
    if words and re.match(r"^\[#.\]$", words[0]):
        words = words[1:]
    return " ".join(words)

def dayPageHeadingLines(text, row):
    """The lines of the heading at row: its line down to the next heading."""
    lines = text.split("\n")
    if row is None or row < 0 or row >= len(lines) or not RE_HEADING_LINE.match(lines[row]):
        return None
    end = row + 1
    while end < len(lines) and not RE_HEADING_LINE.match(lines[end]):
        end += 1
    return lines[row:end]

def dayPageIsHeading(line, n):
    """line is n's heading line (its level and title)."""
    stars = len(line) - len(line.lstrip("*"))
    key = dayPageHeadingKey(line, set(n.env.all_todo_keys))
    return key is not None and (not n.level or stars == n.level) and \
        agnode.DisplayHeading(key) == agnode.DisplayHeading(n.headline)

def dayPageNodeLines(text, n):
    """The lines of n's heading in text. The text was read after the agenda,
    so the row is checked against the title; a heading that has moved is
    looked for again and taken only when that finds exactly one."""
    lines = dayPageHeadingLines(text, n.start_row)
    if lines and dayPageIsHeading(lines[0], n):
        return lines
    rows = [i for i, l in enumerate(text.split("\n")) if RE_HEADING_LINE.match(l) and dayPageIsHeading(l, n)]
    if len(rows) == 1:
        return dayPageHeadingLines(text, rows[0])
    log.warning("day page: '%s' is no longer at line %s of %s; not copied", n.heading,
                (n.start_row or 0) + 1, os.path.basename(n.filename))
    return None

def dayPageTodayTasksText(cl, path, dt):
    """(worker thread) The text of today's open tasks from other files."""
    day = dt.date() if isinstance(dt, datetime.datetime) else dt
    data = agnode.AgendaData.FromResult(cl.agenda(day, day, today=datetime.date.today()))
    picked = []
    seen = set()
    for occ in data.On(day):
        n = occ.node
        if id(n) in seen or occ.done or not n.is_todo or n.is_archived:
            continue
        if not n.filename or (path and PathMap.same(n.filename, path)):
            continue
        seen.add(id(n))
        picked.append(n)
    # Headings already on the page (the server may have carried them over
    # from the last page) are not copied again.
    pageLines = []
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                pageLines = [l for l in f.read().split("\n") if RE_HEADING_LINE.match(l)]
        except (OSError, UnicodeDecodeError):
            log.warning("day page: could not read %s", path)
    texts = {}
    out = ""
    for n in picked:
        # A child of a heading we copy is not copied on its own.
        if any(data.IsUnder(n, p) for p in picked):
            continue
        if n.filename not in texts:
            try:
                texts[n.filename] = cl.file_text(n.filename)
            except orgsclient.Unavailable:
                raise
            except orgsclient.OrgsError as e:
                log.warning("day page: could not read %s: %s", n.filename, e)
                texts[n.filename] = None
        text = texts[n.filename]
        lines = dayPageNodeLines(text, n) if text is not None else None
        if not lines:
            continue
        keys = set(n.env.all_todo_keys)
        key = dayPageHeadingKey(lines[0], keys)
        if any(dayPageHeadingKey(l, keys) == key for l in pageLines):
            continue
        for line in lines:
            out += line + "\n"
    return out

def dayPageCopyTodayTasks(path, tview, dt, onDone=None, atEnd=False):
    after = onDone or (lambda: dayPageCopyOpenPhase(tview, dt))

    def insert(text):
        if(text):
            def go(a, b):
                loc = tview.size() if atEnd else 0
                if atEnd and loc > 0 and tview.substr(loc - 1) != "\n":
                    text2 = "\n" + text
                else:
                    text2 = text
                tview.run_command("org_internal_insert", {"location": loc, "text": text2, "onDone": evt.Make(after)})
            LoadedCheck2(tview, dt, go)
        else:
            after()

    def failed(e):
        if not isinstance(e, orgsclient.Unavailable) or isinstance(e, ServerTimeout):
            orgs.report(e, "day page tasks")
        else:
            sublime.status_message("orgs not connected: today's tasks were not copied to the day page")
        after()
    orgs.run(lambda cl: dayPageTodayTasksText(cl, path, dt), insert, on_error=failed, what="day page tasks")

def dayPageCreateOrOpen(dt):
    dpPath      = dayPageGetName(dt)
    dateString  = dayPageGetDateString(dt)
    didCreate   = False
    if(not os.path.exists(dpPath)):
        with open(dpPath,"w") as f:
            f.write("")
            didCreate = True
    tview = sublime.active_window().open_file(dpPath, sublime.ENCODED_POSITION)
    if(didCreate):
        if(sets.Get("dayPageArchiveOld", True)):
            dayPageArchiveOld(dt)
        if(sets.Get("dayPageCopyTasksForToday", True)):
            dayPageCopyTodayTasks(dpPath, tview, dt)
        else:
            dayPageCopyOpenPhase(tview,dt)

# ----------------------------------------------------------------------
# Day pages from the orgs server. The server owns the day page settings
# (dayPagePath, dayPageMode, template) and, when it makes today's page,
# carries the open tasks over from the last one and archives it.
# When it has no dayPagePath the local settings above are used instead.

class NoServerDayPage(orgsclient.OrgsError):
    pass

ServerTimeout = getattr(orgsclient, "Timeout", orgsclient.Unavailable)

SERVER_DAYPAGE_FORMAT = "%a_%Y_%m_%d"
MAX_DAYPAGE_STEPS = 60

def dayPageServerPath(cl, d):
    """(worker) The server's day page for date d (may not exist yet)."""
    paths = cl.daypage(d)
    p = paths[0] if paths else None
    if not p or not os.path.isabs(p) or not os.path.dirname(p):
        # A bare name means the server has no dayPagePath configured.
        raise NoServerDayPage("the orgs server has no dayPagePath")
    return p

def dayPageServerToday(cl):
    """(worker) (path, created) for today's page, creating it if needed."""
    p = dayPageServerPath(cl, datetime.date.today())
    if os.path.exists(p):
        return (p, False)
    try:
        created = cl.create_daypage()
    except (orgsclient.Unavailable, orgsclient.AuthRequired, ServerTimeout):
        # A timeout may still have made the page: never fall back to a local one.
        raise
    except orgsclient.OrgsError as e:
        raise NoServerDayPage(str(e))
    p = created[0] if created else p
    if not os.path.isabs(p):
        raise NoServerDayPage("the orgs server made a day page without a dayPagePath")
    # The file comes from the server; give a shared or synced folder a moment.
    for i in range(10):
        if os.path.exists(p):
            return (p, True)
        time.sleep(0.2)
    raise orgsclient.OrgsError("the server did not make {0} (does its dayPagePath folder exist, and can it be seen from here?)".format(p))

def dayPageServerStep(cl, start, direction):
    """(worker) Walk from start in steps of the server's increment until a day
    page exists. Walking forward onto today's page opens (or makes) it.
    Returns ("open", path), ("today", None) or (None, None)."""
    inc = cl.daypage_increment() * direction
    today = datetime.date.today()
    todayPath = dayPageServerPath(cl, today)
    d = start
    for i in range(MAX_DAYPAGE_STEPS):
        d = d + datetime.timedelta(days=inc)
        if direction > 0 and d > today + datetime.timedelta(days=abs(inc)):
            break
        p = dayPageServerPath(cl, d)
        if direction > 0 and PathMap.same(p, todayPath):
            return ("today", None)
        if os.path.exists(p):
            return ("open", p)
    return (None, None)

def dayPageViewDate(view):
    """The date of the day page shown in view, or None."""
    fn = view.file_name() if view else None
    if not fn:
        return None
    base = os.path.splitext(os.path.basename(fn))[0]
    for fmt in (SERVER_DAYPAGE_FORMAT, sets.Get("dayPageNameFormat","%a_%Y_%m_%d")):
        try:
            return datetime.datetime.strptime(base, fmt).date()
        except ValueError:
            pass
    return None

def dayPageOpen(path):
    window = sublime.active_window()
    tview = window.open_file(path, sublime.ENCODED_POSITION)
    window.focus_view(tview)
    return tview

def dayPageServerOpenToday(onDone=None):
    """Open today's page from the server; falls back to the local day page."""
    def done(res):
        path, created = res
        tview = dayPageOpen(path)
        if created and sets.Get("dayPageCopyTasksForToday", True):
            now = datetime.datetime.now()
            dayPageCopyTodayTasks(path, tview, now, onDone=lambda: evt.EmitIf(onDone), atEnd=True)
        else:
            evt.EmitIf(onDone)

    def failed(e):
        if isinstance(e, NoServerDayPage):
            if dayPageLocalFallback(str(e)):
                dayPageCreateOrOpen(dayPageGetToday())
            evt.EmitIf(onDone)
        else:
            orgs.report(e, "day page")
            evt.EmitIf(onDone)
    return orgs.run(dayPageServerToday, done, on_error=failed, what="day page")

def dayPageServerMove(view, direction, localMove):
    start = dayPageViewDate(view) or datetime.date.today()

    def done(res):
        kind, path = res
        if kind == "open":
            dayPageOpen(path)
        elif kind == "today":
            dayPageServerOpenToday()
        else:
            sublime.status_message("No {0} day page found".format("earlier" if direction < 0 else "later"))

    def failed(e):
        if isinstance(e, NoServerDayPage):
            if dayPageLocalFallback(str(e)):
                localMove()
        else:
            orgs.report(e, "day page")
    orgs.run(lambda cl: dayPageServerStep(cl, start, direction), done, on_error=failed, what="day page")

def dayPageLocalFallback(why):
    """Say we are using the local day page settings; False if there are none."""
    if dayPageGetPath() is None:
        sublime.status_message("orgs: {0}, and no local dayPagePath is set either".format(why))
        return False
    sublime.status_message("orgs: {0}; using the local day page settings".format(why))
    log.info("day page: %s, falling back to local dayPagePath", why)
    return True

def dayPageLocalOnly():
    if orgs.connected():
        return False
    if dayPageGetPath() is None:
        sublime.status_message("orgs: not connected (and no local dayPagePath set) - run Org: Connect to orgs server")
        return None
    sublime.status_message("orgs: not connected; using the local day page settings")
    return True

r"""
/* SDOC: Reference::Commands::Day Pages
*** Org Day Page Previous
    Command: =org_day_page_previous=  Keys: =alt+o d p=; NeoVintageous normal mode: =space d p=  Args: =onDone=

    Opens the day page before the one shown (or before today, from any
    other file). With the orgs server it steps back by the server's day
    page increment (a day or a week) until a page exists, up to 60 steps.
    Locally it looks up to 90 days back; with =dayPageCreateOldPages= it
    opens (creating if needed) the page of the previous day instead.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Day Pages
*** Day page settings
    With the orgs server these apply only when the server has no
    =dayPagePath= of its own (=dayPageCopyTasksForToday= and
    =dayPageNameFormat=, to recognise a page, apply always).

    | Setting                    | Default            | Effect                                                                                           |
    |----------------------------+--------------------+--------------------------------------------------------------------------------------------------|
    | =dayPagePath=              | not set            | Folder of the day pages (a string; created if missing). Not set: no local day pages.            |
    | =dayPageNameFormat=        | ="%a_%Y_%m_%d"=    | strftime name of a page (without =.org=). Old pages are found by this name, so do not change it lightly. |
    | =dayPageSnippet=           | ="dayPageSnippet"= | The snippet (in =orgsnippets=, without =.sublime-snippet=) a new page is filled from.            |
    | =dayPageMode=              | ="day"=            | ="week"= makes one page per week.                                                                |
    | =dayPageModeWeekDay=       | ="Monday"=         | In week mode, the day whose date names the week's page.                                          |
    | =dayPageArchiveOld=        | =true=             | Tag the previous page =#+FILETAGS: ARCHIVE= when a new page is made.                              |
    | =dayPageCopyOpenTasks=     | =true=             | Copy the previous page's top level open todos to a new page.                                     |
    | =dayPageCopyTasksForToday= | =true=             | Copy today's open tasks from other files (from the orgs agenda) to a new page.                   |
    | =dayPageCreateOldPages=    | =false=            | Org Day Page Previous creates the previous day's page instead of skipping to an existing one.    |

    The day page snippet gets =ORG_INACTIVE_DATE=, =ORG_ACTIVE_DATE=,
    =ORG_DATE=, =ORG_TIME=, =ORG_CLIPBOARD=, =ORG_SELECTION=, =ORG_LINENUM=,
    =ORG_FILENAME= (the page's name) and =ORG_AUTHOR=. ={daypage}= in a
    capture target is today's page.
EDOC */
"""

class OrgDayPagePreviousCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        evt.EmitIf(self.onDone)

    def run(self, edit, onDone=None):
        self.edit   = edit
        self.onDone = onDone
        local = dayPageLocalOnly()
        if local is None:
            return
        if not local:
            dayPageServerMove(self.view, -1, self.LocalPrevious)
            return
        self.LocalPrevious()

    def LocalPrevious(self):
        dt          = dayPageFilenameToDateTime(self.view) or dayPageGetToday()
        maxScan     = 90
        for i in range(maxScan):
            dt = dt - datetime.timedelta(days=1)
            if(sets.Get("dayPageCreateOldPages",False)):
                dayPageCreateOrOpen(dt)
                break
            else:
                fn = dayPageGetName(dt)
                if(os.path.exists(fn)):
                    tview = sublime.active_window().open_file(fn, sublime.ENCODED_POSITION)
                    sublime.active_window().focus_view(tview)
                    break

r"""
/* SDOC: Reference::Commands::Day Pages
*** Org Day Page Next
    Command: =org_day_page_next=  Keys: =alt+o d n=; NeoVintageous normal mode: =space d n=  Args: =onDone=

    Opens the next existing day page after the one shown. Stepping onto
    today opens (or creates) today's page, as Org Day Page Create; it never
    creates a page in the future. Says so when there is no later page.
EDOC */
"""

class OrgDayPageNextCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        evt.EmitIf(self.onDone)

    def run(self, edit, onDone=None):
        self.edit   = edit
        self.onDone = onDone
        local = dayPageLocalOnly()
        if local is None:
            return
        if not local:
            dayPageServerMove(self.view, 1, self.LocalNext)
            return
        self.LocalNext()

    def LocalNext(self):
        self.now    = datetime.datetime.now()
        dt          = dayPageFilenameToDateTime(self.view) or dayPageGetToday()
        maxScan     = 90
        for i in range(maxScan):
            dt = dt + datetime.timedelta(days=1)
            if(dt.date() < self.now.date()):
                fn = dayPageGetName(dt)
                if(os.path.exists(fn)):
                    tview = sublime.active_window().open_file(fn, sublime.ENCODED_POSITION)
                    sublime.active_window().focus_view(tview)
                    break
            elif(dt.date() == self.now.date()):
                dayPageCreateOrOpen(dt)
                break
            else:
                fn = dayPageGetName(dt)
                log.error(" Create day page in the future? " + fn)
                break


r"""
/* SDOC: Reference::Commands::Day Pages
*** Org Day Page Create
    Command: =org_day_page_create=  Keys: =alt+o d c=; NeoVintageous normal mode: =space d c=  Args: =onDone=

    Opens today's day page, creating it if it does not exist yet.

    With the orgs server the server owns day pages: its =dayPagePath=,
    =dayPageMode= and template decide the file, and when it makes today's
    page it carries the open tasks over from the last page and archives
    that one. OrgEverywhere then (with =dayPageCopyTasksForToday=) appends
    today's open tasks from other files: the open todos on today's agenda
    (scheduled, deadlines due or warning, dated), each heading with its
    text, skipping any already on the page. When the server has no
    =dayPagePath= the local settings below are used instead.

    Without a server (and with =dayPagePath= set) the page is made locally:
    =dayPagePath/<dayPageNameFormat>.org=, filled from the =dayPageSnippet=
    snippet; the previous page (looked for up to 90 days back) is tagged
    =#+FILETAGS: ARCHIVE= (=dayPageArchiveOld=) and its top level open todos
    copied over (=dayPageCopyOpenTasks=). Copying today's tasks from other
    files needs the server.
EDOC */
"""

class OrgDayPageCreateCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        evt.EmitIf(self.onDone)

    def run(self, edit, onDone=None):
        self.edit   = edit
        self.onDone = onDone
        local = dayPageLocalOnly()
        if local is None:
            return
        if not local:
            dayPageServerOpenToday(onDone)
            return
        self.dt = dayPageGetToday()
        dayPageCreateOrOpen(self.dt)
        self.OnDone()
