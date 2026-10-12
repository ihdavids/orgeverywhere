"""The agenda's view of a heading, from the orgs server.

GET /agenda and /agenda/nodes send an AgendaNode for every heading the agenda
shows. AgendaNode below wraps one so the agenda's filter, sort and column code
can keep reading it the way it read an orgparse node (todo, tags, scheduled,
clock, get_property, children, parent...). Dates are this repo's orgparse
OrgDate objects, parsed from the stamp text the server sends, so
after_duration, repeating, deadline_start etc. work as before.

AgendaData is one answer from the server: the nodes, and for /agenda the
occurrences of each day (what lands where, worked out by the server).
"""
import datetime
import os
import re

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgparse.date as orgdate

# Stamps written in a headline (shown separately by the agenda) and links
# (shown by their description, as orgparse's plain heading did).
RE_HEADLINE_STAMP = re.compile(
    r"\s*(?:<\d{4}-\d{2}-\d{2}[^>\n]*>(?:--<\d{4}-\d{2}-\d{2}[^>\n]*>)?"
    r"|\[\d{4}-\d{2}-\d{2}[^\]\n]*\](?:--\[\d{4}-\d{2}-\d{2}[^\]\n]*\])?)")
RE_LINK = re.compile(r"\[\[([^\]]*)\](?:\[([^\]]*)\])?\]")


def DisplayHeading(headline):
    s = RE_LINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), headline or "")
    s = RE_HEADLINE_STAMP.sub("", s)
    return " ".join(s.split())


def ParseDay(s):
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' -> datetime (midnight for a day)."""
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.datetime.strptime(s[:16], "%Y-%m-%dT%H:%M")
        return datetime.datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def ParseDate(s):
    d = ParseDay(s)
    return d.date() if d else None


def _sdc(cls, keyword, ad):
    """An AgendaDate as the orgparse SCHEDULED/DEADLINE/CLOSED object
    (a false one, like orgparse's, when there is none)."""
    if ad and ad.get("Text"):
        d = cls.from_str(keyword + ": " + ad["Text"])
        if d:
            return d
    return cls(None)


def _stamp(ad):
    if ad and ad.get("Text"):
        ds = orgdate.OrgDate.list_from_str(ad["Text"])
        if ds:
            return ds[0]
    return None


class AgendaClock(orgdate.OrgDateClock):
    """A CLOCK line. A running clock has no end; its duration runs to now."""
    @property
    def duration(self):
        end = self.end if self.end is not None else datetime.datetime.now()
        return end - self.start


class Env:
    def __init__(self, todo_keys, done_keys):
        self.todo_keys = list(todo_keys or [])
        self.done_keys = list(done_keys or [])
        self.all_todo_keys = self.todo_keys + self.done_keys


class AgendaFile:
    """What the agenda shows and opens for a file."""
    def __init__(self, filename):
        self.filename = filename

    def AgendaFilenameTag(self):
        return os.path.splitext(os.path.basename(self.filename or ""))[0] + ":"


class RootStub:
    """The file above a top level heading."""
    heading = ""
    todo = None
    priority = None
    level = 0
    parent = None
    children = []
    num_children = 0
    tags = set()
    shallow_tags = set()

    def is_root(self):
        return True

    def get_property(self, key, val=None):
        return val


_ROOT = RootStub()


class AgendaNode:
    """An AgendaNode dict with the orgparse node interface the agenda uses."""
    is_server_node = True

    def __init__(self, raw, data=None, stub=False):
        self.raw = raw
        self.data = data
        self.stub = stub
        self.filename = raw.get("Filename") or ""
        self.hash = raw.get("Hash") or ""
        self.headline = raw.get("Headline") or ""
        self.heading = DisplayHeading(self.headline)
        self.todo = raw.get("Status") or None
        self.priority = raw.get("Priority") or None
        self.level = raw.get("Level") or 0
        self.start_row = raw.get("LineNum")
        self.olp = list(raw.get("Olp") or [])
        self.shallow_tags = set(raw.get("Tags") or [])
        self.tags = set(raw.get("AllTags") or []) | self.shallow_tags
        self.env = Env(raw.get("TodoKeys"), raw.get("DoneKeys"))
        self.is_todo = bool(raw["IsTodo"]) if "IsTodo" in raw else bool(self.todo and self.todo in self.env.todo_keys)
        self.is_done = bool(raw["IsDone"]) if "IsDone" in raw else bool(self.todo and self.todo in self.env.done_keys)
        self.is_archived = bool(raw["IsArchived"]) if "IsArchived" in raw else "ARCHIVE" in self.tags
        self.is_habit = bool(raw.get("IsHabit"))
        self._props = dict(raw.get("Props") or {})
        self._propsUpper = dict((k.upper(), v) for k, v in self._props.items())
        self.scheduled = _sdc(orgdate.OrgDateScheduled, "SCHEDULED", raw.get("Scheduled"))
        self.deadline = _sdc(orgdate.OrgDateDeadline, "DEADLINE", raw.get("Deadline"))
        self.closed = _sdc(orgdate.OrgDateClosed, "CLOSED", raw.get("Closed"))
        self._timestamps = [t for t in (_stamp(ad) for ad in (raw.get("Timestamps") or [])) if t]
        self.clock = []
        for c in raw.get("Clocks") or []:
            s = ParseDay(c.get("Start"))
            if s:
                self.clock.append(AgendaClock(s, ParseDay(c.get("End")), c.get("Mins")))
        self.repeated_tasks = []
        for d in raw.get("DoneDates") or []:
            dd = ParseDate(d)
            if dd:
                self.repeated_tasks.append(orgdate.OrgDateRepeatedTask(dd, "TODO", "DONE"))
        kids = raw.get("Children") or {}
        self.num_children = kids.get("Count") or 0
        self._childStates = list(kids.get("States") or [])
        self._children = None
        self._parent = None

    def __repr__(self):
        return "AgendaNode({0!r}, {1}:{2})".format(self.heading, os.path.basename(self.filename), self.start_row)

    # orgparse node interface ------------------------------------------
    def is_root(self):
        return False

    def get_property(self, key, val=None):
        if key in self._props:
            return self._props[key]
        return self._propsUpper.get((key or "").upper(), val)

    @property
    def properties(self):
        return dict(self._props)

    def get_timestamps(self, active=False, inactive=False, range=False, point=False):
        out = []
        for t in self._timestamps:
            if ((active and t.is_active()) or (inactive and not t.is_active())) and \
               ((range and t.has_end()) or (point and not t.has_end())):
                out.append(t)
        return out

    def priorities(self):
        return list(sets.Get("priorities", ["A", "B", "C", "D", "E"]))

    def get_locator(self):
        olp = self.olp or [self.headline]
        return ":".join(DisplayHeading(h) for h in olp)

    def _key(self):
        return (os.path.normcase(self.filename), self.start_row)

    @property
    def parent(self):
        """The heading above: the node the answer has for it (found by the
        server's Parent hash, so a same-titled twin is never taken), else a
        stub with its title."""
        if self._parent is None:
            if self.level <= 1 or len(self.olp) < 2:
                self._parent = _ROOT
            else:
                p = self.data.ParentOf(self) if (self.data and not self.stub) else None
                if p is None:
                    p = AgendaNode({"Filename": self.filename, "Headline": self.olp[-2], "Level": self.level - 1,
                                    "Hash": self.raw.get("Parent") or "", "Olp": self.olp[:-1],
                                    "TodoKeys": self.env.todo_keys, "DoneKeys": self.env.done_keys},
                                   self.data, stub=True)
                self._parent = p
        return self._parent

    @property
    def children(self):
        """The direct children: those the answer has as nodes, plus a stub
        (just its keyword) for each of the others."""
        if self._children is None:
            real = self.data.ChildrenOf(self) if (self.data and not self.stub) else []
            states = list(self._childStates)
            for c in real:
                if (c.todo or "") in states:
                    states.remove(c.todo or "")
            stubs = [AgendaNode({"Filename": self.filename, "Status": s, "Level": self.level + 1,
                                 "TodoKeys": self.env.todo_keys, "DoneKeys": self.env.done_keys},
                                self.data, stub=True)
                     for s in states[:max(0, self.num_children - len(real))]]
            self._children = real + stubs
        return self._children


class Occurrence:
    """One heading landing on one day (an AgendaEntry)."""
    def __init__(self, raw, node, day):
        self.raw = raw
        self.node = node
        self.day = day
        self.kind = raw.get("Kind") or ""
        self.start = ParseDay(raw.get("Start")) or datetime.datetime.combine(day, datetime.time())
        self.end = ParseDay(raw.get("End"))
        self.has_time = bool(raw.get("HasTime"))
        self.repeat = bool(raw.get("Repeat"))
        self.late = raw.get("Late") or 0
        due = raw.get("DueIn")
        self.due_in = -1 if due is None else due
        self.done = bool(raw.get("Done"))

    def __repr__(self):
        return "Occurrence({0}, {1}, {2})".format(self.kind, self.start, self.node.heading)


class AgendaData:
    """One answer from /agenda or /agenda/nodes."""
    def __init__(self, nodes, days=None, today=None, error=None):
        self.error = error
        self.today = ParseDate(today) if today else None
        self._files = {}
        self.nodes = [AgendaNode(r, self) for r in (nodes or [])]
        # Headings are told apart by file and row: two siblings can share a
        # title (and so an outline path and a hash).
        self._byRow = {}
        self._byHash = {}
        for n in self.nodes:
            self._byRow.setdefault(n._key(), n)
            if n.hash:
                self._byHash.setdefault((n._key()[0], n.hash), []).append(n)
        for v in self._byHash.values():
            v.sort(key=lambda n: n.start_row if n.start_row is not None else -1)
        self._outline = {}
        self._kids = {}
        for n in self.nodes:
            p = self.ParentOf(n)
            if p is not None:
                self._kids.setdefault(id(p), []).append(n)
        self.days = {}
        for d in days or []:
            day = ParseDate(d.get("Date"))
            if day is None:
                continue
            occ = []
            for e in d.get("Entries") or []:
                i = e.get("Node")
                if isinstance(i, int) and 0 <= i < len(self.nodes):
                    occ.append(Occurrence(e, self.nodes[i], day))
            self.days[day] = occ

    @staticmethod
    def FromResult(res, error=None):
        res = res or {}
        return AgendaData(res.get("Nodes"), res.get("Days"), res.get("Today"), error)

    def File(self, filename):
        f = self._files.get(filename)
        if f is None:
            f = self._files[filename] = AgendaFile(filename)
        return f

    def Find(self, filename, row):
        return self._byRow.get((os.path.normcase(filename or ""), row))

    def Owner(self, filename, h, row):
        """The node with hash h whose subtree holds row: of the headings with
        hash h (should two share one), the last one above row."""
        best = None
        if row is None:
            return None
        for n in self._byHash.get((os.path.normcase(filename or ""), h), []):
            if n.start_row is not None and n.start_row < row:
                best = n
        return best

    def ParentOf(self, node):
        """The node of node's parent, when the answer has it."""
        ph = node.raw.get("Parent")
        if not ph or node.stub:
            return None
        return self.Owner(node.filename, ph, node.start_row)

    def ChildrenOf(self, node):
        return list(self._kids.get(id(node), []))

    def SetOutline(self, filename, headings):
        """Every heading of a file (Todo dicts with LineNum and Level, as
        /filecontents/headings sends them): IsUnder is then exact for it."""
        rows = sorted((x.get("LineNum"), x.get("Level") or 0) for x in headings or [] if x.get("LineNum") is not None)
        self._outline[os.path.normcase(filename or "")] = rows

    def IsUnder(self, d, a):
        """d is somewhere below a (a node of this answer, or a stub parent)."""
        if d is a or a.is_root() or not d.filename or d.start_row is None:
            return False
        f = os.path.normcase(d.filename)
        if f != os.path.normcase(a.filename or ""):
            return False
        if len(d.olp) <= len(a.olp) or d.olp[:len(a.olp)] != a.olp:
            return False
        if a.stub or a.start_row is None:
            return True
        if not a.start_row < d.start_row:
            return False
        outline = self._outline.get(f)
        if outline is not None:
            # a's subtree runs to the next heading at its level or above.
            return not any(a.start_row < r < d.start_row and lvl <= a.level for r, lvl in outline)
        # Up d's parents (by the server's Parent hash) while the answer has
        # them; past one it does not, no heading of the answer at a's level
        # or above may come between a and d.
        p = d
        while True:
            q = self.ParentOf(p)
            if q is None:
                break
            if q is a:
                return True
            if q.start_row is None or q.start_row < a.start_row:
                return False
            p = q
        if not p.raw.get("Parent") or p.level <= a.level + 1:
            return False
        return not any(n.start_row is not None and a.start_row < n.start_row < d.start_row and n.level <= a.level
                       and os.path.normcase(n.filename) == f for n in self.nodes)

    def Descendants(self, a):
        return [d for d in self.nodes if self.IsUnder(d, a)]

    def On(self, day):
        """The occurrences on a date (a date or datetime), in the server's order."""
        if isinstance(day, datetime.datetime):
            day = day.date()
        return self.days.get(day, [])

    def Covers(self, day):
        if isinstance(day, datetime.datetime):
            day = day.date()
        return day in self.days
