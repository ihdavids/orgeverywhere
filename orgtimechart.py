import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import OrgEverywhere.orgparse.node as node
from   OrgEverywhere.orgparse.sublimenode import * 
import logging
import sys
import traceback 
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgduration as dur
import OrgEverywhere.orgagenda as ag
import OrgEverywhere.orgs.client as orgsclient
from OrgEverywhere.orgs.paths import PathMap
import uuid
import subprocess


log = logging.getLogger(__name__)


RE_FILE_PROPERTY = re.compile(r"^\s*#\+PROPERTY:\s*(\S+)\s+(.*?)\s*$", re.IGNORECASE)


def FileProperties(filename):
    """The #+PROPERTY: lines above a file's first heading, read off the text
    (not parsed: only the file's own header lines are looked at)."""
    props = {}
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("*"):
                    break
                m = RE_FILE_PROPERTY.match(line)
                if m:
                    props[m.group(1).upper()] = m.group(2)
    except (IOError, OSError):
        pass
    return props


class OrgTimesheet(ag.TodoView):
    def __init__(self, name, setup=True, **kwargs):
        super(OrgTimesheet, self).__init__(name, False, **kwargs)
        self._fileProps = {}

    # A timesheet lists finished tasks as well as open ones. Headings marked
    # ORDERED come too (FilterEntry keeps them off the sheet), so a task can
    # see that its parent orders its children.
    def Spec(self):
        return ("nodes", '(IsTodo() || IsDone() || HasProperty("ORDERED")) && !IsArchived()')

    def _nodes(self):
        return self.data.nodes if self.data else []

    # (worker) Where the server says each AFTER id is: {id: (filename, row)},
    # looked up once per fetch so drawing never waits on the network. The
    # files' outlines come too, so the clocked time of a heading's subtree is
    # summed exactly (data is not shared with the ui thread yet).
    def FetchMore(self, cl, data):
        out = {}
        for fn in sorted(set(n.filename for n in (data.nodes if data else []) if n.filename)):
            try:
                data.SetOutline(fn, cl.headings(fn))
            except orgsclient.Unavailable:
                raise
            except orgsclient.OrgsError as e:
                log.warning("timesheet: could not read the outline of %s: %s", fn, e)
        for n in (data.nodes if data else []):
            dep = n.get_property("AFTER", "")
            if not dep or dep in out:
                continue
            try:
                t = cl.by_id(dep)
            except orgsclient.Unavailable:
                raise
            except orgsclient.OrgsError as e:
                log.warning("timesheet: could not look up AFTER %s: %s", dep, e)
                t = None
            out[dep] = (t.get("Filename"), t.get("LineNum")) if t and t.get("Filename") else None
        return out

    # The heading just above n in its parent, or the last heading under that
    # one: what an ORDERED parent makes n wait for (orgparse's
    # get_sibling_and_child_up, over the headings the server sent).
    def PreviousInOrder(self, n):
        par = n.parent
        best = None
        for d in self._nodes():
            if d is n or not PathMap.same(d.filename, n.filename) or d.start_row is None:
                continue
            if d.start_row < n.start_row and d.level >= n.level and \
               (self.data.IsUnder(d, par) if not par.is_root() else True):
                if best is None or d.start_row > best.start_row:
                    best = d
        return best

    def InsertTableHeadings(self, edit):
        self.view.insert(edit, self.view.sel()[0].begin(), "|Name|Estimate|Start|End|Dep|Assigned|Spent|X|\n|-\n")

    # Dependencies are marked with the AFTER tag or an ORDERED list.
    def GetAfter(self, n):
        dep = n.get_property("AFTER", "")
        if dep and not dep == "":
            # Where the server said the heading is when the sheet was fetched;
            # matched to our entries by the server's row.
            dep = (self.fetchedMore or {}).get(dep)
        if not dep or dep == "":
            t = n
            while t.parent is not None and not t.parent.is_root():
                orde = t.parent.get_property("ORDERED", None)
                if orde is not None:
                    dep = self.PreviousInOrder(n)
                    # Chain to parent in ORDERED setup.
                    if dep is None and t != n:
                        dep = n.parent
                    break
                t = t.parent
        return dep

    def GetGlobalProperty(self, name, n, ass):
        fn = n.filename
        if fn not in self._fileProps:
            self._fileProps[fn] = FileProperties(fn)
        return self._fileProps[fn].get(name.upper(), ass)

    def GetAssigned(self, n):
        ass = sets.Get("timesheetDefaultAssigned", "")
        ass = self.GetGlobalProperty("ASSIGNED", n, ass)
        ass = n.get_property("ASSIGNED", ass)
        return ass

    def GetSection(self, n):
        ass = sets.Get("timesheetDefaultSection", None)
        ass = self.GetGlobalProperty("SECTION", n, ass)
        if n.parent is not None:
            ass = n.parent.get_property("SECTION", ass)
        ass = n.get_property("SECTION", ass)
        return ass

    # Clocked time of the heading and everything under it.
    def GetClockingData(self, n):
        mins = n.raw.get("ClockMins") or 0
        for d in (self.data.Descendants(n) if self.data else []):
            mins += d.raw.get("ClockMins") or 0
        if not mins:
            return ""
        return str(datetime.timedelta(minutes=round(mins)))

    def PreprocessAfter(self):
        for entry in self.entries:
            n        = entry['node']
            sec = self.GetSection(n)
            if sec:
                entry['section'] = sec
            dep = self.GetAfter(n)
            if dep:
                entry['after'] = dep
                index = 0
                for dp in self.entries:
                    index += 1
                    d = dp['node']
                    if d is None:
                        continue
                    if d == dep or (isinstance(dep, tuple) and d.start_row == dep[1] and dp.get('file')
                                    and PathMap.same(dp['file'].filename, dep[0])):
                        entry['after_offset'] = index
                        entry['after_name'] = d.heading

    def RenderSheet(self, edit, view):
        self.view = view
        self.InsertTableHeadings(edit)
        newEntries = []
        self.PreprocessAfter()

        for entry in self.entries:
            n        = entry['node']
            filename = entry['file'].AgendaFilenameTag()
            estimate = n.get_property("EFFORT", "")
            dt = None
            timestamps = n.get_timestamps(active=True, point=True, range=True)
            end = ""
            start = ""
            if timestamps and len(timestamps) > 0:
                dt = timestamps[0].start
            if n.deadline:
                dt = n.deadline.start
            if n.scheduled:
                dt = n.scheduled.start
            if dt:
                start = dt.strftime("<%Y-%m-%d>")
                if estimate != "":
                    duration = dur.OrgDuration.Parse(estimate)
                    endtm = dt + duration.timedelta()
                    end = endtm.strftime("<%Y-%m-%d>")  
                    pass
            else:
                start = ""
            done = ""
            if ag.IsDone(n):
                done = "x"

            spent = self.GetClockingData(n)
            dependenton = entry['after_offset'] if 'after_offset' in entry else ""
            # TODO: Adjust index to match table separators
            assigned = self.GetAssigned(n)
            self.view.insert(edit, self.view.sel()[0].begin(), "|{0:15}|{1:12}|{2}|{3}|{4}|{5}|{6}|{7}|\n".format(n.heading,estimate,start,end,dependenton,assigned,spent,done))

    def RenderMermaidGanttFile(self):
        tpath = sets.Get("timesheetPath", None)
        if tpath is None:
            print("ERROR CANNOT RENDER MERMAID WITHOUT timesheetPath in config as destination for file")
            return
        if not os.path.exists(tpath):
            os.makedirs(tpath)
        filename = os.path.join(tpath, "schedule.mermaid")
        with open(filename, "w") as f:
            self.CreateMermaidGanttFile(f)
        self.GenerateMermaidGanttChartFromFile(filename)

    def GenerateMermaidGanttChartFromFile(self, filename):
        # To install on mac or linux:
        # npm install @mermaid-js/mermaid-cli
        # if sys.platform == 'darwin':
        #    mmdc = "./node_modules/.bin/mmdc"
        # elif sys.platform == 'win32':
        #    mmdc = "C:\\Users\\ihdav\\node_modules\\.bin\\mmdc.ps1"
        print("Trying to render: " + filename)
        execs = sets.Get("mermaidPath", None)
        tpath = sets.Get("timesheetPath", None)
        if execs is None:
            print("CANNOT RENDER MERMAID WITHOUT mmdc command line tool. Please install mermaid-cli and fill in mermaidPath")
            return
        outputFilename = os.path.join(tpath, "project_schedule.png")
        # inputFilename = "D:\\Git\\notes\\worklog\\schedule.mermaid"
        if sys.platform == 'win32':
            commandLine = ["powershell.exe", execs, "-i", filename, "-o", outputFilename, "--width", "2500", "--height", "1024"]
        elif sys.platform == 'darwin':
            commandLine = [execs, "-i", filename, "-o", outputFilename, "--width", "2500", "--height", "1024"]
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        except Exception:
            startupinfo = None
        view = sublime.active_window().active_view()
        fn = view.file_name()
        cwd = tpath
        if fn is not None:
            cwd = os.path.dirname(fn)
        popen = subprocess.Popen(commandLine, universal_newlines=True, cwd=cwd, startupinfo=startupinfo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # popen.wait()
        (o, e) = popen.communicate()
        log.debug(o)
        log.debug(e)

    def RenderGoogleGanttFile(self):
        tpath = sets.Get("timesheetPath", None)
        if tpath is None:
            print("ERROR CANNOT RENDER GOOGLE GANTT WITHOUT timesheetPath in config")
            return
        if not os.path.exists(tpath):
            os.makedirs(tpath)
        filename = os.path.join(tpath, "project_schedule.html")
        with open(filename, "w") as f:
            self.CreateGoogleGanttFile(f)

    def CreateGoogleGanttFile(self, f):
        self.PreprocessAfter()
        import re
        idx = 0
        f.write("""
<html>
<head>
  <!-- <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source%20Sans%20Pro">  -->
  <script type="text/javascript" src="https://www.gstatic.com/charts/loader.js"></script>

<style>
text {
  font-family: "Sofia" !important;
}
body {
  font-family: "Sofia", sans-serif;
}
</style>
  <script type="text/javascript">
    google.charts.load('current', {'packages':['gantt']});
    google.charts.setOnLoadCallback(drawChart);

    function daysToMilliseconds(days) {
      return days * 24 * 60 * 60 * 1000;
    }

    function drawChart() {

      var data = new google.visualization.DataTable();
      data.addColumn('string', 'Task ID');
      data.addColumn('string', 'Task Name');
      data.addColumn('string', 'Resource');
      data.addColumn('date', 'Start Date');
      data.addColumn('date', 'End Date');
      data.addColumn('number', 'Duration');
      data.addColumn('number', 'Percent Complete');
      data.addColumn('string', 'Dependencies');
      data.addRows([""")

        curSection = None
        for entry in self.entries:
            n        = entry['node']
            filename = entry['file'].AgendaFilenameTag()
            section  = entry['section'] if 'section' in entry else None
            estimate = n.get_property("EFFORT", "")
            dt = None
            timestamps = n.get_timestamps(active=True,point=True,range=True)
            end = None
            start = None
            duration = "1"
            if estimate != "":
                duration = dur.OrgDuration.Parse(estimate).days()
            if timestamps and len(timestamps) > 0:
                dt = timestamps[0].start
            if n.deadline:
                dt = n.deadline.start
            if n.scheduled:
                dt = n.scheduled.start
            if dt:
                start = dt
                if estimate != "":
                    duration = dur.OrgDuration.Parse(estimate)
                    end = dt + duration.timedelta()
                    duration = duration.days()
                    pass
            else:
                start = None
            done = False
            percentDone = 0
            if ag.IsDone(n):
                done = True
                percentDone = 100

            resource="active"

            spent = self.GetClockingData(n)
            dependenton = entry['after_name'] if 'after_name' in entry else None
            assigned = self.GetAssigned(n)
            idx += 1
            if(idx > 0):
                #if(done):
                #    continue
                #if(curSection != section and section != None):
                #    f.write("section {name}\n".format(name=section))
                #    curSection = section
                date = start
                dep = dependenton
                if assigned and assigned.strip() != None:
                    resource = assigned.strip()
                if done:
                    resource = "done"
                if (date == ""):
                    date = sets.Get("timesheetDefaultStartDate","2021-05-18")
                if start:
                    start = "new Date({year},{month},{day})".format(year=start.year,month=start.month,day=start.day)
                else:
                    start = "null"
                if end:
                    end = "new Date({year},{month},{day})".format(year=end.year,month=end.month,day=end.day)
                else:
                    end = "null"
                line = ""
                if idx != 1:
                    line += ","
                if (dep != None and dep != ""):
                    line += "[\"{name}\",\"{name}\",\"{resource}\", {start},{end},daysToMilliseconds({duration}),{percent},\"{after}\"]\n".format(name=n.heading,idx=idx,after=str(dep),duration=duration,start=start,end=end,percent=percentDone,resource=resource)
                else:
                    line += "[\"{name}\",\"{name}\",\"{resource}\", {start},{end},daysToMilliseconds({duration}),{percent},null]\n".format(name=n.heading,idx=idx,start=start,end=end,duration=duration,percent=percentDone,resource=resource)
                f.write(line)
        # colorByRowLabel: true 
        f.write("""]);
var options = {
        'font-family': 'Sofia',
        height: 1200,
        is3D: true,
        title: 'Hello World',
        gantt: {
          trackHeight: 30,
        }
      };

      var chart = new google.visualization.Gantt(document.getElementById('chart_div'));

      chart.draw(data, options);
    }
  </script>
</head>
<body>
  <div id="chart_div"></div>
</body>
</html>
""")
        
    # This is REALLY rough it gives us a non functional, really basic view.
    def CreateMermaidGanttFile(self,f):
        self.PreprocessAfter()
        import re
        idx = 0
        f.write("gantt\n")
        f.write("\tdateFormat YYYY-MM-DD\n")
        f.write("\taxisFormat %m-%d\n")
        f.write("\ttitle Bench Schedule\n")
        f.write("\ttodayMarker off\n")
        f.write("\texcludes    weekends\n")
        curSection = None
        for entry in self.entries:
            n        = entry['node']
            filename = entry['file'].AgendaFilenameTag()
            section  = entry['section'] if 'section' in entry else None
            estimate = n.get_property("EFFORT", "")
            dt = None
            timestamps = n.get_timestamps(active=True, point=True, range=True)
            end = ""
            start = ""
            duration = "1d"
            if estimate != "":
                duration = dur.OrgDuration.Parse(estimate)
            if timestamps and len(timestamps) > 0:
                dt = timestamps[0].start
            if n.deadline:
                dt = n.deadline.start
            if n.scheduled:
                dt = n.scheduled.start
            if dt:
                start = dt.strftime("%Y-%m-%d")
                if estimate != "":
                    duration = dur.OrgDuration.Parse(estimate)
                    endtm = dt + duration.timedelta()
                    end = endtm.strftime("%Y-%m-%d")  
                    pass
            else:
                start = ""
            ## spent = self.GetClockingData(n)
            dependenton = entry['after_offset'] if 'after_offset' in entry else ""
            # TODO: Adjust index to match table separators
            assigned = self.GetAssigned(n)
            #self.view.insert(edit, self.view.sel()[0].begin(), "|{0:15}|{1:12}|{2}|{3}|{4}|{5}|{6}|{7}|\n".format(n.heading,estimate,start,end,dependenton,assigned,spent,done))
            idx += 1
            if (idx > 0):
                if (curSection != section and section is not None):
                    f.write("section {name}\n".format(name=section))
                    curSection = section
                date = start
                dep = dependenton
                prefix = ""
                if (ag.IsDone(n)):
                    prefix = "done,"
                if (assigned == 'D'):
                    prefix = "done,"
                if (assigned == 'A'):
                    prefix += "active,"
                if (assigned == 'C'):
                    prefix += 'crit,'
                if (assigned == 'M'):
                    prefix += 'milestone,'
                if (assigned == 'X'):
                    prefix += 'crit,done,'
                if (assigned == 'Y'):
                    prefix += 'crit,active,'
                if (date == ""):
                    date = sets.Get("timesheetDefaultStartDate", "2023-01-02")
                line = ""
                if (dep is not None and dep != ""):
                    line = "\t{name}\t:{prefix}{idx},{start},{duration}\n".format(prefix=prefix,name=n.heading,idx=idx,start="after " + str(dep),duration=duration)
                else:
                    line = "\t{name}\t:{prefix}{idx},{start},{duration}\n".format(prefix=prefix,name=n.heading,idx=idx,start=date,duration=duration)
                f.write(line)
    def FilterEntry(self, n, filename):
        return (ag.IsDone(n) or ag.IsTodo(n)) and not ag.IsProject(n) and not ag.IsArchived(n)

# ================================================================================
class TimesheetRegistry:
    def __init__(self):
        self.KnownViews = {}
        self.AddView("Todos", OrgTimesheet)

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

    def CreateCompositeView(self,views,name="Timesheet"):
        vlist = []
        for v in views:
            n, args = self.ParseArgs(v)
            # For timesheets we ONLY have the one view.
            # We may need to pull the filter from the other known views in the future?
            n = "Todos"
            vv = None
            if(args == None):
                vv = self.KnownViews[n](n, False)
            else:
                vv = self.KnownViews[n](n, False, **args)
            if(vv):
                return vv
        return None

timesheetRegistry = TimesheetRegistry()


# A timesheet waiting for the server's answer, by view id, until the render
# command (which gets its own edit) draws it.
_pendingSheets = {}


def FetchTimesheet(toShow, on_done):
    """Build the timesheet view for an AgendaCustomViews entry and fetch its
    headings from the server; on_done(ts) on the ui thread."""
    views = sets.Get("AgendaCustomViews", {"Default": ["Todos"]})
    ts = timesheetRegistry.CreateCompositeView(views.get(toShow or "Default", ["Todos"]), toShow)
    if ts is None:
        return

    def fetched():
        if ts.data is None:
            sublime.status_message("Timesheet: no data from the orgs server (not connected?)")
            return
        ts.entries = []
        ts.FilterEntries()
        on_done(ts)
    ag.FetchViews([ts], fetched, what="timesheet")


r"""
/* SDOC: Reference::Commands::Misc
*** Org Timesheet Table
    Command: =org_insert_timesheet=  Keys: none
    Args: =toShow= (an =AgendaCustomViews= name, default =Default=), =onDone=.

    Asks the orgs server for every todo and done heading (not archived, not
    projects) and inserts a table at the cursor with one row per task:
    Name, Estimate (the =EFFORT= property), Start (scheduled, else deadline,
    else first timestamp), End (start plus the estimate), Dep (the row it
    waits for), Assigned, Spent (clocked time of the heading and its sub
    headings) and X (done). A task waits for the heading named by its
    =AFTER= property (an ID), or, under a parent with an =ORDERED=
    property, for the sibling before it.

    Assigned comes from the heading's =ASSIGNED= property, else a file
    =#+ASSIGNED:=, else =timesheetDefaultAssigned=. Needs the orgs server.
    The =org_render_timesheet= command draws the table once the answer
    arrives (internal).

    Settings: =timesheetDefaultAssigned=, =AgendaCustomViews=.
EDOC */
"""


class OrgInsertTimesheetCommand(sublime_plugin.TextCommand):
    def run(self, edit, toShow=None, onDone=None):
        view = self.view
        pos = view.sel()[0]

        def ready(ts):
            _pendingSheets[view.id()] = (ts, pos, onDone)
            view.run_command("org_render_timesheet")
        FetchTimesheet(toShow or "Default", ready)


class OrgRenderTimesheetCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        pending = _pendingSheets.pop(self.view.id(), None)
        if pending is None:
            return
        ts, pos, onDone = pending
        self.view.sel().clear()
        self.view.sel().add(pos)
        ts.RenderSheet(edit, self.view)
        self.view.sel().clear()
        self.view.sel().add(pos)
        self.view.run_command('table_editor_next_field')
        evt.EmitIf(onDone)


r"""
/* SDOC: Reference::Commands::Misc
*** Org Choose Timesheet Table
    Command: =org_choose_timesheet=  Keys: none
    Args: =onDone=.

    A quick panel of the =AgendaCustomViews= names; then inserts that
    timesheet (as Org Timesheet Table). Every name currently gives the same
    todo based sheet.
EDOC */
"""


class OrgChooseTimesheetCommand(sublime_plugin.TextCommand):
    def on_done_st4(self, index, modifers):
        self.on_done(index)

    def on_done(self, index):
        if (index < 0):
            return
        key = self.keys[index]
        self.view.run_command("org_insert_timesheet", {"toShow": key, "onDone": self.onDone})

    def run(self, edit, toShow=None, onDone=None):
        self.onDone = onDone
        self.pos = self.view.sel()[0]
        self.views = sets.Get("AgendaCustomViews", {"Default": ["Todos"]})
        self.keys = list(self.views.keys())
        self.edit = edit
        if (int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.keys, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.keys, self.on_done_st4, -1, -1)


r"""
/* SDOC: Reference::Commands::Misc
*** Org Timesheet Mermaid Gantt
    Command: =org_generate_mermaid_gantt_chart=  Keys: none
    Args: =toShow= (skip the view picker), =onDone=.

    Builds the timesheet (as Org Timesheet Table) and writes it as a
    mermaid gantt chart to =timesheetPath/schedule.mermaid= (excluding
    weekends, grouped into sections by the =SECTION= property, a file
    =#+SECTION:= or =timesheetDefaultSection=), then renders
    =timesheetPath/project_schedule.png= with =mermaidPath=. An =ASSIGNED=
    of =A=, =C=, =M=, =X= or =Y= marks the task active, critical,
    milestone, critical done or critical active. Tasks with no date start
    at =timesheetDefaultStartDate=. Rendering runs on Windows and macOS
    only.

    Needs the orgs server. Settings: =timesheetPath=, =mermaidPath=,
    =timesheetDefaultSection=, =timesheetDefaultStartDate=.
EDOC */
"""


class OrgGenerateMermaidGanttChart(sublime_plugin.TextCommand):

    def on_done_st4(self, index, modifers):
        self.on_done(index)

    def on_done(self, index):
        if (index < 0):
            return
        key = self.keys[index]
        self.Run(key)

    def Run(self, nameOfShow):
        def ready(ts):
            ts.RenderMermaidGanttFile()
            evt.EmitIf(self.onDone)
        FetchTimesheet(nameOfShow, ready)

    def run(self, edit, toShow=None, onDone=None):
        self.onDone = onDone
        self.pos = self.view.sel()[0]
        self.views = sets.Get("AgendaCustomViews", {"Default": ["Todos"]})
        self.keys = list(self.views.keys())
        if toShow is not None:
            self.Run(toShow)
            return
        if (int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.keys, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.keys, self.on_done_st4, -1, -1)


r"""
/* SDOC: Reference::Commands::Misc
*** Org Timesheet Google Gantt
    Command: =org_generate_google_gantt_chart=  Keys: none
    Args: =toShow= (skip the view picker), =onDone=.

    Builds the timesheet and writes =timesheetPath/project_schedule.html=,
    a Google Charts gantt chart (open it in a browser; it loads the chart
    library from Google). Resources are the assignees (=done= for finished
    tasks), dependencies come from =AFTER= / =ORDERED=.

    Needs the orgs server. Settings: =timesheetPath=,
    =timesheetDefaultStartDate=.
EDOC */
"""


r"""
/* SDOC: Reference::Settings::Misc
*** Timesheet and gantt settings
    - =timesheetPath= :: no default. The folder the gantt charts are written
      to (created when missing); required by both gantt commands.
    - =mermaidPath= :: no default. The =mmdc= program (mermaid-cli) used to
      render the mermaid gantt chart.
    - =timesheetDefaultAssigned= :: default empty. The assignee of tasks with
      no =ASSIGNED= property or =#+ASSIGNED:= line.
    - =timesheetDefaultSection= :: no default. The gantt section of tasks
      with no =SECTION= property or =#+SECTION:= line.
    - =timesheetDefaultStartDate= :: the start of undated tasks; when unset
      the code uses =2023-01-02= for the mermaid chart and =2021-05-18= for
      the Google one.
    - =AgendaCustomViews= :: its names are offered by the timesheet
      commands.
EDOC */
"""


class OrgGenerateGoogleGanttChart(sublime_plugin.TextCommand):

    def on_done_st4(self, index, modifers):
        self.on_done(index)

    def on_done(self, index):
        if (index < 0):
            return
        key = self.keys[index]
        self.Run(key)

    def Run(self, nameOfShow):
        def ready(ts):
            ts.RenderGoogleGanttFile()
            evt.EmitIf(self.onDone)
        FetchTimesheet(nameOfShow, ready)

    def run(self, edit, toShow=None, onDone=None):
        self.onDone = onDone
        self.pos = self.view.sel()[0]
        self.views = sets.Get("AgendaCustomViews", {"Default": ["Todos"]})
        self.keys = list(self.views.keys())
        if toShow is not None:
            self.Run(toShow)
            return
        if (int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.keys, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.keys, self.on_done_st4, -1, -1)

    # def run(self, edit, onDone=None):
    #     self.onDone = onDone
    #     self.views = sets.Get("AgendaCustomViews",{ "Default": ["Calendar", "Day", "Blocked Projects", "Next Tasks", "Loose Tasks"]})
    #     self.keys = list(self.views.keys())
    #     if(int(sublime.version()) <= 4096):
    #         self.view.window().show_quick_panel(self.keys, self.on_done, -1, -1)
    #     else:
    #         self.view.window().show_quick_panel(self.keys, self.on_done_st4, -1, -1)
