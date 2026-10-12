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
import OrgEverywhere.orgcheckbox as checkbox
import OrgEverywhere.orgnumberedlist as numberedlist
import OrgEverywhere.orgdynamicblock as dynamic
import OrgEverywhere.orgsourceblock as src

log = logging.getLogger(__name__)

# Jump to previous heading from current location.
r"""
/* SDOC: Reference::Commands::Navigation
*** Org Up / Org Down
    Commands: =org_up=, =org_down=  Keys: =ctrl+up=, =ctrl+down= (in an org file)

    Moves the cursor to the previous / next heading line (any level) and
    centres it.
EDOC */
"""


class OrgUpCommand(sublime_plugin.TextCommand):
    def run(self,edit):
        nav.navigate_up(self.view)

class OrgDownCommand(sublime_plugin.TextCommand):
    def run(self,edit):
        nav.navigate_down(self.view)


r"""
/* SDOC: Reference::Commands::Navigation
*** Org Jump In File
    Command: =org_jump_in_file=  Keys: none

    A quick panel of every heading in the file; picking one moves the
    cursor to it (keeping the cursor's column) and centres it.
EDOC */
"""


class OrgJumpInFileCommand(sublime_plugin.TextCommand):
    def on_done_st4(self,index,modifers):
        self.on_done(index)
    def on_done(self, index):
        view = self.view
        (curRow,curCol) = view.curRowCol()
        node = db.Get().NodeAtIndex(view, index)
        print(str(node))
        if(node != None):
            row = node.start_row
            linePos = view.text_point(row,curCol)
            view.show_at_center(linePos)
            view.sel().clear()
            view.sel().add(linePos)
        else:
            view.set_status("Error: ", "filename {0} not found in orgDb".format(view.file_name()))
    def run(self, edit):
        headings = db.Get().Headings(self.view)
        if(int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(headings, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(headings, self.on_done_st4, -1, -1)

RE_TABLE_MATCH = re.compile(r"^\s*\|")
def table_tabbing(view):
    cur = view.sel()[0]
    l = view.line(cur.begin())
    line = view.substr(l)
    return RE_TABLE_MATCH.search(line)


# Org Mode style tab cycling.
# Not really navigation but as good a place as any for this
r"""
/* SDOC: Reference::Commands::Folding
*** Org Tab Cycling
    Command: =org_tab_cycling=  Keys: =tab= (in an org file, on a heading,
    link, checkbox, block, table or the file's top)

    Tab does what fits the cursor's place:
    - on a heading: cycles FOLDED, CHILDREN (children shown folded),
      SUBTREE (all open);
    - on a drawer or a block line inside a heading: folds / unfolds it;
    - before the first heading: cycles the whole file like Shift+Tab;
    - on a link: folds / unfolds the link's target;
    - in a table: moves to the next field (Table Editor);
    - on a checkbox with children: folds / unfolds the children;
    - otherwise: inserts the best completion, or a tab.
    The file is re-read first when it changed.
EDOC */
"""


class OrgTabCyclingCommand(sublime_plugin.TextCommand):
    """
    Bind to TAB key, and if the current line is not
    a headline, a \t would be inserted?
    Or do we want something else here?
    """
    def run(self, edit):
        # Keep track of if the buffer has changed. The in memory image
        # will be out of date if it has, so loads it.
        file = db.Get().FindInfo(self.view)
        if(file != None and file.HasChanged(self.view)):
            file.LoadS(self.view)
        
        if(folding.fold_local_cycle(self.view)):
            return
        elif(file != None and type(file.AtInView(self.view,db.Get())) is node.OrgRootNode):
            folding.fold_global_cycle(self.view)
            return
        elif(folding.am_in_link(self.view)):
            folding.toggle_link(self.view)
            return
        # In a table, our tab expansion will take priority over Table Editor
        # So we have to own it and if we are in a table forward the command
        # All the other commands (at the moment) are okay
        elif(table_tabbing(self.view)):
            self.view.run_command("table_editor_next_field")
        elif(folding.ShouldFoldCheckbox(self.view)):
            folding.FoldCheckbox(self.view)
        else:
            log.debug("tab expansion")
            self.view.run_command("insert_best_completion", {"default": "\t", "exact": False})
            # {"name" : "Packages/vhdl-mode/Snippets/vhdl-header.sublime_snippet"}
            #self.view.run_command("insert_snippet") 
            #self.view.insert(edit,self.view.sel()[0].begin(), "\t")

# Global cycling
r"""
/* SDOC: Reference::Commands::Folding
*** Org Global Tab Cycling
    Command: =org_global_tab_cycling=  Keys: =shift+tab= (on a heading)

    Cycles the whole file: OVERVIEW (top headings only) to CONTENTS (all
    headings, text folded) to SHOW ALL (drawers and links still folded) and
    back. In a table =shift+tab= moves to the previous field instead.
EDOC */
"""


class OrgGlobalTabCyclingCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        folding.fold_global_cycle(self.view)


