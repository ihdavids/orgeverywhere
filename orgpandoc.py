
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
import OrgEverywhere.orgutil.temp as tf
import logging
import sys
import traceback 
import OrgEverywhere.orgfolding as folding
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgcapture as capture
import OrgEverywhere.orgproperties as props
import yaml
import sys
import subprocess

log = logging.getLogger(__name__)

DEFAULT_COMMANDS = dict(
	# Standard universal can opener for OSX.
	darwin=['open'],
	win32=['cmd', '/C'],
	linux=['xdg-open'],
)

def Execute(op):
	cmd = DEFAULT_COMMANDS[sys.platform]
	cmd = op
	print(str(cmd))
	if sys.platform != 'win32':
		process = subprocess.Popen(
			cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	else:
		process = subprocess.Popen(
			cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
	stdout, stderr = process.communicate()
	if stdout:
		stdout = str(stdout, sys.getfilesystemencoding())
		sublime.status_message(stdout)
		print(stdout)
	if stderr:
		stderr = str(stderr, sys.getfilesystemencoding())
		sublime.error_message(stderr)

def GetCssForHtmlExport(style):
	# Include user created styles!
	css = os.path.join(sublime.packages_path(),"OrgEverywhere", "pandoc", style + ".css")
	return css

def GetHeaderData(style):
	inHeader = os.path.join(sublime.packages_path(),"OrgEverywhere", "pandoc", style + "_inheader.html")
	if(os.path.isfile(inHeader)):
		return ["-H", inHeader]

def Pandoc():
	return sets.Get("PandocPath",r'C:\Program Files\Pandoc\pandoc.exe')

# Export the entire file using pandoc 
r"""
/* SDOC: Reference::Commands::Exporting
*** Org Pandoc File To Html
    Command: =org_export_file_as_html=  Keys: none

    Exports the file to =NAME.html= next to it with pandoc
    (=pandoc -s -c STYLE.css [-H STYLE_inheader.html] -o NAME.html NAME.org=).
    The style comes from =PandocStyle=; its css and in-header file are taken
    from =Packages/OrgEverywhere/pandoc/= (only =blocky= ships). The file is
    exported as saved on disk. Pandoc's errors are shown in a dialog.

    Settings: =PandocPath=, =PandocStyle=.
EDOC */
"""


class OrgExportFileAsHtmlCommand(sublime_plugin.TextCommand):
	def run(self,edit):
		style    = sets.Get("PandocStyle","blocky")
		css      = GetCssForHtmlExport(style)
		outFile  = tf.GetViewFileAs(self.view, ".html")
		cmd     = [Pandoc(), "-s","-c",css]
		inHeader = GetHeaderData(style)
		if(inHeader):
			cmd.extend(inHeader)
		cmd.extend(["-o", outFile, self.view.file_name()])
		print(cmd)
		Execute(cmd)
		#Execute([r'C:\Program Files\Pandoc\pandoc.exe', '-h'])


r"""
/* SDOC: Reference::Commands::Exporting
*** Org Pandoc Subtree To Html
    Command: =org_export_subtree_as_html=  Keys: none

    Meant to export the heading at the cursor with pandoc (copied to a
    temporary file, with a =#+TITLE:= added when it has none). Currently
    broken: the command line is lost before pandoc runs, so it fails with
    an error. Use Org Export Subtree As Html instead.
EDOC */
"""


r"""
/* SDOC: Reference::Settings::Exporting
*** Pandoc settings
    - =PandocPath= :: default =C:\Program Files\Pandoc\pandoc.exe=. The
      pandoc executable.
    - =PandocStyle= :: default =blocky=. The style whose
      =pandoc/STYLE.css= (and =STYLE_inheader.html=) are passed to pandoc.
EDOC */
"""


class OrgExportSubtreeAsHtmlCommand(sublime_plugin.TextCommand):
	def run(self,edit):
		n = db.Get().AtInView(self.view)
		s = self.view.text_point(n.start_row,0)
		e = self.view.line(self.view.text_point(n.end_row,0)).end()
		r = sublime.Region(s,e)
		ct = self.view.substr(r)
		start = ""
		if "#+TITLE:" not in ct:
			start = "#+TITLE: " + os.path.splitext(os.path.basename(self.view.file_name()))[0]

		tempFile = tf.CreateTempFileFromRegion(self.view, r, ".org", start)
		outFile  = tf.GetViewFileAs(self.view, ".html")
		style = sets.Get("PandocStyle","blocky")
		css = GetCssForHtmlExport(style)
		cmd     = [Pandoc(), "-s","-c",css]
		inHeader = GetHeaderData(style)
		if(inHeader):
			cmd.extend(inHeader)
		cmd = cmd.extend(["-o", outFile, tempFile])	
		Execute(cmd)
# pandoc -s -o output.html input.txt