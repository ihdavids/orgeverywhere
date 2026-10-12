import sublime
import sublime_plugin
import os
import OrgEverywhere.orgparse.node as node
from   OrgEverywhere.orgparse.sublimenode import * 
import OrgEverywhere.orgutil.util as util
import logging
import sys
import traceback 
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgcheckbox as checkbox
import OrgEverywhere.orgnumberedlist as numberedlist
import OrgEverywhere.orgdynamicblock as dynamic
import OrgEverywhere.orgsourceblock as src
import OrgEverywhere.orgediting as editing
import OrgEverywhere.orgtableformula as tbl 

log = logging.getLogger(__name__)


# ====================================================================
# Another Do What I Mean style command.
# Contextually looks at where you are and "does the right thing."
# All about inserting things state.
r"""
/* SDOC: Reference::Commands::Editing
*** Insert the next item here (ctrl+enter)
    Command: =org_generic_insert=  Keys: =ctrl+enter=

    Inserts the next thing of the kind under the cursor, right after the
    current line: a new checkbox item on a checkbox line, a new =-= / =+=
    item in a plain list, the next number in a numbered list (renumbering
    the rest), otherwise a sibling heading (Org Insert Heading Sibling).
    Nothing happens before the first heading.
EDOC */
"""


class OrgGenericInsertCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        line = self.view.curLine()
        cb = checkbox.get_checkbox(self.view, line)
        if(cb):
            self.view.run_command('org_insert_checkbox')
            return
        if(checkbox.isUnorderedList(self.view.substr(line))):
            self.view.run_command('org_insert_unordered_list')
            return
        if(numberedlist.isNumberedLine(self.view)):
            numberedlist.AppendLine(self.view, edit)
            return
        n = db.Get().AtInView(self.view)
        if(not n.is_root()):
            self.view.run_command('org_insert_heading_sibling')

# ====================================================================
r"""
/* SDOC: Reference::Commands::Editing
*** Insert the next item at the end (ctrl+shift+enter)
    Command: =org_generic_insert_aux=  Keys: =ctrl+shift+enter=

    Like ctrl+enter, but list items are added after the item's last child
    (at the end of its sub list) rather than straight after the line, and
    on a heading a child heading is inserted (Org Insert Heading Child).
EDOC */
"""


class OrgGenericInsertAuxCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        line = self.view.curLine()
        cb = checkbox.get_checkbox(self.view, line)
        if(cb):
            self.view.run_command('org_insert_checkbox',{'insertHere': False})
            return
        if(checkbox.isUnorderedList(self.view.substr(line))):
            self.view.run_command('org_insert_unordered_list',{'insertHere': False})
            return
        if(numberedlist.isNumberedLine(self.view)):
            numberedlist.AppendLine(self.view, edit, insertHere=False)
            return
        n = db.Get().AtInView(self.view)
        if(not n.is_root()):
            self.view.run_command('org_insert_heading_child')

# ====================================================================
# Another Do What I Mean style command.
# Contextually looks at where you are and "does the right thing."
# Recalculates checkboxes, recalculates blocks etc.
r"""
/* SDOC: Reference::Commands::Editing
*** Org Recalc
    Command: =org_recalc=  Keys: =alt+o c c=; vi command mode =space c c=

    The "do what I mean" update. On a dynamic block's =#+BEGIN:= or =#+END:=
    line: Org Exec Dynamic Block. On inline source code: Org Exec Inline
    Source Block. In a source block: Org Exec Source Block. On a
    =#+CALL:= line: Org Execute Call. In a table: runs the table's
    formulas. Anywhere else: recalculates every checkbox summary in the
    buffer and renumbers the numbered list at the cursor.
EDOC */
"""


class OrgRecalcCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if(dynamic.IsDynamicBlock(self.view)):
            self.view.run_command('org_execute_dynamic_block')
            return
        if(src.IsInlineSourceBlock(self.view)):
            self.view.run_command('org_execute_inline_source_block')
            return
        if(src.IsSourceBlock(self.view)):
            self.view.run_command('org_execute_source_block')
            return
        if(src.IsCallCommentBlock(self.view)):
            self.view.run_command('org_execute_call_comment')
            return
        if(tbl.isTable(self.view)):
            self.view.run_command('org_execute_table')
            return
        checkbox.recalculate_all_checkbox_summaries(self.view, edit)
        numberedlist.UpdateLine(self.view, edit)

# ====================================================================
# Another Do What I Mean style command.
# Contextually looks at where you are and "does the right thing."
# All about toggling things state.
r"""
/* SDOC: Reference::Commands::Editing
*** Toggle checkbox or todo
    Command: =org_toggle=  Keys: =alt+o t=; vi command mode =space t=
    (that binding is limited to tables by its context)

    On a checkbox line toggles the checkbox (Org Checkbox Toggle);
    anywhere else changes the heading's todo state (Org Change Todo).
EDOC */
"""


class OrgToggleCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        line = self.view.curLine()
        cb = checkbox.is_checkbox_line(self.view)
        #cb = checkbox.get_checkbox(self.view, line)
        if(cb):
            self.view.run_command('org_toggle_checkbox')
            return
        else:
            self.view.run_command('org_todo_change')


# ====================================================================
r"""
/* SDOC: Reference::Commands::Editing
*** Org Change Indent / Org Change DeIndent
    Commands: =org_change_indent=, =org_change_de_indent=
    Keys: =alt+o right= / =alt+o left=; vi command mode =space right= / =space left=

    On a list item (checkbox, plain or numbered) indents / outdents the
    item. On a heading demotes / promotes it (one star more / less).
EDOC */
"""


class OrgChangeIndentCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        line = self.view.curLine()
        cb = checkbox.get_checkbox(self.view, line)
        if(cb):
            editing.indent_list(self.view,self.view.curRow(), edit)
            return
        if(checkbox.isUnorderedList(self.view.substr(line))):
            editing.indent_list(self.view,self.view.curRow(), edit)
            return
        if(numberedlist.isNumberedLine(self.view)):
            editing.indent_list(self.view,self.view.curRow(), edit)
            return
        n = db.Get().AtInView(self.view)
        if(n and type(n) != node.OrgRootNode):
            editing.indent_node(self.view, n, edit)
            file = db.Get().FindInfo(self.view)
            file.LoadS(self.view)

# ====================================================================
# This does not handle indention of sub trees! Need to fix that!
class OrgChangeDeIndentCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        line = self.view.curLine()
        cb = checkbox.get_checkbox(self.view, line)
        if(cb):
            editing.deindent_list(self.view,self.view.curRow(), edit)
            return
        if(checkbox.isUnorderedList(self.view.substr(line))):
            editing.deindent_list(self.view,self.view.curRow(), edit)
            return
        if(numberedlist.isNumberedLine(self.view)):
            editing.deindent_list(self.view,self.view.curRow(), edit)
            return
        n = db.Get().AtInView(self.view)
        if(n and type(n) != node.OrgRootNode):
            editing.deindent_node(self.view, n, edit)
            file = db.Get().FindInfo(self.view)
            file.LoadS(self.view)
