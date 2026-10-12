import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import OrgEverywhere.orgparse.node as node
import OrgEverywhere.orgparse.date as orgdate
from   OrgEverywhere.orgparse.sublimenode import * 
import OrgEverywhere.orgutil.util as util
import OrgEverywhere.orgutil.navigation as nav
import OrgEverywhere.orgutil.template as templateEngine
import logging
import sys
import traceback 
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt





r"""
/* SDOC: Reference::Commands::Editing
*** Internal edit commands
    Commands: =org_internal_erase= (=start=, =end=), =org_internal_replace=
    (=start=, =end=, =text=), =org_internal_insert= (=location=, =text=);
    each also takes =onDone=.

    Used by other commands to change the buffer from a callback (Sublime
    only allows edits inside a command); after the edit the file is
    re-read. Not meant to be bound.
EDOC */
"""


class OrgInternalEraseCommand(sublime_plugin.TextCommand):
    def run(self, edit, start, end, onDone=None):
        region = sublime.Region(start, end)
        self.view.erase(edit, region)
        # Reload the file automatically when we edit it.
        db.Get().Reload(self.view)
        evt.EmitIf(onDone)

class OrgInternalReplaceCommand(sublime_plugin.TextCommand):
    def run(self, edit, start, end, text, onDone=None):
        region = sublime.Region(start, end)
        self.view.replace(edit, region, text)
        # Reload the file automatically when we edit it.
        db.Get().Reload(self.view)
        evt.EmitIf(onDone)

class OrgInternalInsertCommand(sublime_plugin.TextCommand):
    def run(self, edit, location, text, onDone=None):
        self.view.insert(edit, location, text)
        # Reload the file automatically when we edit it.
        db.Get().Reload(self.view)
        evt.EmitIf(onDone)
