"""Read only views onto things the orgs server knows: habits, clock reports,
full text search results and a file's git history.

Every view here is a scratch buffer whose rows remember where they came from
(the orgs_rows view setting: row -> [filename, line0]). Double click or
org_orgs_goto_row jumps there; org_orgs_refresh_view rebuilds the view.
"""
import datetime
import logging
import os
import re

import sublime
import sublime_plugin

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as orgsui

log = logging.getLogger(__name__)

ROWS_KEY = "orgs_rows"
REFRESH_KEY = "orgs_refresh"
VIEW_FLAG = "orgs_rows_view"
ORG_SYNTAX = "Packages/OrgEverywhere/OrgEverywhere.sublime-syntax"
FIND_SYNTAX = "Packages/Default/Find Results.hidden-tmLanguage"

HABITS_VIEW = "Org Habits"
CLOCK_VIEW = "Org Clock Report"
SEARCH_VIEW = "Org Text Search"

CLOCK_BLOCKS = [
    ["today", "Today"],
    ["yesterday", "Yesterday"],
    ["thisweek", "This week"],
    ["lastweek", "Last week"],
    ["thismonth", "This month"],
    ["lastmonth", "Last month"],
    ["thisyear", "This year"],
    ["lastyear", "Last year"],
    ["untilnow", "Everything until now"],
]


# ----------------------------------------------------------------------
# Shared helpers

def ShowRowsView(window, name, lines, rows, refresh=None, syntax=ORG_SYNTAX, extra=None):
    """A read only scratch view of lines whose rows jump somewhere.

    rows: {row: (filename, line0)}. refresh: (command, args) that rebuilds it.
    extra: more view settings to set (dicts keyed by str(row) and the like).
    """
    window = window or sublime.active_window()
    view = orgsui.scratch_view(window, name, "\n".join(lines) + "\n", syntax=syntax)
    s = view.settings()
    s.set(VIEW_FLAG, True)
    s.set(ROWS_KEY, {str(r): [fn, ln] for r, (fn, ln) in rows.items()})
    if refresh:
        s.set(REFRESH_KEY, {"command": refresh[0], "args": refresh[1] or {}})
    for k, v in (extra or {}).items():
        s.set(k, v)
    s.set("line_numbers", False)
    s.set("gutter", False)
    s.set("word_wrap", False)
    return view


def RowAt(view, event=None):
    if event is not None and "x" in event:
        pt = view.window_to_text((event["x"], event["y"]))
    else:
        sel = view.sel()
        if not sel:
            return None
        pt = sel[0].begin()
    return view.rowcol(pt)[0]


def RowTarget(view, row):
    rows = view.settings().get(ROWS_KEY) or {}
    return rows.get(str(row))


def SyncPath(window, filename):
    """(worker thread) If filename is open with unsaved edits, save it and let
    the server catch up before something on the server changes it."""
    if not filename:
        return

    def find():
        w = window or sublime.active_window()
        return w.find_open_file(filename) if w else None
    view = orgs.run_on_main_and_wait(find)
    if view is not None:
        orgs.sync_file(view)


def Plural(n, word):
    if n == 1:
        return "1 " + word
    if word.endswith("y"):
        return "{0} {1}ies".format(n, word[:-1])
    if word.endswith(("s", "ch", "sh", "x")):
        return "{0} {1}es".format(n, word)
    return "{0} {1}s".format(n, word)


def Hm(mins):
    m = int((mins or 0) + 0.5)
    return "{0}:{1:02d}".format(m // 60, m % 60)


_HEAD_RE = re.compile(r'^(\*+)\s+(.*?)\s*$')
_TAGS_RE = re.compile(r'\s+(:[\w@#%:]+:)\s*$')
_PRIO_RE = re.compile(r'^\[#.\]\s*')


def HeadingTitles(text):
    """The ways a heading line's title can be spelled: with and without its
    todo keyword, priority and tags."""
    t = _TAGS_RE.sub('', text).strip()
    out = {t, _PRIO_RE.sub('', t)}
    parts = t.split(None, 1)
    if len(parts) == 2 and parts[0].isupper():
        out.add(parts[1])
        out.add(_PRIO_RE.sub('', parts[1]))
    return out


def FindHeadingLine(lines, headline, level=None):
    """0 based line of the first heading titled headline (0 when not found)."""
    want = (headline or "").strip()
    for i, line in enumerate(lines):
        m = _HEAD_RE.match(line)
        if not m:
            continue
        if level and len(m.group(1)) != level:
            continue
        if want in HeadingTitles(m.group(2)):
            return i
    return 0


def ReadLines(filename, cache):
    if filename not in cache:
        try:
            with open(filename, "r", encoding="utf-8", errors="replace") as f:
                cache[filename] = f.read().split("\n")
        except (IOError, OSError):
            cache[filename] = []
    return cache[filename]


r"""
/* SDOC: Reference::Commands::orgs Server
*** Open the row of an orgs result view
    Command: =org_orgs_goto_row=  Keys: =enter= (in an orgs result view), double click
    Args: =transient= (default =false=)

    In the result views (Org Habits, Org Clock Report, Org Text Search)
    each line remembers the file and line it came from; this opens it. A
    double click on a row does the same. =transient= opens it as a preview.
    Not in the command palette: it only means something in those views.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::orgs Server
*** orgs view settings
    | Setting                    | Default | Effect                                                                                       |
    |----------------------------+---------+----------------------------------------------------------------------------------------------|
    | =orgsHabitDays=            | =21=    | Days of history in Org: orgs Habits (the server shows at least 7).                           |
    | =orgsTextSearchMax=        | =200=   | Matching lines kept per file by Org: orgs Full Text Search; more are cut and you are told.  |
    | =orgsTextSearchIgnoreCase= | =true=  | Full text search ignores case unless the command is given =ignore_case=.                     |
    | =orgsHistoryLimit=         | =100=   | How many commits Org: orgs File History lists.                                               |
EDOC */
"""

class OrgOrgsGotoRowCommand(sublime_plugin.TextCommand):
    """In an orgs result view: open what the row under the cursor (or the
    double click) came from."""
    def run(self, edit, event=None, transient=False):
        row = RowAt(self.view, event)
        if row is None:
            return
        tgt = RowTarget(self.view, row)
        if not tgt:
            sublime.status_message("orgs: nothing to open on this line")
            return
        orgsui.goto(self.view.window(), tgt[0], tgt[1], transient=transient)

    def want_event(self):
        return True

    def is_visible(self, event=None, transient=False):
        return bool(self.view.settings().get(VIEW_FLAG))


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Refresh Result View
    Command: =org_orgs_refresh_view=  Keys: =g= (in an orgs result view)

    Rebuilds the current orgs result view (habits, clock report, text
    search results) by running the command that made it again, with the
    same arguments. Only shown in views that can be refreshed. Needs the
    orgs server.
EDOC */
"""

class OrgOrgsRefreshViewCommand(sublime_plugin.TextCommand):
    """Rebuild an orgs result view (habits, clock report, search results)."""
    def run(self, edit):
        r = self.view.settings().get(REFRESH_KEY)
        if not r:
            sublime.status_message("orgs: this view cannot be refreshed")
            return
        self.view.window().run_command(r["command"], r.get("args") or {})

    def is_visible(self, **kw):
        return bool(self.view.settings().get(REFRESH_KEY))


class OrgsRowsViewListener(sublime_plugin.ViewEventListener):
    """Double click on a row of an orgs result view jumps to it."""
    @classmethod
    def is_applicable(cls, settings):
        return bool(settings.get(VIEW_FLAG))

    def on_text_command(self, command_name, args):
        if command_name == "drag_select" and args and args.get("by") == "words" and "event" in args:
            return ("org_orgs_goto_row", {"event": args["event"]})
        return None


# ----------------------------------------------------------------------
# Habits

HABIT_MARK = {"done": "■", "miss": "×", "ok": "·"}


def HabitBar(days):
    return "".join(HABIT_MARK.get((d or {}).get("s"), "?") for d in (days or []))


def RenderHabits(res):
    """(lines, rows, habit rows) for a /habits answer."""
    habits = res.get("Habits") or []
    lines = ["Habits: last {0} days to {1}   due today {2}, done today {3}, best streak {4}".format(
                 res.get("Days", ""), res.get("Today", ""), res.get("DueToday", 0),
                 res.get("DoneToday", 0), res.get("BestStreak", 0)),
             "  {0} done  {1} missed  {2} nothing due   (enter/double click: open, "
             "Org: Toggle habit done today: tick)".format(HABIT_MARK["done"], HABIT_MARK["miss"], HABIT_MARK["ok"]),
             ""]
    rows = {}
    info = {}
    if not habits:
        lines.append("  No habits (a habit is a repeating TODO with :STYLE: habit).")
        return lines, rows, info
    width = min(40, max(len(h.get("Headline") or "") for h in habits))
    for h in habits:
        if h.get("DoneToday"):
            today = "done"
        elif h.get("Missed"):
            today = "LATE"
        elif h.get("Due"):
            today = "due"
        else:
            today = ""
        name = (h.get("Headline") or "")[:width]
        line = "  {0:<{w}}  {1:<4}  streak {2:>3}  best {3:>3}  {4:>3}%  {5}  {6}".format(
            name, today, h.get("Streak", 0), h.get("Best", 0), int(round(min(1.0, h.get("Rate") or 0) * 100)),
            HabitBar(h.get("Days")), orgsui.short_file(h.get("Filename")), w=width)
        row = len(lines)
        lines.append(line.rstrip())
        rows[row] = (h.get("Filename"), h.get("LineNum", 0))
        info[str(row)] = {"Hash": h.get("Hash"), "Headline": h.get("Headline"), "Filename": h.get("Filename"),
                          "LineNum": h.get("LineNum", 0), "DoneToday": bool(h.get("DoneToday")),
                          "DoneKeyword": h.get("DoneKeyword") or "DONE"}
    return lines, rows, info


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Habits
    Command: =org_orgs_habits=  Keys: none  Args: =days=

    Opens the "Org Habits" view: every habit (a repeating TODO with
    =:STYLE: habit=) with today's state (done, due, LATE), its current and
    best streak, its completion rate and a bar of the last =days= days
    (default =orgsHabitDays=, 21; the server shows at least 7): =■= done,
    =×= missed, =·= nothing due. =enter= or a double click opens the habit,
    =t= ticks it done for today (Org: orgs Toggle Habit Done Today), =g=
    refreshes. Needs the orgs server.
EDOC */
"""

class OrgOrgsHabitsCommand(sublime_plugin.WindowCommand):
    """Every habit with its streak and a consistency bar, from the server."""
    def run(self, days=None):
        days = int(days or sets.Get("orgsHabitDays", 21))

        def done(res):
            lines, rows, info = RenderHabits(res)
            ShowRowsView(self.window, HABITS_VIEW, lines, rows, ("org_orgs_habits", {"days": days}),
                         syntax=None, extra={"orgs_habits": info})
        orgs.run(lambda cl: cl.habits(days), done, what="habits")

    def is_enabled(self, **kw):
        return orgs.connected()


def ToggleHabit(cl, window, h):
    """(worker thread) Tick a habit for today, or take today's tick back."""
    from OrgEverywhere.orgs.session import heading_hash
    SyncPath(window, h["Filename"])
    hh = heading_hash(cl, h["Filename"], h["LineNum"], h["Headline"])
    if h.get("DoneToday"):
        cl.habit_untick(hh)
        return "untick"
    cl.set_status(hh, h.get("DoneKeyword") or "DONE")
    return "done"


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Toggle Habit Done Today
    Command: =org_orgs_habit_toggle=  Keys: =t= (in an orgs result view)

    In the Org Habits view: marks the habit on the cursor's line done for
    today (sets its done keyword, so the server advances the repeat and
    logs it), or, when it is already done today, takes today's tick back.
    An open, modified buffer of the habit's file is saved first. The view
    is refreshed afterwards. Only does something in the habits view (in
    the other result views =t= just says there is no habit on the line).
    Needs the orgs server.
EDOC */
"""

class OrgOrgsHabitToggleCommand(sublime_plugin.TextCommand):
    """In the habits view: mark the habit under the cursor done today (or
    undo today's tick)."""
    def run(self, edit):
        row = RowAt(self.view)
        h = (self.view.settings().get("orgs_habits") or {}).get(str(row))
        if not h:
            sublime.status_message("orgs: no habit on this line")
            return
        window = self.view.window()

        def done(what):
            sublime.status_message("orgs: {0} {1}".format(
                h["Headline"], "un-ticked for today" if what == "untick" else "done for today"))
            window.run_command("org_orgs_habits")
        orgs.run(lambda cl: ToggleHabit(cl, window, h), done, what="habit")

    def is_visible(self, **kw):
        return bool(self.view.settings().get("orgs_habits") is not None)

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Clock report

def RenderClockReport(res, cache=None):
    """(lines, rows) for a /clockreport answer. Reads the files to find the
    line of each heading (the report does not carry one)."""
    cache = {} if cache is None else cache
    entries = res.get("entries") or []
    block = res.get("block", "")
    label = dict(CLOCK_BLOCKS).get(block, block)
    lines = ["#+TITLE: Clock report: {0}".format(label),
             "Total: {0}   ({1})".format(Hm(res.get("totalMin")), Plural(len(entries), "entry")),
             ""]
    rows = {}
    if not entries:
        lines.append("Nothing clocked.")
        return lines, rows
    heads = []
    for e in entries:
        lvl = int(e.get("level") or 1)
        heads.append(("\\_" + "_" * max(0, lvl - 2) + " " if lvl > 1 else "") + (e.get("headline") or ""))
    files = [orgsui.short_file(e.get("filename")) for e in entries]
    hw = max([len("Headline")] + [len(h) for h in heads])
    fw = max([len("File")] + [len(f) for f in files])
    times = [Hm(e.get("mins")) for e in entries]
    tw = max([len("Time"), len(Hm(res.get("totalMin")))] + [len(t) for t in times])

    def row(a, b, c):
        return "| {0:<{hw}} | {1:<{fw}} | {2:>{tw}} |".format(a, b, c, hw=hw, fw=fw, tw=tw)
    sep = "|-{0}-+-{1}-+-{2}-|".format("-" * hw, "-" * fw, "-" * tw)
    lines += [row("Headline", "File", "Time"), sep]
    for e, h, f, t in zip(entries, heads, files, times):
        fn = e.get("filename")
        ln = FindHeadingLine(ReadLines(fn, cache), e.get("headline"), e.get("level"))
        rows[len(lines)] = (fn, ln)
        lines.append(row(h, f, t))
    lines += [sep, row("Total", "", Hm(res.get("totalMin")))]
    return lines, rows


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Clock Report
    Command: =org_orgs_clock_report=  Keys: none  Args: =block=

    The time clocked across every org file for a range, as a table in the
    "Org Clock Report" view: each clocked heading (indented by level), its
    file and its time, with the total. Pick the range from a quick panel or
    pass =block=: =today=, =yesterday=, =thisweek=, =lastweek=,
    =thismonth=, =lastmonth=, =thisyear=, =lastyear=, =untilnow=. =enter=
    on a row opens the heading, =g= refreshes. Needs the orgs server.
EDOC */
"""

class OrgOrgsClockReportCommand(sublime_plugin.WindowCommand):
    """Time clocked across every file for a range (today, this week, ...)."""
    def run(self, block=None):
        if block:
            self.Report(block)
            return
        items = [[b[1], b[0]] for b in CLOCK_BLOCKS]
        self.window.show_quick_panel(items, lambda i: i >= 0 and self.Report(CLOCK_BLOCKS[i][0]))

    def Report(self, block):
        def work(cl):
            return RenderClockReport(cl.clock_report(block))

        def done(res):
            lines, rows = res
            ShowRowsView(self.window, CLOCK_VIEW, lines, rows, ("org_orgs_clock_report", {"block": block}))
        orgs.run(work, done, what="clock report")

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Full text search

def FlattenMatches(res):
    out = []
    for f in res.get("Files") or []:
        for m in f.get("Matches") or []:
            out.append((f.get("Filename"), m.get("Line", 0), m.get("Text", "")))
    return out


def RenderTextSearch(res):
    files = res.get("Files") or []
    lines = ['Searching orgs for "{0}": {1} in {2}'.format(
        res.get("Query", ""), Plural(res.get("Total", 0), "match"), Plural(res.get("FileCount", len(files)), "file")),
        ""]
    rows = {}
    for f in files:
        fn = f.get("Filename")
        lines.append("{0}:".format(fn))
        for m in f.get("Matches") or []:
            ln = m.get("Line", 0)
            rows[len(lines)] = (fn, ln)
            lines.append("  {0:>5}: {1}".format(ln + 1, (m.get("Text") or "").rstrip()))
        if f.get("Truncated"):
            lines.append("        ... more matches in this file not shown")
        lines.append("")
    if not files:
        lines.append("No matches.")
    return lines, rows


def _selected_word(window):
    view = window.active_view() if window else None
    if view is None:
        return ""
    sel = view.sel()
    if not sel:
        return ""
    r = sel[0]
    if r.empty():
        r = view.word(r) if hasattr(view, "word") else r
    text = view.substr(r) if not r.empty() else ""
    return text if "\n" not in text else ""


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Full Text Search
    Command: =org_orgs_text_search=  Keys: none
    Args: =query=, =in_view= (default =false=), =current_file= (default =false=), =ignore_case=

    A regular expression search over the text of every org file the server
    has. Asks for the expression (prefilled with the selection, the word at
    the cursor, or the last search). The matches go in a quick panel
    (=file, line: text=, previewed as you move) or, with =in_view= (palette:
    "Org: orgs Full Text Search (results view)"), in an "Org Text Search"
    view laid out like Find Results, where =enter= or a double click opens
    a match and =g= searches again. =current_file= (palette: "... in This
    File") searches only the active file. =ignore_case= overrides
    =orgsTextSearchIgnoreCase=. Needs the orgs server.

    Settings: =orgsTextSearchMax=, =orgsTextSearchIgnoreCase=, =orgsPreviewOnHighlight=.
EDOC */
"""

class OrgOrgsTextSearchCommand(sublime_plugin.WindowCommand):
    """Regular expression search over the text of every org file the server
    has. Results in a quick panel (in_view: in a find results like view)."""
    last = ""

    def run(self, query=None, in_view=False, current_file=False, ignore_case=None):
        self.in_view = in_view
        self.file = None
        if current_file:
            v = self.window.active_view()
            self.file = v.file_name() if v else None
        self.ignore_case = sets.Get("orgsTextSearchIgnoreCase", True) if ignore_case is None else ignore_case
        if query:
            self.Search(query)
            return
        init = _selected_word(self.window) or OrgOrgsTextSearchCommand.last
        self.window.show_input_panel("orgs text search (regex):", init, self.Search, None, None)

    def Search(self, query):
        if not query:
            return
        OrgOrgsTextSearchCommand.last = query
        mx = int(sets.Get("orgsTextSearchMax", 200))
        fn = self.file
        orgs.run(lambda cl: cl.text_search(query, ignore_case=self.ignore_case, max=mx, file=fn),
                 lambda res: self.Show(query, res), what="text search")

    def Show(self, query, res):
        if any(f.get("Truncated") for f in res.get("Files") or []):
            sublime.status_message("orgs: some files had more matches than shown (orgsTextSearchMax)")
        if self.in_view:
            lines, rows = RenderTextSearch(res)
            v = ShowRowsView(self.window, SEARCH_VIEW, lines, rows,
                             ("org_orgs_text_search", {"query": query, "in_view": True,
                                                       "ignore_case": self.ignore_case}),
                             syntax=FIND_SYNTAX)
            v.settings().set("result_file_regex", r"^(\S.*):$")
            v.settings().set("result_line_regex", r"^\s+(\d+):")
            return
        matches = FlattenMatches(res)
        if not matches:
            sublime.status_message("orgs: no matches for " + query)
            return
        items = [["{0}:{1}: {2}".format(orgsui.short_file(fn), ln + 1, text.strip()), fn] for fn, ln, text in matches]
        window = self.window

        def done(i):
            if i >= 0:
                orgsui.goto(window, matches[i][0], matches[i][1])

        def highlight(i):
            if i >= 0 and sets.Get("orgsPreviewOnHighlight", True):
                orgsui.goto(window, matches[i][0], matches[i][1], transient=True)
        window.show_quick_panel(items, done, 0, 0, highlight)

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# File history

def HistoryItems(res):
    """Quick panel rows and revisions for a /history answer."""
    items, revs = [], []
    detail = []
    if res.get("Modified"):
        detail.append("modified +{0} -{1}".format(res.get("ModifiedAdd", 0), res.get("ModifiedDel", 0)))
    if res.get("Untracked"):
        detail.append("untracked")
    items.append(["Working copy (on disk)", ", ".join(detail) or "as saved"])
    revs.append(("WORKING", "working copy"))
    if res.get("Staged"):
        items.append(["Index (staged)", "+{0} -{1}".format(res.get("StagedAdd", 0), res.get("StagedDel", 0))])
        revs.append(("INDEX", "index"))
    for c in res.get("Commits") or []:
        when = datetime.datetime.fromtimestamp(c.get("When") or 0).strftime("%Y-%m-%d %H:%M")
        refs = " (" + ", ".join(c["Refs"]) + ")" if c.get("Refs") else ""
        items.append(["{0} {1}".format(c.get("Short", ""), c.get("Subject", "")),
                      "{0}  {1}  +{2} -{3}{4}".format(c.get("Author", ""), when, c.get("Added", 0),
                                                      c.get("Removed", 0), refs)])
        revs.append((c.get("Hash"), c.get("Short") or c.get("Hash", "")[:7]))
    return items, revs


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs File History
    Command: =org_orgs_file_history=  Keys: none

    The git history of the current file, from the server: a quick panel
    lists the working copy (with its uncommitted change counts), the index
    when something is staged, and up to =orgsHistoryLimit= commits (subject,
    author, date, lines added and removed). Picking one opens the file as it
    was then in a read only scratch view. Needs the orgs server, and the
    file in a git repository on the server's side.
EDOC */
"""

class OrgOrgsFileHistoryCommand(sublime_plugin.TextCommand):
    """The git history of this file (from the server): pick a commit to see
    the file as it was."""
    def run(self, edit):
        fn = self.view.file_name()
        if not fn:
            sublime.status_message("orgs: this buffer has no file")
            return
        window = self.view.window()

        def got(res):
            if not res.get("InRepo"):
                sublime.status_message("orgs: {0} is not in a git repository".format(os.path.basename(fn)))
                return
            items, revs = HistoryItems(res)
            if res.get("Truncated"):
                sublime.status_message("orgs: showing the newest {0} commits".format(len(revs) - 1))

            def pick(i):
                if i < 0:
                    return
                rev, short = revs[i]
                orgs.run(lambda cl: cl.history_text(fn, rev), lambda r: self.Show(window, fn, short, r),
                         what="history")
            window.show_quick_panel(items, pick)
        orgs.run(lambda cl: cl.history(fn, int(sets.Get("orgsHistoryLimit", 100))), got, what="history")

    def Show(self, window, fn, short, res):
        if res.get("Missing"):
            sublime.status_message("orgs: {0} did not exist at {1}".format(os.path.basename(fn), short))
            return
        orgsui.scratch_view(window, "{0} @ {1}".format(os.path.basename(fn), short), res.get("Text", ""),
                            reuse=True)

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())
