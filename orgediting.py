import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import OrgEverywhere.orgparse.loader as loader
import OrgEverywhere.orgparse.node as node
import OrgEverywhere.orgparse.date as orgdate
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
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgproperties as props
import OrgEverywhere.orgdatepicker as datep
import OrgEverywhere.orginsertselected as insSel
import OrgEverywhere.orglinks as orglink
import OrgEverywhere.orgneovi as nvi
import OrgEverywhere.orgagenda as oa
import OrgEverywhere.orgcheckbox as checkbox
import OrgEverywhere.orgnumberedlist as numberedlist
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as oui

log = logging.getLogger(__name__)

# MOVING TO ANY DONE STATE:
# Support these:
#+STARTUP: lognotedone   Prompt and stored below the item with a Closing Note heading.
#+STARTUP: logdone       CLOSED: [TIMESTAMP] in LOGBOOK
# As well as configuration options
#
# PER TRANSITON MOVEMENT:
# @ - note
# ! - timestamp
# / - when leaving the state if next state doesn't log
#
# Then they go futher with: :LOGGING: WAIT(@) logrepeat properties
#
# - 1) We need the transitions recorded in the node (in the todos list)
# - 2) We need a method to insert CLOSED: and or prompt and note
# - 3) We need to track the state transitions themselves (from / to)
#
# - 4) Habits break all this with LAST_REPEAT and To From transition text.
# 


RE_CLOSED = re.compile(r"^\s*CLOSED:\s*\[.*\]")
def LocateClosed(view,node):
    for row in range(node.start_row, node.local_end_row + 1):
        pt = view.text_point(row,0)
        line = view.line(pt)
        lstr = view.substr(line)
        m = RE_CLOSED.search(lstr)
        if(m):
            return line
    return None

def InsertClosed(view, node, onDone=None):
    stamp = OrgDate.format_clock(datetime.datetime.now(),active=False)
    closedPt = LocateClosed(view,node)
    if(closedPt):
        text = node.indent() + "CLOSED: " + stamp
        view.ReplaceRegion(closedPt, text, onDone)
    else:
        row = node.start_row+1
        pt = view.text_point(row,0)
        newline = "\n" if view.isBeyondLastRow(row) else ""
        text = newline + node.indent() + "CLOSED: " + stamp + "\n"
        view.Insert(pt, text, onDone)
        #props.UpdateLogbook(view,node, "CLOSED:", stamp)

def RemoveClosed(view, node, onDone=None):
    closedPt = LocateClosed(view, node)
    if(closedPt):
        view.ReplaceRegion(closedPt.IncEnd(),"",onDone)
        #view.run_command("org_internal_replace", {"start": closedPt.begin(), "end": closedPt.end() + 1, "text": "", "onDone": onDone})
    else:
        evt.EmitIf(onDone)

def IsDoneState(node, toState):
    return toState in node.env.done_keys

def ShouldRecur(node, fromState, toState):
    if(IsDoneState(node, toState)):
        if(node.scheduled and node.scheduled.repeating):
            return node.scheduled
        if(node.deadline and node.deadline.repeating):
            return node.deadline
        timestamps = node.get_timestamps(active=True,point=True,range=True)
        for t in timestamps:
            if(t and t.repeating):
                return t
    return None

def ShouldClose(node, fromState, toState):
    if(ShouldRecur(node,fromState,toState)):
        return False
    # NOTE: We need to get the todo transitions
    #       into this as well!
    toState = toState.strip()
    startup = node.root.startup()
    logDone = sets.Get("logDone",None)
    if(IsDoneState(node, toState) and (Startup.logdone in startup or logDone)):
        return True

def InsertRecurrence(view, node, fromState, toState, onDone=None):
    #   - State "DONE"       from "TODO"       [2009-09-29 Tue]"
    stamp = OrgDate.format_clock(datetime.datetime.now(),active=False)
    def OnLogAdded():
        props.UpdateProperty(view, node, "LAST_REPEAT", stamp, onDone)
    props.AddLogbook(view,node, "- State {0:12} from {1:12} ".format('"' + toState + '"', '"' + fromState + '"'), stamp, evt.Make(OnLogAdded))

def InsertNote(view, node, text, fromState, toState, onDone=None):
    stamp = OrgDate.format_clock(datetime.datetime.now(),active=False)
    props.AddLogbook(view,node, "Note (to:{0},at:{1}): ".format(toState,stamp), text, onDone)

def ShouldNote(node, fromState, toState):
    if(ShouldRecur(node,fromState,toState)):
        return False
    # NOTE: We need to get the todo transitions
    #       into this as well!
    toState = toState.strip()
    startup = node.root.startup()
    if(IsDoneState(node,toState) and Startup.lognotedone in startup):
        return True



RE_T = re.compile(r'\s(?P<time><\s*\d+-\d+-\d+\s+[^>]+>)(\s+|$)')
r"""
/* SDOC: Reference::Commands::Editing
*** Org Change Todo
    Command: =org_todo_change=  Keys: none  Args: =onDone=

    Picks a new todo state for the heading at the cursor from a quick panel
    of the file's states (=#+TODO:= or =todoStates=) plus "none". Moving to
    a done state:

    - with a repeating =SCHEDULED=, =DEADLINE= or timestamp, the heading
      does not stay done: a =- State "DONE" from "TODO" [stamp]= line goes in
      its =LOGBOOK=, =LAST_REPEAT= is set, and each repeating timestamp moves
      on by its repeater (=+= one step, =++= until in the future, =.+= from
      today);
    - otherwise, with =#+STARTUP: lognotedone= you are asked for a note,
      logged in the =LOGBOOK=;
    - with =#+STARTUP: logdone= (or the =logDone= setting) a =CLOSED:= stamp
      is added. Leaving a done state removes the =CLOSED:= line.

    Works on the buffer; no server needed.
EDOC */
"""

# Use a menu to change the todo state of an item
class OrgTodoChangeCommand(sublime_plugin.TextCommand):
    def on_totally_done(self):
        evt.EmitIf(self.onDone)

    # recurrence needs to update the base timestamp!
    # This needs to respect the .+ ++ and + markers
    def on_update_timestamps_if_needed(self, row=0):
        # We have to reload our node as we updated things.
        self.node = db.Get().At(self.view, self.node.start_row)
        if(row > (self.node.local_end_row+1)):
            self.on_totally_done()
        for i in range(self.node.start_row+row, self.node.local_end_row+1):
            pt = self.view.text_point(i, 0)
            line = self.view.line(pt)
            txt = self.view.substr(line)
            m = RE_T.search(txt)
            now = datetime.datetime.now()
            if(m):
                tsl = OrgDate.list_from_str(m.group('time'))
                if(tsl):
                    t = tsl[0]
                    if(t.repeating):
                        next  = t.start
                        next2 = t.end
                        if(t.repeatpre == "+"):
                            next = t.next_repeat_from(oa.EnsureDateTime(next))
                        elif(t.repeatpre == "++"):
                            while(next < now):
                                next = t.next_repeat_from(oa.EnsureDateTime(next))
                        elif(t.repeatpre == ".+"):
                            next = t.next_repeat_from(now)
                        s = m.start(1)
                        e = m.end(1)
                        rpt = t.repeatpre + str(t.repeatnum) + t.repeatdwmy
                        wrn = ""
                        if(t.warning):
                            wrn = " " + t.warnpre + str(t.warnnum) + t.warndwmy
                        if(t.has_end()):
                            if(t.has_time()):
                                nout = txt[:s] + next.strftime("<%Y-%m-%d %a %H:%M-") + t.end.strftime("%H:%M ")+ rpt + wrn + ">"  + txt[e:]
                            else:
                                # This really shouldn't happen.
                                nout = txt[:s] + next.strftime("<%Y-%m-%d %a ")+ rpt + wrn + ">" + txt[e:]
                        else:
                            if(t.has_time()):
                                nout = txt[:s] + next.strftime("<%Y-%m-%d %a %H:%M ") + rpt + wrn + ">" +txt[e:]
                            else:
                                nout = txt[:s] + next.strftime("<%Y-%m-%d %a ")+ rpt + wrn + ">" + txt[e:]
                        self.view.run_command("org_internal_replace", {"start": line.begin(), "end": line.end(), "text": nout, "onDone": evt.Make(lambda:self.on_update_timestamps_if_needed(i+1)) })
                        return
        self.on_totally_done()

    def do_recurrence_if_needed(self):
        self.rec = ShouldRecur(self.node,self.fromState,self.newState)
        if(self.rec):
            InsertRecurrence(self.view, self.node, self.fromState, self.newState, evt.Make(self.on_update_timestamps_if_needed))
        else:
            self.on_totally_done()

    def do_close_if_needed(self):
        if(ShouldClose(self.node,self.fromState,self.newState)):
            InsertClosed(self.view, self.node, evt.Make(self.do_recurrence_if_needed))
        else:
            RemoveClosed(self.view, self.node, evt.Make(self.do_recurrence_if_needed))

    def on_insert_note(self, text):
        InsertNote(self.view, self.node, text, self.fromState, self.newState, evt.Make(self.do_close_if_needed))

    def do_note_if_needed(self):
        if(ShouldNote(self.node,self.fromState, self.newState)):
            self.view.window().show_input_panel("("+self.fromState + ">>" + self.newState + ") Note:","", self.on_insert_note, None, None)
        else:
            self.do_close_if_needed()

    def on_done_st4(self,index,modifers):
        self.on_done(index)

    def on_done(self, index):
        if(index < 0):
            return
        newState = self.todoStates[index]
        if(newState == "none"):
            newState = ""
        # if we don't have a TODO state then we have to handle that as well.
        m = self.todoRe.search(self.bufferContents)
        fromState = None
        if(m == None):
            self.todoRe = re.compile(r"^([*]+ (\[\#[a-zA-Z0-9]+\]\s+)?)( )*")
        else:
            fromState = m.group(3)
        if(newState != ""):
            newState += " "
        self.bufferContents = self.todoRe.sub(r"\g<1>" + newState, self.bufferContents)
        # We have to do the editing in sequence because the reloads can get mixed up otherwise
        if(fromState):
            self.fromState = fromState.strip()
        else:
            self.fromState = ""
        self.newState  = newState.strip()
        # Recurring events do not hit the done state when you toggle them
        # They bounce back to TODO they just get a new note in them
        if(ShouldRecur(self.node, self.fromState,self.newState)):
            self.do_note_if_needed()
        else:
            self.view.ReplaceRegion(self.row,self.bufferContents, evt.Make(self.do_note_if_needed))

    def run(self, edit, onDone=None):
        self.onDone = onDone
        self.node = db.Get().AtInView(self.view)
        #self.todoStates = sets.Get("todoStates", sets.defaultTodoStates)
        todos = self.node.env.all_todo_keys
        if(len(todos) > 0):
            self.todoStates = todos
            self.todoStates += ["none"]
        else:
            for i in range(0, len(self.todoStates)):
                if(self.todoStates[i] == "|"):
                    self.todoStates[i] = "none" 
        # ACTION vs DONE states
        # TODO" "FEEDBACK" "VERIFY" "|" "DONE" "DELEGATED
        row = self.node.start_row
        self.todoRe = r"^([*]+ (\[\#[a-zA-Z0-9]+\]\s+)?)("
        haveFirst = False
        for state in self.todoStates:
            if state != "|":
                if(haveFirst):
                    self.todoRe += "|"
                self.todoRe += state
                haveFirst = True
        self.todoRe += r")( )*"
        self.todoRe = re.compile(self.todoRe)
        sp  = self.view.text_point(row,0)
        self.row = self.view.line(sp)
        self.bufferContents = self.view.substr(self.row)
        if(int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.todoStates, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.todoStates, self.on_done_st4, -1, -1)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Change Priority
    Command: =org_priority_change=  Keys: =alt+o p=; NeoVintageous normal mode: =space p=  Args: =onDone=

    Sets the priority cookie (=[#A]=) of the heading at the cursor from a
    quick panel of the file's =#+PRIORITIES:= (or the =priorities= setting,
    default A to E) plus "none", which removes it.
EDOC */
"""

# Use a menu to change the priority of an item
class OrgPriorityChangeCommand(sublime_plugin.TextCommand):
    def on_done_st4(self,index,modifers):
        self.on_done(index)
    def on_done(self, index):
        if(index < 0):
            return
        newState = self.priorities[index]
        if(newState == "none"):
            newState = ""
        # if we don't have a TODO state then we have to handle that as well.
        m = self.Re.search(self.bufferContents)
        if(m == None):
            todos = self.node.env.all_todo_keys
            todos = '|'.join(todos)
            self.Re = re.compile(r"^([*]+\s+(" + todos + r")?\s*)( )*")
        if(newState != ""):
            newState = "[#" + newState + "] " 
        self.bufferContents = self.Re.sub(r"\g<1>" + newState, self.bufferContents)
        self.view.ReplaceRegion(self.row, self.bufferContents, self.onDone)
        #self.view.run_command("org_internal_replace", {"start": self.row.begin(), "end": self.row.end(), "text": self.bufferContents, "onDone": self.onDone})
        #self.view.replace(self.edit, self.row, self.bufferContents)

    def run(self, edit, onDone = None):
        self.onDone = onDone
        self.node = db.Get().AtInView(self.view)
        self.priorities = self.node.priorities()
        self.priorities = copy.copy(self.priorities)
        self.priorities.append("none")
        row = self.node.start_row
        self.Re = r"^([*]+ [^\[\]]*\s*)(\[\#[a-zA-Z0-9]+\]\s+)"
        self.Re = re.compile(self.Re)
        sp  = self.view.text_point(row,0)
        self.row = self.view.line(sp)
        self.bufferContents = self.view.substr(self.row)
        if(int(sublime.version()) <= 4096):
            self.view.window().show_quick_panel(self.priorities, self.on_done, -1, -1)
        else:
            self.view.window().show_quick_panel(self.priorities, self.on_done_st4, -1, -1)

def indent_node(view, node, edit):
    # Indent the node itself
    sp  = view.text_point(node.start_row,0)
    view.insert(edit,sp,"*")
    # Indent MY content
    for i in range(node.start_row+1,node.local_end_row+1):
        sp  = view.text_point(i,0)
        view.insert(edit,sp," ")
    # Find my children and indent them.
    for n in node.children:
        indent_node(view, n, edit)

def indent_list(view, row, edit):
    # Indent the node itself
    sp  = view.text_point(row,0)
    view.insert(edit,sp,"  ")

    line = view.lineAt(row)
    children,crow = numberedlist.findChildrenByIndent(view, line)
    for r in range(row+1,crow):
        sp  = view.text_point(r,0)
        view.insert(edit,sp,"  ")

def deindent_list(view, row, edit):
    # Get my position and ensure this node CAN de-indent
    sp  = view.text_point(row,0)
    ep  = view.text_point(row,1)
    np  = view.text_point(row,2)
    bufferContents = view.substr(sublime.Region(sp,np))
    bufferContentsS = view.substr(sublime.Region(sp,ep))
    wasTab = bufferContentsS == "\t"
    if(bufferContents == "  " or wasTab):
        if(wasTab):
            view.erase(edit,sublime.Region(sp,ep))
        else:
            view.erase(edit,sublime.Region(sp,np))
        line = view.lineAt(row)
        children,crow = numberedlist.findChildrenByIndent(view, line)
        for r in range(row+1,crow):
            sp  = view.text_point(r,0)
            ep  = view.text_point(r,1)
            np  = view.text_point(r,2)
            bufferContents = view.substr(sublime.Region(sp,np))
            bufferContentsS = view.substr(sublime.Region(sp,ep))
            wasTab = bufferContentsS == "\t"
            if(bufferContents == "  " or wasTab):
                if(wasTab):
                    view.erase(edit,sublime.Region(sp,ep))
                else:
                    view.erase(edit,sublime.Region(sp,np))

def deindent_node(view, node, edit):
    # Get my position and ensure this node CAN de-indent
    sp  = view.text_point(node.start_row,0)
    ep  = view.text_point(node.start_row,1)
    np  = view.text_point(node.start_row,2)
    bufferContents = view.substr(sublime.Region(ep,np))
    if(bufferContents == "*"):
        view.erase(edit,sublime.Region(sp,ep))
        # Now erase a space at the front of my contents.
        for i in range(node.start_row+1,node.local_end_row+1):
            sp  = view.text_point(i,0)
            ep  = view.text_point(i,1)
            bufferContents = view.substr(sublime.Region(sp,ep))
            if(bufferContents == " " or bufferContents == "\t"):
                view.erase(edit,sublime.Region(sp,ep))
        for n in node.children:
            deindent_node(view, n, edit)
    else:
        log.debug("Did not get star, not deindenting it " + str(len(bufferContents)) + " " + bufferContents)

# Thing is a region, and first line of the thing tuple
# things is a list of thing
def sort_things_alphabetically(things,reverse=False):
    things.sort(key=lambda thing: thing[1],reverse=reverse)


r"""
/* SDOC: Reference::Commands::Editing
*** Org Sort List
    Command: =org_sort_list=  Keys: none

    Sorts the plain or numbered list at the cursor alphabetically by each
    item's first line, keeping each item's continuation lines with it. A
    numbered list is renumbered afterwards. Logs an error when the cursor
    is not in a list.
EDOC */
"""

class OrgSortListCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        # Get a list of things
        things = None
        wasNumbered = False
        if(numberedlist.isNumberedLine(self.view)):
            wasNumbered = True
            things = numberedlist.getListAtPointForSorting(self.view)
        elif(checkbox.isUnorderedList(self.view.getLine(self.view.curRow()))):
            things = checkbox.getListAtPointForSorting(self.view)
        if(not things):
            log.error(" Could not sort at point")
            return
        # Build macro region
        start = things[0][0][0]
        end   = things[len(things)-1][0][1]
        sp  = self.view.text_point(start,0)
        ep  = self.view.text_point(end,0)
        ep  = self.view.line(ep).end()
        reg = sublime.Region(sp,ep)

        # Sort the things
        sort_things_alphabetically(things)

        # Copy from macro region to sorted version
        buffer = ""
        for thing in things:
            bs = self.view.text_point(thing[0][0],0)
            be = self.view.text_point(thing[0][1]-1,0)
            be = self.view.line(be).end()
            breg = sublime.Region(bs,be)
            ss = self.view.substr(breg).rstrip() + "\n"
            buffer += ss
        # Replace the macro region with new str
        self.view.replace(edit, reg, buffer)
        if(wasNumbered):
            self.view.run_command('org_update_numbered_list')
        pass



r"""
/* SDOC: Reference::Commands::Editing
*** Org Select Subtree / Org Copy Subtree
    Command: =org_select_subtree=, =org_copy_subtree=
    Keys: =alt+o m s= / =alt+o y s=; NeoVintageous normal mode: =space m s= / =space y s=

    Selects, or copies to the clipboard (and the NeoVintageous register),
    the heading at the cursor with all its children. Does nothing above the
    first heading.
EDOC */
"""

class OrgSelectSubtreeCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index > 1):
            sp    = self.view.text_point(curNode.start_row, 0)
            ep    = self.view.text_point(curNode.end_row, 0)
            r     = self.view.line(ep)
            reg   = sublime.Region(sp, r.end()+1)
            self.view.sel().clear()
            self.view.sel().add(reg)

class OrgCopySubtreeCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index > 1):
            sp    = self.view.text_point(curNode.start_row, 0)
            ep    = self.view.text_point(curNode.end_row, 0)
            r     = self.view.line(ep)
            reg   = sublime.Region(sp, r.end()+1)
            nodetext = self.view.substr(reg)
            sublime.set_clipboard(nodetext)
            nvi.TestAndSetClip(self.view, nodetext)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Select Entity / Org Copy Entity
    Command: =org_select_entity=, =org_copy_entity=
    Keys: =alt+o m e= / =alt+o y e=; NeoVintageous normal mode: =space m e= / =space y e=

    Selects, or copies, the heading at the cursor and its own text, without
    its children.
EDOC */
"""

class OrgSelectEntityCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index > 1):
            sp    = self.view.text_point(curNode.start_row, 0)
            ep    = self.view.text_point(curNode.local_end_row, 0)
            r     = self.view.line(ep)
            reg   = sublime.Region(sp, r.end()+1)
            self.view.sel().clear()
            self.view.sel().add(reg)

class OrgCopyEntityCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index > 1):
            sp    = self.view.text_point(curNode.start_row, 0)
            ep    = self.view.text_point(curNode.local_end_row, 0)
            r     = self.view.line(ep)
            reg   = sublime.Region(sp, r.end()+1)
            nodetext = self.view.substr(reg)
            sublime.set_clipboard(nodetext)
            nvi.TestAndSetClip(self.view, nodetext)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Copy Href / Org Select Href
    Command: =org_copy_link_href=, =org_select_link_href=  Keys: none

    With the cursor on a link: copies (to the clipboard and the
    NeoVintageous register), or selects, the link's target, without the
    brackets and description.
EDOC */
"""

class OrgCopyLinkHrefCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if(self.view.match_selector(self.view.sel()[0].begin(), "orgmode.link")):
            pt = self.view.sel()[0].end()
            links = self.view.find_by_selector("orgmode.link")
            hrefs = self.view.find_by_selector("orgmode.link.href")
            reg = None
            for link in links:
                line = self.view.line(link.begin())
                if(line.contains(pt)):
                    for href in hrefs:
                        if(line.contains(href.begin())):
                            reg = href
                            break
                    break
            if(reg):
                nodetext = self.view.substr(reg)
                sublime.set_clipboard(nodetext)
                nvi.TestAndSetClip(self.view, nodetext)

class OrgSelectLinkHrefCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if(self.view.match_selector(self.view.sel()[0].begin(), "orgmode.link")):
            pt = self.view.sel()[0].end()
            links = self.view.find_by_selector("orgmode.link")
            hrefs = self.view.find_by_selector("orgmode.link.href")
            reg = None
            for link in links:
                line = self.view.line(link.begin())
                if(line.contains(pt)):
                    for href in hrefs:
                        if(line.contains(href.begin())):
                            reg = href
                            break
                    break
            if(reg):
                self.view.sel().clear()
                self.view.sel().add(reg)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Move Heading Up / Org Move Heading Down
    Command: =org_move_heading_up=, =org_move_heading_down=
    Keys: =alt+o up= / =alt+o down=; NeoVintageous normal mode: =space up= / =space down=

    Swaps the heading at the cursor (with its subtree) with its previous or
    next sibling. Does nothing when there is no sibling that way.
EDOC */
"""

class OrgMoveHeadingUpCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index > 1):
            targetNode = curNode.get_sibling_up()
            if(targetNode):
                index = targetNode._index - 1
                r,c   = self.view.curRowCol()
                sp    = self.view.text_point(curNode.start_row, 0)
                ep    = self.view.text_point(curNode.end_row, 0)
                r     = self.view.line(ep)
                reg   = sublime.Region(sp, r.end()+1)

                nodetext = self.view.substr(reg)
                sp    = self.view.text_point(targetNode.start_row, 0)
                treg   = sublime.Region(sp, sp)

                self.view.erase(edit,reg)
                self.view.insert(edit,sp,nodetext)
                self.view.sel().clear()
                np = self.view.text_point(targetNode.start_row, c)
                self.view.sel().add(np)

class OrgMoveHeadingDownCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        if(curNode and type(curNode) != node.OrgRootNode and curNode._index < (len(curNode.env._nodes) - 1)):
            targetNode = curNode.get_sibling_down()
            if(targetNode):
                temp = curNode
                curNode = targetNode
                targetNode = temp
                index = targetNode._index - 1
                sp    = self.view.text_point(curNode.start_row, 0)
                ep    = self.view.text_point(curNode.end_row, 0)
                r     = self.view.line(ep)
                reg   = sublime.Region(sp, r.end()+1)

                nodetext = self.view.substr(reg)
                sp    = self.view.text_point(targetNode.start_row, 0)
                treg   = sublime.Region(sp, sp)
                endline = self.view.line(self.view.size())

                if(curNode.is_last_node() and curNode.end_row >= self.view.endRow()):
                    line = self.view.substr(self.view.line(self.view.text_point(self.view.endRow(),0)))
                    isEmpty = line.strip() == ""
                    if(not isEmpty):
                        nodetext = nodetext + "\n"
                self.view.erase(edit,reg)
                self.view.insert(edit,sp,nodetext)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Heading Sibling
    Command: =org_insert_heading_sibling=  Keys: =alt+o i h=; NeoVintageous normal mode: =space i h=

    Inserts a new heading at the same level after the current heading and
    all its children, from the =headingN= snippet, so the cursor lands in
    its title. Above the first heading it adds a level 1 heading at the
    end of the file.
EDOC */
"""

class OrgInsertHeadingSiblingCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        curNode = db.Get().AtInView(self.view)
        needsNewline = False
        if(not curNode):
            level = 1
            here = sublime.Region(self.view.size(),self.view.size())
            reg  = here
            text = self.view.substr(self.view.line(reg))
            if(text.strip() != ""):
                needsNewline = True
        else:
            level = curNode.level
            reg = curNode.region(self.view,True)  # trim ending whitespace
            if(level == 0):
                level = 1
                here = sublime.Region(self.view.size(),self.view.size())
                text = self.view.substr(self.view.line(reg))
                if(text.strip() != ""):
                    needsNewline = True
            else:
                here = sublime.Region(reg.end(),reg.end())
                text = self.view.substr(self.view.line(here))
                if(text.strip() != ""):
                    needsNewline = True
        self.view.sel().clear()
        self.view.sel().add(reg.end())
        self.view.show(here)
        if(needsNewline):
            self.view.insert(edit,self.view.sel()[0].begin(),'\n')
        ai = sublime.active_window().active_view().settings().get('auto_indent')
        self.view.settings().set('auto_indent',False)
        self.view.run_command("insert_snippet", {"name" : "Packages/OrgEverywhere/orgsnippets/heading"+str(level)+".sublime-snippet"})
        sublime.active_window().active_view().settings().set('auto_indent',ai)
        
r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Heading Child
    Command: =org_insert_heading_child=  Keys: =alt+o i c=; NeoVintageous normal mode: =space i c=  Args: =onDone=

    Inserts a heading one level deeper at the end of the current heading's
    subtree (its last child), from the =headingN= snippet. The cursor must be on or under a
    heading: above the first heading of a file that has headings the
    command fails (an error in the console).
EDOC */
"""

class OrgInsertHeadingChildCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        curNode = db.Get().AtInView(self.view)
        needsNewline = False
        if(not curNode):
            file = db.Get().FindInfo(self.view)
            if(len(file.org) > 0):
                curNode = file.org[len(file.org) - 1]
        if(not curNode):
            level = 1
            l = self.view.line(self.view.size())
            reg = sublime.Region(l.begin(),l.begin())
            reg  = here
        else:
            level = curNode.level
            reg = curNode.region(self.view, True)
            if(level == 0):
                level = 1
                here = sublime.Region(view.size(),view.size())
            else:
                here = sublime.Region(reg.end(),reg.end())
                text = self.view.substr(self.view.line(here))
                if(text.strip() != ""):
                    needsNewline = True
        if(not needsNewline):
            ll = self.view.line(reg.end())
            text = self.view.substr(ll)
            if(text.strip() == "" and len(text) > 0):
                # This is an empty line! Have to work at the front of this line!
                # Or we will insert to an odd location!
                reg = sublime.Region(ll.begin(), ll.begin())
        self.view.sel().clear()
        self.view.sel().add(reg.end())
        self.view.show(here)
        if(needsNewline):
            self.view.insert(edit,self.view.sel()[0].begin(),'\n')
        ai = sublime.active_window().active_view().settings().get('auto_indent')
        self.view.settings().set('auto_indent',False)
        self.view.run_command("insert_snippet", {"name" : "Packages/OrgEverywhere/orgsnippets/heading"+str((level+1))+".sublime-snippet"})
        sublime.active_window().active_view().settings().set('auto_indent',ai)
        evt.EmitIf(onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Insert text as a child heading
    Command: =org_insert_text_as_child_heading=  Keys: none  (no palette entry)  Args: =heading=, =onDone=

    Adds a child heading titled =heading= at the end of the current
    heading's subtree. Used by Org Link To Today; useful in your own key bindings or
    macros. As with Org Insert Heading Child the cursor must be under a
    heading.
EDOC */
"""

# This will insert whatever text you provide as a child heading of the current node
class OrgInsertTextAsChildHeadingCommand(sublime_plugin.TextCommand):
    def run(self, edit, heading=None, onDone=None):
        curNode = db.Get().AtInView(self.view)
        if(not curNode):
            file = db.Get().FindInfo(self.view)
            if(len(file.org) > 0):
                curNode = file.org[len(file.org) - 1]
        if(not curNode):
            level = 1
            l = self.view.line(self.view.size())
            reg = sublime.Region(l.begin(),l.begin())
            reg  = here
        else:
            level = curNode.level
            reg = curNode.region(self.view)
            if(level == 0):
                level = 1
                here = sublime.Region(view.size(),view.size())
            else:
                here = sublime.Region(reg.end(),reg.end())
        self.view.sel().clear()
        self.view.sel().add(reg.end()+1)
        #self.view.show(here)
        self.view.insert(edit,self.view.sel()[0].begin(),'\n' + ('*'*(level+1)) + ' ' + heading)
        evt.EmitIf(onDone)
        
r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Today / Now (Active / Inactive)
    Command: =org_insert_today_inactive=, =org_insert_today_active=,
    =org_insert_now_inactive=, =org_insert_now_active=  Keys: none

    Inserts today's date (=[2024-05-01 Wed]= inactive, =<2024-05-01 Wed>=
    active) or the date and time (=[2024-05-01 Wed 14:30]=) at the cursor.
EDOC */
"""

class OrgInsertTodayInactiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        now = datetime.datetime.now()
        toInsert = orgdate.OrgDate.format_date(now, False)
        self.view.insert(edit,self.view.sel()[0].begin(), toInsert)

class OrgInsertNowInactiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        now = datetime.datetime.now()
        toInsert = orgdate.OrgDate.format_clock(now, False)
        self.view.insert(edit,self.view.sel()[0].begin(), toInsert)

class OrgInsertTodayActiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        now = datetime.datetime.now()
        toInsert = orgdate.OrgDate.format_date(now, True)
        self.view.insert(edit,self.view.sel()[0].begin(), toInsert)

class OrgInsertNowActiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        now = datetime.datetime.now()
        toInsert = orgdate.OrgDate.format_clock(now, True)
        self.view.insert(edit,self.view.sel()[0].begin(), toInsert)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Date Inactive / Org Insert Date Active
    Command: =org_insert_date_inactive=, =org_insert_date_active=
    Keys: =alt+o i d i= / =alt+o i d a=; NeoVintageous normal mode: =space i d i= / =space i d a=

    Opens the date picker and inserts the date and time you pick at the
    cursor, inactive (=[...]=) or active (=<...>=).
EDOC */
"""

class OrgInsertDateInactiveCommand(sublime_plugin.TextCommand):
    def insert(self, date):
        if(date):
            self.view.Insert(self.view.sel()[0].begin(), OrgDate.format_clock(date.start, active=False))

    def run(self, edit):
        datep.Pick(evt.Make(self.insert))


class OrgInsertDateActiveCommand(sublime_plugin.TextCommand):
    def insert(self, date):
        if(date):
            self.view.Insert(self.view.sel()[0].begin(), OrgDate.format_clock(date.start, active=True))

    def run(self, edit):
        datep.Pick(evt.Make(self.insert))


r"""
/* SDOC: Reference::Commands::Editing
*** Org Schedule / Org Deadline / Org Timestamp
    Command: =org_schedule=, =org_deadline=, =org_active_timestamp=
    Keys: =alt+o i s= / =alt+o i e= / =alt+o i t=; NeoVintageous normal mode: =space i s= / =space i e= / =space i t=
    Args: =dateval=

    Adds a line under the heading at the cursor with =SCHEDULED: <date>=,
    =DEADLINE: <date>= or a plain active timestamp, the date picked with the
    date picker (cancelling removes the line). =dateval= (a date string)
    skips the picker. An existing SCHEDULED or DEADLINE line is not
    replaced: a second one is added. (=org_base_timestamp= is their shared
    base and not a command to bind.)
EDOC */
"""

class OrgBaseTimestampCommand(sublime_plugin.TextCommand):
    def __init__(self,unknown=None, prefix=None):
        super(OrgBaseTimestampCommand, self).__init__(unknown)
        self.prefix = prefix

    def insert(self, date):
        if(date):
            self.view.Insert(self.view.sel()[0].begin(), OrgDate.format_clock(date.start, active=True))
        else:
            self.view.Erase(self.reg)
        self.view.sel().clear()
        self.view.sel().add(self.oldsel)

    def run(self, edit, dateval=None):
        if(type(dateval) == str):
            dateval = orgdate.OrgDateFreeFloating.from_str(dateval)
        # TODO: Find scheduled and replace it as well.
        node = db.Get().AtInView(self.view)
        if(node and not node.is_root()):
            self.oldsel = self.view.sel()[0]
            pt = self.view.text_point(node.start_row,0)
            l = self.view.line(pt)
            # Last row handling If we are the last row we can't jump over the newline
            # we have to add one.
            nl = ""
            addnl = 1
            if(self.view.isBeyondLastRow(node.start_row+1)):
                nl = "\n"
                addnl = 0
            insertpt = l.end() + addnl
            endpt = insertpt + len(nl) + len(node.indent()) + len(self.prefix)
            self.reg = sublime.Region(insertpt, endpt)
            self.view.insert(edit, insertpt, nl + node.indent() + self.prefix)
            pt = self.view.text_point(node.start_row+1,0)
            l = self.view.line(pt)
            self.view.sel().clear()
            self.view.sel().add(l.end())
            if(dateval == None):
                datep.Pick(evt.Make(self.insert))
            else:
                self.insert(dateval)

class OrgScheduleCommand(OrgBaseTimestampCommand):
    def __init__(self,unknown=None):
        super(OrgScheduleCommand, self).__init__(unknown,"SCHEDULED:  \n")

class OrgDeadlineCommand(OrgBaseTimestampCommand):
    def __init__(self,unknown=None):
        super(OrgDeadlineCommand, self).__init__(unknown,"DEADLINE:  \n")

class OrgActiveTimestampCommand(OrgBaseTimestampCommand):
    def __init__(self,unknown=None):
        super(OrgActiveTimestampCommand, self).__init__(unknown,"  \n")

r"""
/* SDOC: Reference::Commands::Editing
*** Org Closed
    Command: =org_insert_closed=  Keys: none

    Adds a =CLOSED: [now]= line under the heading at the cursor.
EDOC */
"""

class OrgInsertClosedCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        node = db.Get().AtInView(self.view)
        if(not node.is_root()):
            self.oldsel = self.view.sel()[0]
            pt = self.view.text_point(node.start_row,0)
            l = self.view.line(pt)
            # Last row handling If we are the last row we can't jump over the newline
            # we have to add one.
            nl = ""
            addnl = 1
            if(self.view.isBeyondLastRow(node.start_row+1)):
                nl = "\n"
                addnl = 0
            now = datetime.datetime.now()
            toInsert = orgdate.OrgDate.format_clock(now, False)
            self.view.insert(edit, l.end() + addnl, nl + node.indent() + "CLOSED: "+toInsert+"\n")

r"""
/* SDOC: Reference::Settings::Editing
*** tagColumn
    Default: =80= in the settings file (=70= when unset; kept between 30 and 150)

    The column the tags of a heading line are aligned to by Org Insert Tag,
    Org Remove Tag and Org Fix Tags.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Editing
*** Todo states, priorities and logDone
    - =todoStates=: the todo keywords of files without =#+TODO:=. Entries
      after the ="|"= entry are done states. Default:
      =["TODO", "NEXT", "BLOCKED", "WAITING", "FLAG", "CLEANUP", "IN-PROGRESS", "DOING", "|", "CANCELLED", "REASSIGNED", "DONE", "MEETING", "PHONE", "NOTE", "FIXED"]=.
    - =priorities= (default =["A", "B", "C", "D", "E"]=): the priorities
      Org Change Priority and the agenda offer, without =#+PRIORITIES:=.
    - =logDone= (not set): =true= adds =CLOSED:= on moving to a done state,
      as =#+STARTUP: logdone= does. The settings file's comment calls it
      =logdone=, but the code reads =logDone=.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Editing
*** orgsTodayMarker and orgsMarkers
    | Setting           | Default     | Effect                                                                                     |
    |-------------------+-------------+--------------------------------------------------------------------------------------------|
    | =orgsTodayMarker= | ="TODAY"=   | The marker Org Set Today sets and Org Link To Today and Org Jump To Marker look for.        |
    | =orgsMarkers=     | =["TODAY"]= | The markers Org Set Marker and Org Jump To Marker offer (the today marker is always added). |
EDOC */
"""

def getTagColumn():
    tagColumn = sets.Get("tagColumn",70)
    if not isinstance(tagColumn,int):
        tagColumn = 70
    elif tagColumn < 30:
        tagColumn = 30
    elif tagColumn > 150:
        tagColumn = 150
    return tagColumn

# ================================================================================
# Completion lists come from orgs: fetch them off the ui thread, then show
# the input. Without a server the input still works, with what we know locally.
def WithCompletions(fetch, offline, show):
    if not orgs.connected():
        show(offline)
        return
    orgs.run(lambda c: fetch(), show, on_error=lambda e: show(offline), what="completions")

def WithAllTags(show):
    WithCompletions(lambda: db.Get().AllTags(), sorted(db.Get().tags), show)

RE_TAGS = re.compile(r'^(?P<heading>[*]+((?![ \t][:]).)+\s*)(\s+(?P<tags>[:]([^: ]+[:])+))?$')
r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Tag
    Command: =org_insert_tag=  Keys: NeoVintageous normal mode: =space i m=  Args: =text=, =onDone=

    Adds a tag to the heading at the cursor and aligns the tags at
    =tagColumn=. Without =text= an input with completion offers every tag
    the orgs server knows (or, offline, the tags of the open files). A tag
    the heading already has is left alone. (=alt+o i t= is also bound to it
    in the keymap, but the later =org_active_timestamp= binding of the same
    keys wins.)
EDOC */
"""

class OrgInsertTagCommand(sublime_plugin.TextCommand):
    def OnDone(self, text):
        if(not text):
            return
        node = db.Get().AtInView(self.view)
        if(node):
            if not text in node.tags:
                (region, line) = self.view.getLineAndRegion(node.start_row)
                m = RE_TAGS.search(line)
                if(m and m.group('tags') != None):
                    tags = m.group('tags').strip() + text + ":"
                else:
                    tags = ":" + text + ":" 
                tagColumn = getTagColumn()
                print("{0:"+str(tagColumn)+"s}{1}")
                toline = ("{0:"+str(tagColumn)+"s}{1}").format(m.group('heading').strip(), tags)
                self.view.ReplaceRegion(region,toline,self.onDone)
            else:
                log.debug("Tag already part of node")
                evt.EmitIf(self.onDone)

    def run(self, edit, text=None, onDone=None):
        self.onDone = onDone
        self.text = text.strip() if text != None else text
        if(self.text != None and self.text != ""):
            self.OnDone(self.text)
        else:
            self.input = insSel.OrgInput()
            WithAllTags(lambda tags: self.input.run("Tag:",tags,evt.Make(self.OnDone)))

r"""
/* SDOC: Reference::Commands::Editing
*** Org Remove Tag
    Command: =org_remove_tag=  Keys: NeoVintageous normal mode: =space r m=  Args: =text=, =onDone=

    Removes a tag (asked for with completion, or =text=) from the heading
    at the cursor, case insensitively, and realigns the rest.
EDOC */
"""

# ================================================================================
class OrgRemoveTagCommand(sublime_plugin.TextCommand):
    def OnDone(self, text):
        if(not text):
            return
        node = db.Get().AtInView(self.view)
        if(node):
            if text in node.tags:
                (region, line) = self.view.getLineAndRegion(node.start_row)
                m = RE_TAGS.search(line)
                if(m and m.group('tags') != None):
                    txt = m.group('tags')
                    tgs = txt.split(':')
                    tags = []
                    for tag in tgs:
                        tag = tag.strip()
                        if tag != None and tag != "" and tag.lower() != text.lower():
                            tags.append(tag)
                    tagColumn = getTagColumn()
                    if len(tags) > 0:
                        tags = (":" + ":".join(tags) + ":").strip()
                        toline = ("{0:"+str(tagColumn)+"s}{1}").format(m.group('heading').strip(), tags)
                        self.view.ReplaceRegion(region,toline,self.onDone)
                    else:
                        toline = ("{0:"+str(tagColumn)+"s}{1}").format(m.group('heading').strip(), "")
                        self.view.ReplaceRegion(region,toline,self.onDone)
            else:
                log.debug("Tag not part of node")
                evt.EmitIf(self.onDone)

    def run(self, edit, text=None, onDone=None):
        self.onDone = onDone
        self.text = text.strip() if text != None else text
        if(self.text != None and self.text != ""):
            self.OnDone(self.text)
        else:
            self.ipt = insSel.OrgInput()
            WithAllTags(lambda tags: self.ipt.run("Tag:",tags,evt.Make(self.OnDone)))

r"""
/* SDOC: Reference::Commands::Editing
*** Org Remove All Tags
    Command: =org_remove_all_tags=  Keys: NeoVintageous normal mode: =space r a m=  Args: =onDone=

    Removes every tag from the heading at the cursor.
EDOC */
"""

# ================================================================================
class OrgRemoveAllTagsCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=None, onDone=None):
        self.onDone = onDone
        node = db.Get().AtInView(self.view)
        if(node):
            if len(node.tags) > 0:
                (region, line) = self.view.getLineAndRegion(node.start_row)
                m = RE_TAGS.search(line)
                toline = ("{0}").format(m.group('heading'))
                self.view.ReplaceRegion(region,toline,self.onDone)
            else:
                log.debug("Tags not part of node")
                evt.EmitIf(self.onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Fix Tags
    Command: =org_fix_tags=  Keys: NeoVintageous normal mode: =space f m=  Args: =onDone=

    Realigns the tags of every heading in the file at =tagColumn=.
EDOC */
"""

# ================================================================================
class OrgFixTagsCommand(sublime_plugin.TextCommand):
    def run(self, edit, text=None, onDone=None):
        self.onDone = onDone

        file = db.Get().Find(self.view)
        if file:
            tagColumn = getTagColumn()
            for node in file:
                if(node):
                    if len(node.tags) > 0:
                        (region, line) = self.view.getLineAndRegion(node.start_row)
                        m = RE_TAGS.search(line)
                        if(m and m.group('tags') != None):
                            txt = m.group('tags').strip()
                            toline = ("{0:"+str(tagColumn)+"s}{1}").format(m.group('heading').strip(), txt)
                            self.view.ReplaceRegion(region,toline,self.onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Archive Tag
    Command: =org_insert_archive_tag=  Keys: none  Args: =onDone=

    Adds the =ARCHIVE= tag to the heading at the cursor (as Org Insert Tag).
    To move a heading to its archive file use Org Archive Subtree.
EDOC */
"""

# ================================================================================
class OrgInsertArchiveTagCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        evt.EmitIf(self.onDone)
    def run(self, edit, onDone=None):
        self.onDone = onDone
        self.view.run_command("org_insert_tag",{"onDone": evt.Make(self.OnDone), "text": "ARCHIVE"})

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert CustomId
    Command: =org_insert_custom_id=  Keys: =alt+o i i=; NeoVintageous normal mode: =space i i=  Args: =onDone=

    Asks for a =CUSTOM_ID= (completing from the ids the orgs server knows)
    and sets it as a property of the heading at the cursor. Spaces become
    =-=.
EDOC */
"""

# ================================================================================
class OrgInsertCustomIdCommand(sublime_plugin.TextCommand):
    def on_done(self, text):
        if(text):
            # No spaces allowed in a custom id
            text = text.replace(" ","-")
            node = db.Get().AtInView(self.view)
            if(node and not node.is_root()):
               props.UpdateProperty(self.view,node,"CUSTOM_ID",text,self.onDone)

    def run(self, edit, onDone=None):
        self.onDone = onDone
        self.input = insSel.OrgInput()
        WithCompletions(lambda: db.Get().customids, [], lambda ids: self.input.run("Custom Id:",ids, evt.Make(self.on_done)))

# ================================================================================
# Markers are orgs exclusive markers: a tag only one heading carries at a
# time (the server moves it). "Today" is the marker named by orgsTodayMarker.
# Before markers, today was the heading with :CUSTOM_ID: TODAY; that is still
# found when no heading carries the marker.
def TodayMarker():
    return sets.Get("orgsTodayMarker", "TODAY") or "TODAY"

def MarkerNames():
    names = sets.Get("orgsMarkers", ["TODAY"]) or []
    if isinstance(names, str):
        names = [names]
    today = TodayMarker()
    if today not in names:
        names = [today] + list(names)
    return names

def PickMarker(window, name, on_pick):
    if name:
        on_pick(name)
        return
    names = MarkerNames()
    if len(names) == 1:
        on_pick(names[0])
        return
    window.show_quick_panel(names, lambda i, *a: on_pick(names[i]) if i >= 0 else None)

def MarkerHeading(todos, name):
    """The heading holding a marker, from /exclusivemarker results.

    The server's query matches inherited tags too, so children of the marked
    heading come back as well: prefer one carrying the tag itself, and the
    first in the file (the parent comes before its children)."""
    if not todos:
        return None
    own = [t for t in todos if name.lower() in [x.lower() for x in (t.get("Tags") or [])]]
    return (own or todos)[0]

def FindOpenView(filename):
    if not filename:
        return None
    for w in sublime.windows():
        v = w.find_open_file(filename)
        if v is not None:
            return v
    return None

def SetMarker(view, name, onDone=None):
    """(ui) Put marker name on the heading at the cursor, via orgs."""
    def work(cl):
        h, tgt, node = orgs.heading_target(view)
        olp = []
        n = node
        while n is not None and not n.is_root():
            olp.insert(0, n.heading)
            n = n.parent
        # The old holder loses the tag. /setexclusivemarker would do it, but
        # its query matches inherited tags, so it would toggle the tag ON for
        # the old holder's children; and the hash it was given goes stale when
        # the tags change. So: untag holders here, one at a time (hashes shift
        # with each write), then address the heading by outline path.
        for i in range(25):
            own = [t for t in cl.marker(name) if name.lower() in [x.lower() for x in (t.get("Tags") or [])]]
            if not own:
                break
            # The server writes the holder's file: save it first, then look
            # again (saving can move the heading).
            orgs.sync_path(cl, own[0].get("Filename"))
            own = [t for t in cl.marker(name) if name.lower() in [x.lower() for x in (t.get("Tags") or [])]]
            if not own:
                break
            cl.toggle_tag(own[0]["Hash"], name)
        cl.set_marker(name, orgs.olp_target(cl, tgt["Filename"] and cl.local(tgt["Filename"]), olp))
        return node.heading
    def done(heading):
        sublime.status_message("orgs: {0} is now on {1}".format(name, heading))
        evt.EmitIf(onDone)
    orgs.run(work, done, what="set marker")

def FindMarker(name, on_found, customIdFallback=None, sync=False):
    """(ui) on_found(filename, line0) for the heading holding a marker, or
    on_found(None, None). Falls back to a :CUSTOM_ID: heading when given.
    sync: save the holder's open buffer first, so line0 is a row of the
    buffer too (for edits made in the buffer)."""
    def fallback():
        # Offline: only open buffers can say where the custom id is.
        if customIdFallback:
            fn, at = db.Get().LocateInBuffers(customIdFallback, "customids")
            if fn is not None:
                on_found(fn, at)
                return
        on_found(None, None)
    if not orgs.connected():
        fallback()
        return
    def done(found):
        on_found(*(found or (None, None)))
    def failed(e):
        log.info("orgs: marker %s: %s", name, e)
        fallback()
    def look(c):
        t = MarkerHeading(c.marker(name), name)
        if t and sync:
            orgs.sync_path(c, t.get("Filename"))
            # Ask again: saving the buffer may have moved it.
            t = MarkerHeading(c.marker(name), name)
        if t:
            return (t.get("Filename"), t.get("LineNum", 0))
        if customIdFallback:
            fn, at = db.Get().LocateByCustomId(customIdFallback, c)
            if fn is not None:
                return (fn, at)
        return None
    orgs.run(look, done, on_error=failed, what="marker")

r"""
/* SDOC: Reference::Commands::Editing
*** Org Set Today
    Command: =org_set_today=  Keys: none  Args: =onDone=

    Puts the today marker (=orgsTodayMarker=, default =TODAY=) on the
    heading at the cursor. A marker is a tag the orgs server keeps on
    exactly one heading: the heading that had it loses it. Org Link To Today
    and the other "today" commands work on the heading carrying it. Needs
    the orgs server.
EDOC */
"""

class OrgSetTodayCommand(sublime_plugin.TextCommand):
    def run(self, edit, onDone=None):
        node = db.Get().AtInView(self.view)
        if(not node or node.is_root()):
            log.debug("Cannot update root node or non existent node as today")
            return
        SetMarker(self.view, TodayMarker(), onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Set Marker
    Command: =org_set_marker=  Keys: none  Args: =name=, =onDone=

    Puts a marker on the heading at the cursor: one of =orgsMarkers= (plus
    the today marker), picked from a quick panel when there is more than
    one, or =name=. The previous holder loses the tag (its file is saved
    first if it is open and modified). Needs the orgs server.
EDOC */
"""

class OrgSetMarkerCommand(sublime_plugin.TextCommand):
    def run(self, edit, name=None, onDone=None):
        node = db.Get().AtInView(self.view)
        if(not node or node.is_root()):
            sublime.status_message("Not on a heading")
            return
        PickMarker(self.view.window(), name, lambda n: SetMarker(self.view, n, onDone))

r"""
/* SDOC: Reference::Commands::Editing
*** Org Jump To Marker
    Command: =org_jump_to_marker=  Keys: none  Args: =name=

    Opens the heading that carries a marker (picked as in Org Set Marker).
    For the today marker a heading with =:CUSTOM_ID: TODAY= is used when no
    heading carries it (the way "today" was found before markers); offline
    only open buffers are searched for that.
EDOC */
"""

class OrgJumpToMarkerCommand(sublime_plugin.WindowCommand):
    def run(self, name=None):
        def jump(n):
            def found(filename, line):
                if filename:
                    oui.goto(self.window, filename, line)
                else:
                    sublime.status_message("No heading carries the marker " + n)
            FindMarker(n, found, "TODAY" if n == TodayMarker() else None)
        PickMarker(self.window, name, jump)


def get_view_for_silent_edit_file(file):
    # First check all sheets for this file.
    window = sublime.active_window()
    view = window.find_open_file(file.filename)
    if(view):
        return view
    # Okay the file is not opened, we have to open it
    # but we don't want it having focus
    # So keep the old view so we can refocus just to
    # be sure.
    currentView = window.active_view()
    view = window.open_file(file.filename, sublime.ENCODED_POSITION)
    window.focus_view(currentView)
    return view

# ================================================================================
class RunEditingCommandOnToday:
    def __init__(self, view, command, cmds = {}):
        self.view    = view
        self.command = command
        self.cmds    = cmds

    def onSaved(self):
        db.Get().Reload(self.savedView)
        evt.EmitIf(self.onDone)

    def onEdited(self):
        # NOTE the save here doesn't seem to be working
        # Not sure why. BUT...
        view = self.savedView
        view.run_command("save")
        sublime.set_timeout_async(lambda: self.onSaved(), 100)

    def onLoaded(self):
        view = self.savedView
        self.n.move_cursor_to(view)
        eventName = util.RandomString()
        evt.Get().once(eventName, self.onEdited)
        log.debug("Trying to run: " + self.command)
        cmds = self.cmds
        cmds["onDone"] = eventName
        view.run_command(self.command, cmds)

    def Run(self,onDone = None):
        self.onDone = onDone
        FindMarker(TodayMarker(), self.onFound, "TODAY", sync=True)

    def onFound(self, filename, at):
        if(filename is None):
            sublime.status_message("Could not locate today (Org: Set Today)")
            log.warning("Could not locate today")
            return
        file = db.Get().ParseFile(filename)
        node = file.At(at) if file else None
        if(node is None or node.is_root()):
            log.warning("COULD NOT LOCATE TODAY")
            return
        self.n         = node
        self.f         = file
        self.savedView = get_view_for_silent_edit_file(file)
        # Give time for the document to be opened.
        sublime.set_timeout_async(lambda: self.onLoaded(), 200)

r"""
/* SDOC: Reference::Commands::Editing
*** Append text to the heading
    Command: =org_append_text=  Keys: none  (no palette entry)  Args: =text=, =onDone=

    Appends =text= as a new line, indented two spaces per level, at the end
    of the current heading's subtree. Used by Org Link To Today; useful in key bindings.
EDOC */
"""

# Append text to a node
class OrgAppendTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text="", onDone=None):
        curNode = db.Get().AtInView(self.view)
        if(not curNode):
            file = db.Get().FindInfo(self.view)
            if(len(file.org) > 0):
                curNode = file.org[len(file.org) - 1]
        if(not curNode):
            level = 1
            l = self.view.line(self.view.size())
            reg = sublime.Region(l.begin(),l.begin())
        else:
            level = curNode.level
            reg = curNode.region(self.view)
            if(level == 0):
                level = 1
        self.view.sel().clear()
        self.view.sel().add(reg.end() + 1)
        #self.view.show(here)
        self.view.insert(edit,self.view.sel()[0].begin(),'\n' + (' '*(level*2)) + text)
        evt.EmitIf(onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Link To Today
    Command: =org_link_to_today=  Keys: none  Args: =onDone=

    Files the heading at the cursor under today's heading: it is scheduled
    for now (=SCHEDULED:= added), a child heading with the same title is
    added under the heading carrying the today marker, and a link back to
    the heading (as Org Create Link) is appended under it. The today file is
    opened in the background, edited and saved. Needs the orgs server to
    find the today marker (or an open buffer with =:CUSTOM_ID: TODAY=).
EDOC */
"""

class OrgLinkToTodayCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        evt.EmitIf(self.onDone)

    def InsertLink(self):
        self.ed = RunEditingCommandOnToday(self.view, "org_append_text", {'text': self.link})
        self.ed.Run(evt.Make(self.OnDone))

    def run(self, edit, onDone=None):
        self.onDone = onDone
        # Schedule this item so it is in the agenda
        self.view.run_command("org_schedule", {"dateval": str(datetime.datetime.now())})
        # Create a link to the current location so we can insert it in our today item
        self.link = orglink.CreateLink(self.view)
        curNode = db.Get().AtInView(self.view)
        # Should we add a heading to this?
        if(curNode and not curNode.is_root()):
            self.ed = RunEditingCommandOnToday(self.view, "org_insert_text_as_child_heading", {'heading': curNode.heading})
            self.ed.Run(evt.Make(self.InsertLink))
        else:
            self.InsertLink()


r"""
/* SDOC: Reference::Commands::Editing
*** Enter on a heading
    Command: =org_enter_on_heading=  Keys: =enter= on a heading line  Args: =Indent= (the heading level)

    =enter= on a heading starts the next line indented under the heading
    (level + 1 spaces) instead of at column 0. The keymap binds it per
    heading level 1 to 9 with the matching =Indent=.
EDOC */
"""

class OrgEnterOnHeadingCommand(sublime_plugin.TextCommand):
    def run(self, edit, Indent=1):
        indent = (" " * Indent) + " "
        self.view.run_command("org_internal_insert", {"location": self.view.sel()[0].begin(), "text": "\n" + indent})
