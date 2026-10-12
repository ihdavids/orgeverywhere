import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import OrgEverywhere.orgparse.node as node
from   OrgEverywhere.orgparse.sublimenode import * 
import OrgEverywhere.orgutil.util as util
import OrgEverywhere.orgutil.navigation as nav
import OrgEverywhere.orgutil.template as templateEngine
import logging
import sys
import traceback 
import OrgEverywhere.orgfolding as folding
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgcapture as capture
import OrgEverywhere.orglinks as links
import OrgEverywhere.orgclocking as clocking
import OrgEverywhere.orgnotifications as notice
import OrgEverywhere.orgdatepicker as datepicker
import OrgEverywhere.pymitter as evt
import OrgEverywhere.packagecon as pkgcon
import OrgEverywhere.orgs.session as orgs

log = None

def InstallIfNeeded(pkg, name):
    log.debug("Checking for " + name)
    if(not pkgcon.IsInstalled(pkg)):
        log.debug("Installing " + name)
        pkgcon.Install(pkg)

# Lets get our windows in sync now that we are loaded.
def sync_up_on_loaded():
    window = sublime.active_window()
    window.run_command("org_on_load_sync_up", {})
    notice.Setup()
    datepicker.SetupMouse()
    # Install required packages to operate org everywhere
    InstallIfNeeded(pkgcon.TABLE_PACKAGE, "Table Editor")
    InstallIfNeeded(pkgcon.PS_PACKAGE, "Powershell")

# This is where we can start to load our DB!
def plugin_loaded():
    evt.Get().off("tagsfound",db.Get().OnTags)
    sets.setup_user_settings()
    # Load our settings file.
    # We probably need a command to reload it
    # When it is modified, and probably to reload
    # the DB automatically when your orgs change.
    sets.Load()
    clocking.Load()

    # To enable debug logging, set the env var to a non-blank value.
    # This is the same pattern that neovintageous uses and I think
    # it is a reasonably decent mechanism
    _DEBUG = bool(os.getenv('SUBLIME_ORGEVERYWHERE_DEBUG'))
    _DEBUG = _DEBUG or sets.Get("enableDebugLogging",False)
    #_DEBUG = True
    if _DEBUG:
        logger = logging.getLogger('OrgEverywhere')
        logger.propagate = 0
        if not logger.hasHandlers():
            logger.setLevel(logging.DEBUG)
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(logging.Formatter(
                'Org: %(levelname)-7s [%(filename)15s:%(lineno)4d] %(message)s'
            ))
            logger.addHandler(stream_handler)
            logger.debug('debug logger initialized')
    global log
    log = logging.getLogger(__name__)
    # The orgs server is the database: find it, log in, and let it tell the
    # local parse cache when files change.
    orgs.on_event(db.Get().OnServerEvent)
    orgs.on_state(db.Get().OnConnectionChanged)
    db.Get().RebuildDb()
    orgs.start()
    #window = sublime.active_window()
    #if window is None:
    sublime.set_timeout_async(lambda: sync_up_on_loaded(), 1000)
    log.debug("DONE INITIALIZING ORG CAPTURE")

# This is called when our plugin unloads!
def plugin_unloaded():
    orgs.off_event(db.Get().OnServerEvent)
    orgs.off_state(db.Get().OnConnectionChanged)
    orgs.stop()
    links.onShutdown()
    if(notice):
        notice.Get().stop()

def onLoad(view):
    if(view and view.file_name() and util.isPotentialOrgFile(view.file_name())):
        folding.onLoad(view)
        links.onLoad(view)

RE_TABLE_MATCH = re.compile(r"^\s*\|")
# Core events to dispatch to each of our subsystems.
# This simple direct call is sufficient for our needs
class OrgCore(sublime_plugin.EventListener):

    def on_load(self, view):
        # If this is an org file we need to let the
        # folding system and others get a crack at this
        # file. OnLoad will potentially pre-fold the file
        # or show images in the file.
        onLoad(view)

    def on_post_window_command(self, window, cmd, args):
        #print("****** CMD: " + cmd)
        capture.onPostWindowCommand(window,cmd,args)

    def on_post_save(self, view):
        if(util.isPotentialOrgFile(view.file_name())):
            db.Get().Reload(view)

    def on_close(self, view):
        # The db only keeps parses of open buffers.
        fn = view.file_name()
        if fn and any(w.find_open_file(fn) for w in sublime.windows()):
            return
        db.Get().Remove(view)

    def on_deactivated(self, view):
        capture.onDeactivated(view)

    def on_activated(self, view):
        if(util.isPotentialOrgFile(view.file_name())):
            folding.onActivated(view)
            orgs.update_status(view)

    def ShouldLocalFold(self, view):
        # FindInfo(view) re-reads the buffer when it changed.
        db.Get().FindInfo(view)
        return folding.ShouldFoldLocalCycle(view)

    def ShouldGlobalFold(self, view):
        file = db.Get().FindInfo(view)
        return (not self.ShouldLocalFold(view)) and (file != None and type(file.AtInView(view,db.Get())) is node.OrgRootNode)

    def ShouldFoldLinks(self, view):
        return (not self.ShouldGlobalFold(view)) and folding.am_in_link(view)

    def ShouldTableTab(self,view):
        cur = view.sel()[0]
        l = view.line(cur.begin())
        line = view.substr(l)
        return RE_TABLE_MATCH.search(line)
    
    def ShouldFoldBlock(self,view):
        cur = view.sel()[0]
        names = view.scope_name(cur.begin())
        return 'fence.dynamicblock' in names

    def IsOrgSyntax(self,view):
        cur = view.sel()[0]
        names = view.scope_name(cur.begin())
        return 'text.orgmode' in names

    def ShouldFoldCheckbox(self, view):
        return (not self.ShouldGlobalFold(view)) and folding.ShouldFoldCheckbox(view)

    def on_query_context(self, view, key, operator, operand, match_all):
        # A key wants to be inserted, what context are we in for that?
        rc = False
        if(operator == sublime.OP_EQUAL and (util.isPotentialOrgFile(view.file_name()) or self.IsOrgSyntax(view))):
            if(key == 'org_heading'):
                 rc = operand == self.ShouldLocalFold(view)
            elif(key == 'org_global'):
                rc = operand == self.ShouldGlobalFold(view)
            elif(key == 'org_link'):
                rc = operand == self.ShouldFoldLinks(view)
            elif(key == 'org_table'):
                rc = operand == self.ShouldTableTab(view)
            elif(key == 'org_block'):
                rc = operand == self.ShouldFoldBlock(view)
            elif(key == 'org_checkbox'):
                rc = operand == self.ShouldFoldCheckbox(view)
        return rc


r"""
/* SDOC: Reference::Commands::Misc
*** Org New File
    Command: =org_new_file=  Keys: none

    Opens a new, unsaved buffer with the org syntax set. Org: orgs New File
    from Template makes a file in one of the orgs server's directories
    instead.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Misc
*** enableDebugLogging
    Default: =false=

    Turns on debug logging from every OrgEverywhere module to the Sublime
    console (=Org: LEVEL [file : line] message=). Read at plugin load, so
    restart Sublime after changing it. Setting the environment variable
    =SUBLIME_ORGEVERYWHERE_DEBUG= to anything non-empty does the same.
EDOC */
"""

# Create a new file.
class OrgNewFileCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.new_file()
        view.set_syntax_file('Packages/OrgEverywhere/OrgEverywhere.sublime-syntax')

r"""
/* SDOC: Reference::Commands::Misc
*** Internal startup commands
    =org_on_load_sync_up= runs once after the plugin loads: it applies the
    startup folding (and inline images) to every org file already open in
    the window. It can be run again by hand after changing =startup=, but is
    not meant to be bound.
EDOC */
"""

# Iterate over all open "tabs" and sync them up so they are folded the way they are supposed to be.
class OrgOnLoadSyncUpCommand(sublime_plugin.WindowCommand):
    def run(self):
        sheets = self.window.sheets()
        for sheet in sheets:
            view = sheet.view()
            if(view):
                # Todo detect buffer type as well in the future?
                # for now we can only do files on disk
                if(view.file_name()):
                    log.debug("OnLoad: " + view.file_name())
                else:
                    log.debug("Loading Unamed Buffer: " + str(view.id()))
                onLoad(view)
            
