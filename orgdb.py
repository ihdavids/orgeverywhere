import sublime
import sublime_plugin
import datetime
import re
import os
import OrgEverywhere.orgparse.loader as loader
import OrgEverywhere.orgutil.util as util
import logging
import traceback
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgs.session as orgs
from OrgEverywhere.orgs.paths import PathMap

log = logging.getLogger(__name__)
headingRe = re.compile("^([*]+) (.+)")


class FileInfo:
    def __init__(self, file, parsed, orgPaths):
        self.isOrgDir = False
        self.org      = parsed
        self.filename = file
        self.key      = file.lower() if file else None
        self.change_count = 0
        self.org.setFile(self)
        displayFn = self.key
        oldLen = len(displayFn) if displayFn else 0
        if (not displayFn):
            self.displayFn = "<BUFFER>"
            self.displayName = "<BUFFER>"
            return
        for prefix in orgPaths:
            displayFn = displayFn.replace(prefix, "")
            displayFn = displayFn.replace(prefix.lower(), "")
        self.isOrgDir = displayFn != self.key
        # Max Slashes!
        # No prefixes. We should count the slashes and truncate
        # if there are to many.
        maxSlash = 3
        if (oldLen == len(displayFn)):
            scount = displayFn.count('/')
            if (scount > maxSlash):
                llist = displayFn.split('/')
                displayFn = '/'.join(llist[-maxSlash:])
            scount = displayFn.count('\\')
            if (scount > maxSlash):
                llist = displayFn.split('\\')
                displayFn = '\\'.join(llist[-maxSlash:])

        if (len(displayFn) > 1 and (displayFn[0] == '\\' or displayFn[0] == '/')):
            displayFn = displayFn[1:]
        self.displayName = displayFn

    def GetFilename(self):
        return self.filename

    def Root(self):
        return self.org[0]

    def RootInView(self, view, db):
        self.ReloadIfChanged(view, db)
        return self.Root()

    def LoadS(self, view):
        bufferContents = view.substr(sublime.Region(0, view.size()))
        self.org = loader.loads(bufferContents, view.file_name() if view.file_name() else "<string>")
        self.org.setFile(self)
        # Keep track of last change count.
        self.change_count = view.change_count()

    def Reload(self):
        self.org = loader.load(self.filename)
        self.org.setFile(self)

    def ResetChangeCount(self):
        self.change_count = 0

    def HeadingCount(self):
        return len(self.org) - 1

    def Save(self):
        f = open(self.filename, "w+", encoding="utf-8")
        for item in self.org:
            f.write(str(item))
        f.close()

    def ReloadIfChanged(self, view, db):
        if (self.HasChanged(view)):
            self.LoadS(view)

    def HasChanged(self, view):
        return self.change_count < view.change_count()

    def At(self, row):
        return self.org.at(row)

    def AtPt(self, view, pt, db):
        self.ReloadIfChanged(view, db)
        row, col = view.rowcol(pt)
        return self.org.at(row)

    def AtRegion(self, view, reg):
        row, col = view.rowcol(reg.begin())
        return self.org.at(row)

    def AtInView(self, view, db):
        self.ReloadIfChanged(view, db)
        (row, col) = view.curRowCol()
        return self.org.at(row)

    def AgendaFilenameTag(self):
        return os.path.splitext(os.path.basename(self.filename))[0] + ":"

    def FindOrCreateNode(self, heading):
        for n in self.org[1:]:
            if (heading == n.full_heading):
                return n

        for n in self.org[1:]:
            if (heading == n.heading):
                return n

        # Okay got here and didn't find the node, have to make it.
        m = headingRe.search(heading)
        if (m is None):
            log.error("FindorCreateNode: failed to parse heading: " + heading)
            return None
        levelGroup = m.group(1)
        level = len(levelGroup)
        cur         = self.org[0]
        parentLevel = level - 1
        while (cur.level < parentLevel):
            if (cur.num_children == 0):
                tree = loader.loads("* " + str(datetime.datetime.now()))
                cur.insert_child(tree[1])
            cur = cur.get_last_child()
        if (heading is None or heading.isspace() or heading.strip() == ""):
            return cur
        else:
            tree = loader.loads(heading)
            cur.insert_child(tree[1])
            cur = cur.get_last_child()
        return cur


def _OpenView(filename):
    """(ui) A view on filename in any window, or None."""
    if not filename:
        return None
    for w in sublime.windows():
        v = w.find_open_file(filename)
        if v is not None:
            return v
    return None


_KINDS = {"ids": "ID", "customids": "CUSTOM_ID", None: None}


# The org database is the orgs server: which files exist, ids, custom ids,
# tags, backlinks and headings all come from there. What lives here:
#
# - files: the parse of each OPEN buffer (unsaved edits and all), keyed by
#   its file name (or buffer id). FindInfo(view), At, AtInView... use it.
#   Nothing else is cached: ParseFile reads any other file fresh each time.
# - caches of server answers (ids, tags, backlinks, the file list), dropped
#   when the server reports a change.
class OrgDb:
    def __init__(self):
        self.files    = {}
        self.orgPaths = []
        self.tags     = set()
        self._serverFiles = None   # local paths of the files the server tracks
        self._idCache     = None   # {"ids": {id: (file, row, headline)}, "customids": {...}}
        self._serverTags  = None
        self._backlinks   = {}

    # --- Server answers ---------------------------------------------------

    def ServerFiles(self):
        """Local paths of every org file the server knows about."""
        if self._serverFiles is None:
            files = orgs.call(lambda c: c.files(), None)
            if files is not None:
                self._serverFiles = files
                self._RefreshOrgPaths()
        return self._serverFiles or []

    def OrgDirs(self):
        """The server's org directories (local paths); the old orgDirs
        setting is only a fallback for when no server is connected."""
        if not self.orgPaths:
            self.ServerFiles()
        if self.orgPaths:
            return list(self.orgPaths)
        dirs = sets.Get("orgDirs", [])
        if isinstance(dirs, str):
            dirs = [dirs]
        return [os.path.expanduser(d) for d in (dirs or [])]

    def _RefreshOrgPaths(self):
        dirs = orgs.call(lambda c: c.dirs(), None)
        if dirs:
            self.orgPaths = [d.replace('\\', '/') for d in dirs]

    def LocateFile(self, name, client=None):
        """Local path of the org file called name (a file name or part of a
        path), asking the server; open buffers as a fallback. None if unknown."""
        if client is not None:
            found = client.find_file(name)
            files = client.files() if not found else []
        else:
            found = orgs.call(lambda c: c.find_file(name))
            files = self.ServerFiles() if not found else []
        if found:
            return found
        for fn in files:
            if name in fn:
                return fn
        for f in list(self.files.values()):
            if f.filename and name in f.filename:
                return f.filename
        return None

    def OnServerEvent(self, ev):
        kind = ev.get("kind")
        if kind == "reload":
            fn = ev.get("file")
            if fn and self._serverFiles is not None and not any(PathMap.same(fn, f) for f in self._serverFiles):
                self._serverFiles = None
            self._idCache = None
            self._serverTags = None
            self._backlinks = {}

    def OnConnectionChanged(self, state):
        self.ForgetServerAnswers()
        if state == orgs.CONNECTED:
            sublime.set_timeout_async(self.ServerFiles, 0)

    def ForgetServerAnswers(self):
        self._serverFiles = None
        self._idCache = None
        self._serverTags = None
        self._backlinks = {}

    # --- Ids --------------------------------------------------------------

    def _Ids(self):
        if self._idCache is None:
            cache = {"ids": {}, "customids": {}}

            def build(c):
                for prop, key in (("ID", "ids"), ("CUSTOM_ID", "customids")):
                    for t in c.search('HasProperty("{0}")'.format(prop)):
                        v = _Prop(t, prop)
                        if v and v not in cache[key]:
                            cache[key][v] = (t.get("Filename"), t.get("LineNum", 0), t.get("Headline", ""))
                return cache
            if orgs.call(build) is None:
                return {"ids": {}, "customids": {}}
            self._idCache = cache
        return self._idCache

    @property
    def ids(self):
        return sorted(self._Ids()["ids"].keys())

    @property
    def customids(self):
        return sorted(self._Ids()["customids"].keys())

    def IdEntries(self, which="ids"):
        """[(id, filename, row, headline)] for quick panels."""
        d = self._Ids()[which]
        return [(k, v[0], v[1], v[2]) for k, v in sorted(d.items())]

    def GetIds(self):
        return self.ids

    @staticmethod
    def _IdRow(fi, id, kind):
        env = fi.org.env
        tables = [env.ids] if kind == "ids" else [env.customids] if kind == "customids" else [env.ids, env.customids]
        for t in tables:
            if id in t:
                return t[id][1]
        return None

    def BufferRow(self, filename, id, kind=None):
        """The row of the heading carrying id in filename's open buffer, None
        when filename is not open or its buffer does not have the id."""
        def look():
            view = _OpenView(filename)
            fi = self.FindInfo(view) if view is not None else None
            return self._IdRow(fi, id, kind) if fi is not None else None
        return orgs.run_on_main_and_wait(look)

    def LocateInBuffers(self, id, kind=None):
        """(filename, row) of id in an open buffer (ids the server has not
        seen yet), (None, None) if none has it."""
        def look():
            for w in sublime.windows():
                for view in w.views():
                    if not util.isPotentialOrgFile(view.file_name()):
                        continue
                    fi = self.FindInfo(view)
                    row = self._IdRow(fi, id, kind) if fi is not None else None
                    if row is not None:
                        return (view.file_name(), row)
            return (None, None)
        return orgs.run_on_main_and_wait(look)

    def Locate(self, id, kind=None, client=None):
        """(filename, row) of the heading whose ID (kind "ids"), CUSTOM_ID
        ("customids") or either (None) is id; (None, None) if not found.
        Asks the server (with client when given, on a worker; else
        synchronously). An open buffer's row wins: it may have moved."""
        prop = _KINDS.get(kind)
        if client is not None:
            t = client.by_id(id)
        else:
            t = orgs.call(lambda c: c.by_id(id))
        if t and (prop is None or _Prop(t, prop) == id):
            fn = t.get("Filename")
            row = self.BufferRow(fn, id, kind)
            return (fn, t.get("LineNum", 0) if row is None else row)
        return self.LocateInBuffers(id, kind)

    def LocateById(self, id, client=None):
        return self.Locate(id, "ids", client)

    def LocateByCustomId(self, id, client=None):
        return self.Locate(id, "customids", client)

    def LocateByAnyId(self, id, client=None):
        return self.Locate(id, None, client)

    def NodeById(self, id, kind=None, client=None):
        """The parsed heading carrying id: from its open buffer, else from a
        one off parse of its file. None if not found."""
        fn, row = self.Locate(id, kind, client)
        if fn is None:
            return None
        return self.ParseAt(fn, row)

    def JumpToId(self, id, kind=None, window=None):
        """Open the heading carrying id. Asks the server off the ui thread."""
        window = window or sublime.active_window()

        def done(found):
            fn, row = found or (None, None)
            if fn is None:
                sublime.status_message("Could not find a heading with id " + id)
                return
            window.open_file("{0}:{1}".format(fn, row + 1), sublime.ENCODED_POSITION)
        if not orgs.connected():
            done(self.LocateInBuffers(id, kind))
            return

        def failed(e):
            log.info("orgs id lookup failed: %s", e)
            done(self.LocateInBuffers(id, kind))
        orgs.run(lambda c: self.Locate(id, kind, c), done, on_error=failed, what="id")

    def JumpToCustomId(self, id, window=None):
        self.JumpToId(id, "customids", window)

    def JumpToAnyId(self, id, window=None):
        self.JumpToId(id, None, window)

    # --- Tags and backlinks ---------------------------------------------

    def OnTags(self, tags):
        for i in tags:
            self.tags.add(i)

    def AllTags(self):
        if self._serverTags is None:
            t = orgs.call(lambda c: c.all_tags(), None)
            if t is not None:
                self._serverTags = set(t)
        return sorted(set(self.tags) | (self._serverTags or set()))

    def GetBacklinks(self, view):
        """Links into this file from elsewhere, as orgs reports them:
        [{"From": {"Filename", "Line", "Headline", "Olp"}, "To": {...},
          "Raw", "Desc", "Kind", "Broken"}]"""
        fn = view.file_name() if util.isView(view) else view
        if not fn:
            return None
        key = fn.lower()
        if key not in self._backlinks:
            res = orgs.call(lambda c: c.links(fn), None)
            if res is None:
                return None
            self._backlinks[key] = res.get("In") or []
        return self._backlinks[key]

    # --- Open buffers -------------------------------------------------------

    def _LoadView(self, view):
        if not (util.isPotentialOrgFileOrBuffer(view)):
            log.debug("Not an org buffer, not parsing: " + str(view.file_name()))
            return None
        bufferContents = view.substr(sublime.Region(0, view.size()))
        filename = view.file_name()
        file = FileInfo(filename if filename else util.getKey(view), loader.loads(bufferContents, filename or "<string>"), self.orgPaths)
        file.change_count = view.change_count()
        self.files[file.key] = file
        return file

    def Remove(self, fileOrView):
        key = util.getKey(fileOrView) if fileOrView else None
        if key and key.lower() in self.files:
            del self.files[key.lower()]

    def Reload(self, view):
        """Re-read an open buffer (after a save, say)."""
        if not util.isView(view):
            return self.FindInfo(view)
        fi = self.files.get(util.getKey(view).lower())
        if fi is None:
            return self._LoadView(view)
        fi.LoadS(view)
        return fi

    def GetIndentForRegion(self, view, region):
        node = self.AtRegion(view, region)
        return node.level + 1

    def FilenameFromFileOrView(self, fileOrView):
        if util.isView(fileOrView):
            return fileOrView.file_name()
        return fileOrView

    def RebuildDb(self):
        """Forget the server answers and closed buffers; nothing is scanned."""
        if (evt.Get().listeners('tagsfound')):
            evt.Get().clear_listeners('tagsfound')
        evt.Get().on("tagsfound", self.OnTags)
        self.files = {}
        self.ForgetServerAnswers()
        if orgs.connected():
            sublime.set_timeout_async(self.ServerFiles, 0)

    def FindInfo(self, fileOrView):
        """A view: its buffer's parse, kept up to date. A path: ParseFile."""
        if (not fileOrView):
            return None
        if not util.isView(fileOrView):
            return self.ParseFile(fileOrView)
        try:
            f = self.files.get(util.getKey(fileOrView).lower())
            if f is None:
                return self._LoadView(fileOrView)
            f.ReloadIfChanged(fileOrView, self)
            return f
        except Exception:
            try:
                return self._LoadView(fileOrView)
            except Exception:
                log.warning("FAILED PARSING: \n  %s", traceback.format_exc())
                return None

    # --- Other files: parsed when asked, never kept --------------------------

    def ParseFile(self, filename, copy=False):
        """FileInfo for filename. Open in a view: that buffer's parse (copy:
        a fresh parse of the buffer text, for callers that change the tree).
        Otherwise a one off parse of the file on disk, not cached."""
        if not filename:
            return None

        def fromView():
            view = _OpenView(filename)
            if view is None:
                return None
            if not copy:
                return self.FindInfo(view)
            text = view.substr(sublime.Region(0, view.size()))
            return FileInfo(view.file_name(), loader.loads(text, view.file_name()), self.orgPaths)
        fi = orgs.run_on_main_and_wait(fromView)
        if fi is not None:
            return fi
        if not util.isPotentialOrgFile(filename) or not os.path.isfile(filename):
            return None
        try:
            return FileInfo(filename, loader.load(filename), self.orgPaths)
        except Exception:
            log.warning("FAILED PARSING %s: \n  %s", filename, traceback.format_exc())
            return None

    def ParseAt(self, filename, row):
        """The node at row of filename (see ParseFile)."""
        fi = self.ParseFile(filename)
        return fi.At(row) if fi is not None else None

    def Find(self, fileOrView):
        n = self.FindInfo(fileOrView)
        if (n is not None):
            return n.org
        return None

    def At(self, fileOrView, line):
        x = self.Find(fileOrView)
        if (x is not None):
            return x.at(line)
        return None

    def AtInView(self, view):
        (row, col) = view.curRowCol()
        return self.At(view, row)

    def AtPt(self, view, pt):
        file = self.FindInfo(view)
        return file.AtPt(view, pt, self)

    def RootInView(self, view):
        file = self.FindInfo(view)
        if file:
            return file.RootInView(view, self)
        return None

    def AtRegion(self, view, reg):
        file = self.FindInfo(view)
        return file.AtRegion(view, reg)

    def NodeAtIndex(self, fileOrView, index):
        return self.Find(fileOrView).node_at(index + 1)

    def Headings(self, view):
        f = self.Find(view)
        headings = []
        if (f is not None):
            for n in f[1:]:
                headings.append((". " * (n.level)) + n.heading)
        return headings

    # Try to find a node by filename and locator
    def FindNode(self, filename, locator):
        file = self.ParseFile(filename)
        if (not file):
            return None

        # Basic locator search through the headings
        headings = locator.split(":")
        cur = file.org[0]
        for index in range(len(headings)):
            heading = headings[index]
            for n in cur.children:
                if (n.heading == heading):
                    cur = n
                    break
            # Did not find this level of heading darn
            if (not cur or cur.is_root() or cur.heading != heading):
                break
        heading = headings[len(headings)-1]
        if (not cur or cur.is_root() or cur.heading == heading):
            return cur

        parent = headings[-2] if len(headings) > 1 else None
        bestMatch = None

        # fuzzy search, heading must match (hopefully)
        for n in file.org[1:]:
            if (n.heading == heading):
                bestMatch = n
                if (parent and n.parent and n.parent.heading == parent):
                    return n
        return bestMatch


def _Prop(todo, name):
    """A property of a server heading. Org property names are case
    insensitive; the server keeps them as written (:custom_id: and friends)."""
    name = name.upper()
    for k, v in (todo.get("Props") or {}).items():
        if k.upper() == name:
            return v
    return None


# EXPORTED ORGDB
orgDb = OrgDb()


def Get():
    global orgDb
    return orgDb


r"""
/* SDOC: Reference::Commands::Misc
*** Org Rebuild Db
    Command: =org_rebuild_db=  Keys: none

    Forgets what OrgEverywhere has cached: the parses of buffers and closed
    files, and the orgs server's answers (file list, ids, tags), which are
    asked for again. Nothing is scanned locally: the orgs server is the
    database of which org files exist. Connecting or logging in does this by
    itself.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Misc
*** orgDirs (only without a server)
    Default: not set

    The orgs server's =orgDirs= decide which files are org files;
    OrgEverywhere no longer scans folders itself (=orgFiles=,
    =orgExcludeDirs= and =orgExcludeFiles= are not read at all). =orgDirs=
    is only used when no server is connected, as the org directories
    whose =images= folders inline images and exports look in. =~= is
    expanded; a single string is accepted.
EDOC */
"""

# Forget what the server told us (ids, tags, files) and ask again.
class OrgRebuildDbCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        Get().RebuildDb()


r"""
/* SDOC: Reference::Commands::Misc
*** Org Reload File
    Command: =org_reload_file=  Keys: none

    Re-parses the current buffer. Normally not needed: a buffer is
    re-parsed when it changes or is saved.
EDOC */
"""

# Just reload the current file.
class OrgReloadFileCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        file = Get().FindInfo(self.view)
        if (file):
            file.LoadS(self.view)
        else:
            log.debug("FAILED TO FIND FILE INFO?")


class _JumpToIdBase(sublime_plugin.TextCommand):
    which = "ids"

    def run(self, edit):
        def done(entries):
            self.entries = entries
            if not entries:
                sublime.status_message("No {0} found (is the orgs server connected?)".format(self.which))
                return
            items = [[e[0], "{0} - {1}".format(e[3], os.path.basename(e[1] or ""))] for e in entries]
            self.view.window().show_quick_panel(items, self.on_done)
        orgs.run(lambda c: Get().IdEntries(self.which), done, what="ids")

    def on_done(self, index):
        if (index < 0 or index >= len(self.entries)):
            return
        id, fn, row, headline = self.entries[index]
        # The buffer may have moved the heading since the server read it.
        brow = Get().BufferRow(fn, id, self.which)
        if brow is not None:
            row = brow
        self.view.window().open_file("{0}:{1}".format(fn, row + 1), sublime.ENCODED_POSITION)


r"""
/* SDOC: Reference::Commands::Links
*** Org Jump To CustomId
    Command: =org_jump_to_custom_id=  Keys: none

    Lists every =CUSTOM_ID= the orgs server knows (id, heading and file) and
    opens the one you pick; when the file is open the row comes from the
    buffer, which may have moved the heading since the server read it.
    Needs the orgs server.
EDOC */
"""

class OrgJumpToCustomIdCommand(_JumpToIdBase):
    which = "customids"


r"""
/* SDOC: Reference::Commands::Links
*** Jump to an ID
    Command: =org_jump_to_id=  Keys: none  (no palette entry)

    As Org Jump To CustomId, for =ID= properties. Needs the orgs server.
EDOC */
"""

class OrgJumpToIdCommand(_JumpToIdBase):
    which = "ids"


r"""
/* SDOC: Reference::Commands::Editing
*** Org Jump To Today
    Command: =org_jump_to_today=  Keys: none

    Opens the heading carrying the today marker (=orgsTodayMarker=): Org
    Jump To Marker for that marker, falling back to a =:CUSTOM_ID: TODAY=
    heading.
EDOC */
"""

class OrgJumpToTodayCommand(sublime_plugin.TextCommand):
    # Today is a server marker now (Org: Set Today); the marker command
    # falls back to the old CUSTOM_ID TODAY heading.
    def run(self, edit):
        self.view.window().run_command("org_jump_to_marker", {"name": sets.Get("orgsTodayMarker", "TODAY")})
