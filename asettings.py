import sublime
import sublime_plugin
import datetime
import os
import logging
import shutil
import OrgEverywhere.orgutil.template as temp


log = logging.getLogger(__name__)



defaultTodoStates = ["TODO", "NEXT", "BLOCKED","WAITING","FLAG", "CLEANUP", "IN-PROGRESS", "DOING", "|", "REASSIGNED", "CANCELLED", "DONE","MEETING","PHONE","NOTE", "FIXED"]
weekdayNames = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ASettings:
	def __init__(self, settingsName):
		self.settings = sublime.load_settings(settingsName + '.sublime-settings')

	def Get(self, name, defaultVal):
		val = self.settings.get(name)
		if(val == None):
			val = defaultVal
		return val


configFilename    = "OrgEverywhere"

def setup_user_settings():
	# This is old and I should remove it!
	# We don't need this mechanism anymore!
	filename = configFilename + ".sublime-settings"
	user_settings_path = os.path.join(
		sublime.packages_path(),
		"User",
		filename)

	if not os.path.exists(user_settings_path):
		default_settings_path = os.path.join(
			sublime.packages_path(),
			"OrgEverywhere",
			filename)
		if os.path.exists(default_settings_path):
			shutil.copyfile(default_settings_path, user_settings_path)

def setup_mousemap():
	filename = "Default.sublime-mousemap"
	user_settings_path = os.path.join(
		sublime.packages_path(),
		"User",
		filename)
	mousemap = """
[
    {
        "button": "button1", 
        "count": 1, 
        "modifiers": ["alt"],
        "press_command": "drag_select",
        // In the future we may vector this to a more generic handler
        "command": "org_mouse_handler",
    }
]
	"""	
	if not os.path.exists(user_settings_path):
		with open(user_settings_path,"w") as o:
			o.write(mousemap)
			o.write("\n")

r"""
/* SDOC: Reference::Commands::Misc
*** Org Setup Mouse Map
    Command: =org_setup_mouse_map=  Keys: none (not in the command palette)

    Writes =Packages/User/Default.sublime-mousemap= binding alt+click to
    =org_mouse_handler= (keeping the normal click to place the cursor),
    only when that file does not exist yet. Run it from the console:
    =view.run_command("org_setup_mouse_map")=.
EDOC */
"""


class OrgSetupMouseMapCommand(sublime_plugin.TextCommand):
    def run(self, edit, event=None):
    	setup_mousemap()


# Singleton access
_sets = None

def Load():
	global configFilename
	global _sets
	_sets          = ASettings(configFilename)



def Get(name, defaultValue, formatDictionary = None):
	global _sets
	if(_sets == None):
		log.warning("SETTINGS IS NULL? IS THIS BEING CALLED BEFORE PLUGIN START?")
		Load()
	rv = _sets.Get(name, defaultValue)
	formatDict = {
		"date":     str(datetime.date.today()),
		"time":     datetime.datetime.now().strftime("%H:%M:%S"),
		"datetime": str(datetime.datetime.now().strftime("%Y-%m-%d %a %H:%M")),
	}

	if(formatDictionary != None):
		formatDict.update(formatDictionary)

	if(str == type(rv)):
		formatter = temp.TemplateFormatter(Get)
		rv  = formatter.format(rv, **formatDict)
	if(list == type(rv)):
		formatter = temp.TemplateFormatter(Get)
		rv = [ (formatter.format(r, **formatDict) if str == type(r) else r) for r in rv ]
	return rv

def RepresentsInt(s):
    try: 
        int(s)
        return True
    except ValueError:
        return False	

def GetInt(name, defaultValue):
	v = Get(name, defaultValue)
	try:
		i = int(v)
		return i
	except:
		return defaultValue


def GetWeekdayIndexByName(name):
    try:
        weekdayIndex = weekdayNames.index(name)
    except:
        weekdayIndex = 6
    return weekdayIndex

# Will return a date or an index as an integer where 0 means Sunday
# and so on in the week.
def GetDateAsIndex(name, defaultValue):
	global daysOfWeek
	val = Get(name, defaultValue)
	if(RepresentsInt(val)):
		return int(val) % 7
	val = val.lower()
	for i in range(0,len(daysOfWeek)):
		if daysOfWeek[i] in val:
			return i
	return 0

# ================================================================================
r"""
/* SDOC: Reference::Settings::Misc
*** Values that refer to other values
    Every string setting (and each string in a list setting) is expanded as
    a template when read: ={date}= today (=2026-10-11=), ={time}= the time
    (=14:03:00=), ={datetime}= (=2026-10-11 Sun 14:03=), and ={NAME}= the
    value of another setting. =OrgEverywhere.sublime-settings= is read from
    =Packages/User/= over the package's defaults; on first start a copy of
    the defaults is put there.
EDOC */
"""


r"""
/* SDOC: Reference::Commands::Misc
*** Org Test Template
    Command: =org_test_template=  Keys: none

    A developer check: prints a few expanded templates (date, time, file
    name, a setting) to the console.
EDOC */
"""


class OrgTestTemplateCommand(sublime_plugin.TextCommand):
	def run(self, edit, onDone=None):
		v = temp.ExpandTemplate(self.view, "DATE: {date} TIME: {time} DT: {datetime} FILE: {file.lower:call}")
		print(str(v))
		formatDict = { "aaa":     str(datetime.date.today()),}
		formatter = temp.TemplateFormatter(Get)
		rv  = formatter.format("{aaa} {archive}", **formatDict)
		print(str(rv))

