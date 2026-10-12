import sublime
import sublime_plugin
import OrgEverywhere.pymitter as evt

r"""
/* SDOC: Reference::Commands::Misc
*** Mouse handler
    Command: =org_mouse_handler=  Keys: alt+click, once the mouse map is set up

    Turns a click into an "orgmouse" event for the plugin; today the date
    picker uses it (alt+click a day in its calendar). Sublime has no mouse
    binding for it by default: Org Setup Mouse Map writes one.
EDOC */
"""


class OrgMouseHandlerCommand(sublime_plugin.TextCommand):
    def run(self, edit, event=None):
        pt = 0 if not event else self.view.window_to_text((event["x"], event["y"]))
        evt.Get().emit("orgmouse", pt, self.view, edit)

    def want_event(self):
        return True	
