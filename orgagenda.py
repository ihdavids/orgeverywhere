import re
import os
import sublime, sublime_plugin
import datetime
import OrgEverywhere.orgparse.date as orgdate
import OrgEverywhere.orgutil.util as util
import OrgEverywhere.orgduration as dur
import logging
import OrgEverywhere.orgdb as db
import OrgEverywhere.orgdatepicker as dpick
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orginsertselected as insSel
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.client as orgsclient
import OrgEverywhere.orgagendanode as agnode
from OrgEverywhere.orgs.paths import PathMap
import uuid

import dateutil.parser as dp
import dateutil.relativedelta as drel

log = logging.getLogger(__name__)
AGENDA_VIEW = "Org Mode Agenda"
TODO_VIEW   = "Org Todos"
NOT_CONNECTED_MSG = "orgs server not connected - run Org: Connect to orgs server"

ViewMappings = {}


# The agenda is built from the orgs server, which reads saved files: unsaved
# buffers are not parsed here any more (see UnsavedOrgBuffers). Kept for
# callers in other modules.
def ReloadAllUnsavedBuffers():
    pass

def UnsavedOrgBuffers():
    """Open org views (in every window) with unsaved changes (ui thread)."""
    out = []
    for w in sublime.windows():
        for view in w.views():
            try:
                if view.is_dirty() and util.isPotentialOrgFileOrBuffer(view):
                    out.append(view)
            except Exception:
                pass
    return out

def UnsavedNote():
    n = len(UnsavedOrgBuffers())
    if n == 0:
        return None
    return "{0} unsaved org buffer{1} not in the agenda - save to include {2}".format(
        n, "s are" if n > 1 else " is", "them" if n > 1 else "it")

def IsRawDate(ts):
    return isinstance(ts,datetime.date) or isinstance(ts,datetime.datetime)

def EnsureDateTime(ts):
    if(ts and not isinstance(ts,datetime.datetime)):
        return datetime.datetime.combine(ts, datetime.datetime.min.time())
    return ts

def EnsureDate(ts):
    if(isinstance(ts,datetime.datetime)):
        return ts.date()
    return ts

def FindMappedView(view):
    if(view.name() in ViewMappings):
        return ViewMappings[view.name()]
    log.debug("Could not find view named: " + view.name())
    return None

def move_file_other_group(myview, view):
    window = sublime.active_window()
    if (window.num_groups() < 2):
        #self.window.run_command('clone_file')
        window.set_layout({
            "cols": [0.0, 0.5, 1.0],
            "rows": [0.0, 1.0],
            "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]
            })
        mygroup    = 0
        othergroup = 1
    else:
        window.focus_view(view)
        mygroup    = 1
        othergroup = 0
        if (window.get_view_index(myview)[0] == 0):
            othergroup = 1
            mygroup    = 0
    window.focus_view(view)
    window.run_command('move_to_group', {'group': othergroup})
    window.run_command('focus_group', {'group': mygroup})
    window.focus_view(myview)

def CreateUniqueViewNamed(name, mapped=None):
    # Close the view if it exists
    win = sublime.active_window()
    for view in win.views():
        if view.name() == name:
            win.focus_view(view)
            win.run_command('close')
    win.run_command('new_file')
    view = win.active_view()
    view.set_name(name)
    # TODO: Change this.
    view.set_syntax_file("Packages/OrgEverywhere/orgagenda.sublime-syntax")
    ViewMappings[view.name()] = mapped
    return view


def IsPhone(n):
    return n and n.todo and "PHONE" in n.todo

def IsMeeting(n):
    return n and n.todo and "MEETING" in n.todo

def IsNote(n):
    return n and n.todo and "NOTE" in n.todo

# Agenda nodes come from the server, which says what is a todo, done or
# archived (by the heading's own file). orgparse nodes (other modules still
# hand them in) are judged the old way.
def IsTodo(n):
    if getattr(n, "is_server_node", False):
        return n.is_todo
    return n.todo and n.todo in n.env.todo_keys

def IsDone(n):
    if getattr(n, "is_server_node", False):
        return n.is_done
    return n.todo and n.todo in n.env.done_keys

def IsArchived(n):
    if getattr(n, "is_server_node", False):
        return n.is_archived
    return "ARCHIVE" in n.tags

def HasChildTasks(n):
    for c in n.children:
        if(IsTodo(c)):
            return True
    return False

# Task that belongs to a project. This means we have to have a parent and that parent has to be
# identified as a project.
def IsProjectTask(n):
    if(not n or n.is_root()):
        return False
    return (IsTodo(n) and n.parent and not n.parent.is_root() and IsProject(n.parent))

def IsBlockedProject(n):
    if(IsProject(n) and n.num_children > 0):
        isBlocked = True
        for c in n.children:
            if(c.todo and c.todo == 'NEXT'):
                isBlocked = False
        return isBlocked
    else:
        return False

def IsProjectTodoWithTodos(n):
    if(IsTodo(n) and n.num_children > 0):
        for c in n.children:
            if(IsTodo(c)):
                return True
    return False

def IsProjectTaskWithProjectTag(n):
    if(n):
        tags = n.shallow_tags
        if("PROJECT" in tags or "Project" in tags or "project" in tags):
            return True
    return False

def IsProjectTaskWithProjectProperty(n):
    if(n and (n.get_property("PROJECT") or n.get_property("Project") or n.get_property("project"))):
        return True
    return False

def ProjectMode():
    return sets.Get("agendaProjectIs", "nested_todo").lower()

def IsProject(n):
    projType = ProjectMode()
    if(projType == "nested_todo"):
        return IsProjectTodoWithTodos(n)
    if(projType == "tag"):
        return IsProjectTaskWithProjectTag(n)
    if(projType == "property"):
        return IsProjectTaskWithProjectProperty(n)
    return IsProjectTodoWithTodos(n)

def HasTimestamp(n):
    if(not n):
        return False
    timestamps = n.get_timestamps(active=True,point=True,range=True)
    return n.scheduled or (timestamps and len(timestamps) > 0) or n.deadline

def Overlaps(s,e,rs,re):
    # s | e |
    # +---+
    if(s <= rs and e >= rs and e <= re):
        return True
    # | s | e
    #   +---+
    if(s >= rs and s < re and e >= re):
        return True
    # | s  e |
    #   +-+
    if(s >= rs and e <= re):
        return True
    # s |    | e
    # +-------+
    if(s <= rs and e >= re):
        return True
    return False

# OrgDate.before_duration/after_duration compare with a datetime, which a
# date without a time is not.
def BeforeDuration(d, duration):
    return EnsureDateTime(d.start) <= (datetime.datetime.now() + duration.timedelta())

def AfterDuration(d, duration):
    end = d.end if d.end is not None else d.start
    return EnsureDateTime(end) >= (datetime.datetime.now() - duration.timedelta())

# ----------------------------------------------------------------------
# Timed occurrences. The server says when an occurrence is on a day (a range
# clipped to the day); a point in time is drawn DEFAULT_SLOT_MINS long.
DEFAULT_SLOT_MINS = 30

class TimedItem:
    """A timed occurrence of an agenda entry, placed in the day's slots."""
    def __init__(self, entry, occ):
        self.entry = entry
        self.occ = occ
        self.node = entry['node']
        self.start = occ.start
        self.s = occ.start.hour * 60 + occ.start.minute
        e = occ.end
        if e is not None and e.date() == occ.start.date() and e > occ.start:
            self.e = e.hour * 60 + e.minute
        elif e is not None and e.date() > occ.start.date():
            self.e = 24 * 60 - 1
        else:
            self.e = self.s + DEFAULT_SLOT_MINS

    def InMinutes(self, rs, re):
        return Overlaps(self.s, self.e, rs, re)

    def InHour(self, hour):
        return self.InMinutes(hour * 60, hour * 60 + 59)

def bystartitem(item):
    return item.start

# How far into its heading a week view slot is. An item that started before
# the first slot shown (a range from the day before) starts its heading there.
def distanceFromStart(item, hour, minSlot, firstHour=0):
    s = max(item.s, firstHour * 60)
    return 5*(hour - s // 60) + (minSlot - int((s % 60)/12))

# ================================================================================
# IDEA Make a base class that has all the functionality needed to
#      render an agenda view. Then create an agenda folder with
#      all my views, like dynamic blocks.
#
#      The goal being to allow people to extend and create custom
#      agenda views from their user folder, without having to
#      write all the "stuff"
#
#      Also create a BUNCH of filters that filter by tag, properties
#      and other things so there are example versions of each.
#
# statefilter
# tagfilter
# priorityfilter
class AgendaBaseView:
    def __init__(self, name, setup=True, **kwargs):
        self.name = name
        # What the server sent for this view (agnode.AgendaData); set by
        # FetchViews before the entries are filtered.
        self.data = None
        self.fetchedMore = None
        self.fetchError = None
        self.SetTagFilter(kwargs)
        self.SetPriorityFilter(kwargs)
        self.SetStateFilter(kwargs)
        self.SetDurationFilter(kwargs)
        self.SetClockedDurationFilter(kwargs)
        self.SetDateFilter(kwargs)
        self.hasclock = "hasclock" in kwargs
        self.clockedtoday = "clockedtoday" in kwargs
        self.clockfilter = "clockfilter" in kwargs
        self.hasclose = "hasclose" in kwargs
        self.hasdeadline = "hasdeadline" in kwargs
        self.hasschedule = "hasschedule" in kwargs
        self.noclock = "noclock" in kwargs
        self.noclose = "noclose" in kwargs
        self.nodeadline = "nodeadline" in kwargs
        self.noschedule = "noschedule" in kwargs
        self.onlyTasks  = "onlytasks" in kwargs
        self.haschildtasks = "haschildtasks" in kwargs
        self.nochildtasks = "nochildtasks" in kwargs

        if(setup):
            self.SetupView()
        else:
            self.BasicSetup()

    # Default view does not have a filter view
    # Only todo view
    def OpenFilterView(self):
        pass

    def BasicSetup(self):
        self.UpdateNow()
        self.entries = []

    def SetDurationFilter(self,kwargs):
        self._beforeDuration     = []
        self._afterDuration      = []
        if("durationfilter" in kwargs):
            self._durationFilter = kwargs["durationfilter"]
        else:
            self._durationFilter = None
            return
        tags = self._durationFilter.split(' ')
        for tag in tags:
            tag = tag.strip()
            if not tag or len(tag) <= 0:
                continue
            m = RE_IN_OUT_TAG.search(tag)
            if(m):
                inout = m.group('inout')
                tagdata = m.group('tag')
                if(not inout or inout == '+'):
                    self._beforeDuration.append(dur.OrgDuration.Parse(tagdata.strip()))
                else:
                    self._afterDuration.append(dur.OrgDuration.Parse(tagdata.strip()))
    
    def SetClockedDurationFilter(self,kwargs):
        self._beforeClockedDuration     = []
        self._afterClockedDuration      = []
        if("clockfilter" in kwargs):
            self._clockedDurationFilter = kwargs["clockfilter"]
        else:
            self._clockedDurationFilter = None
            return
        tags = self._clockedDurationFilter.split(' ')
        for tag in tags:
            tag = tag.strip()
            if not tag or len(tag) <= 0:
                continue
            m = RE_IN_OUT_TAG.search(tag)
            if(m):
                inout = m.group('inout')
                tagdata = m.group('tag')
                if(not inout or inout == '+'):
                    self._beforeClockedDuration.append(dur.OrgDuration.Parse(tagdata.strip()))
                else:
                    self._afterClockedDuration.append(dur.OrgDuration.Parse(tagdata.strip()))

    def SetStateFilter(self,kwargs):
        if("statefilter" in kwargs):
            self._stateFilter = kwargs["statefilter"]
        else:
            self._stateFilter = None
            return
        self._inStateTags     = []
        self._oneofStateTags  = []
        self._outStateTags    = []
        tags = self._stateFilter.split(' ')
        for tag in tags:
            tag = tag.strip()
            if not tag or len(tag) <= 0:
                continue
            m = RE_IN_OUT_TAG.search(tag)
            if(m):
                inout = m.group('inout')
                tagdata = m.group('tag')
                if(not inout or inout == '+'):
                    self._inStateTags.append(tagdata.strip())
                elif(inout == '|'):
                    self._oneofStateTags.append(tagdata.strip())
                else:
                    self._outStateTags.append(tagdata.strip())

    def SetPriorityFilter(self,kwargs):
        if("priorityfilter" in kwargs):
            self._priorityFilter = kwargs["priorityfilter"]
        else:
            self._priorityFilter = None
            return
        self._inPriorityTags     = []
        self._oneofPriorityTags  = []
        self._outPriorityTags    = []
        tags = self._priorityFilter.split(' ')
        for tag in tags:
            tag = tag.strip()
            if not tag or len(tag) <= 0:
                continue
            m = RE_IN_OUT_TAG.search(tag)
            if(m):
                inout = m.group('inout')
                tagdata = m.group('tag')
                if(not inout or inout == '+'):
                    self._inPriorityTags.append(tagdata.strip())
                elif(inout == '|'):
                    self._oneofPriorityTags.append(tagdata.strip())
                else:
                    self._outPriorityTags.append(tagdata.strip())

    def SetTagFilter(self,kwargs):
        if("tagfilter" in kwargs):
            self._tagfilter = kwargs["tagfilter"]
        else:
            self._tagfilter = None
            return
        self._intags     = []
        self._oneoftags  = []
        self._outtags    = []
        tags = self._tagfilter.split(' ')
        for tag in tags:
            tag = tag.strip()
            if not tag or len(tag) <= 0:
                continue
            m = RE_IN_OUT_TAG.search(tag)
            if(m):
                inout = m.group('inout')
                tagdata = m.group('tag')
                if(not inout or inout == '+'):
                    self._intags.append(tagdata.strip())
                elif(inout == '|'):
                    self._oneoftags.append(tagdata.strip())
                else:
                    self._outtags.append(tagdata.strip())

    def SetDateFilter(self, kwargs):
        self._startDoneDateComparator = None
        self._endDoneDateComparator = None

        if "datefilter" not in kwargs:
            return

        DATE_PATTERN = r"([><=]+)(\d+)"
        datefilter = kwargs["datefilter"].split(" ")
        for cond in datefilter:
            match = re.search(DATE_PATTERN, cond)
            if match is None:
                continue
            operator = match.group(1)
            date_str = match.group(2)

            try:
                date = dp.parse(date_str)
            except:
                log.error("Failed to parse datefilter")
                continue

            if operator == ">":
                self._startDoneDateComparator = lambda d, date=date: d is not None and date < d
            if operator == ">=":
                self._startDoneDateComparator = lambda d, date=date: d is not None and date <= d
            if operator == "<":
                self._endDoneDateComparator = lambda d, date=date: d is not None and d < date
            if operator == "<=":
                date = date + datetime.timedelta(days=1)
                self._endDoneDateComparator = lambda d, date=date: d is not None and d < date

    def MatchHas(self, node):
        if(self.hasclock and not node.clock):
            return False
        if(self.hasdeadline and not node.deadline):
            return False
        if(self.hasclose and not node.closed):
            return False
        if(self.hasschedule and not node.scheduled):
            return False
        if(self.haschildtasks and not HasChildTasks(node)):
            return False
        if(self.noclock and node.clock):
            return False
        if(self.nodeadline and node.deadline):
            return False
        if(self.noclose and node.closed):
            return False
        if(self.noschedule and node.scheduled):
            return False
        if(self.nochildtasks and HasChildTasks(node)):
            return False
        return True

    def MatchTags(self, node):
        if(not self._tagfilter):
            return True
        if(self._intags and len(self._intags) > 0 and not all(elem in node.tags  for elem in self._intags)):
            return False
        if(self._outtags and any(elem in node.tags for elem in self._outtags)):
            return False
        if(self._oneoftags and len(self._oneoftags) > 0 and not any(elem in node.tags for elem in self._oneoftags)):
            return False
        return True

    def MatchPriorities(self, node):
        if(not self._priorityFilter):
            return True
        p = node.priority or ""
        if(self._inPriorityTags and len(self._inPriorityTags) > 0 and not all(elem in p  for elem in self._inPriorityTags)):
            return False
        if(self._outPriorityTags and any(elem in p for elem in self._outPriorityTags)):
            return False
        if(self._oneofPriorityTags and len(self._oneofPriorityTags) > 0 and not any(elem in p for elem in self._oneofPriorityTags)):
            return False
        return True
    
    def MatchState(self, node):
        t = node.todo
        if(not node.todo):
            t = ""
        if(not self._stateFilter):
            return True
        if(self._inStateTags and len(self._inStateTags) > 0 and not all(re.search(elem,t) for elem in self._inStateTags)):
            return False
        if(self._outStateTags and any(re.search(elem,t) for elem in self._outStateTags)):
            return False
        if(self._oneofStateTags and len(self._oneofStateTags) > 0 and not any(re.search(elem,t) for elem in self._oneofStateTags)):
            return False
        return True

    def MatchDuration(self, node):
        t = node.closed
        if(t and self._afterDuration and any(not AfterDuration(t, elem) for elem in self._afterDuration)):
            return False
        if self._beforeDuration:
            s = node.scheduled
            d = node.deadline
            ts = node.get_timestamps()
            if(s and any(s and BeforeDuration(s, elem) for elem in self._beforeDuration)):
                return True
            if(d and any(d and BeforeDuration(d, elem) for elem in self._beforeDuration)):
                return True
            if(ts and len(ts) > 0 and any(ts[0] and BeforeDuration(ts[0], elem) for elem in self._beforeDuration)):
                return True
            return False
        return True

    def MatchDate(self, node):
        start = node.closed.start
        end = node.closed.end if node.closed.end is not None else start
        if self._startDoneDateComparator is not None \
                and not self._startDoneDateComparator(start):
            return False
        if self._endDoneDateComparator is not None \
                and not self._endDoneDateComparator(end):
            return False
        return True

    def MatchClock(self, node):
        if self.clockedtoday:
            if not node.clock:
                return False
            now = datetime.datetime.now()
            for c in node.clock:
                if c.start.date() == now.date():
                    return True
                # A running clock has no end yet.
                if c.end is None or c.end.date() == now.date():
                    return True
            return False
        if self.clockfilter:
            if not node.clock:
                return False
            for t in node.clock:
                if t:
                    if(self._afterClockedDuration and any(not t.after_duration(elem) for elem in self._afterClockedDuration)):
                        return False
                    if(self._beforeClockedDuration and any(not t.before_duration(elem) for elem in self._beforeClockedDuration)):
                        return False
        return True


    def SetupView(self):
        self.view = CreateUniqueViewNamed(self.name, self)
        self.view.set_read_only(True)
        self.view.set_scratch(True)
        self.view.set_name(self.name)
        self.BasicSetup()
        self.FilterEntries()
        # Keep ourselves attached to this agenda
        # This doesn't work BOO
        self.view.agenda = self

    def DoRenderView(self,edit, clear = False):
        self.StartEditing()
        if clear:
            self.view.erase(edit, sublime.Region(0, self.view.size()))
        # The agenda reads the files the orgs server knows about. Without it
        # there is nothing to show, and an empty agenda would look like a
        # quiet day.
        if not orgs.connected():
            self.view.insert(edit, self.view.size(), NOT_CONNECTED_MSG + "\n\n")
        else:
            err = self.FetchError()
            if err:
                self.view.insert(edit, self.view.size(), "agenda: " + err + "\n")
            # The server only sees saved files.
            note = UnsavedNote()
            if note:
                self.view.insert(edit, self.view.size(), note + "\n")
        self.RenderView(edit, False)
        self.DoneEditing()

    # What this view needs from the server: ("agenda", from, to) for the
    # days it shows, ("nodes", query) for a list, or None. FetchViews asks
    # every view first, then fetches on a worker and sets self.data.
    def Spec(self):
        return None

    def Leaves(self):
        return [self]

    # (worker) Anything else the view needs from the server to draw data,
    # fetched along with it; set as self.fetchedMore.
    def FetchMore(self, cl, data):
        return None

    def FetchError(self):
        return self.fetchError

    def OpenFilterView(self):
        first = True
        for v in self.agendaViews:
            v.view = self.view
            v.OpenFilterView()


    def InsertAgendaHeading(self, edit):
        if(hasattr(self,'_tagfilter') and self._tagfilter):
            self.view.insert(edit, self.view.size(), self.name + "\t\t TAGS( " + self._tagfilter + " )\n")
        else:
            self.view.insert(edit, self.view.size(), self.name + "\n")

    # A new day refetches (FetchViews) before it is filtered and drawn.
    def UpdateNow(self, now=None):
        if(now == None):
            self.now = datetime.datetime.now()
        else:
            self.now = now

    # You have to bookend your editing session with these
    def StartEditing(self):
        self.view.set_read_only(False)

    def DoneEditing(self):
        self.view.set_read_only(True)

    def RestoreCursor(self, pos):
        # Restore the cursor
        self.view.sel().clear()
        self.view.sel().add(pos)

    def AddEntry(self, node, file):
        self.entries.append({"node": node, "file": file})


    def ClearEntriesAt(self):
        for e in self.entries:
            if 'at' in e:
                del e['at']

    def MarkEntryAt(self, entry, ts= None):
        if(not 'at' in entry):
            entry['at'] = []
        entry['at'].append(self.view.rowcol(self.view.size())[0])
        entry['ts'] = ts
    
    def MarkEntryAtRegion(self, entry, reg, ts=None):
        if(not 'at' in entry):
            entry['at'] = []
        entry['at'].append(reg)
        entry['ts'] = ts

    def EntryAt(self, row, col):
        for e in self.entries:
            if 'at' in e:
                for  ea in e['at']:
                    if(type(ea) == int):
                        if(ea == row):
                            return e
                    elif(type(ea) == sublime.Region):
                        if(ea.contains(self.view.text_point(row,col))):
                            return e
        return None

    def At(self, row, col):
        e = self.EntryAt(row, col)
        if e:
            return (e.get('node'), e.get('file'))
        return (None, None)

    def Clear(self, edit):
        self.StartEditing()
        self.view.erase(edit, sublime.Region(0,self.view.size()))
        self.DoneEditing()

    # ----------------------------------------------
    # These are user extended views!
    def RenderView(self, edit, clear=False):
        pass

    # The headings this view considers: what the server sent for it.
    def Candidates(self):
        return self.data.nodes if self.data else []

    def FilterEntries(self):
        # Built from what FetchViews fetched (nothing when not connected;
        # DoRenderView says so). Never touches the network.
        data = self.data
        for n in self.Candidates():
            file = data.File(n.filename)
            if(self.MatchHas(n)
               and self.MatchPriorities(n)
               and self.MatchTags(n)
               and self.MatchState(n)
               and self.MatchDuration(n)
               and self.MatchDate(n)
               and self.MatchClock(n)
               and self.FilterEntry(n, file)):
                self.AddEntry(n, file)

    def FilterEntry(self, node, file):
        pass

    # Date views: the headings with an occurrence on any of these days, and
    # the occurrences on one day of the headings that passed the filters.
    def CandidatesOn(self, days):
        seen = set()
        out = []
        if self.data:
            for d in days:
                for occ in self.data.On(d):
                    if id(occ.node) not in seen:
                        seen.add(id(occ.node))
                        out.append(occ.node)
        return out

    def OccurrencesOn(self, day):
        byNode = dict((id(e['node']), e) for e in self.entries)
        out = []
        if self.data:
            for occ in self.data.On(day):
                e = byNode.get(id(occ.node))
                if e is not None:
                    out.append((e, occ))
        return out

def DaysFrom(first, last):
    out = []
    d = first
    while d <= last:
        out.append(d)
        d += datetime.timedelta(days=1)
    return out

# ============================================================ 
class CalendarView(AgendaBaseView):
    def __init__(self, name, setup=True,**kwargs):
        super(CalendarView, self).__init__(name, setup, **kwargs)
        firstDayIndex = sets.GetWeekdayIndexByName(sets.Get("firstDayOfWeek","Sunday"))
        self.dv = dpick.DateView("orgagenda.now",firstDayIndex = firstDayIndex)

    def UpdateNow(self, now=None):
        if(now == None):
            self.now = datetime.datetime.now()
        else:
            self.now = now
            self.dv.MoveCDateToDate(self.now)

    # The three months the calendar draws.
    def Range(self):
        pm, py = dpick.DateView.PrevMonth(self.now)
        nm, ny = dpick.DateView.NextMonth(self.now)
        first = datetime.date(py, pm, 1)
        last = datetime.date(ny, nm, 1) + drel.relativedelta(months=1) - datetime.timedelta(days=1)
        return (first, last)

    def Spec(self):
        first, last = self.Range()
        return ("agenda", first, last)

    def Candidates(self):
        return self.CandidatesOn(DaysFrom(*self.Range()))

    def AddRepeating(self, date):
        self.dv.AddToDayHighlights(date, "repeat", "orgagenda.blocked")

    def AddTodo(self, date):
        if(isinstance(date,orgdate.OrgDate)):
            date = date.start
        self.dv.AddToDayHighlights(date, "todo", "orgagenda.todo", sublime.DRAW_NO_FILL)

    def RenderView(self, edit, clear=False):
        # The calendar is drawn over the rows from its heading down.
        row = self.view.rowcol(self.view.size())[0]
        self.InsertAgendaHeading(edit)
        self.dv.SetView(self.view)
        self.dv.SetStartRow(row)
        self.dv.headingLines = row + 2
        self.dv.Render(self.now)
        self.dv.ClearDayHighlights("todo")
        self.dv.ClearDayHighlights("repeat")
        # A day with something on it is marked; a repeat marks it as one.
        for day in DaysFrom(*self.Range()):
            occs = self.OccurrencesOn(day)
            if occs:
                date = datetime.datetime.combine(day, datetime.time())
                self.AddTodo(date)
                if any(occ.repeat for e, occ in occs):
                    self.AddRepeating(date)

    def FilterEntry(self, n, filename):
        return (not self.onlyTasks or (IsTodo(n) and not IsDone(n) and not IsArchived(n))) and not IsProject(n) and HasTimestamp(n)

def bystartdate(a, b):
    if a.scheduled.start > b.scheduled.start:
        return 1
    if a.scheduled.start < b.scheduled.start:
        return -1
    return 0

def bystartdatekey(a):
    return a.scheduled.start

def getdatefromnode(n):
    dt = datetime.datetime.min
    timestamps = n.get_timestamps(active=True,point=True,range=True)
    if timestamps and len(timestamps) > 0:
        dt = timestamps[0]
    if n.deadline:
        dt = n.deadline.start
    if n.scheduled:
        dt = n.scheduled.start
    if n.closed:
        dt = n.closed
    if(isinstance(dt,orgdate.OrgDate)):
        dt=dt.start
    if(isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime)):
        return datetime.datetime.combine(dt, datetime.datetime.min.time())
    return dt

def getdate(a):
    n = a['node']
    return getdatefromnode(n)

def getsortkey(a):
    n = a['node']
    dt = getdatefromnode(n)
    result = 0
    if dt:
        result = (dt - datetime.datetime(1970, 1, 1)).total_seconds()
    result = int(result)
    result = float(result)
    result *= 1000
    # Include priority after datetime information
    if n and n.priority and n.priority != "":
        result += (ord(n.priority[0]) - ord('A'))*100
    else:
        result += 0
    # Ensure todos end up before done before archived
    if IsTodo(n):
        result += 1
    if IsDone(n):
        result += 2
    if IsArchived(n):
        result += 3
    return result

def DaySettings():
    dayStart = sets.Get("agendaDayStartTime",6)
    dayEnd   = sets.Get("agendaDayEndTime",19)
    if(dayEnd > 23):
        dayEnd = 23
    if(dayStart < 0):
        dayStart = 0
    if(dayStart > dayEnd):
        dayStart = 0
        dayEnd   = 23
    return (dayStart, dayEnd)

# ============================================================ 
class WeekView(AgendaBaseView):
    def __init__(self, name, setup=True,**kwargs):
        super(WeekView, self).__init__(name, setup, **kwargs)

    def WeekStart(self):
        firstDayIndex = sets.GetWeekdayIndexByName(sets.Get("firstDayOfWeek", "Sunday"))
        return self.now - datetime.timedelta(days=(self.now.weekday() - firstDayIndex) % 7)

    def Range(self):
        numDays = sets.Get("agendaWeekViewNumDays",7)
        first = self.WeekStart().date()
        return (first, first + datetime.timedelta(days=max(numDays, 1) - 1))

    def Spec(self):
        first, last = self.Range()
        return ("agenda", first, last)

    def Candidates(self):
        return self.CandidatesOn(DaysFrom(*self.Range()))

    def InsertTimeHeading(self, edit, hour):
        self.startOffset = 9
        self.cellSize    = 5
        pt = self.view.size()
        row, c = self.view.rowcol(pt)
        dayStart, dayEnd = DaySettings()
        header = "     "
        for i in range(dayStart, dayEnd+1):
            if(i == 10):
                header += " "
            header += "   {:2d}".format(i)
        header +="  \n"
        self.view.insert(edit, self.view.size(), header)
        dayStart = sets.Get("agendaDayStartTime",6)
        col = self.startOffset + (hour-dayStart)*self.cellSize
        s = self.view.text_point(row,col)
        e = self.view.text_point(row,col+2)
        reg = sublime.Region(s, e)
        style = "orgagenda.now"
        self.view.add_regions("curw",[reg],style,"",sublime.DRAW_NO_OUTLINE)   

    def InsertDay(self, name, date, edit):
        pt = self.view.size()
        row, c = self.view.rowcol(pt)
        if(date.day == datetime.datetime.now().day):
            if(date.day == self.now.day):
                self.view.insert(edit, self.view.size(),"@" + name + " " + "{0:2}".format(date.day) + "W[")
            else:
                self.view.insert(edit, self.view.size(),"#" + name + " " + "{0:2}".format(date.day) + "W[")
        elif(date.day == self.now.day):
            self.view.insert(edit, self.view.size(),"&" + name + " " + "{0:2}".format(date.day) + "W[")
        else:
            self.view.insert(edit, self.view.size()," " + name + " " + "{0:2}".format(date.day) + "W[")

        # The timed occurrences on this day, as the server placed them.
        daydata = [TimedItem(e, occ) for e, occ in self.OccurrencesOn(date) if occ.has_time]
        daydata.sort(key=bystartitem)

        lastMatchStart = 0
        lastMatch      = None
        lastMatchEntry = None
        matchCount     = 0
        doneMatchCount = 0
        dayStart, dayEnd = DaySettings()
        for hour in range(dayStart,dayEnd+1):
            for minSlot in range(0,self.cellSize):
                match = None
                for item in daydata:
                    if item.InMinutes(hour*60 + minSlot*12, hour*60 + (minSlot+1)*12):
                        match = item
                if(lastMatch != match and lastMatch != None):
                    s = self.view.text_point(row,lastMatchStart)
                    e = self.view.text_point(row,self.startOffset + (hour-dayStart)*self.cellSize + minSlot)
                    reg = sublime.Region(s, e)
                    lastNode = lastMatch.node
                    if(IsDone(lastNode) or IsArchived(lastNode) or lastMatch.occ.done):
                        style = "orgagenda.week.done." + str(doneMatchCount)
                        doneMatchCount = (doneMatchCount + 1) % 2
                        self.MarkEntryAtRegion(lastMatchEntry,reg)
                        self.view.add_regions("week_done_" + str(date.day) + "_" + str(hour) + "_" + str(minSlot),[reg],style,"", sublime.DRAW_SQUIGGLY_UNDERLINE)   
                    else:
                        style = "orgagenda.week." + str(matchCount)
                        matchCount = (matchCount + 1) % 10
                        self.MarkEntryAtRegion(lastMatchEntry,reg)
                        self.view.add_regions("week_" + str(date.day) + "_" + str(hour) + "_" + str(minSlot),[reg],style,"",sublime.DRAW_NO_FILL)   
                if(match != None):
                    if(lastMatch != match):
                        lastMatch      = match
                        lastMatchEntry = match.entry
                        lastMatchStart = self.startOffset + (hour-dayStart)*self.cellSize + minSlot
                    d = distanceFromStart(match, hour, minSlot, dayStart)
                    heading = match.node.heading
                    # If the time slot is larger than the name we space pad it
                    c = " "
                    if(d < len(heading) and d >= 0):
                        c = heading[d:d+1]
                    self.view.insert(edit, self.view.size(), c)
                else:
                    if(lastMatch != match):
                        lastMatch      = match
                        lastMatchStart = self.startOffset + (hour-dayStart)*self.cellSize + minSlot
                        lastMatchEntry = None
                    if(minSlot < 4):
                        self.view.insert(edit, self.view.size(), ".")
                    else:
                        self.view.insert(edit, self.view.size(), "_")
        self.view.insert(edit, self.view.size(),"]\n")

    def RenderView(self, edit, clear=False):
        self.InsertAgendaHeading(edit)
        self.InsertTimeHeading(edit,self.now.hour)
        wstart = self.WeekStart()
        firstDayIndex = sets.GetWeekdayIndexByName(sets.Get("firstDayOfWeek", "Sunday"))
        dayNames  = sets.Get("weekViewDayNames",["Mon", "Tue", "Wed", "Thr", "Fri", "Sat", "Sun"])
        numDays   = sets.Get("agendaWeekViewNumDays",7)
        for i in range(0,numDays):
            index = (firstDayIndex + i) % len(dayNames)
            self.InsertDay(dayNames[index], wstart + datetime.timedelta(days=i), edit)

    def FilterEntry(self, n, filename):
        rc = (not self.onlyTasks or (IsTodo(n) or IsDone(n))) and not IsProject(n) and HasTimestamp(n)
        return rc


# ============================================================ 
class AgendaView(AgendaBaseView):
    def __init__(self, name, setup=True, **kwargs):
        super(AgendaView, self).__init__(name, setup, **kwargs)
        self.blocks = [None,None,None,None,None,None,None]
        self.sym     = ("$","@","!","#","%","^","&")
        self.symUsed = [-1,-1,-1,-1,-1,-1,-1]

    def Spec(self):
        day = self.now.date()
        return ("agenda", day, day)

    def Candidates(self):
        return self.CandidatesOn([self.now.date()])

    def RenderDateHeading(self, edit, now):
        headerFormat = sets.Get("agendaHeaderFormat","%A \t%d %B %Y")
        self.view.insert(edit, self.view.size(), now.strftime(headerFormat) + "\n\n")

    def BuildHabitDisplay(self, n):
        if(n.scheduled and n.get_property("STYLE",None)):
            #OrgDateRepeatedTask
            repeats = n.repeated_tasks
            habitbar = "[_____________________]"
            hb = list(habitbar) 
            # not schedule but done
            # not schedule not done
            # scheduled but not done
            # scheduled but last day
            # late
            # done
            if(repeats):
                start = self.now-drel.relativedelta(days=20)
                cur = self.now-drel.relativedelta(days=21)
                if(n.scheduled.repeating):
                    while(cur < self.now):
                       cur = n.scheduled.next_repeat_from(cur)
                       if(cur is None):
                            break
                       if(cur < self.now):
                            diff = (self.now - cur).days
                            diff = 21 - diff
                            hb[diff] = '.'
                for i in range(0,21):
                    cur = start + drel.relativedelta(days=i)
                    for r in repeats:
                        if r.has_overlap(cur):
                            hb[i+1] = '*'
            return "H" + ''.join(hb)
        return ""

    def GetUnusedSymbol(self, blk):
        start = 0
        for i in range(0,len(self.symUsed)):
            if(self.symUsed[i] >= 0):
                start = i
                break
        for i in range(start,len(self.symUsed)):
            if(self.symUsed[i] < 0):
                self.symUsed[i] = blk
                return i
        return -1

    def ReleaseSymbol(self, blk):
        for i in range(0,len(self.symUsed)):
            if(self.symUsed[i] == blk):
                self.symUsed[i] = -1
                break

    def FindSymbol(self, blk):
        for i in range(0,len(self.symUsed)):
            if(self.symUsed[i] == blk):
                return i
        return 0

    # Blocks are the timed items still running in an hour.
    def ClearAgendaBlocks(self,h):
        for i in range(0, len(self.blocks)):
            item = self.blocks[i]
            if(not item or not item.InHour(h)):
                self.ReleaseSymbol(i)
                self.blocks[i] = None

    def UpdateWithThisBlock(self, item, h):
        idx = -1
        for i in range(0, len(self.blocks)):
            if(idx == -1 and self.blocks[i] == None):
                idx = i
            if(self.blocks[i] == item):
                idx = -1
                return i
        if(idx != -1):
            self.blocks[idx] = item
            return idx
        return 0

    def GetAgendaBlocks(self,item,h):
        out = ""
        if(item != None):
            symIdx = self.GetUnusedSymbol(0)
            self.ClearAgendaBlocks(h)
            myIdx = self.UpdateWithThisBlock(item, h)
            self.symUsed[symIdx] = myIdx
        else:
            self.ClearAgendaBlocks(h)
        spaceSym = "."
        for i in range(0, len(self.blocks)):
            if(self.blocks[i]):
                spaceSym = " "
        if(spaceSym == "."):
            out = ".."
        for i in range(0, len(self.blocks)):
            if(not self.blocks[i]):
                out = out + spaceSym
            else:
                symIdx = self.FindSymbol(i)
                out = out + self.sym[symIdx]
        return out

    def RenderAgendaEntry(self,edit,filename,item,h):
        view = self.view
        n = item.node
        view.insert(edit, view.size(), "{0:12} {1:02d}:{2:02d}B[{7}] {3} {4:45} {5}{6}\n".format(filename if (len(filename) <= 12) else filename[:11] + ":" , item.start.hour, item.start.minute, n.todo if n.todo else "", n.heading, self.BuildDeadlineDisplay(n), self.BuildHabitDisplay(n), self.GetAgendaBlocks(item,h)))

    # What the server says about a heading today: a deadline due or gone
    # past, a scheduled date carried forward.
    def BuildDeadlineDisplay(self, node):
        out = []
        for e, occ in self.dayOccurrences:
            if e['node'] is not node:
                continue
            if occ.kind == "deadline" and not occ.done:
                if occ.late > 0:
                    out.append("D: Overdue")
                elif occ.due_in > 0:
                    # DueIn counts from today, also on the deadline's own day.
                    today = (self.data.today if self.data else None) or datetime.date.today()
                    out.append("D:@" + str(today + datetime.timedelta(days=occ.due_in)))
                else:
                    out.append("D: Due Today")
            elif occ.kind == "scheduled" and occ.late > 0:
                out.append("Sched. {0}x".format(occ.late))
        return " ".join(sorted(set(out), key=out.index))

    def RenderItems(self, edit, items, h):
        for item in items:
            filename = item.entry['file'].AgendaFilenameTag()
            self.MarkEntryAt(item.entry, item.start)
            self.RenderAgendaEntry(edit, filename, item, h)

    def RenderView(self, edit, clear=False):
        self.InsertAgendaHeading(edit)
        self.RenderDateHeading(edit, self.now)
        view     = self.view
        dayStart = sets.Get("agendaDayStartTime",6)
        dayEnd   = sets.Get("agendaDayEndTime",19)
        self.blocks  = [None,None,None,None,None,None,None]
        self.symUsed = [-1,-1,-1,-1,-1,-1,-1]
        self.dayOccurrences = self.OccurrencesOn(self.now.date())
        timed = []
        allday = []
        for e, occ in self.dayOccurrences:
            if occ.has_time:
                timed.append(TimedItem(e, occ))
            elif not any(x is e for x in allday):
                allday.append(e)
        timed.sort(key=bystartitem)
        # Times before the day's first hour are shown in it (a range from
        # the day before starts at 00:00); later ones get hours of their own.
        if timed:
            dayEnd = max(dayEnd, max(i.start.hour for i in timed) + 1)
        for h in range(dayStart, dayEnd):
            didNotInsert = True
            hourItems = [i for i in timed if i.start.hour == h or (h == dayStart and i.start.hour < dayStart)]
            if(self.now.hour == h):
                before = [i for i in hourItems if i.start.time() < self.now.time()]
                after  = [i for i in hourItems if not i.start.time() < self.now.time()]
                if before:
                    self.RenderItems(edit, before, h)
                    didNotInsert = False
                view.insert(edit, view.size(), "{0:12} {1:02d}:{2:02d} - - - - - - - - - - - - - - - - - - - - - \n".format("now =>", self.now.hour, self.now.minute) )
                if after:
                    self.RenderItems(edit, after, h)
                    didNotInsert = False
            elif hourItems:
                self.RenderItems(edit, hourItems, h)
                didNotInsert = False
            if(didNotInsert):
                empty = " " * 12
                blocks = self.GetAgendaBlocks(None,h)
                sep = ""
                esep = " "
                if(not '.' in blocks):
                    sep = "B["
                    esep = "]"
                view.insert(edit, view.size(), "{0:12} {1:02d}:00{3}{2}{4}---------------------\n".format(empty, h, blocks, sep, esep))
        view.insert(edit,view.size(),"\n")
        # One line per heading with something on today but no time.
        for entry in allday:
            n = entry['node']
            filename = entry['file'].AgendaFilenameTag()
            self.MarkEntryAt(entry, self.now)
            view.insert(edit, view.size(), "{0:12} {1} {2:69} {3} {4}\n".format(filename, n.todo if n.todo else "", n.heading, self.BuildDeadlineDisplay(n), self.BuildHabitDisplay(n)))

    def FilterEntry(self, node, file):
        rc = (not self.onlyTasks or IsTodo(node)) and not IsDone(node) and not IsArchived(node)
        return rc


RE_IN_OUT_TAG = re.compile('(?P<inout>[|+-])?(?P<tag>[^ ]+)')
# ================================================================================
class TodoView(AgendaBaseView):

    def GetParam(self, name, defaultVal, kwargs):
        v       = kwargs[name] if name in kwargs else None
        if isinstance(v,bool):
            v = ">10.10"
        return v
    def __init__(self, name, setup=True, **kwargs):
        super(TodoView, self).__init__(name, setup, **kwargs)
        self.showduration = self.GetParam("showduration","7.7",kwargs)
        self.showfilename = "15.15" if "hidefilename" not in kwargs else None
        self.showheading  = "hideheading" not in kwargs
        self.showstatus   = "11.11" if "hidestatus" not in kwargs else None
        self.showdate     = self.GetParam("showdate","15.15",kwargs)
        self.showtime     = self.GetParam("showtime","6.6",kwargs)
        self.showeffort   = self.GetParam("showeffort",">6.6",kwargs)
        self.showafter    = self.GetParam("showafter",">12.12",kwargs)
        self.showassigned = self.GetParam("showassigned",">12.12", kwargs)
        self.showid       = self.GetParam("showid",">10.10", kwargs)
        self.showtotalduration = "showtotalduration" in kwargs
        self.byproject    = "byproject" in kwargs
        self.input        = None
        self.search_filter = None
        self.havesortorder = "sortascend" in kwargs or "sortdescend" in kwargs
        self.sortorder = False
        if self.havesortorder:
            self.sortorder = False if "sortascend" in kwargs else self.sortorder
            self.sortorder = True  if "sortdescend" in kwargs else self.sortorder

    def GetFormatHeaders(self, n, filename):
        data = {}
        data2 = {}
        if self.showfilename:
            data['filename'] = "File"
            data2['filename'] = "---------------"
        if self.showstatus:
            data['status']   = "Status"
            data2['status']   = "-----------"
        if self.showduration:
            data['duration'] = "Duration"
            data2['duration']= "--------"
        if self.showheading:
            data['heading'] =  "Heading"
            data2['heading'] = "--------------------"
        if self.showdate:
            data['date'] = "Date"
            data2['date']= "---------------"
        if self.showtime:
            data['time'] = "Time"
            data2['time'] = "------"
        if self.showeffort:
            data['effort'] = "Effort"
            data2['effort'] = "------"
        if self.showafter:
            data['after'] = "After"
            data2['after'] = "------------"
        if self.showid:
            data['id'] = "ID"
            data2['id'] = "-----------------"
        if self.showassigned:
            data['assigned'] = "Who"
            data2['assigned'] = "------------"
        return (data, data2)




    def GetFormatData(self, n, filename):
        data = {}
        if self.showfilename:
            data['filename'] = filename
        if self.showstatus:
            todo = ""
            if n:
                todo = n.todo
                if todo == None:
                    todo = ""
            data['status'] = todo
        if self.showduration:
            duration = ""
            dur = datetime.timedelta(days=0)
            if n:
                for c in n.clock:
                    dur += c.duration
                self.totalduration += dur
                duration = orgdate.OrgDate.format_duration(dur)
            data['duration'] = duration
        if self.showheading:
            heading = ""
            if n:
                heading = n.heading
            data['heading'] = heading
        if self.showdate:
            date = ""
            if n:
                dt = getdatefromnode(n)
                if dt != datetime.datetime.min and dt.date() != datetime.date.min:
                    date = dt.strftime("%Y-%m-%d %a")  
            data['date'] = date
        if self.showtime:
            time = ""
            if n:
                dt = getdatefromnode(n)
                if dt != datetime.datetime.min and dt.time() != datetime.time.min:
                    time = dt.time().strftime("%H:%M")  
            data['time'] = time
        if self.showeffort:
            effort = ""
            if n:
                effort = n.get_property("EFFORT","")
            data['effort'] = effort
        if self.showafter:
            after = ""
            if n:
                after = n.get_property("AFTER","")
            data['after'] = after
        if self.showid:
            idd = ""
            if n:
                idd = n.get_property("ID","")
                if not idd or idd == "":
                    idd = n.get_property("CUSTOM_ID","")
            data['id'] = idd
        if self.showassigned:
            ass = ""
            if n:
                ass = n.get_property("ASSIGNED","")
            data['assigned'] = ass
        return data


    def GetF(self,p,name):
        if p:
            return "{" + name + ":" + p + "} "
        return ""
    def GetFormatString(self):
        formatstr = ""
        formatstr += self.GetF(self.showfilename,"filename")
        formatstr += self.GetF(self.showstatus,"status")
        formatstr += self.GetF(self.showduration,"duration")
        formatstr += self.GetF(self.showdate,"date")
        formatstr += self.GetF(self.showtime,"time")
        formatstr += self.GetF(self.showeffort,"effort")
        formatstr += self.GetF(self.showafter,"after")
        formatstr += self.GetF(self.showid,"id")
        formatstr += self.GetF(self.showassigned,"assigned")
        if self.showheading:
            formatstr += "{heading}"
        formatstr += "\n"
        return formatstr

    def OnFilter(self, text):
        #print("FILTER: " + str(text))
        self.input.onRecalc = evt.Make(self.OnFilter)
        if text == None:
            self.search_filter = None
        else:
            self.search_filter = re.compile(text + ".*")
        self.view.run_command("org_agenda_re_render_view")

    def OpenFilterView(self):
        if self.input != None:
            self.input.onRecalc = evt.Make(self.OnFilter)
            self.input.run("Filter:",None,evt.Make(self.OnFilter))

    def getSortOrdering(self):
        order = not sets.Get("agendaTodoSortAscending",True)
        if self.havesortorder:
            order = self.sortorder
        return order

    def RenderView(self, edit, clear = False):
        self.ClearEntriesAt()
        if clear:
            self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.InsertAgendaHeading(edit)
        formatstr = self.GetFormatString()
        data,under      = self.GetFormatHeaders(None,"")
        self.view.insert(edit, self.view.size(), formatstr.format(**data))
        self.view.insert(edit, self.view.size(), formatstr.format(**under))
        self.totalduration = datetime.timedelta(days=0)
        if self.byproject:
            projects   = {}
            loosetasks = []
            for entry in self.entries:
                n        = entry['node']
                if n == None:
                    continue
                if self.search_filter and not n.is_root() and not self.search_filter.match(n.heading):
                    continue
                if n.is_root() or n.parent == None or n.parent.is_root() or not IsProject(n.parent):
                    loosetasks.append(entry)
                else:
                    pname = n.parent.heading
                    if pname not in projects:
                        projects[pname] = []
                    projects[pname].append(entry)
            for pname,vals in projects.items():
                vals.sort(key=getsortkey,reverse=self.getSortOrdering())
                projects[pname] = vals

            for pname,vals in projects.items():
                self.view.insert(edit, self.view.size(), "\n== [{0}] ==\n".format(pname))
                for entry in vals:
                    n        = entry['node']
                    filename = entry['file'].AgendaFilenameTag()
                    self.MarkEntryAt(entry)
                    self.RenderEntry(n, filename, edit)
            if len(loosetasks) > 0:
                self.view.insert(edit, self.view.size(), "\n== [] ==\n")
                for entry in loosetasks:
                    n        = entry['node']
                    filename = entry['file'].AgendaFilenameTag()
                    self.MarkEntryAt(entry)
                    self.RenderEntry(n, filename, edit)
        else:
            self.entries.sort(key=getsortkey,reverse=self.getSortOrdering())
            for entry in self.entries:
                n        = entry['node']
                if self.search_filter and not n.is_root() and not self.search_filter.match(n.heading):
                    continue
                filename = entry['file'].AgendaFilenameTag()
                self.MarkEntryAt(entry)
                self.RenderEntry(n, filename, edit)
        if self.showtotalduration:
            formatstr = self.GetFormatString()
            data      = self.GetFormatData(None,"")
            data['duration'] = orgdate.OrgDate.format_duration(self.totalduration)
            data['filename'] = "TOTAL: "
            self.view.insert(edit, self.view.size(), "----------------------------------------------------------------------------\n")
            self.view.insert(edit, self.view.size(), formatstr.format(**data))
        if(self.input == None):
            self.input = insSel.OrgInput()
            shouldFilter = sets.Get("agendaTodoFilterByDefault", False)
            if shouldFilter:
                self.OpenFilterView()

    def RenderEntry(self, n, filename, edit):
        formatstr = self.GetFormatString()
        data      = self.GetFormatData(n, filename)
        self.view.insert(edit, self.view.size(), formatstr.format(**data))

    # The server narrows the headings to what FilterEntry could take (the
    # filters here still decide). A view that looks at projects (or a
    # heading's project) needs every heading in the tag and property modes;
    # in nested_todo mode a project is itself a todo.
    serverQuery = "IsTodo() && !IsArchived()"
    needsAll = False

    def ServerQuery(self):
        if (self.needsAll or self.byproject) and ProjectMode() != "nested_todo":
            return "!IsArchived()"
        return self.serverQuery

    def Spec(self):
        return ("nodes", self.ServerQuery())

    def FilterEntry(self, n, filename):
        return IsTodo(n) and not IsProject(n) and not IsArchived(n)

# ================================================================================
class ProjectsView(TodoView):
    needsAll = True

    def __init__(self, name, setup=True, **kwargs):
        super(ProjectsView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsProject(n) and not IsArchived(n)

# ================================================================================
class NotBlockedProjectsView(TodoView):
    needsAll = True

    def __init__(self, name, setup=True, **kwargs):
        super(NotBlockedProjectsView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsProject(n) and not IsBlockedProject(n) and not IsArchived(n)

# ================================================================================
class BlockedProjectsView(TodoView):
    needsAll = True

    def __init__(self, name, setup=True, **kwargs):
        super(BlockedProjectsView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsBlockedProject(n) and not IsArchived(n)

# ================================================================================
class LooseTasksView(TodoView):
    needsAll = True

    def __init__(self, name, setup=True, **kwargs):
        super(LooseTasksView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        rc = IsTodo(n) and not IsProject(n) and not IsProjectTask(n) and not IsArchived(n)
        return rc


# ================================================================================
class DoneTasksView(TodoView):
    serverQuery = "IsDone() && !IsArchived()"

    def __init__(self, name, setup=True, **kwargs):
        super(DoneTasksView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        rc = IsDone(n) and not IsArchived(n)
        return rc

# ================================================================================
class ClockedView(TodoView):
    serverQuery = "HasClock()"

    def ServerQuery(self):
        return self.serverQuery

    def __init__(self, name, setup=True, **kwargs):
        super(ClockedView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return n and n.clock

# ================================================================================
class HasStatusView(TodoView):
    serverQuery = "HasAStatus()"

    def ServerQuery(self):
        return self.serverQuery

    def __init__(self, name, setup=True, **kwargs):
        super(HasStatusView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return n and (IsDone(n) or IsTodo(n))

# ================================================================================
class NextTasksProjectsView(TodoView):
    needsAll = True

    def __init__(self, name, setup=True, **kwargs):
        super(NextTasksProjectsView, self).__init__(name, setup, **kwargs)

    # TODO Print project and then the next task
    def RenderView(self, edit, clear=False):
        self.InsertAgendaHeading(edit)
        newEntries = []
        for entry in self.entries:
            n        = entry['node']
            filename = entry['file'].AgendaFilenameTag()
            self.MarkEntryAt(entry)
            self.view.insert(edit, self.view.size(), "{0:15} {1:12} {2}\n".format(filename,"-------", n.heading))
            #self.RenderEntry(n, filename, edit)
            for c in n.children:
                if(c.todo and c.todo == "NEXT"):
                    nentry = {'node': c, 'file': entry['file']}
                    newEntries.append(nentry)
                    self.MarkEntryAt(nentry)
                    self.view.insert(edit, self.view.size(), "{0:15} {1:12} {2}\n".format(" ", c.todo, c.heading))
                    #self.RenderEntry(c, filename, edit)
        for e in newEntries:
            self.entries.append(e)
                    
    def FilterEntry(self, n, filename):
        return IsProject(n) and not IsBlockedProject(n) and not IsArchived(n)


# ================================================================================
class NoteView(TodoView):
    serverQuery = "HasAStatus() && !IsArchived()"
    needsAll = True

    def __init__(self, name, setup=True,**kwargs):
        super(NoteView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsNote(n) and not IsProject(n) and not IsProjectTask(n) and not IsArchived(n)

# ================================================================================
class PhoneView(TodoView):
    serverQuery = "HasAStatus() && !IsArchived()"
    needsAll = True

    def __init__(self, name, setup=True,**kwargs):
        super(PhoneView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsPhone(n) and not IsProject(n) and not IsProjectTask(n) and self.MatchTags(n) and not IsArchived(n)

# ================================================================================
class MeetingView(TodoView):
    serverQuery = "HasAStatus() && !IsArchived()"
    needsAll = True

    def __init__(self, name, setup=True,**kwargs):
        super(MeetingView, self).__init__(name, setup, **kwargs)

    def FilterEntry(self, n, filename):
        return IsMeeting(n) and not IsProject(n) and not IsProjectTask(n) and self.MatchTags(n) and not IsArchived(n)

r"""
/* SDOC: Reference::Settings::Agenda
*** AgendaCustomViews
    Default:
    #+BEGIN_SRC json
    {
        "Default": ["Calendar", "Week", "Day", "Blocked Projects", "Next Tasks", "Loose Tasks"],
        "Todos":   ["Todos"],
        "Notes":   ["Notes"],
        "Flags":   ["Todos : tagfilter |TAG1 |OTHERTAGS", "Todos : tagfilter OTHERTAGS"]
    }
    #+END_SRC

    The agendas Org Agenda Custom View and Org Agenda Choose View offer:
    each name maps to a list of views shown one under the other. A view is
    =Name : arg value : flag ...=. The names: =Calendar=, =Week=, =Day=,
    =Todos=, =Blocked Projects=, =Not Blocked Projects=, =Projects=,
    =Next Tasks=, =Loose Tasks=, =Done=, =Clocked=, =Has Status=, =Notes=,
    =Meetings=, =Phone=, and =OrgsQuery= (=query EXPR=, or =filter NAME= for
    a server filter, and =title TEXT=; a query cannot contain a =:=).

    Filters every view takes: =tagfilter=, =priorityfilter=, =statefilter=
    (space separated; plain or =+= must match, =|= one of, =-= must not),
    =durationfilter= and =clockfilter= (durations, =+= before / =-= after),
    =datefilter= (~>DATE~, ~>=DATE~, ~<DATE~, ~<=DATE~ on the closed date, e.g. ~>=20240101~), and the flags
    =hasclock=, =hasdeadline=, =hasclose=, =hasschedule=, =haschildtasks=,
    =noclock=, =nodeadline=, =noclose=, =noschedule=, =nochildtasks=,
    =onlytasks=. Todo list views also take =byproject=, =showtotalduration=,
    =sortascend= / =sortdescend=, =hidefilename=, =hidestatus=,
    =hideheading= and column widths =showduration=, =showdate=,
    =showtime=, =showeffort=, =showafter=, =showassigned=, =showid=.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Agenda
*** Agenda layout settings
    | Setting                 | Default                                         | Effect                                                                              |
    |-------------------------+-------------------------------------------------+-------------------------------------------------------------------------------------|
    | =firstDayOfWeek=        | ="Sunday"=                                      | First day of the week view and of the calendar (=Monday= ... =Sunday=).           |
    | =agendaWeekViewNumDays= | =7=                                             | Days in the week view; 5 with =firstDayOfWeek= =Monday= shows the work week.       |
    | =weekViewDayNames=      | =["Mon","Tue","Wed","Thr","Fri","Sat","Sun"]=   | Day labels of the week view, Monday first.                                          |
    | =agendaDayStartTime=    | =6=                                             | First hour of the day and week hour grids.                                          |
    | =agendaDayEndTime=      | =19=                                            | Last hour of the grids (at most 23).                                                |
    | =agendaHeaderFormat=    | ="%A \t%d %B %Y"=                               | strftime format of the date heading of the Day view.                                |
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Agenda
*** agendaProjectIs
    Default: ="nested_todo"=

    What the agenda counts as a project (for Blocked Projects, Next Tasks,
    Loose Tasks, Projects and =byproject=): ="nested_todo"=, a todo with
    todos under it; ="tag"=, a heading tagged =:PROJECT:=; ="property"=, a
    heading with a =PROJECT= property.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Agenda
*** Todo list settings
    | Setting                     | Default | Effect                                                                                   |
    |-----------------------------+---------+------------------------------------------------------------------------------------------|
    | =agendaTodoFilterByDefault= | =false= | Open the filter input (type a regular expression to narrow the list) with todo views.   |
    | =agendaTodoSortAscending=   | =true=  | Sort order of todo lists; a view's =sortascend= / =sortdescend= overrides it.           |
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Agenda
*** agendaOrgsQueryDefault and agendaSaveBeforeRender
    | Setting                  | Default                      | Effect                                                                                       |
    |--------------------------+------------------------------+----------------------------------------------------------------------------------------------|
    | =agendaOrgsQueryDefault= | ="IsTodo() && !IsArchived()"= | The query offered by Org: Agenda Query, and run by an =OrgsQuery= view with no query or filter. |
    | =agendaSaveBeforeRender= | =false=                      | Save modified org buffers (and wait for the server to read them) before fetching an agenda.   |

    The agenda comes from the orgs server, which reads saved files; without
    =agendaSaveBeforeRender= an agenda notes how many unsaved org buffers
    are not in it.
EDOC */
"""

# ================================================================================
# Headings matching an orgs server query (GET /search). Usable in
# AgendaCustomViews, e.g.
#   "Work": ["OrgsQuery : query IsTodo() && InTagGroup(\"WORK\")"]
#   "Next": ["OrgsQuery : filter NextTasks : title Next tasks"]
# (filter NAME runs the server filter of that name.) Arguments are split on
# ':' so a query cannot itself contain one.
DEFAULT_ORGS_QUERY = "IsTodo() && !IsArchived()"

class OrgsQueryView(TodoView):
    def __init__(self, name, setup=True, **kwargs):
        q = kwargs.get("query")
        if "filter" in kwargs and isinstance(kwargs["filter"], str):
            q = "{{" + kwargs["filter"].strip() + "}}"
        if not q or not isinstance(q, str):
            q = sets.Get("agendaOrgsQueryDefault", DEFAULT_ORGS_QUERY)
        self.query      = q
        self.title      = kwargs.get("title") if isinstance(kwargs.get("title"), str) else None
        super(OrgsQueryView, self).__init__(name, setup, **kwargs)

    def Spec(self):
        return ("nodes", self.query)

    # A query failing (bad syntax) is this view's problem, shown in it.
    def FetchError(self):
        return None

    def QueryError(self):
        return (self.data.error if self.data else None) or self.fetchError

    # Every heading the query finds is shown.
    def FilterEntries(self):
        if self.data:
            for n in self.data.nodes:
                self.AddEntry(n, self.data.File(n.filename))

    def InsertAgendaHeading(self, edit):
        self.view.insert(edit, self.view.size(), (self.title or self.name) + "\t\t QUERY( " + self.query + " )\n")

    def RenderView(self, edit, clear=False):
        self.ClearEntriesAt()
        if clear:
            self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.InsertAgendaHeading(edit)
        err = self.QueryError()
        if err:
            self.view.insert(edit, self.view.size(), "query failed: " + err + "\n")
        fmt = "{0:15.15} {1:11.11} {2:20.20} {3}\n"
        self.view.insert(edit, self.view.size(), fmt.format("File", "Status", "Date", "Heading"))
        self.view.insert(edit, self.view.size(), fmt.format("-" * 15, "-" * 11, "-" * 20, "-" * 20))
        shown = 0
        for e in self.entries:
            n = e['node']
            if self.search_filter and not self.search_filter.match(n.headline):
                continue
            self.MarkEntryAt(e)
            self.view.insert(edit, self.view.size(), fmt.format(*ServerTodoColumns(n)))
            shown += 1
        if shown == 0 and not err and orgs.connected():
            self.view.insert(edit, self.view.size(), "(nothing matches)\n")
        if(self.input == None):
            self.input = insSel.OrgInput()

def ServerTodoColumns(n):
    """(file, status, date, heading) text for an agenda node."""
    fname = os.path.splitext(os.path.basename(n.filename or ""))[0] + ":"
    date = ""
    stamps = n.get_timestamps(active=True, point=True, range=True)
    for d, prefix in ((n.scheduled, ""), (stamps[0] if stamps else None, ""), (n.deadline, "D:")):
        if d:
            date = prefix + d.start.strftime("%Y-%m-%d %a" + (" %H:%M" if d.has_time() else ""))
            break
    heading = n.headline
    if n.priority:
        heading = "[#" + n.priority + "] " + heading
    if n.shallow_tags:
        heading += "  :" + ":".join(n.raw.get("Tags") or []) + ":"
    return (fname, n.todo or "", date, heading)

# ================================================================================
_queryHistory = []

def ShowOrgsQueryView(query, title=None):
    """Fetch the query's headings, then show them in a view of their own."""
    name = "Query: " + (title or query)
    if len(name) > 60:
        name = name[:57] + "..."
    v = OrgsQueryView(name, False, query=query, title=title)

    def show():
        v.SetupView()
        v.view.run_command("org_agenda_re_render_view")
    FetchViews([v], show, what="query")
    return v

r"""
/* SDOC: Reference::Commands::Agenda
*** Org: Agenda Query (orgs)
    Command: =org_agenda_query=  Keys: none  Args: =query=, =title=

    Shows the headings matching an orgs query language expression in an
    agenda style view of their own ("Query: ..."): file, status, date and
    heading per line; =enter= opens one, and the agenda actions (change
    todo, priority, tags, clock in...) work on its lines.

    Without =query= a quick panel offers "New query..." (prefilled with
    =agendaOrgsQueryDefault=), the queries run this session, your saved
    queries and the server's filters; whichever you pick can be edited
    before it runs. With =query= it runs at once, titled =title=. The palette
    entry "Org: Agenda All Tasks (orgs filter AllTasks)" runs
    ={{AllTasks}}=, the server filter of that name. A bad query shows the
    server's error in the view. Needs the orgs server.
EDOC */
"""

class OrgAgendaQueryCommand(sublime_plugin.WindowCommand):
    """Show the headings matching an orgs query in an agenda view.

    Offers this session's recent queries, the server's stored queries and its
    filters first; whichever is picked can be edited before it runs."""
    NEW = "New query..."

    def run(self, query=None, title=None):
        if query:
            self.Execute(query, title)
            return

        def fetch(cl):
            stored = []
            try:
                stored = cl.stored_queries() or []
            except orgsclient.OrgsError as e:
                log.info("orgs: stored queries: %s", e)
            return (stored, cl.filters() or {})
        orgs.run(fetch, self.Offer, what="queries")

    def Offer(self, res):
        stored, filters = res
        self.choices = [(None, self.NEW, "type a query")]
        for q in _queryHistory:
            self.choices.append((q, q, "recent"))
        for sq in stored:
            if isinstance(sq, dict):
                name = sq.get("name") or sq.get("Name") or ""
                q = sq.get("query") or sq.get("Query") or ""
            else:
                name, q = str(sq), str(sq)
            if q:
                self.choices.append((q, name, "stored query: " + q))
        for name in sorted(filters.keys() if isinstance(filters, dict) else []):
            self.choices.append(("{{" + name + "}}", name, "filter: " + str(filters[name])))
        items = [[c[1], c[2]] for c in self.choices]
        self.window.show_quick_panel(items, self.Picked)

    def Picked(self, i, *args):
        if i < 0:
            return
        q = self.choices[i][0]
        if q is None:
            q = sets.Get("agendaOrgsQueryDefault", DEFAULT_ORGS_QUERY)
        self.window.show_input_panel("orgs query:", q, lambda text: self.Execute(text), None, None)

    def Execute(self, query, title=None):
        query = (query or "").strip()
        if not query:
            return
        if query in _queryHistory:
            _queryHistory.remove(query)
        _queryHistory.insert(0, query)
        del _queryHistory[20:]
        ShowOrgsQueryView(query, title)


# ================================================================================
class CompositeViewListener(sublime_plugin.ViewEventListener):

    @classmethod
    def is_applicable(cls, settings):
        # 4095 seems to crash when querying settings
        if(int(sublime.version()) != 4095):
            try:
                return "orgagenda" in settings.get("color_scheme","not here")
            except:
                return False
        return False

    def __init__(self, view):
        super(CompositeViewListener, self).__init__(view)
        self.agenda = FindMappedView(self.view)
        self.phantoms = []

    def clear_phantoms(self):
        for f in self.phantoms:
            self.view.erase_phantoms(f)
        self.phantoms = []

    def on_hover_done(self):
        self.clear_phantoms()
    
    def on_hover(self, point, hover_zone):
        if(not hasattr(self,'agenda') or self.agenda == None):
            return
        if(hover_zone == sublime.HOVER_TEXT):
            row,col = self.view.rowcol(point)
            n, f = self.agenda.At(row, col)
            if(n and f):
                self.clear_phantoms()
                line = self.view.line(point)
                reg = sublime.Region(point, line.end())
                body = """
                <body id="agenda-week-popup">
                <style>
                    div.block {{
                        display: block;
                        background-color: #333333;
                        border-style: solid;
                        border: 1px;
                        border-color: #666666;
                    }}
                    div.heading {{
                        color: #880077;
                        padding: 5px;
                        font-size: 20px;
                        font-weight: bold;
                        }}
                    div.file {{
                        color: grey;
                        padding-left: 5px;
                        font-size: 15px;
                        }}
                </style>
                <div class="block"/>
                <div class="heading">{0}</div>
                <div class="file">{1}</div>
                </div>
                </body>
                """.format(n.heading,f.filename)

                self.view.add_phantom(n.heading, reg, body, sublime.LAYOUT_INLINE)
                #sublime.Phantom(sublime.Region(point, point), "<html><body>" + n.heading + "</body></html>",sublime.LAYOUT_INLINE, None) 
                self.phantoms.append(n.heading)
                print(n.heading)
                sublime.set_timeout(self.on_hover_done, 1000*2) 

# ================================================================================
# ORG has this custom composite view feature.
# I want that. Make a view up of a couple of views.
class CompositeView(AgendaBaseView):
    def __init__(self, name, views):
        self.agendaViews = views
        # The base constructor sets up the view (and filters) once.
        super(CompositeView, self).__init__(name)

    def RenderView(self, edit, clear=False):
        if clear:
            self.view.erase(edit, sublime.Region(0, self.view.size()))
        first = True
        for v in self.agendaViews:
            if not first:
                self.view.insert(edit, self.view.size(), ("=" * 75) + "\n")
            first = False
            v.view = self.view
            v.RenderView(edit, False)
        # These get updated when rendered
        self.entries = []
        for v in self.agendaViews:
            self.entries += v.entries

    def UpdateNow(self, now=None):
        if(now == None):
            self.now = datetime.datetime.now()
        else:
            self.now = now
        for v in self.agendaViews:
            v.UpdateNow(now)

    def FilterEntries(self):
        self.entries = []
        for v in self.agendaViews:
            v.entries = []
            v.FilterEntries()
            self.entries += v.entries

    def At(self, row, col):
        for av in self.agendaViews:
            n, f  = av.At(row,col)
            if(n and f):
                return (n,f)
        return (None, None)

    def EntryAt(self, row, col):
        for av in self.agendaViews:
            e = av.EntryAt(row, col)
            if e:
                return e
        return None

    def Leaves(self):
        out = []
        for v in self.agendaViews:
            out += v.Leaves()
        return out

    def FetchError(self):
        for v in self.agendaViews:
            err = v.FetchError()
            if err:
                return err
        return None

# ================================================================================
# Fetching. Each view says what it needs (Spec); one worker job fetches it
# all: the date views' days in one /agenda call (their ranges merged), each
# list query once from /agenda/nodes. The views are then filtered and drawn
# on the ui thread from what came back.

def FetchData(cl, specs, today=None):
    """(worker) {spec: AgendaData} for the specs."""
    out = {}
    ranges = [s for s in specs if s and s[0] == "agenda"]
    if ranges:
        frm = min(r[1] for r in ranges)
        to = max(r[2] for r in ranges)
        data = agnode.AgendaData.FromResult(cl.agenda(frm, to, today=today))
        for r in ranges:
            out[r] = data
    for s in specs:
        if not s or s[0] != "nodes" or s in out:
            continue
        try:
            out[s] = agnode.AgendaData(cl.agenda_nodes(s[1]))
        except orgsclient.Unavailable:
            raise
        except orgsclient.OrgsError as e:
            out[s] = agnode.AgendaData([], error=str(e).strip())
    return out

def SaveBeforeFetch(cl, filenames):
    """(worker) agendaSaveBeforeRender: save the dirty org buffers and wait
    for the server to read them."""
    for fn in filenames:
        try:
            orgs.sync_path(cl, fn)
        except orgsclient.OrgsError as e:
            log.warning("agenda: could not save %s: %s", fn, e)

def FetchViews(views, on_done, what="agenda"):
    """Fetch what the views need off the ui thread, set it on them and call
    on_done() on the ui thread (also when the fetch failed or there is no
    server: the views then say so)."""
    leaves = []
    for v in views:
        leaves += v.Leaves()
    specs = [l.Spec() for l in leaves]
    # Each fetch is numbered per view: a reply that a later fetch of the same
    # view has superseded (next day pressed twice quickly) is dropped, so
    # only the newest one is drawn.
    gens = []
    for l in leaves:
        l.fetchGen = getattr(l, "fetchGen", 0) + 1
        gens.append(l.fetchGen)
    save = []
    if sets.Get("agendaSaveBeforeRender", False):
        save = [v.file_name() for v in UnsavedOrgBuffers() if v.file_name()]
    today = datetime.date.today()

    def work(cl):
        if save:
            SaveBeforeFetch(cl, save)
        out = FetchData(cl, specs, today)
        more = [l.FetchMore(cl, out.get(s) if s is not None else None) for l, s in zip(leaves, specs)]
        return (out, more)

    def current():
        return [(l, s, i) for i, (l, s, g) in enumerate(zip(leaves, specs, gens)) if l.fetchGen == g]

    def done(res):
        out, more = res
        cur = current()
        for l, s, i in cur:
            l.fetchError = None
            l.data = out.get(s) if s is not None else None
            l.fetchedMore = more[i]
        # A superseded view is drawn by the newer fetch when it lands.
        if len(cur) == len(leaves):
            on_done()

    def failed(e):
        cur = current()
        if isinstance(e, orgsclient.Unavailable):
            for l, s, i in cur:
                l.data = None
                l.fetchError = None
        else:
            if cur:
                orgs.report(e, what)
            for l, s, i in cur:
                l.fetchError = "could not fetch the agenda ({0})".format(str(e).strip() or e.__class__.__name__)
        if len(cur) == len(leaves):
            on_done()
    orgs.run(work, done, on_error=failed, what=what)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Todo View
    Command: =org_todo_view=  Keys: none

    Opens the "Org Todos" view: every heading in an active todo state that
    is not a project and not archived, from every file the orgs server
    knows, one per line (file, status, date, effort, heading...). Sorted by
    =agendaTodoSortAscending=. Type to filter when =agendaTodoFilterByDefault=
    is on, or open the filter with Org Agenda Reopen Filter. Run again from
    the view it refreshes in place, keeping the cursor. Needs the orgs
    server (without it the view says it is not connected).
EDOC */
"""

# ================================================================================
class OrgTodoViewCommand(sublime_plugin.TextCommand):
    # This recursive crap seems to be an ST4 issue that
    # I don't fully understand yet. But am working around.
    def onDone(self, edit, todo):
        todo.DoRenderView(edit)
        if self.pos is not None:
            todo.RestoreCursor(self.pos)

    def run(self, edit, draw=False, pos=None):
        if(draw and self.view.name() == TODO_VIEW):
            todo = FindMappedView(self.view)
            self.pos = pos
            self.onDone(edit, todo)
        else:
            if self.view.name() == TODO_VIEW:
                pos = self.view.sel()[0].begin()
            todo = TodoView(TODO_VIEW, False)

            def show():
                todo.SetupView()
                todo.view.run_command("org_todo_view", {"draw": True, "pos":pos})
            FetchViews([todo], show, what="todo view")

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Day View
    Command: =org_agenda_day_view=  Keys: =alt+o a d= (example global keymap); =g= in an agenda view; NeoVintageous normal mode: =shift+space=

    The default agenda: one "Agenda" view made of a three month calendar
    (days with entries highlighted), the week (an hour grid per day), today
    (scheduled and timed entries on an hour grid, deadlines, habits), blocked
    projects, projects with their NEXT tasks, and loose tasks (todos not in
    any project). Fetched from the orgs server; =g= rebuilds it (from any
    agenda view, =g= shows this day view). Unsaved org buffers are not in
    it; the view says how many (see =agendaSaveBeforeRender=).

    Settings: =firstDayOfWeek=, =agendaWeekViewNumDays=,
    =agendaDayStartTime=, =agendaDayEndTime=, =agendaHeaderFormat=,
    =agendaProjectIs=.
EDOC */
"""

# ================================================================================
# Right now this is a composite view... Need to allow the user to define
# Their own versions of this.
class OrgAgendaDayViewCommand(sublime_plugin.TextCommand):
    def onDone(self, edit, agenda):
        agenda.DoRenderView(edit)
        if(self.pos is not None):
            agenda.RestoreCursor(self.pos)
        log.info("Day view refreshed")

    def run(self, edit, draw=False, pos=None):
        VIEW_NAME="Agenda"
        if draw:
            # This is a hack: A bug in sublime 4 seems to not alow multiple inserts
            # on the edit object when the buffer is created. For now just work around
            # I HATE this but it at least stops this from being just plain broken
            self.pos = pos
            agenda = FindMappedView(self.view)
            self.onDone(edit, agenda)
            return
        else:
            if(self.view.name() == VIEW_NAME):
                pos = self.view.sel()[0].begin()
            # Save and restore the cursor
            views = [CalendarView("Calendar",False), WeekView("Week", False), AgendaView("Agenda", False), BlockedProjectsView("Blocked Projects",False), NextTasksProjectsView("Next",False), LooseTasksView("Loose Tasks",False)]

            def show():
                agenda = CompositeView(VIEW_NAME, views)
                agenda.view.run_command("org_agenda_day_view", {"draw": True, "pos":pos})
            FetchViews(views, show)

# ================================================================================
# Goto the file in the current window (ENTER)
def AgendaGotoPath(view):
    """file:line for the agenda entry under the cursor, or None."""
    agenda = FindMappedView(view)
    if(agenda):
        row, col    = view.curRowCol()
        tgt = AgendaEntryTarget(agenda.EntryAt(row, col))
        if tgt:
            return "{0}:{1}".format(tgt[0], tgt[1] + 1)
        log.warning("COULD NOT LOCATE AGENDA ROW")
    return None

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Goto
    Command: =org_agenda_go_to=  Keys: =enter= (in an agenda view)

    Opens the file of the agenda entry under the cursor at its heading.
EDOC */
"""

class OrgAgendaGoToCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        path = AgendaGotoPath(self.view)
        if path:
            self.view.window().open_file(path, sublime.ENCODED_POSITION)

# ================================================================================
class CalendarViewRegistry:
    def __init__(self):
        self.KnownViews = {}
        self.AddView("Calendar", CalendarView)
        self.AddView("Day", AgendaView)
        self.AddView("Blocked Projects", BlockedProjectsView)
        self.AddView("Next Tasks", NextTasksProjectsView)
        self.AddView("Loose Tasks", LooseTasksView)
        self.AddView("Has Status", HasStatusView)
        self.AddView("Todos", TodoView)
        self.AddView("Notes", NoteView)
        self.AddView("Meetings", MeetingView)
        self.AddView("Phone", PhoneView)
        self.AddView("Week", WeekView)
        self.AddView("Done", DoneTasksView)
        self.AddView("Clocked", ClockedView)
        self.AddView("Projects", ProjectsView)
        self.AddView("Not Blocked Projects", NotBlockedProjectsView)
        self.AddView("OrgsQuery", OrgsQueryView)

    def AddView(self,name,cls):
        self.KnownViews[name] = cls

    # ViewName: <NAME> <ARGS> : <NAME> <ARGS>
    def ParseArgs(self, n ):
        tokens = n.split(':')
        name = tokens[0].strip()
        args = {}
        i = 1
        while(i < len(tokens)):
            p = tokens[i].strip()
            if(len(p) > 0):
                idx = p.find(' ')
                if(idx > 0):
                    pname = p[:idx].strip()
                    pval = p[idx:].strip()
                    args[pname] = pval
                    #print(pname + " -> " + pval)
                else:
                    args[p] = True
            i += 1
        return (name, args)

    def CreateViews(self, views):
        vlist = []
        for v in views:
            n, args = self.ParseArgs(v)
            vv = None
            if(args == None):
                vv = self.KnownViews[n](n, False)
            else:
                vv = self.KnownViews[n](n, False, **args)
            if(vv):
                vlist.append(vv)
        return vlist

    def CreateCompositeView(self,views,name="Agenda",vlist=None):
        if vlist is None:
            vlist = self.CreateViews(views)
        if len(vlist) == 1:
            vlist[0].name = name + " [" + vlist[0].name + "]"
        cview = CompositeView(name, vlist)
        cview.view.set_name(name)
        ViewMappings[cview.view.name()] = cview
        return cview

viewRegistry = CalendarViewRegistry()


r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Custom View
    Command: =org_agenda_custom_view=  Keys: =alt+o o a= (example global keymap); NeoVintageous normal mode: =space o a=
    Args: =toShow= (default ="Default"=)

    Builds the agenda named =toShow= in =AgendaCustomViews= (the ="Default"=
    one is titled "Agenda"): each listed view, one under the other. Bind it
    with ={"toShow": "Todos"}= for a key per agenda. An unknown name raises
    an error in the console. Needs the orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaCustomViewCommand(sublime_plugin.TextCommand):
    def onDone(self, edit, agenda, onDone, pos):
        agenda.DoRenderView(edit)
        if pos:
            agenda.RestoreCursor(pos)
        log.info("Custom view refreshed")
        evt.EmitIf(onDone)

    def run(self, edit, toShow="Default", onDone=None, draw=False, pos=None):
        if draw:
            agenda = FindMappedView(self.view)
            self.onDone(edit, agenda, onDone, pos)
            return
        #if(self.view.name() == "Agenda"):
        pos = self.view.sel()[0].begin()
        views = sets.Get("AgendaCustomViews",{ "Default": ["Calendar", "Week", "Day", "Blocked Projects", "Next Tasks", "Loose Tasks"]})
        views = views[toShow]
        nameOfShow = toShow
        if(toShow == "Default"):
            nameOfShow = "Agenda"

        vlist = viewRegistry.CreateViews(views)

        def show():
            agenda = viewRegistry.CreateCompositeView(views, nameOfShow, vlist)
            agenda.view.run_command("org_agenda_custom_view", {"draw": True, "onDone": onDone, "toShow": toShow, "pos": pos})
        # Everything is fetched off the ui thread before the view is built.
        FetchViews(vlist, show)

        #agenda = CompositeView("Agenda", views)
        #agenda = AgendaView(AGENDA_VIEW)


r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Choose View
    Command: =org_agenda_choose_custom_view=  Keys: none

    Lists the agendas defined in =AgendaCustomViews= in a quick panel and
    shows the one you pick (as Org Agenda Custom View).
EDOC */
"""

# ================================================================================
# TODO: This is a work in progress that only lists them right now.
#       The goal is add parameters for filtered todos and support
#       multiple calendar views in the end. I should probably
#       rename the command above so we know it is intended to directly
#       select a view rather than use a quick panel.
#       I would like to add:
#       1. Vertical Week View (Org Style)
#       2. Horizontal Week View (Calendar Style)
#       3. Month View (Vim Style)
#       4. Month View Quick Highlight (My Style)
#       5. Various Filtered Todo lists.
#       6. Try an HTML version of a todo?
class OrgAgendaChooseCustomViewCommand(sublime_plugin.TextCommand):
    def on_done_st4(self,index,modifers):
        self.on_done(index)
    def on_done(self, index):
        if(index < 0):
            return
        key = self.keys[index]
        self.view.run_command("org_agenda_custom_view", { "toShow": key })
        evt.EmitIf(self.onDone)

    def run(self, edit, onDone=None):
        self.onDone = onDone
        self.views = sets.Get("AgendaCustomViews",{ "Default": ["Calendar", "Day", "Blocked Projects", "Next Tasks", "Loose Tasks"]})
        self.keys = list(self.views.keys())
        if(int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.keys, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.keys, self.on_done_st4, -1, -1)

r"""
/* SDOC: Reference::Commands::Agenda
*** Internal agenda commands
    =org_agenda_re_render_view= (args =refilter=, =pos=) redraws an agenda
    view from what was last fetched; the filter input and the agenda actions
    use it. =org_tag_filtered_todo_view_internal= (arg =tags=) is what Org
    Filtered Todo View runs. Neither is meant to be bound.
EDOC */
"""

# ================================================================================
# When the view filter changes we re-render the todo view.
class OrgAgendaReRenderViewCommand(sublime_plugin.TextCommand):
    def run(self, edit, refilter=False, pos=None):
        v = FindMappedView(self.view)
        if v != None:
            if refilter:
                v.entries = []
                v.FilterEntries()
            v.DoRenderView(edit, True)
            if pos is not None:
                v.RestoreCursor(min(pos, self.view.size()))

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Reopen Filter
    Command: =org_agenda_re_open_filter_view=  Keys: =alt+o f f= (in an agenda view); NeoVintageous normal mode: =f f=  (no palette entry)

    Opens the filter input of the todo lists in the current agenda view. As
    you type, only headings whose text matches the regular expression from
    their start are shown. Views that are not todo lists (calendar, week,
    day) have no filter.
EDOC */
"""

# ================================================================================
class OrgAgendaReOpenFilterViewCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        v = FindMappedView(self.view)
        if v != None:
            v.OpenFilterView()

# ================================================================================
# Agenda actions. The heading lives in some other file, so these go through the
# orgs server: find the entry's heading, save its buffer if it has unsaved
# changes, look up the server's hash for it (checked by headline), make the
# change on the server and re-render the agenda from the rewritten file.

def AgendaEntryTarget(entry):
    """(filename, row, headline, hash, level) for an agenda entry, or None. The
    headline is the server's (stamps included), the hash the one it sent
    with the agenda (maybe stale by now)."""
    if not entry:
        return None
    n = entry.get('node')
    if n is None or n.is_root() or not getattr(n, "filename", None) or n.start_row is None:
        return None
    return (n.filename, n.start_row, n.headline, n.hash, n.level)

def AgendaTargetAt(view):
    agenda = FindMappedView(view)
    if not agenda:
        return None
    rc = view.curRowCol()
    if rc is None:
        return None
    tgt = AgendaEntryTarget(agenda.EntryAt(rc[0], rc[1]))
    if tgt is None:
        sublime.status_message("Org agenda: not on an agenda entry")
    return tgt

def SameHeadline(a, b):
    return " ".join(agnode.RE_LINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), a or "").split()) == \
           " ".join(agnode.RE_LINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), b or "").split())

def AgendaEntryLocate(cl, filename, row, heading, h=None, level=None):
    """(worker thread) Save the file if it is open with unsaved changes (in any
    window), wait for the server to re-read it, then return (hash, row) of the
    heading, checked against the server now: the agenda was fetched a while
    ago. When the file has changed since (the hash the agenda had is gone or
    elsewhere) the heading is looked for again by its headline and level
    (and its hash, if that is one heading's alone), and only taken when that
    finds exactly one; more than one is refused, as the old row may now be
    any of them."""
    orgs.sync_path(cl, filename)
    t = cl.by_hash(h) if h else None
    if t is None or t.get("LineNum") != row or not PathMap.same(t.get("Filename"), filename):
        cands = [x for x in (cl.headings(filename) or [])
                 if SameHeadline(x.get("Headline"), heading) and (level is None or x.get("Level") == level)]
        if len(cands) > 1 and h:
            # The hash still tells them apart, when it is one heading's only.
            same = [x for x in cands if x.get("Hash") == h]
            if len(same) == 1:
                cands = same
        if len(cands) == 1:
            row = cands[0].get("LineNum")
        elif len(cands) > 1:
            raise orgsclient.OrgsError(
                "'{0}' moved in {1} since the agenda was drawn and {2} headings now have that headline; "
                "refresh the agenda and try again".format(agnode.DisplayHeading(heading), os.path.basename(filename), len(cands)))
    return (orgs.heading_hash(cl, filename, row, heading), row)

def AgendaEntryHash(cl, filename, row, heading, h=None, level=None):
    """(worker thread) The hash of AgendaEntryLocate."""
    return AgendaEntryLocate(cl, filename, row, heading, h, level)[0]

def SyncClockedFile(cl):
    """(worker) A clock in or out writes the CLOCK line of the running clock:
    save that file first if it is open and modified."""
    cur = cl.clock()
    if cur.get("Active"):
        orgs.sync_path(cl, (cur.get("Target") or {}).get("Filename"))
    return cur

def RefreshAgenda(view, message=None):
    """Refetch, re-filter and re-render an agenda view (after the server
    changed a file, or for another day)."""
    agenda = FindMappedView(view)
    if not agenda:
        return
    pos = view.sel()[0].begin() if len(view.sel()) > 0 else None

    def done():
        if view.is_valid():
            view.run_command("org_agenda_re_render_view", {"refilter": True, "pos": pos})
        if message:
            sublime.status_message(message)
    FetchViews([agenda], done, what="agenda refresh")

class AgendaEntry:
    """The entry one run of an agenda command acts on: (file, row, heading)
    in tgt and its server hash h. Each run has its own, so two commands in
    flight at once never swap entries."""
    def __init__(self, tgt, h):
        self.tgt = tgt
        self.h = h

    def Locate(self, cl):
        """(worker) Sync and verify again, right before a write: a panel may
        have been open for a while and the file edited meanwhile."""
        fn, row, heading, _, level = self.tgt
        self.h, row = AgendaEntryLocate(cl, fn, row, heading, self.h, level)
        self.tgt = (fn, row, heading, self.h, level)
        return self.h

class AgendaServerCommand(sublime_plugin.TextCommand):
    """Base for agenda commands that act on the entry under the cursor.

    Look(cl, h, tgt) runs on a worker thread and returns whatever Choose
    needs; Choose(info, e) runs on the ui thread and calls Apply(e, fn) to
    make the change with fn(cl, h)."""
    what = "agenda"

    def run(self, edit):
        tgt = AgendaTargetAt(self.view)
        if not tgt:
            return

        def look(cl):
            h, row = AgendaEntryLocate(cl, *tgt)
            t = (tgt[0], row, tgt[2], h, tgt[4])
            return (h, t, self.Look(cl, h, t))

        def got(res):
            h, t, info = res
            self.Choose(info, AgendaEntry(t, h))
        orgs.run(look, got, what=self.what)

    def Look(self, cl, h, tgt):
        return None

    def Choose(self, info, e):
        self.Apply(e, self.Change)

    def Change(self, cl, h):
        pass

    def Apply(self, e, fn, message=None):
        view = self.view
        orgs.run(lambda cl: fn(cl, e.Locate(cl)), lambda r: RefreshAgenda(view, message), what=self.what)

    def QuickPanel(self, items, on_pick, selected=-1):
        def done(i, *args):
            if i >= 0:
                on_pick(i)
        self.view.window().show_quick_panel(items, done, 0, selected)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Change Todo
    Command: =org_agenda_change_todo=  Keys: =s= (in an agenda view); NeoVintageous normal mode: =t=

    Changes the todo state of the agenda entry under the cursor: a quick
    panel lists the states that heading's file allows (active and done, plus
    "none"), with the current one selected. The change is made by the orgs
    server in the heading's file (an open, modified buffer of that file is
    saved first) and the agenda is refetched. When the file changed since
    the agenda was drawn the heading is found again by its headline, and
    the command refuses when that is ambiguous. Needs the orgs server.
EDOC */
"""

# ================================================================================
# Change the TODO status of the node.
class OrgAgendaChangeTodoCommand(AgendaServerCommand):
    what = "change status"

    def Look(self, cl, h, tgt):
        st = cl.todo_states(h) or {}
        cur = (cl.by_hash(h) or {}).get("Status", "")
        return (list(st.get("Active") or []) + list(st.get("Done") or []), cur)

    def Choose(self, info, e):
        states, cur = info
        if not states:
            sublime.status_message("orgs: no todo states for this heading")
            return
        items = states + ["none"]
        sel = items.index(cur) if cur in items else -1

        def pick(i):
            v = "" if items[i] == "none" else items[i]
            self.Apply(e, lambda cl, h: cl.set_status(h, v), "Status: " + (v or "none"))
        self.QuickPanel(items, pick, sel)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Change Priority
    Command: =org_agenda_change_priority=  Keys: =,= (in an agenda view)

    Sets the priority of the agenda entry under the cursor from a quick
    panel of =priorities= (default A to E) plus "none". Done by the orgs
    server in the heading's file, then the agenda is refetched. Needs the
    orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaChangePriorityCommand(AgendaServerCommand):
    what = "change priority"

    def Look(self, cl, h, tgt):
        return (cl.by_hash(h) or {}).get("Priority", "")

    def Choose(self, cur, e):
        prios = list(sets.Get("priorities", ["A", "B", "C", "D", "E"]))
        items = prios + ["none"]
        sel = items.index(cur) if cur in items else -1

        def pick(i):
            v = "" if items[i] == "none" else items[i]
            self.Apply(e, lambda cl, h: cl.set_priority(h, v), "Priority: " + (v or "none"))
        self.QuickPanel(items, pick, sel)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda ClockIn
    Command: =org_agenda_clock_in=  Keys: =c i= (in an agenda view)

    Clocks in to the agenda entry under the cursor. The server clocks out a
    running clock first and writes both CLOCK lines; open, modified buffers
    of those files are saved first. The clock is the server's, shared with
    every other orgs client. Needs the orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaClockInCommand(AgendaServerCommand):
    what = "clock in"

    def Choose(self, info, e):
        fn = e.tgt[0]

        def clock_in(cl, h):
            # The server clocks out a running clock first and writes its file.
            SyncClockedFile(cl)
            return cl.clock_in(orgsclient.target(cl.remote(fn), h, "hash"))
        self.Apply(e, clock_in, "Clocked in: " + agnode.DisplayHeading(e.tgt[2]))

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda ClockOut
    Command: =org_agenda_clock_out=  Keys: =c o= (in an agenda view)

    Clocks out the running clock, wherever it is (not only the entry under
    the cursor), and refreshes the agenda; says so when no clock is
    running. Needs the orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaClockOutCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view

        def clock_out(cl):
            if not SyncClockedFile(cl).get("Active"):
                return False
            cl.clock_out()
            return True
        orgs.run(clock_out, lambda r: RefreshAgenda(view, "Clocked out" if r else "No clock is running"),
                 what="clock out")

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Insert Tag
    Command: =org_agenda_insert_tag=  Keys: =t= (in an agenda view, without NeoVintageous)

    Toggles a tag on the agenda entry under the cursor. A quick panel lists
    every tag the server and the local database know, marked =[x]= when the
    heading has it; "New tag..." asks for one. Picking a tag adds it or
    removes it. Needs the orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaInsertTagCommand(AgendaServerCommand):
    what = "tags"
    NEW_TAG = "New tag..."

    def Look(self, cl, h, tgt):
        cur = (cl.by_hash(h) or {}).get("Tags") or []
        return (cur, cl.all_tags() or [])

    def Choose(self, info, e):
        cur, alltags = info
        tags = sorted(set(alltags) | set(cur) | set(db.Get().tags))
        items = [self.NEW_TAG] + [("[x] " if t in cur else "[ ] ") + t for t in tags]
        window = self.view.window()

        def toggle(tag):
            tag = tag.strip().strip(':')
            if tag:
                self.Apply(e, lambda cl, h: cl.toggle_tag(h, tag), "Toggled tag: " + tag)

        def pick(i):
            if i == 0:
                window.show_input_panel("Tag:", "", toggle, None, None)
            else:
                toggle(tags[i - 1])
        self.QuickPanel(items, pick)

# ================================================================================
class AgendaPropertyCommand(AgendaServerCommand):
    prop = None

    def Look(self, cl, h, tgt):
        return ((cl.by_hash(h) or {}).get("Props") or {}).get(self.prop, "")

    def Choose(self, cur, e):
        name = self.prop

        def done(value):
            value = value.strip()
            if value:
                self.Apply(e, lambda cl, h: cl.set_property(h, name, value), name + ": " + value)
        self.view.window().show_input_panel(name + ":", cur or "", done, None, None)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Insert Effort
    Command: =org_agenda_insert_effort=  Keys: =e= (in an agenda view)

    Asks for the =EFFORT= property of the agenda entry under the cursor
    (prefilled with the current one) and sets it through the orgs server.
    An empty answer changes nothing.
EDOC */
"""

class OrgAgendaInsertEffortCommand(AgendaPropertyCommand):
    what = "effort"
    prop = "EFFORT"

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Insert Assign
    Command: =org_agenda_assign=  Keys: =a= (in an agenda view)

    Asks for the =ASSIGNED= property of the agenda entry under the cursor
    (prefilled with the current one) and sets it through the orgs server.
EDOC */
"""

# ================================================================================
class OrgAgendaAssignCommand(AgendaPropertyCommand):
    what = "assign"
    prop = "ASSIGNED"

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Insert Id
    Command: =org_agenda_id=  Keys: =i= (in an agenda view)

    Gives the agenda entry under the cursor an =ID= property (a new uuid)
    through the orgs server. A heading that already has one keeps it; the
    status bar shows it.
EDOC */
"""

# ================================================================================
class OrgAgendaIdCommand(AgendaPropertyCommand):
    what = "id"
    prop = "ID"

    def Choose(self, cur, e):
        if cur:
            sublime.status_message("Heading already has ID " + cur)
            return
        value = str(uuid.uuid4())
        self.Apply(e, lambda cl, h: cl.set_property(h, "ID", value), "ID: " + value)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Goto Split
    Command: =org_agenda_go_to_split=  Keys: =space= (in an agenda view)

    Opens the agenda entry under the cursor in the other group, making a
    two column layout when the window has one group, so the agenda stays
    visible.
EDOC */
"""

# ================================================================================
# Goto the file but in a split (SPACE)
class OrgAgendaGoToSplitCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        path = AgendaGotoPath(self.view)
        if path:
            newView = self.view.window().open_file(path, sublime.ENCODED_POSITION)
            move_file_other_group(self.view, newView)

# ================================================================================
class OrgTagFilteredTodoViewInternalCommand(sublime_plugin.TextCommand):
    def run(self,edit,tags):
        # TODO: add filtering to this and name it nicely
        todo = TodoView(TODO_VIEW + " Filtered By: " + tags, False, tagfilter=tags)

        def show():
            todo.SetupView()
            todo.view.run_command("org_agenda_re_render_view")
        FetchViews([todo], show, what="todo view")

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Filtered Todo View
    Command: =org_tag_filtered_todo_view=  Keys: none

    Asks for a tag filter and shows the todo view ("Org Todos Filtered By:
    ...") of the todos that pass it. The filter is space separated tags:
    =TAG= or =+TAG= must be present, =|TAG= at least one of these, =-TAG=
    must not be present. Needs the orgs server.
EDOC */
"""

# ================================================================================
class OrgTagFilteredTodoViewCommand(sublime_plugin.TextCommand):
    def run(self,edit):
        self.view.window().show_input_panel(
                    "Tags:",
                    "",
                    self.showTodos, None, None)

    def showTodos(self, tags):
        if(not tags):
            return
        self.view.run_command('org_tag_filtered_todo_view_internal', {"tags": tags})

# ================================================================================
# Another day: the agenda is refetched for it, then redrawn.
def MoveAgendaDay(view, days):
    agenda = FindMappedView(view)
    if agenda is None or not hasattr(agenda, "now"):
        return
    agenda.UpdateNow(agenda.now + datetime.timedelta(days=days))
    RefreshAgenda(view)

r"""
/* SDOC: Reference::Commands::Agenda
*** Org Agenda Goto Next Day / Prev Day
    Command: =org_agenda_goto_next_day=, =org_agenda_goto_prev_day=  Keys: =>= / =<= (in an agenda view)

    Moves the agenda a day forward or back: the views are refetched for the
    new date and redrawn. Pressed quickly several times, only the newest
    fetch is drawn.
EDOC */
"""

class OrgAgendaGotoNextDayCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        MoveAgendaDay(self.view, 1)

# ================================================================================
class OrgAgendaGotoPrevDayCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        MoveAgendaDay(self.view, -1)
