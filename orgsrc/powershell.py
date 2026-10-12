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

# Python Babel Mode

def FormatText(txt):
    if(isinstance(txt,str)):
        txt = txt.strip()
        return "'" + txt + "'"
    return txt


def HandleValue(cmd):
    if(cmd.CheckResultsFor('value')):
        out = "function OrgEverywherePowershellWrapperFunction {\n"
        outs = cmd.source.split('\n')
        for o in outs:
            out += " " + o + "\n"
        out += '}\n'
        out += "$orgEverywhereWrapperVar = OrgEverywherePowershellWrapperFunction\nWrite-Host RETURNVALUESTART\nWrite-Host $orgEverywhereWrapperVar\nWrite-Host RETURNVALUEEND\n"
        cmd.source = out

def PreProcessSourceFile(cmd):
    HandleValue(cmd)
    var = cmd.params.Get('var',None)
    if(var):
        out = ""
        for k in var:
            v = var[k]
            if(isinstance(v,src.TableData)):
                out += "$" + str(k) + " = @("
                fr = True
                for r in v.ForEachRow():
                    first = True
                    out += '(' if fr else ",("
                    fr = False
                    for c in v.ForEachCol():
                        txt = v.GetCell(r,c)
                        out += (',' if not first else "") + str(FormatText(txt))
                        first = False
                    out += ')'
                out += ")\n"
            if(isinstance(v,fml.TableDef)):
                out += "$" + str(k) + " = @("
                fr = True
                for r in v.ForEachRow():
                    first = True
                    out += '(' if fr else ",("
                    fr = False
                    for c in v.ForEachCol():
                        txt = fml.Cell(r,c,v).GetVal()
                        out += (',' if not first else "") + str(FormatText(txt))
                        first = False
                    out += ')'
                out += ")\n"
            elif(isinstance(v,lst.ListData)):
                out += "$" + str(k) + " = @("
                first = True
                for txt in v:
                    out += (',' if not first else "") + str(FormatText(txt))
                    first = False
                out += ")\n"
            elif(isinstance(v,int) or isinstance(v,float) or (isinstance(v,str) and util.numberCheck(v))):
                out += "$" + str(k) + " = " + str(v) + "\n"
            elif(isinstance(v,str)):
                out += "$" + str(k) + " = \'" + str(v) + "\'\n"
        cmd.source = out + cmd.source


def Extension(cmd):
    return ".ps1"

def LineCommentPrefix():
    return "#"
    
# Actually do the work, return an array of output.
def Execute(cmd,sets):
    commandLine = [r"C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe", "-ExecutionPolicy", "Unrestricted", cmd.filename]
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
** powershell
    Language: =powershell=  Tangles to: =.ps1=

    Windows only: runs the block as a =.ps1= file with the 32 bit Windows
    PowerShell at a fixed path
    (=C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe=,
    =-ExecutionPolicy Unrestricted=). =:var= tables become arrays of arrays
    (=@(...)=), lists arrays, numbers and strings variables. With
    =:results value= the block runs as a function and its output value is
    the result.
EDOC */
"""
