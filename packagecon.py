# Package Control
import os
import sublime

PCKGCTRL_SETTINGS = "Package Control.sublime-settings"
TABLE_PACKAGE     = "Table Editor"
PS_PACKAGE        = "PowerShell"
#      support the languages and grammars we support.

def IsInstalled(pkg):
	settings = sublime.load_settings(PCKGCTRL_SETTINGS)
	return pkg in set(settings.get("installed_packages", []))

#def IsPkgInstalled(pkgName):
#	settings = sublime.load_settings(PCKGCTRL_SETTINGS)
#	return pkgName in set(settings.get("installed_packages", []))

def Install(pkg):
	print("Installing `{}` ..".format(pkg))
	sublime.active_window().run_command("advanced_install_package", {"packages": pkg})
	#Hide()

def Hide():
	sublime.active_window().active_view().hide_popup()

ORGEXTENDED_PACKAGE = "OrgExtended"

# OrgExtended and OrgEverywhere handle the same files and overlap heavily,
# so they cannot run side by side.
def IsPresent(pkg):
	if os.path.isdir(os.path.join(sublime.packages_path(), pkg)):
		return True
	if os.path.isfile(os.path.join(sublime.installed_packages_path(), pkg + ".sublime-package")):
		return True
	return IsInstalled(pkg)

def IsIgnored(pkg):
	prefs = sublime.load_settings("Preferences.sublime-settings")
	return pkg in prefs.get("ignored_packages", [])

def Disable(pkg):
	prefs = sublime.load_settings("Preferences.sublime-settings")
	ignored = prefs.get("ignored_packages", [])
	if pkg not in ignored:
		ignored.append(pkg)
		prefs.set("ignored_packages", ignored)
		sublime.save_settings("Preferences.sublime-settings")

def CheckForOrgExtended():
	pkg = ORGEXTENDED_PACKAGE
	if not IsPresent(pkg) or IsIgnored(pkg):
		return
	msg = ("OrgEverywhere: the {0} package is installed and enabled.\n\n"
		"OrgEverywhere and {0} cannot coexist; having both enabled "
		"will cause them to interfere with each other.\n\n"
		"Disable {0} now? (It will be added to ignored_packages; you can "
		"re-enable it later with Package Control: Enable Package.)").format(pkg)
	if sublime.ok_cancel_dialog(msg, "Disable " + pkg):
		Disable(pkg)
		sublime.message_dialog("OrgEverywhere: {0} has been disabled. "
			"Please restart Sublime Text to finish the switch.".format(pkg))
	else:
		print("OrgEverywhere: WARNING {0} is still enabled; the two packages cannot coexist.".format(pkg))
