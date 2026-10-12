"""Server side exporting, reformatting, tangling, dynamic blocks and source
block execution for the file in the current view.

Each one saves the buffer and waits for the server to read it first
(orgs.sync_file), so the server works on what you see. Whatever the server
writes to the org file comes back into a clean buffer by itself.
"""
import logging
import os

import sublime
import sublime_plugin

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgs.session as orgs
from OrgEverywhere.orgs.client import OrgsError

log = logging.getLogger(__name__)

# The file extension each exporter's output wants. Anything not listed is
# html when it looks like html and text otherwise.
EXPORT_EXT = {
    "html": ".html",
    "revealjs": ".html",
    "reveal": ".html",
    "impressjs": ".html",
    "impress": ".html",
    "deckjs": ".html",
    "gantt": ".html",
    "mindmap": ".html",
    "latex": ".tex",
    "tex": ".tex",
    "beamer": ".tex",
    "mermaid": ".mmd",
    "markdown": ".md",
    "md": ".md",
    "gfm": ".md",
    "rst": ".rst",
    "text": ".txt",
    "ascii": ".txt",
    "json": ".json",
    "csv": ".csv",
    "plantuml": ".puml",
    "dot": ".dot",
}
SLIDE_EXPORTERS = ("revealjs", "reveal", "impressjs", "impress", "deckjs")
PDF = "pdf"


def ExportExt(kind, text):
    ext = EXPORT_EXT.get(kind.lower())
    if ext:
        return ext
    head = (text or "").lstrip()[:200].lower()
    if head.startswith("<!doctype html") or head.startswith("<html") or "<head" in head:
        return ".html"
    return ".txt"


def ExportPath(filename, kind, text):
    base = os.path.splitext(filename)[0]
    ext = ExportExt(kind, text)
    if ext == ".txt" or (ext == ".html" and kind.lower() != "html"):
        # Keep a slide deck and the plain html export of the same file apart.
        return base + "." + kind.lower() + ext
    return base + ext


def ExportChoices(names):
    names = list(names or [])
    if PDF not in names and ("latex" in names or "pdf" in names):
        names.append(PDF)
    return names


def ThemesFor(cl, kind):
    if kind == "html":
        return cl.themes("html")
    if kind in SLIDE_EXPORTERS:
        return cl.themes("slides")
    return []


def DoExport(cl, view, kind, theme=None):
    """(worker thread) Export the view's file; returns the output path."""
    fn = orgs.sync_file(view)
    if not fn:
        raise OrgsError("save the file first")
    if kind == PDF:
        try:
            data = cl.pdf(fn, refresh=True)
        except OrgsError as e:
            body = e.body.decode("utf-8", "replace") if isinstance(e.body, bytes) else (e.body or str(e))
            raise OrgsError(str(body).strip())
        if not data or not data.startswith(b"%PDF"):
            msg = (data or b"").decode("utf-8", "replace").strip() or "no pdf came back"
            raise OrgsError(msg)
        out = os.path.splitext(fn)[0] + ".pdf"
        with open(out, "wb") as f:
            f.write(data)
        return out
    opts = {"theme": theme} if theme else {}
    try:
        text = cl.export(kind, fn, **opts)
    except OrgsError as e:
        raise OrgsError(str(e).strip())
    out = ExportPath(fn, kind, text)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return out


def OpenExport(window, path):
    import webbrowser
    ext = os.path.splitext(path)[1].lower()
    if ext in (".html", ".pdf", ".svg"):
        webbrowser.open("file://" + path)
    else:
        window.open_file(path)


r"""
/* SDOC: Reference::Commands::Exporting
*** Org: orgs Export File
    Command: =org_orgs_export=  Keys: none  Args: =kind=, =theme=, =open=

    Exports the current file with one of the orgs server's exporters (the
    ones in =server.exporters= of its =orgs.yaml=: html, latex, reveal.js,
    impress.js, mermaid, markdown...) and writes the result next to the
    file. A quick panel lists the exporters, plus =pdf= when the server has
    latex; =kind= skips it (the palette's "Org: orgs Export File as PDF"
    passes ="pdf"=). For html and slide exporters with more than one theme
    you pick a theme, or the file's own; =theme= skips that.

    The output is =file.html=, =file.tex=, =file.md=, =file.pdf=...; slide
    decks and other html exporters get =file.KIND.html= so they do not
    overwrite the plain html. The buffer is saved and synced to the server
    first. Afterwards, depending on =open= (default =orgsExportOpen=):
    ="ask"= offers to open it, ="always"= opens it (html, pdf and svg in the
    browser, anything else in Sublime), ="never"= only reports the path.
    Needs the orgs server and a buffer with a file.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Exporting
*** orgsExportOpen
    Default: ="ask"=

    What Org: orgs Export File does with the file it wrote: ="ask"= offers
    to open it, ="always"= opens it (html and pdf in the browser, the rest
    in Sublime), ="never"= just says where it is.
EDOC */
"""

class OrgOrgsExportCommand(sublime_plugin.TextCommand):
    """Export this file with one of the server's exporters (html, latex,
    reveal.js, mermaid, pdf, ...) and write the result next to it."""
    def run(self, edit, kind=None, theme=None, open=None):
        self.open = sets.Get("orgsExportOpen", "ask") if open is None else open
        if kind:
            self.PickTheme(kind, theme)
            return

        def got(names):
            names = ExportChoices(names)
            if not names:
                sublime.status_message("orgs: the server has no exporters configured (server.exporters in orgs.yaml)")
                return
            self.view.window().show_quick_panel(names, lambda i: i >= 0 and self.PickTheme(names[i], None))
        orgs.run(lambda cl: cl.exporters(), got, what="exporters")

    def PickTheme(self, kind, theme):
        if theme is not None or kind == PDF:
            self.Export(kind, theme)
            return
        window = self.view.window()

        def got(themes):
            if not themes or len(themes) < 2:
                self.Export(kind, None)
                return
            items = ["(the file's own theme)"] + themes
            window.show_quick_panel(items, lambda i: i >= 0 and self.Export(kind, None if i == 0 else themes[i - 1]))
        orgs.run(lambda cl: ThemesFor(cl, kind), got, what="themes")

    def Export(self, kind, theme):
        view = self.view
        window = view.window()

        def done(path):
            sublime.status_message("orgs: exported " + path)
            if self.open == "always" or self.open is True:
                OpenExport(window, path)
            elif self.open == "ask":
                items = [["Open " + os.path.basename(path), "in the browser or viewer" if
                          os.path.splitext(path)[1] in (".html", ".pdf") else "in Sublime"], ["Done", path]]
                window.show_quick_panel(items, lambda i: i == 0 and OpenExport(window, path))
        orgs.run(lambda cl: DoExport(cl, view, kind, theme), done, what="export " + kind)

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())


r"""
/* SDOC: Reference::Commands::Editing
*** Reformat the file with the orgs server
    Command: =org_orgs_reformat_file=  Keys: none  (no palette entry)

    Has the server rewrite the current file in its canonical layout
    (aligned tags, property drawers, planning lines); the buffer reloads
    with the result. The buffer is saved first and the file on disk must
    then be exactly the buffer, or it refuses. Needs the orgs server.
EDOC */
"""

class OrgOrgsReformatFileCommand(sublime_plugin.TextCommand):
    """Have the server rewrite this file in its canonical layout (aligned
    tags, property drawers, planning lines). The buffer reloads."""
    def run(self, edit):
        view = self.view

        def work(cl):
            # The server rewrites the whole file: it must be exactly the buffer.
            fn = orgs.file_text_current(cl, view)
            return cl.reformat([fn])

        orgs.run(work, lambda res: sublime.status_message("orgs: " + ((res or {}).get("Msg") or "reformatted")),
                 what="reformat")

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())


def TangleSummary(res):
    files = (res or {}).get("files") or []
    if not files:
        return "nothing to tangle (no source blocks with :tangle)"
    return "tangled {0}: {1}".format(
        "1 file" if len(files) == 1 else "{0} files".format(len(files)),
        ", ".join("{0} ({1} lines)".format(os.path.basename(f.get("filename", "")), f.get("lines", 0))
                  for f in files))


r"""
/* SDOC: Reference::Commands::Source Blocks
*** Org: orgs Tangle File
    Command: =org_orgs_tangle=  Keys: none  Args: =write= (default =true=)

    Tangles the current file's source blocks (those with =:tangle=) on the
    server, which writes the tangled files; the status bar lists each file
    and its line count, or says there was nothing to tangle. The buffer is
    saved and synced first. Needs the orgs server.
EDOC */
"""

class OrgOrgsTangleCommand(sublime_plugin.TextCommand):
    """Tangle this file's source blocks on the server and write the results."""
    def run(self, edit, write=True):
        view = self.view

        def work(cl):
            fn = orgs.sync_file(view)
            if not fn:
                raise OrgsError("save the file first")
            res = cl.tangle(fn, write=write)
            if isinstance(res, dict) and res.get("error"):
                raise OrgsError(res["error"])
            return res

        orgs.run(work, lambda res: sublime.status_message("orgs: " + TangleSummary(res)), what="tangle")

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())


def DblocksSummary(res):
    n = sum(len(f.get("Blocks") or []) for f in (res or {}).get("Files") or [])
    return (res or {}).get("Msg") or "{0} block(s) refreshed".format(n)


r"""
/* SDOC: Reference::Commands::Dynamic Blocks
*** Org: orgs Update Dynamic Blocks
    Command: =org_orgs_update_dblocks=  Keys: none  Args: =all= (default =true=)

    Has the server refresh the dynamic blocks (clocktable, columnview, ...)
    of the current file: all of them, or with =all= =false= (palette: "Org:
    orgs Update Dynamic Block at Cursor") only the one at the cursor. The
    server rewrites the file and the buffer reloads. The buffer is saved
    first and must then match the file on disk exactly. Needs the orgs
    server.
EDOC */
"""

class OrgOrgsUpdateDblocksCommand(sublime_plugin.TextCommand):
    """Refresh dynamic blocks (clocktable, columnview, ...) on the server:
    every block in the file, or (all=False) the one at the cursor."""
    def run(self, edit, all=True):
        view = self.view
        row = view.rowcol(view.sel()[0].begin())[0] if view.sel() else 0

        def work(cl):
            # The server rewrites the whole file: it must be exactly the buffer.
            fn = orgs.file_text_current(cl, view)
            return cl.dblocks_update(fn, line=row, all=all)

        orgs.run(work, lambda res: sublime.status_message("orgs: " + DblocksSummary(res)), what="dynamic blocks")

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())


r"""
/* SDOC: Reference::Commands::Source Blocks
*** Org: orgs Execute Source Block
    Command: =org_orgs_babel_exec=  Keys: none  Args: =all= (default =false=)

    Runs the source block or =#+CALL:= line at the cursor on the server and
    writes its results into the file; with =all= =true= (palette: "Org:
    orgs Execute All Source Blocks") every block in the file. The buffer is
    saved first and must then match the file on disk exactly; the buffer
    reloads with the results. The server refuses unless =babel.enable= is
    set in its =orgs.yaml=; that refusal is shown in a dialog. Needs the
    orgs server.
EDOC */
"""

class OrgOrgsBabelExecCommand(sublime_plugin.TextCommand):
    """Run the source block (or #+CALL) at the cursor on the server, or every
    one in the file (all=True). The server refuses unless babel.enable is set
    in its orgs.yaml."""
    def run(self, edit, all=False):
        view = self.view
        row = view.rowcol(view.sel()[0].begin())[0] if view.sel() else 0

        def work(cl):
            # The server rewrites the whole file: it must be exactly the buffer.
            fn = orgs.file_text_current(cl, view)
            return cl.babel_exec(fn, row, all=all)

        def failed(e):
            msg = str(e)
            if "babel" in msg and "enable" in msg:
                sublime.message_dialog("The orgs server will not run source blocks:\n\n" + msg)
            elif orgs.connected():
                orgs.report(e, "execute")
        orgs.run(work, lambda res: sublime.status_message("orgs: " + ((res or {}).get("Msg") or "done")),
                 on_error=failed, what="execute")

    def is_enabled(self, **kw):
        return orgs.connected() and bool(self.view.file_name())
