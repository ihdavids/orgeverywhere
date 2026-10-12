"""Features the orgs server provides that OrgEverywhere had no equivalent of:
query search and stored queries, contacts and birthdays, the worg web ui,
updater plugins (jira), heading encryption, attachments, flashcards and new
files from server templates.

The views (habits, clock report, text search, history) are in orgsviews.py
and exporting, tangling and blocks in orgsexport.py.
"""
import logging
import os
import subprocess
import sys
import tempfile
import urllib.parse

import sublime
import sublime_plugin

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as orgsui
from OrgEverywhere.orgs.client import OrgsError, hash_path

log = logging.getLogger(__name__)


def _placeholder(kw, text):
    if text and int(sublime.version()) >= 4081:
        kw["placeholder"] = text
    return kw


def _quick(window, items, on_done, placeholder=None, on_highlight=None):
    kw = _placeholder({}, placeholder)
    if on_highlight:
        window.show_quick_panel(items, on_done, 0, 0, on_highlight, **kw)
    else:
        window.show_quick_panel(items, on_done, **kw)


def _open_with_os(path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # noqa: pyflakes does not know it
    else:
        subprocess.Popen(["xdg-open", path])


# ----------------------------------------------------------------------
# Query search and stored queries

NEW_QUERY = "New query..."


def SearchChoices(filters, stored):
    """Quick panel rows and the query each runs (None: ask for one)."""
    items = [[NEW_QUERY, "type a query: IsTodo() && HasTags(\"WORK\")"]]
    queries = [None]
    for q in stored or []:
        name = q.get("name") or q.get("Name") or ""
        query = q.get("query") or q.get("Query") or ""
        items.append(["Saved: " + name, query])
        queries.append(query)
    for name in sorted(filters or {}):
        items.append(["Filter: " + name, filters[name]])
        queries.append("{{" + name + "}}")
    return items, queries


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Query Search
    Command: =org_orgs_search=  Keys: none  Args: =query=, =edit= (default =false=)

    Searches every heading the server knows with the orgs query language
    (e.g. =IsTodo() && HasTags("WORK")=). Without arguments a quick panel
    offers "New query..." (an input panel, prefilled with the last query),
    your saved queries and the server's named filters (run as
    ={{FilterName}}=). The matches are listed in a quick panel; picking one
    opens it at its line. With =query= the search runs at once; with
    =edit= as well it is put in the input panel first.

    Needs the orgs server (disabled when not connected).
    Settings: =orgsPreviewOnHighlight=.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::orgs Server
*** orgs feature settings
    | Setting              | Default              | Effect                                                                      |
    |----------------------+----------------------+-----------------------------------------------------------------------------|
    | =orgsBirthdayDays=   | =60=                 | How many days ahead Org: orgs Upcoming Birthdays looks.                     |
    | =orgsWebUrl=         | =""=                 | The worg web ui url for Org: orgs Open Web UI; empty uses the server url.  |
    | =orgsDefaultUpdater= | ="jira"=             | The updater plugin offered by Org: orgs Update Heading.                     |
    | =orgsDrillQuery=     | =HasTags("drill")=   | The query offered for flashcards when no deck is chosen.                    |
EDOC */
"""

class OrgOrgsSearchCommand(sublime_plugin.WindowCommand):
    """Search every heading the server knows with the orgs query language.
    Offers the server's filters and your saved queries first."""
    last = ""

    def run(self, query=None, edit=False):
        if query:
            if edit:
                self.Ask(query)
            else:
                self.Search(query)
            return

        def work(cl):
            return cl.filters(), cl.stored_queries()

        def done(res):
            items, queries = SearchChoices(res[0], res[1])

            def pick(i):
                if i < 0:
                    return
                if queries[i] is None:
                    self.Ask(OrgOrgsSearchCommand.last)
                else:
                    self.Search(queries[i])
            _quick(self.window, items, pick, "query, saved query or filter")
        orgs.run(work, done, what="filters")

    def Ask(self, init):
        self.window.show_input_panel("orgs query:", init or "", self.Search, None, None)

    def Search(self, query):
        query = (query or "").strip()
        if not query:
            return
        OrgOrgsSearchCommand.last = query

        def done(todos):
            if not todos:
                sublime.status_message("orgs: nothing matches " + query)
                return
            sublime.status_message("orgs: {0} headings match".format(len(todos)))
            orgsui.show_todos(self.window, todos, placeholder=query)
        orgs.run(lambda cl: cl.search(query), done, what="search")

    def is_enabled(self, **kw):
        return orgs.connected()


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Save Query
    Command: =org_orgs_save_query=  Keys: none  Args: =query=, =name=

    Saves a query on the server under a name. Saved queries belong to your
    login and are shared with worg and the orgs command line; they show up
    in Org: orgs Query Search. Asks for the query (prefilled with the last
    search) and then the name, unless given as arguments. Needs the orgs
    server.
EDOC */
"""

class OrgOrgsSaveQueryCommand(sublime_plugin.WindowCommand):
    """Save a query on the server under a name (shared with worg and the cli)."""
    def run(self, query=None, name=None):
        if query and name:
            self.Save(name, query)
            return
        if query:
            self.AskName(query)
            return
        self.window.show_input_panel("orgs query to save:", OrgOrgsSearchCommand.last or "", self.AskName, None, None)

    def AskName(self, query):
        query = query.strip()
        if not query:
            return
        self.window.show_input_panel("name for this query:", "", lambda n: self.Save(n, query), None, None)

    def Save(self, name, query):
        name = name.strip()
        if not name:
            return
        orgs.run(lambda cl: cl.save_query(name, query),
                 lambda res: sublime.status_message("orgs: " + (res.get("Msg") or "saved " + name)),
                 what="save query")

    def is_enabled(self, **kw):
        return orgs.connected()


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Delete Saved Query
    Command: =org_orgs_delete_query=  Keys: none  Args: =name=

    Lists your saved queries, asks for confirmation and deletes the one you
    pick (or =name= directly, without asking). Needs the orgs server.
EDOC */
"""

class OrgOrgsDeleteQueryCommand(sublime_plugin.WindowCommand):
    """Delete one of your saved queries."""
    def run(self, name=None):
        if name:
            self.Delete(name)
            return

        def got(stored):
            if not stored:
                sublime.status_message("orgs: you have no saved queries")
                return
            names = [q.get("name") or q.get("Name") for q in stored]
            items = [[n, q.get("query") or q.get("Query") or ""] for n, q in zip(names, stored)]

            def pick(i):
                if i >= 0 and sublime.ok_cancel_dialog("Delete the saved query '{0}'?".format(names[i]), "Delete"):
                    self.Delete(names[i])
            _quick(self.window, items, pick, "saved query to delete")
        orgs.run(lambda cl: cl.stored_queries(), got, what="saved queries")

    def Delete(self, name):
        orgs.run(lambda cl: cl.delete_query(name),
                 lambda res: sublime.status_message("orgs: " + (res.get("Msg") or "deleted " + name)),
                 what="delete query")

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Records, contacts and birthdays

def RecordItem(r):
    """[name, detail] for a record: its first few fields and its file."""
    bits = []
    for f in r.get("Fields") or []:
        if f.get("Value") and f.get("Kind") not in ("image",):
            bits.append(f["Value"])
        if len(bits) >= 3:
            break
    kind = r.get("Type") or ""
    name = r.get("Name") or ""
    if kind and kind != "contact":
        name += "  [" + kind + "]"
    return [name, "  ".join(bits + [orgsui.short_file(r.get("Filename"))])]


def _goto_rows(window, rows, items, placeholder):
    def done(i):
        if i >= 0:
            orgsui.goto(window, rows[i].get("Filename"), rows[i].get("LineNum", 0))

    def highlight(i):
        if i >= 0 and sets.Get("orgsPreviewOnHighlight", True):
            orgsui.goto(window, rows[i].get("Filename"), rows[i].get("LineNum", 0), transient=True)
    _quick(window, items, done, placeholder, highlight)


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Contacts / Org: orgs Records
    Command: =org_orgs_contacts=  Keys: none  Args: =query=, =collection= (default ="contact"=)

    Searches the server's records (headings the server reads as contacts,
    equipment, and other record types) and jumps to the one you pick. Asks
    for a search text matched against names, numbers, addresses and notes;
    empty lists them all. Each row shows the first few fields and the file.

    =collection= picks the record type; ="*"= (the palette's "Org: orgs
    Records") first lists the collections with their counts. With =query=
    given there is no prompt. Needs the orgs server.
    Settings: =orgsPreviewOnHighlight=.
EDOC */
"""

class OrgOrgsContactsCommand(sublime_plugin.WindowCommand):
    """Find a contact (or any record) and jump to it.

    collection: a record type ("contact", "equipment", ...); "*" asks which.
    query: searches names, numbers, addresses and notes. Empty lists them all.
    """
    def run(self, query=None, collection="contact"):
        if collection == "*":
            def got(cols):
                if not cols:
                    sublime.status_message("orgs: there are no records")
                    return
                items = [[c.get("Name") or c.get("Type"), "{0}  ({1})".format(c.get("Type"), c.get("Count", 0))]
                         for c in cols]
                _quick(self.window, items, lambda i: i >= 0 and self.run(query, cols[i].get("Type")),
                       "collection")
            orgs.run(lambda cl: cl.get("/records/collections") or [], got, what="collections")
            return
        self.collection = collection
        if query is None:
            self.window.show_input_panel("search {0}s (empty for all):".format(collection or "record"), "",
                                         self.Search, None, None)
        else:
            self.Search(query)

    def Search(self, q):
        q = q.strip() or None
        col = self.collection

        def work(cl):
            if col == "contact":
                return cl.contacts(q)
            return cl.records(col or None, q)

        def done(recs):
            if not recs:
                sublime.status_message("orgs: no {0}s found".format(col or "record"))
                return
            _goto_rows(self.window, recs, [RecordItem(r) for r in recs], q or col)
        orgs.run(work, done, what="records")

    def is_enabled(self, **kw):
        return orgs.connected()


def BirthdayItem(b):
    age = " turns {0}".format(b["Age"]) if b.get("Age") else ""
    field = (b.get("Field") or "BIRTHDAY").lower()
    return ["{0}  {1}".format(b.get("Date", ""), b.get("Name", "")),
            "{0}{1}  ({2})  {3}".format(field, age, b.get("Birthday", ""), orgsui.short_file(b.get("Filename")))]


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Upcoming Birthdays
    Command: =org_orgs_birthdays=  Keys: none  Args: =days=

    Lists the birthdays and anniversaries from your records in the next
    =days= days (default =orgsBirthdayDays=, 60): date, name, the field,
    the age they turn when the year is known, and the file. Picking one
    jumps to the record. Needs the orgs server.
EDOC */
"""

class OrgOrgsBirthdaysCommand(sublime_plugin.WindowCommand):
    """Upcoming birthdays and anniversaries from your records."""
    def run(self, days=None):
        import datetime
        days = int(days or sets.Get("orgsBirthdayDays", 60))
        today = datetime.date.today()
        frm = today.strftime("%Y-%m-%d")
        to = (today + datetime.timedelta(days=days)).strftime("%Y-%m-%d")

        def done(rows):
            if not rows:
                sublime.status_message("orgs: no birthdays in the next {0} days".format(days))
                return
            _goto_rows(self.window, rows, [BirthdayItem(b) for b in rows], "next {0} days".format(days))
        orgs.run(lambda cl: cl.birthdays(frm, to), done, what="birthdays")

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# The worg web ui

WORG_APPS = [
    ["Agenda", "Agenda"],
    ["Search", "Search"],
    ["StoredQueries", "Saved Queries"],
    ["Kanban", "Kanban"],
    ["GanttChart", "Gantt Chart"],
    ["Columns", "Column View"],
    ["ClockReport", "Clock Report"],
    ["Tables", "Tables"],
    ["Files", "Files"],
    ["Links", "Links"],
    ["MindMap", "Mind Map"],
    ["Collections", "Collections"],
    ["Contacts", "Contacts"],
    ["Code", "Code"],
    ["Drill", "Flashcards"],
    ["Diff", "Diff & History"],
    ["Presentation", "Presentation"],
    ["VoiceNotes", "Voice Notes"],
    ["Starmap", "Starmap"],
]


def WebUrl(app=None):
    """The worg url for app. The login is worg's own: no token on the url."""
    base = (sets.Get("orgsWebUrl", None) or "").strip()
    if not base:
        cl = orgs.client()
        base = cl.url if cl else (sets.Get("orgsUrl", None) or "")
    base = base.rstrip("/")
    if not base:
        return None
    if app:
        return base + "/?app=" + urllib.parse.quote(app)
    return base + "/"


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Open Web UI
    Command: =org_orgs_open_web=  Keys: none  Args: =app=

    Opens worg, the orgs web ui, in your browser. A quick panel picks the
    worg app (Agenda, Search, Saved Queries, Kanban, Gantt Chart, Column
    View, Clock Report, Tables, Files, Links, Mind Map, Collections,
    Contacts, Code, Flashcards, Diff & History, Presentation, Voice Notes,
    Starmap); =app= (e.g. ="Kanban"=) skips it. The url is =orgsWebUrl=, or
    the server url when that is empty. When you are logged in (and
    =orgsWebUrl= is not set) it opens a one-use login link the server hands
    out (=POST /login/ticket=), so the browser is logged in too; it is good
    once, for a minute, and no token is put on the url. Otherwise worg asks
    for its own login. Enabled when a server is known or =orgsWebUrl= is set.
EDOC */
"""

class OrgOrgsOpenWebCommand(sublime_plugin.WindowCommand):
    """Open the orgs web ui (worg) in the browser, on one of its apps."""
    def run(self, app=None):
        if app:
            self.Open(app)
            return
        items = [[label, app] for app, label in WORG_APPS]
        _quick(self.window, items, lambda i: i >= 0 and self.Open(WORG_APPS[i][0]), "worg app")

    def Open(self, app):
        import webbrowser
        url = WebUrl(app)
        if not url:
            sublime.status_message("orgs: no server url (set orgsUrl or orgsWebUrl)")
            return
        cl = orgs.Get()
        if cl is None or sets.Get("orgsWebUrl", None) or cl.auth_disabled:
            webbrowser.open(url)
            sublime.status_message("orgs: opened " + url)
            return

        # Logged in: a one-use link logs the browser in as us too.
        def ticket(c):
            return c.url + c.login_ticket() + ("?app=" + urllib.parse.quote(app) if app else "")

        def opened(u):
            webbrowser.open(u)
            sublime.status_message("orgs: opened worg, logged in")

        def plain(e):
            webbrowser.open(url)
            sublime.status_message("orgs: opened " + url)
        orgs.run(ticket, opened, on_error=plain)

    def is_enabled(self, **kw):
        return bool(orgs.client() is not None or sets.Get("orgsWebUrl", None))


# ----------------------------------------------------------------------
# Updater plugins (jira and friends)

r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Update Heading (Jira/plugin)
    Command: =org_orgs_update_heading=  Keys: none  Args: =name=

    Runs a server updater plugin on the heading at the cursor, for example
    refreshing a heading from its Jira issue. Asks for the plugin name,
    prefilled with =orgsDefaultUpdater= (="jira"=); =name= skips the
    question. The buffer is saved and synced first (=orgsSaveBeforeServerEdits=)
    and the server rewrites the heading in the file. Fails with a message
    when the plugin is not configured on the server. Needs the orgs server.
EDOC */
"""

class OrgOrgsUpdateHeadingCommand(sublime_plugin.TextCommand):
    """Run a server updater plugin (default: jira) on the heading at the cursor."""
    def run(self, edit, name=None):
        if name:
            self.Update(name)
            return
        self.view.window().show_input_panel("orgs updater plugin:", sets.Get("orgsDefaultUpdater", "jira"),
                                            self.Update, None, None)

    def Update(self, name):
        name = name.strip()
        if not name:
            return
        view = self.view

        def work(cl):
            h, tgt, node = orgs.heading_target(view)
            try:
                res = cl.update_plugin(name, tgt)
            except OrgsError as e:
                raise OrgsError(str(e).strip())
            if not isinstance(res, dict) or res.get("Ok") is not True:
                raise OrgsError("{0}: the server did not run the update (is the {0} plugin configured?)".format(name))
            return node.heading, res

        def done(res):
            heading, r = res
            sublime.status_message("orgs: {0} updated '{1}'{2}".format(
                name, heading, (": " + r["Msg"].strip()) if r.get("Msg") else ""))
        orgs.run(work, done, what=name)

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Heading encryption (org-crypt, done by the server's gpg)

def _crypt_config(cl):
    cfg = cl.get("/crypt/config") or {}
    if not isinstance(cfg, dict) or cfg.get("Mode") in (None, "", "off"):
        raise OrgsError("encryption is off on the server: " + ((cfg.get("Msg") if isinstance(cfg, dict) else "")
                                                               or "no gpg"))
    return cfg


def AskPassphrase(window, caption, on_done, confirm=False):
    """Masked input; with confirm, asks twice and checks they match."""
    def first(p1):
        if not p1:
            sublime.status_message("orgs: no passphrase given")
            return
        if not confirm:
            on_done(p1)
            return

        def second(p2):
            if p1 != p2:
                sublime.error_message("The passphrases do not match.")
                return
            on_done(p1)
        orgs.MaskedInput(window, "again, to confirm:", second, lambda: None)
    orgs.MaskedInput(window, caption, first, lambda: None)


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Encrypt Heading
    Command: =org_orgs_encrypt_heading=  Keys: none

    Encrypts the body of the heading at the cursor in the org-crypt format,
    done by the server's gpg. With a key configured on the server (or the
    server's gpg already unlocked) it just encrypts; otherwise it asks for a
    passphrase twice (masked) and checks they match. Needs the orgs server
    with encryption turned on in its config; otherwise it says encryption is
    off.
EDOC */
"""

class OrgOrgsEncryptHeadingCommand(sublime_plugin.TextCommand):
    """Encrypt the body of the heading at the cursor (org-crypt format)."""
    def run(self, edit):
        view = self.view
        window = view.window()

        def got_cfg(cfg):
            if cfg.get("Mode") == "key" or cfg.get("Unlocked"):
                self.Encrypt("")
            else:
                AskPassphrase(window, "passphrase to encrypt with:", self.Encrypt, confirm=True)
        orgs.run(_crypt_config, got_cfg, what="encrypt")

    def Encrypt(self, passphrase):
        view = self.view

        def work(cl):
            h, tgt, node = orgs.heading_target(view)
            return cl.crypt("encrypt", h, passphrase)

        orgs.run(work, lambda res: sublime.status_message("orgs: " + (res.get("Msg") or "encrypted")),
                 what="encrypt")

    def is_enabled(self, **kw):
        return orgs.connected()


DECRYPT_PANEL = "orgs_decrypted"


def ShowDecrypted(window, heading, text):
    """Plain text goes in an output panel, not a view: views (scratch ones
    too) are written into the hot exit session. The panel is emptied as soon
    as it is hidden."""
    panel = window.create_output_panel(DECRYPT_PANEL)
    panel.settings().set("orgs_decrypted", True)
    panel.settings().set("word_wrap", True)
    panel.assign_syntax("Packages/OrgEverywhere/OrgEverywhere.sublime-syntax")
    panel.set_read_only(False)
    panel.run_command("select_all")
    panel.run_command("right_delete")
    panel.run_command("append", {"characters":
                                 "# Decrypted: {0}\n# Not saved anywhere; cleared when this panel is closed.\n\n{1}".format(
                                     heading, text)})
    panel.set_read_only(True)
    window.run_command("show_panel", {"panel": "output." + DECRYPT_PANEL})


def ClearDecrypted(window):
    if window is not None and window.find_output_panel(DECRYPT_PANEL) is not None:
        window.destroy_output_panel(DECRYPT_PANEL)


class DecryptedPanelListener(sublime_plugin.EventListener):
    def on_post_window_command(self, window, cmd, args):
        if cmd == "hide_panel" or (cmd == "show_panel" and (args or {}).get("panel") != "output." + DECRYPT_PANEL):
            if window.active_panel() != "output." + DECRYPT_PANEL:
                ClearDecrypted(window)

    def on_pre_close_window(self, window):
        ClearDecrypted(window)


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Decrypt Heading
    Command: =org_orgs_decrypt_heading=  Keys: none  Args: =write=

    Decrypts the heading at the cursor. A quick panel asks whether to show
    the text (the file stays encrypted) or decrypt into the file (writes the
    text back in the clear); =write= =true= / =false= answers it in advance.
    Asks for the passphrase unless the server's gpg is unlocked.

    Shown text goes in an output panel, never a view, so it is not written
    into Sublime's session; the panel is emptied as soon as it is hidden or
    the window closes. Needs the orgs server with encryption turned on.
EDOC */
"""

class OrgOrgsDecryptHeadingCommand(sublime_plugin.TextCommand):
    """Decrypt the heading at the cursor: show the text in an output panel,
    or (write=True) put it back in the file in the clear."""
    def run(self, edit, write=None):
        view = self.view
        window = view.window()

        def got_cfg(cfg):
            def go(w):
                if cfg.get("Unlocked"):
                    self.Decrypt("", w)
                else:
                    AskPassphrase(window, "passphrase:", lambda p: self.Decrypt(p, w))
            if write is None:
                items = [["Show the decrypted text", "the file stays encrypted"],
                         ["Decrypt into the file", "writes the text back in the clear"]]
                _quick(window, items, lambda i: i >= 0 and go(i == 1))
            else:
                go(bool(write))
        orgs.run(_crypt_config, got_cfg, what="decrypt")

    def Decrypt(self, passphrase, write):
        view = self.view
        window = view.window()

        def work(cl):
            h, tgt, node = orgs.heading_target(view)
            return node.heading, cl.crypt("decrypt", h, passphrase, write=write)

        def done(res):
            heading, r = res
            sublime.status_message("orgs: " + (r.get("Msg") or "decrypted"))
            if not write:
                ShowDecrypted(window, heading, r.get("Text", ""))
        orgs.run(work, done, what="decrypt")

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Attachments

def AttachmentItems(res):
    items = []
    for a in res.get("Attachments") or []:
        size = a.get("Size") or 0
        human = "{0} B".format(size) if size < 1024 else (
            "{0:.1f} KB".format(size / 1024.0) if size < 1024 * 1024 else "{0:.1f} MB".format(size / 1048576.0))
        items.append([a.get("Name", ""), "{0}  {1}  {2}".format(a.get("Media", ""), human, a.get("Modified", ""))])
    return items


def AttachmentPath(h, name):
    return "/attachment/" + hash_path(h) + "/" + urllib.parse.quote(name)


def DownloadAttachment(cl, h, att):
    """(worker thread) Fetch an attachment into a new private directory
    (0700, made fresh for each download); returns the path."""
    url = att.get("Url") or ""
    path = url if url.startswith("/attachment/") else AttachmentPath(h, att["Name"])
    data = cl.get(path, raw=True)
    d = tempfile.mkdtemp(prefix="orgs-attachment-")
    name = os.path.basename((att.get("Name") or "").replace("\\", "/")) or "attachment"
    if name in (".", ".."):
        name = "attachment"
    out = os.path.join(d, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(out, flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data or b"")
    return out


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Attachments
    Command: =org_orgs_attachments=  Keys: none

    Lists the attachments of the heading at the cursor (its attachment
    folder from =:ID:= or =:DIR:=) with type, size and date, and opens the
    one you pick. The file is downloaded from the server into a new private
    temporary directory; text, =.org=, =.txt= and =.md= files open in
    Sublime, anything else in the system's default application. Says why
    when the heading has no attachment folder. Needs the orgs server.
EDOC */
"""

class OrgOrgsAttachmentsCommand(sublime_plugin.TextCommand):
    """List the attachments of the heading at the cursor and open one."""
    def run(self, edit):
        view = self.view
        window = view.window()

        def work(cl):
            h, tgt, node = orgs.heading_target(view)
            return h, cl.attachments(h)

        def done(res):
            h, r = res
            atts = r.get("Attachments") or []
            if not atts:
                why = "no attachment folder (no :ID: or :DIR:)" if not r.get("From") else "folder " + (r.get("Dir") or "")
                sublime.status_message("orgs: '{0}' has no attachments - {1}".format(r.get("Headline", ""), why))
                return

            def pick(i):
                if i < 0:
                    return
                att = atts[i]
                orgs.run(lambda cl: DownloadAttachment(cl, h, att), lambda p: self.Open(window, att, p),
                         what="attachment")
            _quick(window, AttachmentItems(r), pick, r.get("Headline"))
        orgs.run(work, done, what="attachments")

    def Open(self, window, att, path):
        if att.get("Media") == "text" or os.path.splitext(path)[1].lower() in (".org", ".txt", ".md"):
            window.open_file(path)
        else:
            _open_with_os(path)
        sublime.status_message("orgs: opened " + path)

    def is_enabled(self, **kw):
        return orgs.connected()


# ----------------------------------------------------------------------
# Flashcards: a small review loop over the server's drill decks.

DRILL_VIEW = "Org Drill"
DUE = ("failed", "overdue", "young", "new", "old")
GRADES = [
    ["0 - Wrong, and the answer is unfamiliar", 0],
    ["1 - Wrong, but the answer is familiar", 1],
    ["2 - Wrong, but the answer was almost there", 2],
    ["3 - Right, but it took a lot of effort", 3],
    ["4 - Right, after a little thought", 4],
    ["5 - Right, and easy", 5],
]


def ClozeHide(text):
    """Hide [cloze] and [cloze||hint] markup: org-drill's simplest cloze."""
    import re
    return re.sub(r'\[([^\[\]|]+?)(\|\|([^\[\]]*))?\](?!\])',
                  lambda m: "[" + (m.group(3) or "...") + "]", text or "")


def DueCards(reply):
    order = {s: i for i, s in enumerate(DUE)}
    cards = [c for c in reply.get("cards") or [] if c.get("status") in order]
    cards.sort(key=lambda c: order[c.get("status")])
    mx = (reply.get("deck") or {}).get("maxItems") or 30
    return cards[:mx]


def CardText(card, answer):
    title = card.get("name") or card.get("title") or ""
    body = card.get("body") or ""
    lines = ["* " + title, ""]
    if not answer:
        if card.get("clozes"):
            lines.append(ClozeHide(body))
        sides = card.get("sides") or []
        if sides:
            lines.append(sides[0].get("name") or "")
        lines += ["", "(Show answer to reveal)"]
    else:
        lines.append(body)
        for s in card.get("sides") or []:
            lines += ["", "** " + (s.get("name") or ""), s.get("body") or ""]
    return "\n".join(lines)


r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs Drill Flashcards
    Command: =org_orgs_drill=  Keys: none  Args: =deck=, =query=

    Reviews org-drill style flashcards. Pick one of the server's saved decks
    or "Query..." (prefilled with =orgsDrillQuery=); =deck= or =query= skip
    the choice. Due cards (failed, overdue, young, new, old; up to the
    deck's maximum, 30 by default) are shown one at a time in an "Org Drill"
    view with clozes (=[text]= or =[text||hint]=) hidden; a quick panel
    offers Show answer, Skip, Open the card or Stop. After the answer you
    grade it 0-5 and the server schedules the next review (a failed card
    comes back in the same session). Grading rewrites the card's
    properties in its file. Needs the orgs server.
EDOC */
"""

class OrgOrgsDrillCommand(sublime_plugin.WindowCommand):
    """Review flashcards (org-drill cards) from a saved deck or a query."""
    def run(self, deck=None, query=None):
        if deck or query:
            self.Start(deck, query)
            return

        def got(decks):
            items = [["Query...", "cards are the headings a query matches, e.g. HasTags(\"drill\")"]]
            for d in decks or []:
                items.append([d.get("name", ""), d.get("description") or d.get("query") or d.get("storedQuery") or ""])

            def pick(i):
                if i < 0:
                    return
                if i == 0:
                    self.window.show_input_panel("drill cards query:", sets.Get("orgsDrillQuery", 'HasTags("drill")'),
                                                 lambda q: q.strip() and self.Start(None, q.strip()), None, None)
                else:
                    self.Start(decks[i - 1].get("name"), None)
            _quick(self.window, items, pick, "deck")
        orgs.run(lambda cl: cl.drill_decks(), got, what="drill decks")

    def Start(self, deck, query):
        self.deck, self.query = deck, query
        self.reviewed = 0

        def done(reply):
            if reply.get("ok") is False:
                sublime.status_message("orgs: " + (reply.get("msg") or "no cards"))
                return
            self.queue = DueCards(reply)
            if not self.queue:
                c = reply.get("counts") or {}
                sublime.status_message("orgs: nothing due ({0} tomorrow)".format(c.get("dueTomorrow", 0)))
                return
            self.Ask()
        orgs.run(lambda cl: cl.drill_cards(deck, query), done, what="drill")

    def Show(self, text):
        orgsui.scratch_view(self.window, DRILL_VIEW, text, reuse=True)

    def Ask(self):
        if not self.queue:
            self.Show("* Done\n\n{0} reviewed.".format(Plural(self.reviewed, "card")))
            return
        card = self.queue[0]
        self.Show(CardText(card, False))
        items = [["Show answer", "{0} left".format(len(self.queue))], ["Skip"], ["Open the card", "ends the review"], ["Stop"]]

        def pick(i):
            if i == 0:
                self.Grade(card)
            elif i == 1:
                self.queue.pop(0)
                self.Ask()
            elif i == 2:
                orgsui.goto(self.window, card.get("file"), card.get("line", 0))
        sublime.set_timeout(lambda: _quick(self.window, items, pick, card.get("name")), 10)

    def Grade(self, card):
        self.Show(CardText(card, True))

        def pick(i):
            if i < 0:
                return
            self.Review(card, GRADES[i][1])
        sublime.set_timeout(lambda: _quick(self.window, [g[0] for g in GRADES], pick, "how well did you know it?"), 10)

    def Review(self, card, quality):
        deck, query = self.deck, self.query
        window = self.window

        def work(cl):
            SyncCardFile(window, card.get("file"))
            h = FreshCardHash(cl, card, deck, query)
            res = cl.drill_review(h, quality, deck)
            if not isinstance(res, dict) or res.get("ok") is not True:
                raise OrgsError((res or {}).get("msg") if isinstance(res, dict) else "review failed")
            return res

        def done(res):
            self.reviewed += 1
            self.queue.pop(0)
            if res.get("failed"):
                # org-drill asks a failed card again in the same session.
                self.queue.append(card)
            nxt = " next in {0:.0f} days".format(res["days"]) if res.get("days") else ""
            sublime.status_message("orgs: rated {0}.{1}".format(quality, nxt))
            self.Ask()
        orgs.run(work, done, what="drill review")

    def is_enabled(self, **kw):
        return orgs.connected()


def Plural(n, word):
    return "{0} {1}{2}".format(n, word, "" if n == 1 else "s")


def SyncCardFile(window, filename):
    from OrgEverywhere.orgsviews import SyncPath
    SyncPath(window, filename)


def FreshCardHash(cl, card, deck, query):
    """(worker thread) Reviewing rewrites a card's properties, which changes
    hashes in that file: look the card up again by file and title."""
    reply = cl.drill_cards(deck, query) or {}
    best = None
    for c in reply.get("cards") or []:
        if c.get("file") == card.get("file") and c.get("name") == card.get("name"):
            if best is None or abs(c.get("line", 0) - card.get("line", 0)) < abs(best.get("line", 0) - card.get("line", 0)):
                best = c
    if best is None:
        raise OrgsError("the card '{0}' is no longer in the deck".format(card.get("name")))
    return best["hash"]


# ----------------------------------------------------------------------
# New file from a server template

r"""
/* SDOC: Reference::Commands::orgs Server
*** Org: orgs New File from Template
    Command: =org_orgs_new_file=  Keys: none

    Creates an org file through the server: pick one of the server's org
    directories (no choice when there is one), type a file name (=.org= is
    added), a title, and a server template (or none). The new file is
    opened. A name that already exists just opens that file. Needs the orgs
    server.
EDOC */
"""

class OrgOrgsNewFileCommand(sublime_plugin.WindowCommand):
    """Create an org file on the server (in one of its directories, from one of
    its templates) and open it."""
    def run(self):
        def work(cl):
            return cl.dirs(), cl.new_file_templates()

        def done(res):
            self.dirs, self.templates = res[0] or [], res[1] or []
            if not self.dirs:
                sublime.status_message("orgs: the server has no directories to create a file in")
                return
            if len(self.dirs) == 1:
                self.AskName(self.dirs[0])
            else:
                _quick(self.window, self.dirs, lambda i: i >= 0 and self.AskName(self.dirs[i]), "directory")
        orgs.run(work, done, what="new file")

    def AskName(self, d):
        self.dir = d
        self.window.show_input_panel("new org file in {0}:".format(d), "", self.AskTitle, None, None)

    def AskTitle(self, name):
        name = name.strip()
        if not name:
            return
        if not name.endswith(".org"):
            name += ".org"
        self.path = os.path.join(self.dir, name)
        if os.path.exists(self.path):
            sublime.status_message("orgs: {0} already exists".format(self.path))
            self.window.open_file(self.path)
            return
        title = os.path.splitext(os.path.basename(name))[0]
        self.window.show_input_panel("title:", title, self.AskTemplate, None, None)

    def AskTemplate(self, title):
        self.title = title.strip()
        if not self.templates:
            self.Create("")
            return
        items = [["(no template)", ""]] + [[os.path.splitext(os.path.basename(t))[0], t] for t in self.templates]
        _quick(self.window, items, lambda i: i >= 0 and self.Create("" if i == 0 else self.templates[i - 1]),
               "template")

    def Create(self, template):
        path, title = self.path, self.title

        def done(fn):
            fn = fn or path
            sublime.status_message("orgs: created " + fn)
            self.window.open_file(fn)
        orgs.run(lambda cl: cl.new_file(path, title, template), done, what="new file")

    def is_enabled(self, **kw):
        return orgs.connected()
