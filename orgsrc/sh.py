import sublime
import sublime_plugin
import sys
import io
import re
import logging
import subprocess, os
import threading, time, signal
import OrgEverywhere.orgtableformula as fml
import OrgEverywhere.orgsourceblock as src
import OrgEverywhere.orgparse.date as odate
import OrgEverywhere.orgutil.util as util
import OrgEverywhere.orglist as lst

# Bash Babel Mode

def FormatText(txt):
    if(isinstance(txt,str)):
        txt = txt.strip()
        #return "'" + txt + "'"
    return txt


def HandleValue(cmd):
    if(cmd.CheckResultsFor('value')):
        out = "OrgEverywhereBashWrapperFunction () {\n"
        outs = cmd.source.split('\n')
        for o in outs:
            out += " " + o + "\n"
        out += '}\n'
        out += "OrgEverywhereBashWrapperFunction\norgEverywhereWrapperVar=$?\necho RETURNVALUESTART\necho ${orgEverywhereWrapperVar}\necho RETURNVALUEEND\n"
        cmd.source = out

def PreProcessSourceFile(cmd):
    HandleValue(cmd)
    var = cmd.params.Get('var',None)
    if(var):
        out = ""
        for k in var:
            v = var[k]
            if(isinstance(v,src.TableData)):
                out += str(k) + "=$(cat <<'END_HERE_DOC'\n"
                for r in v.ForEachRow():
                    first = True
                    for c in v.ForEachCol():
                        txt = v.GetCell(r,c)
                        out += ('\t' if not first else "") + str(FormatText(txt))
                        first = False
                    out += '\n'
                out += "END_HERE_DOC\n)\n"
            elif(isinstance(v,lst.ListData)):
                out += str(k) + "=("
                first = True
                for txt in v:
                    out += (' ' if not first else "") + str(FormatText(txt))
                    first = False
                out += ")\n"
            elif(isinstance(v,int) or isinstance(v,float) or (isinstance(v,str) and util.numberCheck(v))):
                out += str(k) + "=" + str(v) + "\n"
            elif(isinstance(v,str)):
                out += str(k) + "=\'" + str(v) + "\'\n"
        cmd.source = out + cmd.source


def Extension(cmd):
    return ".sh"

def LineCommentPrefix():
    return "#"

def GetCommandLine(cmd,sets):
    filename = cmd.filename
    if(sublime.platform() == 'windows'):
        bashLocation = sets.Get("bashPath",r"C:\\Windows\\System32\\wsl.exe")
        filename = filename.replace("C:\\","/mnt/c/").replace("c:\\","/mnt/c/").replace("\\","/")
    else:
        bashLocation = sets.Get("bashPath",r"/bin/bash")
    commandLine = [bashLocation, filename]
    return commandLine

    
# Actually do the work, return an array of output.
def Execute(cmd,sets):
    commandLine = GetCommandLine(cmd,sets)
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    except:
        startupinfo = None
    # cwd=working_dir, env=my_env,
    cwd = os.path.join(sublime.packages_path(),"User") 
    popen = subprocess.Popen(commandLine, universal_newlines=True, cwd=cwd, startupinfo=startupinfo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    #popen.wait()
    (o,e) = popen.communicate()
    if(cmd.CheckResultsFor('value')):
        out = []
        outs = o.split('\n')
        seenStart = False
        for oo in outs:
            if(oo.strip() == "RETURNVALUESTART"):
                seenStart = True
                continue
            if(oo.strip() == "RETURNVALUEEND"):
                seenStart = False
                continue
            if(seenStart):
                out.append(oo)
        return out + e.split('\n')

    return o.split('\n') + e.split('\n')


# Run after results are in the buffer. We can do whatever
# Is needed to the buffer post execute here.
def PostExecute(cmd):
    pass


r"""
/* SDOC: Reference::Source Languages
** sh (bash)
    Language: =sh=, alias =bash=  Tangles to: =.sh=

    Writes the block to a temporary =.sh= file and runs it with =bashPath=
    (default =/bin/bash=; on Windows =C:\Windows\System32\wsl.exe=, with the
    file path rewritten to =/mnt/c/...=). Runs in =Packages/User=.
    =:var= numbers and strings become shell variables, lists become arrays,
    tables become a tab separated here-document string. With
    =:results value= the block runs as a function and its exit status is
    the result.

    Settings: =bashPath=.
EDOC */
"""


r"""
/* SDOC: Reference::Settings::Source Blocks
*** Language program paths
    Where the source block languages find their programs. Defaults are the
    code's (Windows paths); set them for your system.

    - =bashPath= :: =/bin/bash= (Windows: =C:\Windows\System32\wsl.exe=). sh / bash blocks.
    - =nodejsPath= :: =C:\Program Files\nodejs\node.exe=. javascript blocks.
    - =perlPath= :: =C:\Program Files\Git\usr\bin\perl.exe=. perl blocks.
    - =dotnetPath= :: =C:\Program Files\dotnet\dotnet.exe=. csharp blocks.
    - =gnuplot= :: =C:\Program Files\gnuplot\bin\gnuplot.exe=. gnuplot blocks
      and Org Plot Table.
    - =graphviz= :: none. The graphviz executable (=dot=).
    - =plantuml= :: the settings file ships a sample path
      (=D:\Build\.imacs\plantuml.jar=); set it to your plantuml.jar.
    - =ditaa= :: none. The ditaa jar.
    - =java= :: =java=. The java used for ditaa (plantuml always runs
      =java= from the PATH).
    - =mermaid= :: =mmdc=, found on the PATH. Set it to the full path of
      your =mmdc= when it is not on the PATH.
    - =beancountPath= :: read by the unfinished beancount handler only.
EDOC */
"""
