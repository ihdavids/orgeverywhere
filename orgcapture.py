import sublime
import sublime_plugin
import datetime
import re
import os
import textwrap
import OrgEverywhere.orgparse.loader as loader
import OrgEverywhere.orgparse.node as node
import OrgEverywhere.orgutil.template as templateEngine
import OrgEverywhere.orgparse.date as orgdate
import OrgEverywhere.orgextension as ext
import OrgEverywhere.orgclocking as clk
import logging
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgdaypage as daypage
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as oui
import OrgEverywhere.orgs.client as ocl
from OrgEverywhere.orgs.paths import PathMap

log = logging.getLogger(__name__)
captureBufferName = "*capture*"
lastHeader        = None


def GetViewById(id):
    win = sublime.active_window()
    for v in win.views():
        if (v.id() == id):
            return v
    return None


def GetDict():
    tempDict = {
        'refile': sets.Get('refile', ''),
        'daypage': daypage.dayPageGetName(daypage.dayPageGetToday()),
    }
    return tempDict


def GetCaptureFile(view, template, target):
    # temp = templateEngine.TemplateFormatter(sets.Get)
    filename = templateEngine.ExpandTemplate(view, target[1], GetDict(), sets.Get)[0]
    return filename


def GetCaptureFileAndParam(view, template, target):
    # temp = templateEngine.TemplateFormatter(sets.Get)
    tempDict = GetDict()
    filename = templateEngine.ExpandTemplate(view, target[1], tempDict, sets.Get)[0]
    headline = None
    if (len(target) > 2):
        headline = templateEngine.ExpandTemplate(view, target[2], tempDict, sets.Get)[0]
    return (filename, headline)


def FindNodeByPath(n, target, idx):
    if (idx >= len(target)):
        return n
    for c in n.children:
        if (c.heading == target[idx]):
            return FindNodeByPath(c, target, idx + 1)
    return None


def GetCapturePath(view, template):
    target    = ['file', '{refile}']
    if 'target' in template:
        target    = template['target']
    filename = None
    file = None
    at = None
    toinsert = ""
    if ('file' in target[0]):
        filename = GetCaptureFile(view, template, target)
    if ('file+headline' in target[0]):
        filename, headline = GetCaptureFileAndParam(view, template, target)
        file = db.Get().ParseFile(filename, copy=True)
        if (file and headline):
            at = file.FindOrCreateNode(headline)
            if (at):
                at = at.local_end_row
    elif ('file+regexp' in target[0]):
        filename, reg = GetCaptureFileAndParam(view, template, target)
        file = db.Get().ParseFile(filename, copy=True)
        if (file and reg):
            r = re.compile(reg)
            row = 0
            for n in file.org:
                for line in n._lines:
                    m = r.search(line)
                    if (m):
                        at = row
                        break
                    row += 1
    elif ('file+olp+datetree' in target[0]):
        filename, reg = GetCaptureFileAndParam(view, template, target)
        file = db.Get().ParseFile(filename, copy=True)
        n = FindNodeByPath(file.org, target, 2)
        if (not n):
            n = file.org
        # Now we assume this is a date tree. Default is per day.
        t = datetime.datetime.now()
        yearformat  = t.strftime(GetProp(template, "year-format",  "%Y"))
        monthformat = t.strftime(GetProp(template, "month-format", "%Y-%m %B"))
        dayformat   = t.strftime(GetProp(template, "day-format",   "%Y-%m-%d %A"))
        nyear  = None
        nmonth = None
        nday   = None
        prefix   = ""
        if (n.level > 0):
            prefix = "*" * n.level
        for c in n.children:
            if (c.heading == yearformat):
                nyear = c
                break
        if (nyear is None):
            toinsert += "{prefix}* {yearformat}\n".format(prefix=prefix, yearformat=yearformat)
        else:
            n = nyear
            for c in nyear.children:
                if (c.heading == monthformat):
                    nmonth = c
                    break
        if (nmonth is None):
            toinsert += "{prefix}** {monthformat}\n".format(prefix=prefix, monthformat=monthformat)
        else:
            n = nmonth
            for c in nmonth.children:
                if (c.heading == dayformat):
                    nday = c
                    break
        if (nday is None):
            toinsert += "{prefix}*** {dayformat}\n".format(prefix=prefix, dayformat=dayformat)
        else:
            n = nday
        if (n and toinsert == ""):
            at = n.local_end_row
        else:
            at = n.end_row
    elif ('file+olp' in target[0]):
        filename, reg = GetCaptureFileAndParam(view, template, target)
        file = db.Get().ParseFile(filename, copy=True)
        n = FindNodeByPath(file.org, target, 2)
        if (n):
            at = n.local_end_row

    elif ('id' == target[0]):
        filename, at = db.Get().LocateByCustomId(target[1])
        if (filename is None):
            log.error("Could not find id: " + target[1])
            return
    elif ('clock' == target[0]):
        if (not clk.ClockManager.ClockRunning()):
            sublime.status_message("Capture: no clock is running")
            raise Exception("capture: clock is not running")
        filename = clk.ClockManager.GetActiveClockFile()
        at       = clk.ClockManager.GetActiveClockAt()
    # Try to create my capture file if I can
    try:
        # Ensure the directory exists first
        dirName = os.path.dirname(os.path.abspath(filename))
        if (not os.path.exists(dirName)):
            os.makedirs(dirName, exist_ok=True)
        if (not os.path.isfile(str(filename))):
            with open(filename, "w", encoding=sets. Get("captureWriteFormat", "utf-8")) as fp:
                fp.write("#+FILETAGS: :refile:\n")
    except Exception as e:
        log.error("@@@@@@@@@@@@\nFailed to create capture file: " + str(filename) + "\n" + str(e))
    # A private parse of the target (its open buffer's text if it is open):
    # the capture edits this tree and writes it out.
    if (file is None):
        file = db.Get().ParseFile(filename, copy=True)
    if (not at and file):
        at = file.org.start_row
    return (target, filename, file, at, toinsert)


# This is a bit hokey. We track the last header so
# if you change the header we can still find it
# in your target file. Every time you swap your
# mouse away from the capture window we auto save
# the capture to your target location.
# TODO: Add more target locations than your refile file.
def onDeactivated(view):
    global captureBufferName
    global lastHeader
    if view.settings().get('orgs_capture_panel'):
        # The panel holds (or held) a server capture: never the local path.
        if view.settings().get('orgs_capture'):
            FinishServerCapture(view)
        return
    if (view.name() == captureBufferName and view.settings().get('cap_index') is not None):
        tempIndex = view.settings().get('cap_index')
        templates = sets.Get("captureTemplates", [])
        template  = templates[tempIndex]
        # outpath, outfile = GetCaptureOutput()
        # print('template index was: ' + str(tempIndex))
        target, capturePath, captureFile, at, toinsert = GetCapturePath(GetViewById(view.settings().get('cap_view')), template)
        # refilePath = sets.Get("refile","UNKNOWN")
        # refile = load(refilePath)
        captureFileRoot = None
        if (captureFile is not None):
            captureFileRoot = captureFile.org

        bufferContentsToInsert = view.substr(sublime.Region(0, view.size()))
        if not bufferContentsToInsert.endswith('\n'):
            bufferContentsToInsert += "\n"
        didInsert = False
        # Get a capture node
        captureNode = loader.loads(bufferContentsToInsert)
        # if we moused out of the window we might be replacing
        # ourselves again.
        if (lastHeader is None):
            lastHeader = str(captureNode[1].heading)
        # We may haved moved around in the capture file
        # so find the old heading.
        for heading in captureFileRoot:
            if (type(heading) is node.OrgRootNode):
                continue
            if str(heading.heading) == lastHeader:
                # log.debug("REPLACING: " + str(heading.heading))
                heading.replace_node(captureNode[1])
                didInsert = True
                continue
        if (not didInsert):
            insertAt = captureFileRoot
            inserted = captureNode[1]
            if (at is not None and at > 0):
                # This does not work? its a node not a file
                insertAt = captureFile.At(at)
            insertAt.insert_child(inserted)
        f = open(capturePath, "w+", encoding=sets.Get("captureWriteFormat", "utf-8"))
        # reauthor the ENTIRE file.
        # This can get expensive!
        for item in captureFileRoot:
            f.write(str(item))
        f.close()
        lastHeader = str(captureNode[1].heading)
        log.debug("***************>>>>> [" + view.name() + "] is no more")


# Respond to panel events. If the panel is hidden we stop
# tracking the last header.
def onPostWindowCommand(window, cmd, args):
    if cmd == "hide_panel":
        global lastHeader
        # This stops the capture refiling
        # From finding an entry across capture
        # window openings
        lastHeader = None


r"""
/* SDOC: Reference::Commands::Capture
*** Org Open Refile
    Command: =org_open_refile=  Keys: =ctrl+alt+x= (example global keymap)

    Opens the file named by the =refile= setting, where captures land by
    default. Logs an error to the console when =refile= is not set.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Capture
*** Capture settings
    | Setting                | Default                   | Effect                                                                                           |
    |------------------------+---------------------------+--------------------------------------------------------------------------------------------------|
    | =captureTemplates=     | Todo, Note, Capture, Meeting snippets to ={refile}= | The templates Org Capture offers (see Org Capture for their keys).                      |
    | =refile=               | =""= (unset in code)      | The file captures go to (={refile}= in a target) and that Org Open Refile opens.                 |
    | =captureBufferName=    | ="*capture*"=             | The name given to the capture panel.                                                             |
    | =captureWriteFormat=   | ="utf-8"=                 | The python encoding capture writes target files with (=utf-16=, =utf-32=...).                   |
    | =orgsCaptureTemplates= | =true=                    | Offer the orgs server's capture templates ("orgs: Name") in Org Capture.                         |

    A capture target file that does not exist yet is created (with its
    directory) and given =#+FILETAGS: :refile:=.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Capture
*** orgsRefileDepth
    Default: =3=

    How many heading levels deep the refile and copy pickers (Org Refile,
    Org Copy) list a file's headings. Org Refile File and Heading always
    lists every heading of the chosen file.
EDOC */
"""

class OrgOpenRefileCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        refile = sets.Get("refile", None)
        log.debug("REFILE: ", refile)
        if (refile):
            self.view.window().open_file(refile)
        else:
            log.error("Could not find refile file, have you set it in your config?")


# Refile, copy and archive go through orgs: the server moves the heading and
# rewrites both files. The source buffer is saved first (heading_target) and
# Sublime reloads it once the server has written it.
def RefileDepth():
    try:
        return int(sets.Get("orgsRefileDepth", 3))
    except (TypeError, ValueError):
        return 3


def TargetLabel(t):
    """Quick panel row for a /refile/targets entry."""
    fn = oui.short_file(t.get("Filename"))
    olp = t.get("Olp") or []
    if not olp:
        return [fn + "  (end of file)", t.get("Filename") or ""]
    head = " / ".join(olp)
    if t.get("Todo"):
        head = t["Todo"] + " " + head
    if t.get("Tags"):
        head += "  :" + ":".join(t["Tags"]) + ":"
    return [head, fn]


def HeadingLabel(t):
    """Quick panel row for a heading of one file (Todo dict)."""
    return ["  " * max(0, (t.get("Level") or 1) - 1) + oui.todo_label(t), oui.short_file(t.get("Filename"))]


def DestTarget(cl, t):
    """A Target for a refile target or heading entry: its hash, or its file."""
    if t.get("Hash"):
        return ocl.target(cl.remote(t["Filename"]), t["Hash"], "hash")
    return orgs.file_target(cl, t.get("Filename"))


def ShowPanel(window, items, on_pick, placeholder=None):
    def done(i, *args):
        if i is not None and i >= 0:
            on_pick(i)
    kw = {}
    if placeholder and int(sublime.version()) >= 4081:
        kw["placeholder"] = placeholder
    window.show_quick_panel(items, done, 0, 0, None, **kw)


def MoveHeading(view, op, dest, onDone=None):
    """(ui) Refile or copy the heading at the cursor to a target entry."""
    def work(cl):
        h, src, node = orgs.heading_target(view)
        if dest.get("Hash") and dest.get("Hash") == h:
            raise ocl.OrgsError("cannot {0} a heading onto itself".format(op))
        to = DestTarget(cl, dest)
        # The server rewrites the destination too.
        orgs.sync_path(cl, dest.get("Filename"))
        res = cl.copy(src, to) if op == "copy" else cl.refile(src, to)
        return (node.heading, res)

    def done(r):
        heading, res = r
        where = " / ".join(dest.get("Olp") or []) or oui.short_file(dest.get("Filename"))
        sublime.status_message("orgs: {0} {1} -> {2}".format("copied" if op == "copy" else "refiled", heading, where))
        evt.EmitIf(onDone)
    orgs.run(work, done, what=op)


class OrgServerMoveBase(sublime_plugin.TextCommand):
    op = "refile"
    filesOnly = False
    placeholder = "Refile to"

    def run(self, edit, onDone=None):
        self.onDone = onDone
        node = db.Get().AtInView(self.view)
        if node is None or node.is_root():
            sublime.status_message("Not on a heading")
            return
        depth = 0 if self.filesOnly else RefileDepth()
        orgs.run(lambda c: c.refile_targets(depth=max(1, depth)), self.on_targets, what="refile targets")

    def on_targets(self, targets):
        if self.filesOnly:
            targets = [t for t in targets if not t.get("Olp")]
        if not targets:
            sublime.status_message("orgs: no refile targets")
            return
        self.targets = targets
        ShowPanel(self.view.window(), [TargetLabel(t) for t in targets], self.on_pick, self.placeholder)

    def on_pick(self, i):
        MoveHeading(self.view, self.op, self.targets[i], self.onDone)


r"""
/* SDOC: Reference::Commands::Capture
*** Org Copy
    Command: =org_copy=  Keys: none  Args: =onDone=

    Copies the heading at the cursor (with its subtree) under another
    heading or to the end of a file. A quick panel lists the server's refile
    targets: every org file ("end of file") and its headings to
    =orgsRefileDepth= levels, shown as outline paths with todo state and
    tags. The orgs server writes the destination; open, modified buffers of
    both files are saved first. Copying a heading onto itself is refused.
    Needs the orgs server.
EDOC */
"""

# Copy the current heading under any heading (or to the end of a file).
class OrgCopyCommand(OrgServerMoveBase):
    op = "copy"
    placeholder = "Copy to"


r"""
/* SDOC: Reference::Commands::Capture
*** Org Refile
    Command: =org_refile=  Keys: =alt+o R=; NeoVintageous normal mode: =R=  Args: =onDone=

    Moves the heading at the cursor (with its subtree) under a heading
    picked from all refile targets (as Org Copy, to =orgsRefileDepth=
    levels) or to the end of a file. The orgs server rewrites both files and
    the buffers reload. Needs the orgs server.
EDOC */
"""

# This refile gives you a list of all headings to make it quicker to jump to
# "That heading" This one is bound to R for really quick refiling.
class OrgRefileCommand(OrgServerMoveBase):
    op = "refile"


r"""
/* SDOC: Reference::Commands::Capture
*** Org Refile To EOF
    Command: =org_refile_to_file=  Keys: NeoVintageous normal mode: =space r f=  Args: =onDone=

    Moves the heading at the cursor to the end of an org file picked from
    the files the orgs server knows. Needs the orgs server.
EDOC */
"""

# This refile, files to the end of the file.
class OrgRefileToFileCommand(OrgServerMoveBase):
    op = "refile"
    filesOnly = True
    placeholder = "Refile to the end of"


r"""
/* SDOC: Reference::Commands::Capture
*** Org Refile File and Heading
    Command: =org_refile_to_file_and_headline=  Keys: NeoVintageous normal mode: =space r r=  Args: =onDone=

    Refiles in two steps: first pick the file, then the heading in it (all
    its headings, indented by level) or its end. Quicker than Org Refile
    with many files. Needs the orgs server.
EDOC */
"""

# This refile version takes a filename prompt first
# then it shows the headings in the file.
class OrgRefileToFileAndHeadlineCommand(OrgServerMoveBase):
    op = "refile"
    filesOnly = True
    placeholder = "Refile to file"

    def on_pick(self, i):
        self.file = self.targets[i]
        fn = self.file.get("Filename")
        orgs.run(lambda c: c.headings(fn), self.on_headings, what="headings")

    def on_headings(self, heads):
        self.heads = [self.file] + list(heads or [])
        rows = [[oui.short_file(self.file.get("Filename")) + "  (end of file)", self.file.get("Filename") or ""]]
        rows += [HeadingLabel(t) for t in heads or []]
        ShowPanel(self.view.window(), rows, self.on_heading, "Refile under")

    def on_heading(self, i):
        t = self.heads[i]
        if i > 0:
            # Olp only for the status message.
            t = dict(t)
            t["Olp"] = [t.get("Headline", "")]
        MoveHeading(self.view, self.op, t, self.onDone)


# The server's default archive location (its ArchiveDefaultTarget setting,
# org's own default). Only used to know which buffer to save first.
ARCHIVE_DEFAULT = "%s_archive::"


def ArchivePath(filename, node):
    """The file the server archives node (of filename) into, as the server
    works it out: the heading's ARCHIVE property, the file's #+ARCHIVE, else
    the default. None when it is unknown or this same file."""
    at = None
    if node is not None and not node.is_root():
        at = node.get_property("ARCHIVE")
    if not at and node is not None:
        vals = node.root.get_comment("ARCHIVE", None)
        if vals:
            at = vals[-1] if isinstance(vals, list) else vals
    at = (at or ARCHIVE_DEFAULT).strip()
    parts = at.split("::")
    if len(parts) != 2 or not parts[0].strip():
        return None
    fname = parts[0].strip()
    if "%s" in fname:
        fname = fname.replace("%s", os.path.basename(filename))
    fname = os.path.expanduser(fname)
    if not os.path.isabs(fname):
        fname = os.path.join(os.path.dirname(os.path.abspath(filename)), fname)
    if PathMap.same(fname, filename):
        return None
    return fname


def ArchivePaths(view, rows):
    """(ui) The archive files of the headings at rows of view."""
    fi = db.Get().FindInfo(view)
    out = []
    if fi is None:
        return out
    for row in rows:
        p = ArchivePath(fi.filename, fi.At(row))
        if p and p not in out:
            out.append(p)
    return out


r"""
/* SDOC: Reference::Commands::Capture
*** Org Archive Subtree
    Command: =org_archive_subtree=  Keys: none  Args: =onDone=

    Moves the heading at the cursor to its archive location, worked out by
    the orgs server: the heading's =ARCHIVE= property, else the file's
    =#+ARCHIVE:=, else the server's default (org's =%s_archive::=, i.e.
    =file.org_archive=). The source and archive buffers are saved first when
    open and modified. Needs the orgs server.
EDOC */
"""

# Archiving moves the current heading to its archive file. Where that is is
# the server's call: the heading's or file's ARCHIVE property, else the
# server's archive default (org's %s_archive::).
class OrgArchiveSubtreeCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        node = db.Get().AtInView(self.view)
        if node is None or node.is_root():
            sublime.status_message("Not on a heading")
            return

        def work(cl):
            h, tgt, n = orgs.heading_target(self.view)
            for p in orgs.run_on_main_and_wait(lambda: ArchivePaths(self.view, [n.start_row])):
                orgs.sync_path(cl, p)
            cl.archive(tgt)
            return n.heading

        def done(heading):
            sublime.status_message("orgs: archived " + heading)
            evt.EmitIf(onDone)
        orgs.run(work, done, what="archive")


def InFileQuery(filename):
    """A govaluate InFile() argument matching this file's name and no other.
    InFile takes a regular expression; brackets rather than backslashes keep
    it clear of string escaping."""
    base = os.path.basename(filename)
    out = ""
    for ch in base:
        if ch.isalnum() or ch in "_- ":
            out += ch
        elif ch in '"\\':
            out += "."
        else:
            out += "[" + ch + "]"
    return "(^|/)" + out + "$"


r"""
/* SDOC: Reference::Commands::Capture
*** Org Archive All Done
    Command: =org_archive_all_done=  Keys: none  Args: =onDone=

    Archives every heading of the current file that is in a done state and
    not archived yet, in one request to the orgs server (each to its own
    archive location, as Org Archive Subtree). The file must be saved
    somewhere first. Says so when there is nothing to archive. Needs the
    orgs server.
EDOC */
"""

# Archive every done heading in this file, in one request to the server
# (hashes shift after each write, so they cannot be archived one at a time).
class OrgArchiveAllDoneCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        if not self.view.file_name():
            sublime.status_message("Save the file first")
            return
        view = self.view

        def work(cl):
            fn = orgs.sync_file(view)
            q = 'IsDone() && !IsArchived() && InFile("{0}")'.format(InFileQuery(fn))
            done = [t for t in cl.search(q) if PathMap.same(t.get("Filename") or "", fn)]
            if not done:
                return None
            rows = [t.get("LineNum") for t in done if t.get("LineNum") is not None]
            for p in orgs.run_on_main_and_wait(lambda: ArchivePaths(view, rows)):
                orgs.sync_path(cl, p)
            frm = [ocl.target(cl.remote(t["Filename"]), t["Hash"], "hash") for t in done]
            return cl.move("archive", frm, {})

        def done(res):
            if res is None:
                sublime.status_message("orgs: nothing done to archive")
            else:
                sublime.status_message("orgs: " + (res.get("Msg") or "archived"))
            evt.EmitIf(onDone)
        orgs.run(work, done, what="archive all done")


# --- Capture with orgs templates -------------------------------------------
#
# The server's capture templates (/capture/templates) are forms:
# {{name}}, {{name|prompt}}, {{name|=default}}, {{name|prompt|=default}} and
# {{CONTENT}}. The server does not fill them in; the client does, and posts
# the result. Here the form becomes a snippet in the capture panel (each
# placeholder a tab stop, defaults pre-filled) and when the panel loses focus
# the text is sent once to /capture. For an entry the server writes
# "* Headline :tags:" and indents the content under it, so the first heading
# line becomes Headline (keyword and priority kept in it) and Tags, and the
# rest (property drawer included) is the content.
CAP_PLACEHOLDER = re.compile(r'\{\{\s*([^}|]+?)\s*(?:\|([^}]*?))?\}\}')
CAP_HEADING = re.compile(r'^\*+\s+(.*)$')
CAP_TAGS = re.compile(r'\s+(:(?:[^\s:]+:)+)\s*$')


def CapSplitRest(rest):
    """(prompt, default) of what follows the name in a placeholder."""
    rest = rest or ""
    i = rest.rfind("|=")
    if i >= 0:
        return rest[:i].strip(), rest[i + 2:].strip()
    t = rest.strip()
    if t.startswith("="):
        return "", t[1:].strip()
    return t, ""


def CapLabel(name):
    words = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', name.replace("_", " ").replace("-", " ")).strip()
    return (words[:1].upper() + words[1:].lower()) if words else name


def SnippetEscape(text):
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("}", "\\}")


def SnippetFromTemplate(template, first=1):
    """A sublime snippet for a capture form. Returns (snippet, next field)."""
    out = []
    pos = 0
    fields = {}
    n = first
    for m in CAP_PLACEHOLDER.finditer(template):
        out.append(SnippetEscape(template[pos:m.start()]))
        pos = m.end()
        name = m.group(1).strip()
        key = name.lower()
        if key in fields:
            # Same name twice is one value: mirror the field.
            out.append("$" + str(fields[key]))
            continue
        prompt, default = CapSplitRest(m.group(2))
        fields[key] = n
        if key == "content":
            text = default
        else:
            text = default or prompt or CapLabel(name)
        out.append("${" + str(n) + (":" + SnippetEscape(text) if text else "") + "}")
        n += 1
    out.append(SnippetEscape(template[pos:]))
    return "".join(out), n


def ServerCaptureSnippet(temp):
    typ = (temp.get("Type") or "entry").lower()
    body = temp.get("Template") or ""
    if not body.strip():
        body = "{{CONTENT}}"
    if typ == "entry" and not body.lstrip().startswith("*"):
        rest, _ = SnippetFromTemplate(body, 2)
        return "* ${1:Heading}\n" + rest
    return SnippetFromTemplate(body, 1)[0]


def ParseCaptureEntry(text):
    """(headline, content, tags) of a captured entry."""
    lines = text.replace("\r\n", "\n").split("\n")
    at = None
    for i, line in enumerate(lines):
        if CAP_HEADING.match(line):
            at = i
            break
    if at is None:
        # No heading: the first line is the headline.
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            return ("", "", [])
        at = 0
        head = lines[0].strip()
    else:
        head = CAP_HEADING.match(lines[at]).group(1).strip()
    tags = []
    m = CAP_TAGS.search(head)
    if m:
        tags = [t for t in m.group(1).split(":") if t]
        head = head[:m.start()].rstrip()
    body = lines[:at] + lines[at + 1:]
    content = textwrap.dedent("\n".join(body)).strip("\n")
    return (head, content, tags)


def ServerCaptureArgs(typ, text):
    """Keyword arguments for OrgsClient.capture."""
    if (typ or "entry") == "entry":
        head, content, tags = ParseCaptureEntry(text)
        return {"headline": head, "content": content, "tags": tags}
    return {"content": text.strip("\n")}


def StartServerCapture(view, temp):
    global captureBufferName, lastHeader, captureSeq
    captureBufferName = sets.Get("captureBufferName", captureBufferName)
    window = view.window()
    panel = window.create_output_panel("orgcapture")
    # The panel is shared with local captures and keeps its settings: drop
    # the local capture's, and mark it so focus loss never writes it locally.
    s = panel.settings()
    s.erase('cap_index')
    s.erase('cap_view')
    s.set('orgs_capture_panel', True)
    s.set('orgs_capture', None)
    lastHeader = None
    panel.set_read_only(False)
    panel.run_command("select_all")
    panel.run_command("right_delete")
    s.set('auto_indent', False)
    panel.set_syntax_file('Packages/OrgEverywhere/OrgEverywhere.sublime-syntax')
    window.run_command('show_panel', {'panel': 'output.orgcapture'})
    window.focus_view(panel)
    panel.run_command("insert_snippet", {"contents": ServerCaptureSnippet(temp)})
    panel.set_name(captureBufferName)
    captureSeq += 1
    s.set('orgs_capture_seq', captureSeq)
    s.set('orgs_capture', temp.get("Name"))
    s.set('orgs_capture_type', (temp.get("Type") or "entry").lower())
    s.set('orgs_capture_initial', panel.substr(sublime.Region(0, panel.size())))


captureSeq = 0
CaptureTimeout = getattr(ocl, "Timeout", None)


def IsCaptureTimeout(e):
    """A request that may have reached the server (and written) anyway."""
    if CaptureTimeout is not None:
        return isinstance(e, CaptureTimeout)
    return isinstance(e, ocl.Unavailable) and "timed out" in str(e)


def CaptureWindow(view):
    """The window whose capture panel is view, or None."""
    for w in sublime.windows():
        find = getattr(w, "find_output_panel", None)
        p = find("orgcapture") if find else None
        if p is not None and p.id() == view.id():
            return w
    return None


def SyncCaptureTarget(cl, name):
    """(worker) Save the capture target if it is open and modified. When the
    target cannot be worked out here, save every modified org buffer the
    server tracks."""
    temps = [t for t in cl.capture_templates() if t.get("Name") == name]
    fn = (temps[0].get("CapTarget") or {}).get("Filename") if temps else None
    path = None
    if fn and "{{" not in fn:
        path = cl.local(fn) if os.path.isabs(fn) else cl.find_file(fn)
    if path:
        orgs.sync_path(cl, path)
        return

    def dirty():
        return list({v.file_name() for w in sublime.windows() for v in w.views()
                     if v.file_name() and v.is_dirty() and v.file_name().endswith(".org")})
    names = orgs.run_on_main_and_wait(dirty)
    if not names:
        return
    tracked = cl.files()
    for f in names:
        if any(PathMap.same(f, t) for t in tracked):
            orgs.sync_path(cl, f)


def FinishServerCapture(view):
    """(ui) Send a server capture panel's text to orgs, once."""
    s = view.settings()
    name = s.get('orgs_capture')
    if not name:
        return
    text = view.substr(sublime.Region(0, view.size()))
    if not text.strip() or text == s.get('orgs_capture_initial'):
        # Nothing typed yet: wait for the next time it loses focus.
        return
    typ = s.get('orgs_capture_type') or "entry"
    args = ServerCaptureArgs(typ, text)
    seq = s.get('orgs_capture_seq')
    s.set('orgs_capture', None)

    def current():
        return s.get('orgs_capture_seq') == seq

    def work(cl):
        # The server writes the target file: save it first if it is open.
        SyncCaptureTarget(cl, name)
        return cl.capture(name, **args)

    def done(res):
        if not current():
            sublime.status_message("orgs: captured with " + name)
            return
        if view.substr(sublime.Region(0, view.size())) != text:
            sublime.status_message("orgs: captured with {0}; what was typed after it was sent was NOT captured".format(name))
            return
        sublime.status_message("orgs: captured with " + name)
        view.run_command("select_all")
        view.run_command("right_delete")
        w = CaptureWindow(view)
        if w is not None:
            w.run_command("hide_panel", {"panel": "output.orgcapture"})

    def failed(e):
        if not current():
            orgs.report(e, "capture")
            return
        if IsCaptureTimeout(e):
            # The server may have written it: never send it again unasked.
            again = sublime.ok_cancel_dialog(
                "orgs did not answer in time. The capture may already have been saved.\n\n"
                "Check the capture target first. Send it again?", "Send again")
            if again and current():
                s.set('orgs_capture', name)
                FinishServerCapture(view)
            else:
                sublime.status_message("orgs: capture not sent again; the text is still in the capture panel")
            return
        # Not written: keep the text and try again next time the panel loses focus.
        s.set('orgs_capture', name)
        orgs.report(e, "capture")
    orgs.run(work, done, on_error=failed, what="capture")


r"""
/* SDOC: Reference::Commands::Capture
*** Org: Abort Capture (orgs template)
    Command: =org_capture_abort=  Keys: none

    Throws away the capture in progress in the capture panel instead of
    sending it, and hides the panel. Nothing in the panel is written
    anywhere afterwards. Meant for captures with an orgs server template,
    which are sent when the panel loses focus.
EDOC */
"""

# Throw away an orgs capture in progress instead of sending it.
class OrgCaptureAbortCommand(sublime_plugin.WindowCommand):
    def run(self):
        panel = self.window.find_output_panel("orgcapture")
        if panel is not None:
            s = panel.settings()
            s.set('orgs_capture', None)
            # Nothing is written from this panel any more, locally either.
            s.set('orgs_capture_panel', True)
            s.erase('cap_index')
            s.erase('cap_view')
        self.window.run_command("hide_panel", {"panel": "output.orgcapture"})


r"""
/* SDOC: Reference::Commands::Capture
*** Internal capture commands
    =org_capture_base= and =org_server_move_base= are base classes that
    Sublime also registers as commands; they do nothing useful on their own.
    Bind =org_capture=, =org_refile=, =org_copy= and friends instead.
EDOC */
"""

class OrgCaptureBaseCommand(sublime_plugin.TextCommand):
    def on_done(self, index):
        pass

    def on_done_base_st3(self, index):
        if (index < 0):
            return
        self.templates         = sets.Get("captureTemplates", [])
        self.on_done(index)

    def on_done_base_st4(self, index, modifiers):
        self.on_done_base_st3(index)

    def run(self, edit):
        templates = sets.Get("captureTemplates", [])
        temps = []
        for temp in templates:
            log.debug("TEMPLATE: ", temp)
            temps.append(temp['name'])
        if (int(sublime.version()) >= 4096):
            self.view.window().show_quick_panel(temps, self.on_done_base_st4, -1, -1)
        else:
            self.view.window().show_quick_panel(temps, self.on_done_base_st3, -1, -1)


def IsType(val, template):
    return 'type' in template and template['type'].strip() == val


def GetProp(template, name, defaultVal=None):
    if ('properties' in template):
        props = template['properties']
        if (isinstance(props, dict)):
            if (name in props):
                return props[name]
    return defaultVal


r"""
/* SDOC: Reference::Commands::Capture
*** Org Capture
    Command: =org_capture=  Keys: =ctrl+shift+x=, =alt+o z= (example global keymap); NeoVintageous normal mode: =Z=

    Captures a note, todo or other text without leaving what you are doing.
    A quick panel lists the =captureTemplates= from your settings, then,
    when connected and =orgsCaptureTemplates= is on, the orgs server's
    templates (marked "orgs: ").

    A settings template fills the capture panel at the bottom of the window
    from its =snippet= (a =.sublime-snippet= in =orgsnippets=, built in or in
    =Packages/User/orgsnippets=; =$0= is where the cursor ends up; the
    snippet gets =ORG_INACTIVE_DATE=, =ORG_ACTIVE_DATE=, =ORG_DATE=,
    =ORG_TIME=, =ORG_CLIPBOARD=, =ORG_SELECTION=, =ORG_LINENUM= and
    =ORG_FILENAME=) or its =template= text. Its =target= says where it goes:
    =["file", F]= (end of file), =["file+headline", F, H]= (under heading
    H, created if missing), =["file+olp", F, H1, H2...]= (under an outline
    path), =["file+regexp", F, RE]=, =["file+olp+datetree", F, H1...]=
    (under year / month / day headings, formats from the template's
    =year-format=, =month-format=, =day-format= properties), =["id", ID]=
    (a heading's =CUSTOM_ID=, looked up on the orgs server) or =["clock"]= (under the heading being
    clocked). =F= may use ={refile}= and ={daypage}= (today's day page).
    =type= is =entry=, =item=, =checkitem=, =table-line= or =plain=. With
    ="openas": "direct"= the target file itself is opened at the spot;
    otherwise (="panel"=) the panel's text is written into the target each
    time the panel loses focus, replacing what that capture wrote before.

    An orgs template is a form: each ~{{name}}~, ~{{name|prompt}}~,
    ~{{name|=default}}~ placeholder becomes a tab stop in the panel. The
    text is sent to the server once, when the panel loses focus with
    something typed; the server writes it to the template's target (that
    file is saved first if it is open and modified). If the server does not
    answer in time you are asked before it is sent again. Org: Abort Capture
    throws it away.

    Settings: =captureTemplates=, =refile=, =captureBufferName=,
    =captureWriteFormat=, =orgsCaptureTemplates=.
EDOC */
"""

# Capture some text into our refile org file
class OrgCaptureCommand(OrgCaptureBaseCommand):
    # The picker offers the captureTemplates setting first, then the orgs
    # server's templates (marked "orgs: ").
    def run(self, edit):
        local = sets.Get("captureTemplates", []) or []
        if orgs.connected() and sets.Get("orgsCaptureTemplates", True):
            def failed(e):
                log.info("orgs: capture templates: %s", e)
                self.show_templates(local, [])
            orgs.run(lambda c: c.capture_templates(), lambda st: self.show_templates(local, st or []),
                     on_error=failed, what="capture templates")
        else:
            self.show_templates(local, [])

    def show_templates(self, local, server):
        self.localCount = len(local)
        self.serverTemplates = server
        names = [t.get('name', '?') for t in local] + ["orgs: " + t.get("Name", "") for t in server]
        if not names:
            sublime.status_message("No capture templates (captureTemplates setting, or the orgs server)")
            return
        ShowPanel(self.view.window(), names, self.on_template)

    def on_template(self, index):
        if index < self.localCount:
            self.on_done_base_st3(index)
        else:
            StartServerCapture(self.view, self.serverTemplates[index - self.localCount])

    def insert_template(self, template, panel):
        # template          = templates[index]['template']
        startPos = -1
        template, startPos = templateEngine.ExpandTemplate(self.view, template)

        panel.run_command("insert", {"characters": template})
        if (startPos >= 0):
            startPos = sublime.Region(startPos)
        else:
            startPos = panel.sel()[0]
        return startPos

    def cleanup_capture_panel(self):
        global captureBufferName
        if (not self.openas):
            self.panel.set_syntax_file('Packages/OrgEverywhere/OrgEverywhere.sublime-syntax')
            self.panel.set_name(captureBufferName)

    # In OpenAs mode we insert some pre-heading stars
    # Before inserting the snippet. This SHOULD get our
    # heading where it needs to be?
    def on_added_stars(self):
        self.pt = self.panel.text_point(self.insertRow, 0)
        linev = self.panel.line(self.pt)
        linetxt = self.panel.substr(linev)
        self.panel.sel().clear()
        self.panel.sel().add(linev.begin() + len(linetxt.rstrip()))
        self.insert_snippet(self.index)

    def find_end_of_thing(self, panel, insertAt, itemre):
        self.insertRow = insertAt.local_end_row
        self.pt = panel.text_point(insertAt.local_end_row, 0)
        # item_line_regex   = re.compile(r'^(\s*)([-+])\s*[^\[]')
        have = False
        itemtype = '-'
        preprefix = " " * (insertAt.level + 1)
        for row in range(insertAt.start_row, insertAt.local_end_row + 1):
            pt = panel.text_point(row, 0)
            line  = panel.substr(panel.line(pt))
            m = itemre.search(line)
            if (m):
                preprefix = m.group(1)
                itemtype = m.group(2)
                have = True
            elif (have):
                self.insertRow = row
                self.pt = pt
                break
        return (preprefix, itemtype)

    def on_panel_ready(self, index, openas, panel, cnt=0):
        self.panel = panel
        global captureBufferName
        captureBufferName = sets.Get("captureBufferName", captureBufferName)
        window = self.view.window()
        template = self.templates[index]
        target, capturePath, captureFile, at, toinsert = GetCapturePath(self.view, template)
        if (panel.is_loading()):
            sublime.set_timeout_async(lambda: self.on_panel_ready(index, openas, panel), 100)
            return

        if (toinsert != "" and cnt == 0):
            pt = panel.text_point(at, 0)
            linev = panel.line(pt)
            linetxt = panel.substr(linev)
            if ((linetxt and not linetxt.strip() == "") or at == 0 or panel.lastRow() == at):
                toinsert = "\n" + toinsert
                pt = linev.end()
            else:
                pt = linev.begin()
            panel.Insert(pt, toinsert, evt.Make(lambda: self.on_panel_ready(index, openas, panel, cnt + 1)))
            return

        startPos = -1
        # Try to store the capture index
        panel.settings().set('orgs_capture', None)
        panel.settings().erase('orgs_capture_panel')
        panel.settings().set('cap_index', index)
        panel.settings().set('cap_view', self.view.id())
        self.openas = openas
        if ((IsType('item', template) or IsType('checkitem', template) or IsType('plain', template) or IsType('table-line', template)) and 'snippet' not in template):
            template['snippet'] = 'plain_template'
            if ('template' in template):
                del template['template']

        if ('template' in template):
            startPos = self.insert_template(template['template'], panel)
            window.run_command('show_panel', args={'panel': 'output.orgcapture'})
            panel.sel().clear()
            panel.sel().add(startPos)
            window.focus_view(panel)
            self.cleanup_capture_panel()
        elif ('snippet' in template):
            self.level = 0
            self.pt = None
            prefix = ""
            preprefix = ""
            itemtype = '-'
            if (self.openas):
                insertAt = captureFile.At(at)
                if (IsType('plain', template)):
                    self.pt = panel.text_point(insertAt.local_end_row, 0)
                    self.insertRow = insertAt.local_end_row
                elif (IsType('checkitem', template)):
                    preprefix, itemtype = self.find_end_of_thing(panel, insertAt, re.compile(r'^(\s*)([-+])\s*(\[[xX\- ]\])\s+'))
                elif (IsType('item', template)):
                    preprefix, itemtype = self.find_end_of_thing(panel, insertAt, re.compile(r'^(\s*)([-+])\s*[^\[]'))
                elif (IsType('table-line', template)):
                    preprefix, itemtype = self.find_end_of_thing(panel, insertAt, re.compile(r'^(\s*)([|])'))
                else:
                    self.pt = panel.text_point(insertAt.end_row, 0)
                    self.insertRow = insertAt.end_row
                linev = panel.line(self.pt)
                linetxt = panel.substr(linev)
                if ((linetxt and not linetxt.strip() == "") or at == 0 or (insertAt and panel.lastRow() == insertAt.end_row)):
                    prefix = "\n"
                    self.pt = linev.end()
                    self.insertRow += 1
                else:
                    self.pt = linev.begin()
                panel.sel().clear()
                panel.sel().add(self.pt)
                self.level = insertAt.level
            else:
                window.run_command('show_panel', args={'panel': 'output.orgcapture'})
            if (self.openas and self.level > 0):
                self.index = index
                self.panel = panel
                if (IsType('plain', template)):
                    self.panel.Insert(self.pt, prefix + (" " * (self.level + 1)), evt.Make(self.on_added_stars))
                elif (IsType('item', template)):
                    self.panel.Insert(self.pt, prefix + preprefix + itemtype + " ", evt.Make(self.on_added_stars))
                elif (IsType('checkitem', template)):
                    self.panel.Insert(self.pt, prefix + preprefix + itemtype + " [ ] ", evt.Make(self.on_added_stars))
                elif (IsType('table-line', template)):
                    self.panel.Insert(self.pt, prefix + preprefix + itemtype, evt.Make(self.on_added_stars))
                else:
                    self.panel.Insert(self.pt, prefix + ("*" * self.level), evt.Make(self.on_added_stars))
            else:
                if (IsType('plain', template)):
                    self.panel.Insert(self.pt, prefix, evt.Make(self.on_added_stars))
                elif (prefix != ""):
                    self.index = index
                    self.panel = panel
                    self.panel.Insert(self.pt, prefix, evt.Make(self.on_added_stars))
                else:
                    self.insert_snippet(index)

    def insert_snippet(self, index):
        window = self.view.window()
        template = self.templates[index]
        ai = sublime.active_window().active_view().settings().get('auto_indent')
        self.panel.settings().set('auto_indent', False)
        snippet = template['snippet']
        snipName = ext.find_extension_file('orgsnippets', snippet, '.sublime-snippet')
        window.focus_view(self.panel)
        # panel.meta_info("shellVariables", 0)[0]['TM_EMAIL'] = "Trying to set email value"
        self.panel.run_command('_enter_insert_mode', {"count": 1, "mode": "mode_internal_normal"})
        now = datetime.datetime.now()
        inow = orgdate.OrgDate.format_date(now, False)
        anow = orgdate.OrgDate.format_date(now, True)
        # "Packages/OrgEverywhere/orgsnippets/"+snippet+".sublime-snippet"
        # OTHER VARIABLES:
        # TM_FULLNAME - Users full name
        # TM_FILENAME - File name of the file being edited
        # TM_CURRENT_WORD - Word under cursor when snippet was triggered
        # TM_SELECTED_TEXT - Selected text when snippet was triggered
        # TM_CURRENT_LINE - Line of snippet when snippet was triggered
        self.panel.run_command("insert_snippet", {
            "name": snipName,
            "ORG_INACTIVE_DATE": inow,
            "ORG_ACTIVE_DATE":   anow,
            "ORG_DATE":          str(datetime.date.today()),
            "ORG_TIME":          datetime.datetime.now().strftime("%H:%M:%S"),
            "ORG_CLIPBOARD":     sublime.get_clipboard(),
            "ORG_SELECTION":     self.view.substr(self.view.sel()[0]),
            "ORG_LINENUM":       str(self.view.curRow()),
            "ORG_FILENAME":      self.view.file_name()
        })
        sublime.active_window().active_view().settings().set('auto_indent', ai)
        self.cleanup_capture_panel()

    def on_done_st4(self, index, modifiers):
        self.on_done(index)

    def on_done(self, index):
        if (index < 0):
            return
        global captureBufferName
        captureBufferName = sets.Get("captureBufferName", captureBufferName)
        window = self.view.window()
        template = self.templates[index]
        target, capturePath, captureFile, at, toinsert = GetCapturePath(self.view, template)
        openas = False
        if ('openas' in template and 'direct' == template['openas']):
            panel = window.open_file(capturePath)
            openas = True
        else:
            panel = window.create_output_panel("orgcapture")
        self.on_panel_ready(index, openas, panel)
