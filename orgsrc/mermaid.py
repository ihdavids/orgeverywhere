import sublime
import sublime_plugin
import sys
import io
import re
import logging
import subprocess, os
import threading, time, signal
from shutil import copyfile
import OrgEverywhere.asettings as sets

# Mermaid Babel Mode
def Extension(cmd):
	return ".mmd"

# Actually do the work, return an array of output.
def Execute(cmd,sets):
	mmdc = sets.Get("mermaid",'mmdc')
	if(mmdc == None):
		print("ERROR: cannot find mmdc file. Please setup the mermaid key in your settings file")
		return ["ERROR - missing mermaid file"]
	cmd.output = cmd.params.Get('file','diagram.png')
	outpath     = os.path.dirname(cmd.filename)
	sourcepath = os.path.dirname(cmd.sourcefile)
	convertFile = os.path.join(outpath,os.path.splitext(os.path.basename(cmd.filename))[0] + ".png")
	destFile    = os.path.join(sourcepath,cmd.output)
	basedir = os.path.dirname(mmdc)
	if sys.platform == 'win32':
		commandLine = ["powershell",mmdc, "-i", cmd.filename, "-o", '"' + destFile + '"']
	else:
		commandLine = [mmdc, "-i", cmd.filename, "-o", '"' + destFile + '"']
	
	print(str(commandLine))
	try:
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
	except:
		startupinfo = None
	# cwd=working_dir, env=my_env,
	os.makedirs(outpath, exist_ok=True)
	#cwd = os.path.join(sublime.packages_path(),"User") 
	cwd = basedir + "/../.."
	popen = subprocess.Popen(commandLine, universal_newlines=True, cwd=cwd, startupinfo=startupinfo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

	(o,e) = popen.communicate()
	
	out = o
	o = ""
	return o.split('\n') + e.split('\n')


# Run after results are in the buffer. We can do whatever
# Is needed to the buffer post execute here.
def PostExecute(cmd):
	pass

# Create one of these and return true if we should show images after a execution.
def GeneratesImages(cmd):
	return True


r"""
/* SDOC: Reference::Source Languages
** mermaid
    Language: =mermaid=  Tangles to: =.mmd=

    Runs the mermaid command line tool, =MMDC -i FILE -o OUTPUT= (through
    powershell on Windows), writing =:file= (default =diagram.png=)
    relative to the org file. The result is a link to the picture.

    Settings: =mermaid= (the path of =mmdc=; install it with
    =npm install @mermaid-js/mermaid-cli=).
EDOC */
"""
