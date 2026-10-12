import json
import sublime
import sublime_plugin


def sortMessages(x):
    r = x
    xs = x.split('.')
    if(len(xs) <= 1):
        return 0
    m = 1
    c = 0
    for i in range(len(xs)-1,-1,-1):
        x = xs[i]
        c = c+(int(x)*m)
        m *= 100
    return c

# ===================================================================================
r"""
/* SDOC: Reference::Commands::Misc
*** Org Show Worklog
    Command: =org_build_dev_docs=  Keys: none

    Opens a new org buffer with every release note
    (=messages/VERSION.org=), newest first: the change log.
EDOC */
"""


class OrgBuildDevDocsCommand(sublime_plugin.TextCommand):
    """Show the current worklog to the user"""
    def run(self, edit):
        view = sublime.active_window().new_file()
        view.set_syntax_file("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
        messData = sublime.load_resource("Packages/OrgEverywhere/messages.json")
        msgs = json.loads(messData)
        ks = msgs.keys()
        ks = list(ks)
        ks = sorted(ks, key=sortMessages, reverse=True)
        for n in ks:
            name = n + ".org"
            pdata = None
            try:
                pdata = sublime.load_resource("Packages/OrgEverywhere/messages/" + name)
            except:
                pass
            if(not pdata):
                continue
            pdata = pdata.replace("\r","")
            view.insert(edit,view.size(),"\n" + pdata + "\n")
        pass

# ===================================================================================
r"""
/* SDOC: Reference::Commands::Misc
*** Org Show TestFile / Org Show Table Tests / Org Show Source Tests
    Commands: =org_show_testfile=, =org_show_table_tests=,
    =org_show_source_block_tests=  Keys: none

    Each opens a copy of a sample org file that ships with the package
    (=tests/testfile.org=, =tests/tableunittests.org=,
    =tests/sourceunittests.org=) and asks where to save it. The table and
    source files double as worked examples of formulas and source blocks.
EDOC */
"""


class OrgShowTestfileCommand(sublime_plugin.TextCommand):
    """Show an org testfile to help users get started"""
    def run(self, edit):
        view = sublime.active_window().new_file()
        view.set_syntax_file("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
        data = sublime.load_resource("Packages/OrgEverywhere/tests/testfile.org")
        data = data.replace("\r","")
        view.insert(edit,view.size(),"\n" + data + "\n")
        view.run_command("save")

# ===================================================================================
class OrgShowTableTestsCommand(sublime_plugin.TextCommand):
    """Show the org everywhere table unit tests to help users get started"""
    def run(self, edit):
        view = sublime.active_window().new_file()
        view.set_syntax_file("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
        data = sublime.load_resource("Packages/OrgEverywhere/tests/tableunittests.org")
        data = data.replace("\r","")
        view.insert(edit,view.size(),"\n" + data + "\n")
        view.run_command("save")

# ===================================================================================
class OrgShowSourceBlockTestsCommand(sublime_plugin.TextCommand):
    """Show the org everywhere babel unit tests to help users get started"""
    def run(self, edit):
        view = sublime.active_window().new_file()
        view.set_syntax_file("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
        data = sublime.load_resource("Packages/OrgEverywhere/tests/sourceunittests.org")
        data = data.replace("\r","")
        view.insert(edit,view.size(),"\n" + data + "\n")
        view.run_command("save")
