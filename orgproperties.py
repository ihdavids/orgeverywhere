import sublime
import sublime_plugin
import datetime
import re
from pathlib import Path
import os
import fnmatch
import OrgEverywhere.orgparse.node as node
from   OrgEverywhere.orgparse.sublimenode import * 
import OrgEverywhere.orgparse.date as orgdate
import OrgEverywhere.orgutil.util as util
import OrgEverywhere.orgutil.navigation as nav
import OrgEverywhere.orgutil.template as templateEngine
import logging
import sys
import traceback 
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.pymitter as evt
import OrgEverywhere.orgduration as dur
import uuid
#import yaml

log = logging.getLogger(__name__)

RE_SDC = re.compile(r'^\s*(SCHEDULED|DEADLINE|CLOSED):')
def InsertDrawerIfNotPresent(view, node, drawer = ":PROPERTIES:", onDone=None):
    if not node or node.is_root():
        evt.EmitIf(onDone)
        return False
    if(drawer == ":PROPERTIES:"):
        if(node.property_drawer_location):
            evt.EmitIf(onDone)
            return False
    else:
        for d in node.drawers:
            if((":" + d["name"] + ":") == drawer):
                evt.EmitIf(onDone)
                return False
    level = node.level
    indent = " " + (" " * level)
    drawerRow = node.body_lines_start
    # Properties should be the first drawer...
    if(drawer != ":PROPERTIES:" and node.property_drawer_location):
        drawerRow = node.property_drawer_location[1] + 1
    # Also skip over SCHEDULED, DEADLINE and CLOSED lines
    for row in range(drawerRow, node.local_end_row + 1):
        pt = view.text_point(row, 0)
        line = view.line(pt)
        txt = view.substr(line)
        m = RE_SDC.search(txt)
        if(not m):
            drawerRow = row
            break
        else:
            drawerRow = row + 1

    drawerHere = view.text_point(drawerRow, 0)
    newline = "\n" if view.isBeyondLastRow(drawerRow) else ""
    if(drawer == ":PROPERTIES:"):
        node.set_property_drawer_location((drawerRow,drawerRow+1))
    view.run_command("org_internal_insert", {"location": drawerHere, "text": newline + indent + drawer +"\n" + indent + ":END:\n","onDone":onDone})
    return True

def AddProperty(view, node, key, value, onDone=None):
    if(InsertDrawerIfNotPresent(view, node)):
        # File is reloaded have to regrab node
        node = db.Get().At(view, node.start_row)
    end = node.property_drawer_location[1]
    pt = view.text_point(end, 0)
    level = node.level
    indent = "   " + (" " * level)
    view.run_command("org_internal_insert", {"location": pt, "text": indent + ":" + str(key) + ": " + str(value) + "\n", "onDone": onDone})
    node.set_property_drawer_location((node.property_drawer_location[0],node.property_drawer_location[1] + 1))

# KEY has to have : and everything.
def AddLogbook(view, node, key, value, onDone=None):
    if(InsertDrawerIfNotPresent(view, node, ":LOGBOOK:")):
        # File is reloaded have to regrab node
        node = db.Get().At(view, node.start_row)
    drawer = node.get_drawer("LOGBOOK")
    loc = drawer['loc']
    end = loc[1]
    pt = view.text_point(end, 0)
    level = node.level
    indent = "   " + (" " * level)
    drawer['loc'] = (loc[0], loc[1] + 1)
    view.run_command("org_internal_insert", {"location": pt, "text": indent + str(key) + " " + str(value) + "\n", "onDone": onDone})


RE_PROPERTY_EXTRACT = re.compile(r"^\s*:([a-zA-Z0-9_@-]+):\s*(.*)")
def GetProperty(view, node, key):
    if(not node or not node.property_drawer_location):
        return False
    start = node.property_drawer_location[0]
    end   = node.property_drawer_location[1]
    lkey = key.lower()
    v = None
    mrow = None
    # Work backwards, want to find the latest incarnation of this
    for row in range(end-1, start, -1):
        line = view.getLine(row)
        m = RE_PROPERTY_EXTRACT.search(line)
        if(m != None):
            k = m.group(1).strip()
            v = m.group(2).strip()
            if(k.lower() == lkey):
                mrow = row
                break
    return v

def UpdateProperty(view, node, key, value, onDone=None):
    def OnDrawer():
        # File is reloaded have to regrab node
        n = db.Get().At(view, node.start_row)
        if(not n):
            log.error("Failed to look up property drawer! Something is wrong!")
        start, end = n.property_drawer_location
        lkey = key.lower().strip()
        mrow = None
        # Work backwards, want to find the latest incarnation of this
        for row in range(end-1, start, -1):
            line = view.getLine(row)
            m = RE_PROPERTY_EXTRACT.search(line)
            if(m):
                k = m.group(1).strip()
                v = m.group(2).strip()
                if(k.lower() == lkey):
                    mrow = row
                    break
        # We found our property, replace it!
        if(mrow != None):
            pt = view.text_point(mrow, 0)
            rw = view.line(pt)
            text = "  {0}:{1}: {2}".format(n.indent(),key,value)
            view.ReplaceRegion(rw,text,onDone)
        # Otherwise add a new property
        else:
            AddProperty(view, n, key, value, onDone)
    InsertDrawerIfNotPresent(view, node, ":PROPERTIES:", evt.Make(OnDrawer))


def UpdateLogbook(view, node, key, value, onDone=None):
    if(InsertDrawerIfNotPresent(view, node, ":LOGBOOK:")):
        # File is reloaded have to regrab node
        node = db.Get().At(view, node.start_row)
    if not node or node.is_root():
        evt.EmitIf(onDone)
        return
    drawer = node.get_drawer("LOGBOOK")
    loc    = drawer['loc']
    start  = loc[0]
    end    = loc[1]
    lkey   = key.lower()
    mrow   = None
    extract = re.compile(r'^\s*' + key)
    # Work backwards, want to find the latest incarnation of this
    for row in range(end-1, start, -1):
        line = view.getLine(row)
        m = extract.search(line)
        if(m != None):
            mrow = row
            break
    level = node.level
    indent = "   " + (" " * level)
    # We found our property, replace it!
    if(mrow != None):
        pt = view.text_point(mrow, 0)
        rw = view.line(pt)
        view.run_command("org_internal_replace", {"start": rw.begin(), "end": rw.end(), "text": indent + str(key) + " " + str(value), "onDone": onDone})
    # Otherwise add a new property
    else:
        AddLogbook(view, node, key, value, onDone)

def RemoveProperty(view, node, key):
    if(not node or not node.property_drawer_location):
        return False
    start = node.property_drawer_location[0]
    end   = node.property_drawer_location[1]
    lkey = key.lower()
    mrow = None
    # Work backwards, want to find the latest incarnation of this
    for row in range(end-1, start, -1):
        line = view.getLine(row)
        m = RE_PROPERTY_EXTRACT.search(line)
        if(m != None):
            k = m.group(1).strip()
            v = m.group(2).strip()
            if(k.lower() == lkey):
                mrow = row
                break
    # We found our property, erase it!
    if(mrow != None):
        pt = view.text_point(mrow, 0)
        rw = view.line(pt)
        view.run_command("org_internal_erase", {"start": rw.begin(), "end": (rw.end()+1)})
        node.set_property_drawer_location((node.property_drawer_location[0],node.property_drawer_location[1] - 1))
        return True
    return False

def RemoveLogbook(view, node, key):
    if(not node):
        return False
    drawer = node.get_drawer("LOGBOOK")
    if(not drawer):
        return False
    loc    = drawer['loc']
    start  = loc[0]
    end    = loc[1]
    lkey   = key.lower()
    mrow   = None
    # Work backwards, want to find the latest incarnation of this
    for row in range(end-1, start, -1):
        line = view.getLine(row)
        m = re.search(key,line)
        if(m != None):
            mrow = row
            break
    # We found our property, erase it!
    if(mrow != None):
        pt = view.text_point(mrow, 0)
        rw = view.line(pt)
        view.run_command("org_internal_erase", {"start": rw.begin(), "end": (rw.end()+1)})
        drawer['loc'] = (start,end-1)
        return True
    return False

def GetLogbook(view, node, key):
    if(not node):
        return False
    drawer = node.get_drawer("LOGBOOK")
    if not drawer:
        return False
    loc    = drawer['loc']
    start  = loc[0]
    end    = loc[1]
    lkey   = key.lower()
    mrow   = None
    v = False
    # Work backwards, want to find the latest incarnation of this
    for row in range(end-1, start, -1):
        line = view.getLine(row)
        m = re.search(key,line)
        if(m != None):
            mrow = row
            break
    if(mrow != None):
        pt = view.text_point(mrow, 0)
        rw = view.line(pt)
        #view.run_command("org_internal_erase", {"start": rw.begin(), "end": (rw.end()+1)})
        #drawer['loc'] = (start,end-1)
        v = re.sub(key,"",line).strip()
    return v

# Removes all instances of a named property from the drawer.
def RemoveAllInstances(view, node, key):
    while(RemoveProperty(view, node, key)):
        pass

# Removes all instances of a named logbook entry from the drawer.
def RemoveAllLogbookInstances(view, node, key):
    while(RemoveLogbook(view, node, key)):
        pass

# Insert a new drawer into the node.
r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Drawer
    Command: =org_insert_drawer=  Keys: none

    Asks for a name and adds an empty =:NAME:= ... =:END:= drawer to the
    heading at the cursor, unless it already has one of that name.
EDOC */
"""


class OrgInsertDrawerCommand(sublime_plugin.TextCommand):
    def run(self,edit):
        self.view.window().show_input_panel(
                    "Drawer Name:",
                    "",
                    self.createDrawer, None, None)

    def createDrawer(self, drawer):
        if(not drawer):
            return
        node = db.Get().AtInView(self.view)
        if(node and node.level > 0):
            InsertDrawerIfNotPresent(self.view, node, ":" + drawer + ":")

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Property Drawer
    Command: =org_insert_property_drawer=  Keys: none

    Adds an empty =:PROPERTIES:= drawer to the heading at the cursor when
    it has none.
EDOC */
"""


class OrgInsertPropertyDrawerCommand(sublime_plugin.TextCommand):
    def run(self,edit):
        node = db.Get().AtInView(self.view)
        if(node and node.level > 0):
            InsertDrawerIfNotPresent(self.view, node, ":PROPERTIES:")

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Property
    Command: =org_insert_property=  Keys: none
    Args: =name=, =value= (skip the prompts), =onDone=.

    Asks for a property name and a value and sets it in the heading's
    =:PROPERTIES:= drawer (the drawer is created when missing, an existing
    value replaced).
EDOC */
"""


class OrgInsertPropertyCommand(sublime_plugin.TextCommand):
    def run(self,edit,onDone=None,name=None,value=None):
        self.onDone=onDone
        if name != None:
            self.pname = name
            if value != None:
                self.prop = value
                self.createPropertyV2(self.prop)
            else:
                self.createProperty(name)
        else:
            self.view.window().show_input_panel(
                        "Property Name:",
                        "",
                        self.createProperty, None, None)

    def createProperty(self, prop):
        if(not prop):
            return
        self.pname = prop
        self.view.window().show_input_panel(
                    "Property Value:",
                    "",
                    self.createPropertyV2, None, None)

    def createPropertyV2(self, prop):
        if(not prop):
            return
        node = db.Get().AtInView(self.view)
        if(node and node.level > 0):
            UpdateProperty(self.view, node, self.pname, prop, self.onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Created Property
    Command: =org_insert_created_property=  Keys: none
    Args: =onDone=.

    Sets the heading's =Created= property to the current time as an
    inactive timestamp (=[2026-10-11 Sun 14:03]=).
EDOC */
"""


class OrgInsertCreatedPropertyCommand(sublime_plugin.TextCommand):
    def run(self,edit,onDone=None):
        self.onDone=onDone
        now = datetime.datetime.now()
        toInsert = orgdate.OrgDate.format_clock(now, False)
        self.view.run_command("org_insert_property", {"name": "Created","value": toInsert, "onDone":onDone})


r"""
/* SDOC: Reference::Commands::Editing
*** Org Insert Effort
    Command: =org_insert_effort=  Keys: none
    Args: =onDone=.

    Asks for an effort estimate (offering the current =EFFORT= value) in
    org duration form (=1d 3h=, =2h=, =45min=, =1:30=). A plain number gets
    the unit from =defaultEffortEstimateUnit=. Stores it, normalised, in
    the heading's =Effort= property. The agenda and timesheets read it.

    Settings: =defaultEffortEstimateUnit=.
EDOC */
"""


class OrgInsertEffortCommand(sublime_plugin.TextCommand):
    def run(self,edit,onDone=None):
        self.onDone=onDone
        node = db.Get().AtInView(self.view)
        if(node and node.level > 0):
            v = GetProperty(self.view, node, "EFFORT")
            if(not v):
                v = ""
            self.view.window().show_input_panel(
                    "Effort:",
                    v,
                    self.createProperty, None, None)

    def createProperty(self, prop):
        if(not prop):
            return
        isint = False
        try:
            i = int(prop)
            isint = True
        except:
            pass
        if(isint):
            inc = sets.Get("defaultEffortEstimateUnit","d")
            prop = prop + inc
        d = dur.OrgDuration.Parse(prop)
        if(d):
            node = db.Get().AtInView(self.view)
            if(node and node.level > 0):
                UpdateProperty(self.view, node, "Effort", str(d), self.onDone)
        else:
            log.error("Could not generate effort, please use org duration formation for your estimate")
            view.set_status("Error: ", prop + "is not a valid effort, please use org duration formation 1y 1d 1h 1min")
            evt.EmitIf(self.onDone)

r"""
/* SDOC: Reference::Commands::Editing
*** Org Create Heading Id
    Command: =org_create_heading_id=  Keys: none
    Args: =onDone=.

    Gives the heading at the cursor an =ID= property with a new uuid, when
    it has none. Links and remote table references can then find it.
EDOC */
"""


r"""
/* SDOC: Reference::Settings::Editing
*** defaultEffortEstimateUnit
    Default: =d= (not in the settings file).

    The unit added to an effort typed as a bare number in Org Insert
    Effort: =d= days, =h= hours, =min= minutes, =y= years.
EDOC */
"""


class OrgCreateHeadingIdCommand(sublime_plugin.TextCommand):
    def run(self,edit,onDone=None):
        self.onDone=onDone
        node = db.Get().AtInView(self.view)
        if(node and node.level > 0):
            v = GetProperty(self.view, node, "ID")
            if(not v):
                value = uuid.uuid4()
                UpdateProperty(self.view, node, "ID", str(value), self.onDone)
