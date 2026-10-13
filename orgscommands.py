"""Commands for the orgs server connection itself.

The orgs backed features live with the features they replace (agenda,
capture, clocking, ...). This module holds connecting, logging in and out,
and the status of the connection.
"""
import sublime
import sublime_plugin

import OrgEverywhere.asettings as sets
import OrgEverywhere.orgdb as db
import OrgEverywhere.orgs.session as orgs


r"""
/* SDOC: Reference::Commands::Connection
*** Org: Connect to orgs server
    Command: =orgs_connect=  Keys: none

    Finds the orgs server and logs in. The url is =orgsUrl= when it is set;
    otherwise each url in =orgsDiscoverUrls= is probed in order and the first
    orgs server that answers wins. A stored login (session token, or a
    remembered password hash) is reused; otherwise you are asked for a user
    name and password. When no server is found a dialog lists every url
    tried and why it failed.

    On success the file database is rebuilt from the server (the list of org
    files, ids, tags, backlinks). OrgEverywhere also connects by itself at
    startup, so this is for after the server was started late or moved.

    Settings: =orgsEnabled=, =orgsUrl=, =orgsDiscoverUrls=, =orgsNoVerify=,
    =orgsCaFile=, =orgsTimeout=.
EDOC */
"""

class OrgsConnectCommand(sublime_plugin.WindowCommand):
    """Find the server (orgsUrl, or probe orgsDiscoverUrls) and log in."""
    def run(self):
        orgs.connect(interactive=True, on_done=self.done)

    def done(self, ok):
        if ok:
            sublime.status_message(orgs.describe())
            db.Get().RebuildDb()


r"""
/* SDOC: Reference::Commands::Connection
*** Org: Log in to orgs
    Command: =orgs_login=  Keys: none

    Asks for a user name (offering the last one used, or =orgsUser=) and a
    password (typed masked), and logs in to the connected server. When no
    server has been found yet it connects first (as Org: Connect to orgs
    server). After a login the file database is rebuilt.

    Commands that need the server and find no login show "log in first" and
    open this prompt themselves; run the command again once logged in.

    Settings: =orgsUser=, =orgsRememberLogin=, =orgsCredentialStore=.
EDOC */
"""

class OrgsLoginCommand(sublime_plugin.WindowCommand):
    def run(self):
        if orgs.client() is None:
            orgs.connect(interactive=True)
            return
        orgs.prompt_login(self.window, lambda ok: ok and db.Get().RebuildDb())


r"""
/* SDOC: Reference::Commands::Connection
*** Org: Log out of orgs (forget stored login)
    Command: =orgs_logout=  Keys: none  Args: =forget= (default =true=)

    Logs out of the server and stops listening for its change events. With
    =forget= (the default) the stored session token and the remembered
    password hash are deleted from the credential store too, so the next
    connect asks for a password. Bind it with ={"forget": false}= to keep
    them. Enabled only when a server has been found.
EDOC */
"""

class OrgsLogoutCommand(sublime_plugin.WindowCommand):
    """Log out and forget the stored login (token and remembered password hash)."""
    def run(self, forget=True):
        orgs.logout(forget=forget)
        sublime.status_message("orgs: logged out" + (" and forgot the stored login" if forget else ""))

    def is_enabled(self):
        return orgs.client() is not None


r"""
/* SDOC: Reference::Commands::Connection
*** Org: Set orgs server url
    Command: =orgs_set_url=  Keys: none

    Asks for the server url (prefilled with =orgsUrl=, or
    =http://localhost:8010=), writes it to =orgsUrl= in your user
    OrgEverywhere settings, and connects to it. A trailing slash is dropped.
    An empty answer changes nothing.
EDOC */
"""

class OrgsSetUrlCommand(sublime_plugin.WindowCommand):
    """Point OrgEverywhere at a server and connect."""
    def run(self):
        current = sets.Get("orgsUrl", None) or "http://localhost:8010"
        self.window.show_input_panel("orgs server url:", current, self.on_done, None, None)

    def on_done(self, url):
        url = url.strip().rstrip('/')
        if not url:
            return
        s = sublime.load_settings("OrgEverywhere.sublime-settings")
        s.set("orgsUrl", url)
        sublime.save_settings("OrgEverywhere.sublime-settings")
        orgs.connect(interactive=True, on_done=lambda ok: ok and db.Get().RebuildDb())


r"""
/* SDOC: Reference::Commands::Connection
*** Org: orgs connection status
    Command: =orgs_status=  Keys: none

    Shows a dialog with the connection state (connected, login, searching,
    unavailable, disabled) and why; when a server is known also its url, the
    logged in user (or that the server runs without auth), when the session
    expires, which credential store holds the login, whether a password hash
    is remembered, whether live updates (the server's event stream) are
    connected, and how many files the server reports. With no server it
    lists the urls being tried.

    The same state, in short, is in the status bar of org views
    (=orgsShowStatus=).
EDOC */
"""

class OrgsStatusCommand(sublime_plugin.WindowCommand):
    """Show what the connection is doing and why."""
    def run(self):
        st, msg = orgs.state()
        cl = orgs.client()
        lines = ["State: " + st]
        if msg:
            lines.append("Detail: " + msg)
        if cl is not None:
            lines.append("Server: " + cl.url)
            lines.append("User: " + ("(server runs without auth)" if cl.auth_disabled else (cl.user or "-")))
            tok = cl.token
            if tok and tok.expires:
                lines.append("Session expires: " + tok.expires.astimezone().strftime("%Y-%m-%d %H:%M"))
            if cl.store is not None:
                lines.append("Login stored in: " + cl.store.name)
                u, h = cl.remembered_hash()
                lines.append("Remembered login: " + ("yes" if h else "no"))
            lst = orgs.listener()
            lines.append("Live updates: " + ("connected" if lst and lst.connected else "not connected"))
            lines.append("Files tracked: " + str(len(db.Get().ServerFiles())))
        else:
            cands = sets.Get("orgsUrl", None) or ", ".join(orgs.discover_urls())
            lines.append("Looking at: " + cands)
        sublime.message_dialog("\n".join(lines))


r"""
/* SDOC: Reference::Commands::Connection
*** Org: orgs reload server config
    Command: =orgs_reload_config=  Keys: none

    Asks the server to re-read its =orgs.yaml= (capture templates, filters,
    plugins...). The status bar shows the server's answer, including any
    settings that only take effect after the server is restarted. Needs the
    orgs server.
EDOC */
"""

class OrgsReloadConfigCommand(sublime_plugin.WindowCommand):
    """Ask the server to re-read its yaml (capture templates, filters, ...)."""
    def run(self):
        def done(res):
            if isinstance(res, dict):
                msg = res.get("msg") or "reloaded"
                if res.get("needsRestart"):
                    msg += " (restart the server for: " + ", ".join(res["needsRestart"]) + ")"
                sublime.status_message("orgs: " + msg)
        orgs.run(lambda c: c.reload_config(), done, what="reload config")
