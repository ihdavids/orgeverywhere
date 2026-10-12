"""Small ui helpers shared by the orgs backed commands."""
import os

import sublime

from .client import parse_time


def goto(window, filename, line0=0, transient=False):
    """Open filename at a 0 based line."""
    if not filename:
        return None
    flags = sublime.ENCODED_POSITION
    if transient:
        flags |= sublime.TRANSIENT
    return (window or sublime.active_window()).open_file("{0}:{1}".format(filename, (line0 or 0) + 1), flags)


def short_file(filename):
    return os.path.splitext(os.path.basename(filename or ""))[0]


def todo_date(t, key="Date"):
    d = t.get(key) if t else None
    if not d:
        return None
    return parse_time(d.get("Start"))


def todo_label(t):
    """One line for a server heading (Todo dict)."""
    parts = []
    if t.get("Status"):
        parts.append(t["Status"])
    if t.get("Priority"):
        parts.append("[#" + t["Priority"] + "]")
    parts.append(t.get("Headline", ""))
    s = " ".join(parts)
    if t.get("Tags"):
        s += "  :" + ":".join(t["Tags"]) + ":"
    return s


def todo_detail(t):
    bits = [short_file(t.get("Filename"))]
    d = todo_date(t)
    if d:
        bits.append("S: " + d.strftime("%Y-%m-%d %a" + (" %H:%M" if (t.get("Date") or {}).get("HaveTime") else "")))
    dl = todo_date(t, "Deadline")
    if dl:
        bits.append("D: " + dl.strftime("%Y-%m-%d %a"))
    return "  ".join(bits)


def show_todos(window, todos, on_pick=None, placeholder=None):
    """Quick panel of server headings; picking one opens it (or on_pick(t))."""
    window = window or sublime.active_window()
    if not todos:
        sublime.status_message("orgs: nothing found")
        return
    items = [[todo_label(t), todo_detail(t)] for t in todos]

    def done(i):
        if i < 0:
            return
        if on_pick:
            on_pick(todos[i])
        else:
            goto(window, todos[i].get("Filename"), todos[i].get("LineNum", 0))

    def highlight(i):
        if i >= 0 and sublime.load_settings("OrgEverywhere.sublime-settings").get("orgsPreviewOnHighlight", True):
            goto(window, todos[i].get("Filename"), todos[i].get("LineNum", 0), transient=True)
    kw = {}
    if placeholder and int(sublime.version()) >= 4081:
        kw["placeholder"] = placeholder
    window.show_quick_panel(items, done, 0, 0, highlight, **kw)


def scratch_view(window, name, text, syntax="Packages/OrgEverywhere/OrgEverywhere.sublime-syntax", reuse=True):
    """A read only scratch view holding text (reused by name)."""
    window = window or sublime.active_window()
    view = None
    if reuse:
        for v in window.views():
            if v.name() == name and v.is_scratch():
                view = v
                break
    if view is None:
        view = window.new_file()
        view.set_name(name)
        view.set_scratch(True)
        if syntax:
            view.assign_syntax(syntax)
    view.set_read_only(False)
    view.run_command("select_all")
    view.run_command("right_delete")
    view.run_command("append", {"characters": text})
    view.set_read_only(True)
    window.focus_view(view)
    return view
