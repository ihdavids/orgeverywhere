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
import OrgEverywhere.orgextension as ext
import OrgEverywhere.pymitter as evt
from   OrgEverywhere.orgplist import *
import importlib

log = logging.getLogger(__name__)


RE_END = re.compile(r"^\s*\#\+(END|end)[:]")
RE_DYN_BLOCK = re.compile(r"^\s*\#\+(BEGIN|begin)[:]\s+(?P<name>[^: ]+)\s*")
RE_FN_MATCH = re.compile(r"\s+[:]([a-zA-Z][a-zA-Z0-9-_]+)\s+(([^ ()]+)|([(][^)]+[)]))")


def IsDynamicBlock(view):
	line = view.getLine(view.curRow())
	return RE_DYN_BLOCK.search(line) or RE_END.search(line)

r"""
/* SDOC: Reference::Commands::Dynamic Blocks
*** Org Exec Dynamic Block
    Command: =org_execute_dynamic_block=  Keys: =alt+o c c= (through Org Recalc,
    on the =#+BEGIN:= or =#+END:= line); vi command mode =space c c=

    With the cursor on the =#+BEGIN: NAME :param value...= line of a
    dynamic block, runs the block's writer and replaces everything between
    =#+BEGIN:= and =#+END:= with its output, indented to the heading.
    Writers: =clocktable=, =columnview=, =insertdatetime= (see Reference >
    Dynamic Blocks), plus any =NAME.py= in =Packages/User/orgdynamic/=.
    Run it on the =#+BEGIN:= line: on the =#+END:= line (which Org Recalc
    also accepts) the region comes out empty and the =#+END:= line itself
    is overwritten.
    This updates the block locally; Org: orgs Update Dynamic Blocks has the
    orgs server do it instead.
EDOC */
"""


r"""
/* SDOC: Reference::Dynamic Blocks
** Writing a dynamic block
    A dynamic block writer is a python module in =Packages/User/orgdynamic/=
    (or the bundled =orgdynamic/=), named as the block:

    - =Execute(view, params)= (required) returns the block's new lines as a
      list of strings. =params= holds the =:name value= pairs of the
      =#+BEGIN:= line (=params.Get(name, default)=, =GetInt=, =GetList=).
    - =PostExecute(view, params, region)= (optional) runs after the text is
      in the buffer, for example to align a table.
EDOC */
"""


class OrgExecuteDynamicBlockCommand(sublime_plugin.TextCommand):
	def on_replaced(self):
		if(hasattr(self.curmod,"PostExecute")):
			self.curmod.PostExecute(self.view, self.params, self.region)

	def run(self, edit):
		view = self.view
		at = view.sel()[0]
		if(view.match_selector(at.begin(),'orgmode.fence.dynamicblock')):
			# Okay we have a dynamic block, now we need to know where it ends.
			start = at.begin()
			end   = None
			erow = view.endRow()
			row  = view.curRow()
			for rw in range(row,erow+1):
				line = view.substr(view.line(view.text_point(rw,0)))
				if(RE_END.search(line)):
					end = rw
					break
			if(not end):
				log.debug("Could not locate #+END: tag")
				return
			# Okay now we have a start and end to build a region out of.
			# time to run a command and try to get the output.
			dynamic = ext.find_extension_modules('orgdynamic', ["insertdatetime", "clocktable", "columnview"])
			line = view.substr(view.line(start))
			m = RE_DYN_BLOCK.search(line)
			if(not m):
				log.error("FAILED TO PARSE DYNAMIC BLOCK: " + line)
				return
			fnname = m.group('name')
			#log.debug("DYN NAME: " + fnname)
			params = PList.createPList(line[len(m.group(0)):])
			# Now find me that function!
			if(fnname not in dynamic):
				log.error("Function not found in dynamic folder! Cannot execute!")
				return
			# Run the "writer"
			if(hasattr(dynamic[fnname],"Execute")):
				self.curmod = dynamic[fnname]
				self.params = params
				outputs = self.curmod.Execute(view, params)
				#log.debug("OUTPUT: " + str(outputs))

			# Okay now time to replace the contents of the block
			s = view.text_point(row+1,0)
			e = view.text_point(end,0)
			self.region = sublime.Region(s,e)
			# Reformat adding indents to each line!
			# No bad formatting allowed!
			n = db.Get().AtInView(view)
			level = n.level
			indent = "\n"+ (" " * level) + " "
			#outputs = output.split('\n')
			output = indent.join(outputs)
			print(output)
			self.view.run_command("org_internal_replace", {"start": s, "end": e, "text": (" " * level + " ") + output+"\n","onDone": evt.Make(self.on_replaced)})
		else:
			log.error("NOT in A DynamicBlock, nothing to run")
