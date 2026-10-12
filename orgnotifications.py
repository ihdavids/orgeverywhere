import sublime
import sublime_plugin
import threading, time, signal
from datetime import timedelta
from datetime import datetime
import os
import sys
import datetime as dt
import OrgEverywhere.orgagenda as agenda
import OrgEverywhere.orgagendanode as agnode
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgs.session as orgs
from   OrgEverywhere.orgparse.date import OrgDate
import logging
import subprocess, os

CHECK_PERIOD = 60*1

log = logging.getLogger(__name__)

class Notification(agenda.TodoView):
	# Only shows what it is handed; no need to scan every file.
	def FilterEntries(self):
		pass

	def Show(self, notifications, newItem):
		# Windows notice.
		n = newItem['node']
		f = newItem['file']
		heading = ""
		if(newItem.get('start')):
			heading = OrgDate.format_clock(newItem['start'],active=False)
		else:
			heading = "SOON!"
		body = f.AgendaFilenameTag() + "  " + n.heading
		ShowBalloon(body, heading)
		# Show the in sublime version, clear out other
		# todos and just show our notices.
		self.entries = []
		if(notifications and type(notifications) is dict):
			for key,item in notifications.items():
				self.AddEntry(item['node'],item['file'])
		window = sublime.active_window() 
		window.active_view().run_command('org_show_notifications')
		return

notification = None
r"""
/* SDOC: Reference::Commands::Notifications
*** Show the notifications view
    Command: =org_show_notifications=  Keys: none  (no palette entry)

    Draws the "Notifications" view: the reminders raised so far today. The
    notification thread runs it itself each time it raises a reminder;
    running it before any reminder was raised fails (there is nothing to
    draw yet).
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Notifications
*** Notification settings
    OrgEverywhere starts a notification thread a second after it loads. It
    asks the orgs server for today's agenda (rebuilt every fourth check,
    on a new day, when the server re-reads a file and when it connects) and
    raises a reminder for each timed entry that is not done, once, from the
    lead time before it starts until it starts. A reminder runs the
    external notification command and shows the Notifications view. Without
    the server nothing is scheduled.

    | Setting                       | Default | Effect                                                                                 |
    |-------------------------------+---------+----------------------------------------------------------------------------------------|
    | =notifyHoursBefore=           | =0=     | Lead time, hours part.                                                                 |
    | =notifyMinsBefore=            | =15=    | Lead time, minutes part (the window is at least 1.5 check periods long).               |
    | =noticePeriod=                | =1=     | Minutes between checks (at least 1). Read when the thread starts.                      |
    | =ExternalNotificationCommand= | per OS  | The command line (a list) run for a reminder; ={todo}= and ={time}= are filled in.     |

    The default command is =osascript -e 'display notification ...'= on
    macOS and PowerShell with =balloontip.ps1= (from the package folder) on
    Windows. Linux has no default: set =ExternalNotificationCommand= there,
    or the reminder fails.
EDOC */
"""

class OrgShowNotifications(sublime_plugin.TextCommand):
	def run(self, edit):	
		notification.DoRenderView(edit)

def ShowBalloon(todo, time):
	log.debug("SHOW BALLOON POPUP")
	formatDict = {
	"time": time,
	"todo": todo
	}
	if(sublime.platform() == 'windows'):
		commandLine = sets.Get("ExternalNotificationCommand",[r"C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe", "-ExecutionPolicy", "Unrestricted", ".\\balloontip.ps1", "\"{todo}\"", "\"{time}\""], formatDict)
	elif(sublime.platform() == 'osx'):
		commandLine = sets.Get("ExternalNotificationCommand",['osascript','-e',"display notification \"{time}\" with title \"{todo}\" subtitle \""+"Org Mode TODO"+"\""], formatDict)
	else:
		print("ERROR: platform not yet supported for notifications")
	# Expand all potential macros.
	for i in range(len(commandLine)):
		commandLine[i] = commandLine[i].format(todo=todo,time=time)
	try:
		startupinfo = subprocess.STARTUPINFO()
		startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
	except:
		startupinfo = None
	# cwd=working_dir, env=my_env,
	cwd = os.path.join(sublime.packages_path(),"OrgEverywhere") 
	if(sublime.platform() == 'windows'):
		popen = subprocess.Popen(commandLine, universal_newlines=True, cwd=cwd, startupinfo=startupinfo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		popen.wait()
	elif(sublime.platform() == 'osx'):
		subprocess.check_call(commandLine,stderr=subprocess.STDOUT)

# Notify from the lead time before the start until the start. The window is
# at least one check period long (and a little: a check can run late), or a
# check could step over it (with no lead time it would be a single instant).
def IsWithinNotificationWindow(item, hours, minutes, period=timedelta(seconds=CHECK_PERIOD)):
	start = item.get('start')
	if(not start):
		return False
	now = datetime.now()
	lead = max(timedelta(hours=hours, minutes=minutes), period * 1.5)
	return start - lead <= now <= start

def GetUID(item):
	n = item['node']
	f = item['file']
	return f.filename + "@" + n.get_locator() + "@" + str(item.get('start'))

# Today's timed occurrences that are not done: what the server's agenda for
# today says, nothing parsed here.
def TodaysReminders(res):
	data = agnode.AgendaData.FromResult(res)
	today = []
	seen = set()
	for occ in data.On(dt.date.today()):
		n = occ.node
		if not occ.has_time or occ.done or agenda.IsDone(n) or agenda.IsArchived(n):
			continue
		key = (id(n), occ.start)
		if key in seen:
			continue
		seen.add(key)
		today.append({'node': n, 'file': data.File(n.filename), 'start': occ.start})
	return today

class NotificationSystem(threading.Thread):
	def __init__(self, interval):
		threading.Thread.__init__(self)
		self.daemon     = False
		self.stopped    = threading.Event()
		self.interval   = interval
		self.today      = None
		self.notified   = {}
		self.todaysDate = datetime.now().day
		self.checkcount = 0
		self.lock       = threading.Lock()
		
	def stop(self):
		self.stopped.set()
		self.join()

	def run(self):
		while not self.stopped.wait(self.interval.total_seconds()):
			self.CheckNotifications()

	def HaveNotifiedFor(self, item):
		return GetUID(item) in self.notified


	def DoNotify(self,item):
		self.notified[GetUID(item)] = item
		global notification
		notification = Notification("Notifications")
		notification.Show(self.notified, item)
		
	def CheckNotifications(self):
		log.debug("CHECKING...")
		if(datetime.now().day > self.todaysDate or self.today == None):
			self.todaysDate = datetime.now().day
			self.notified = {}
			self.BuildToday()
		# Periodically rebuild the day.
		self.checkcount += 1
		if((self.checkcount % 4) == 0):
			self.BuildToday()

		hours   = sets.Get("notifyHoursBefore", 0)
		minutes = sets.Get("notifyMinsBefore", 15)
		for item in list(self.today or []):
			if(IsWithinNotificationWindow(item, hours, minutes, self.interval) and not self.HaveNotifiedFor(item)):
				log.debug("DO NOTIFY CALLED")
				self.DoNotify(item)

	def AddEntry(self,n,f):
		self.today.append({'node': n, 'file': f})

	# Off the ui thread (the notification thread, a rebuild timer): the
	# synchronous call is fine here. Not connected: nothing is scheduled.
	def BuildToday(self):
		if orgs.on_ui_thread():
			ScheduleRebuild(0)
			return
		with self.lock:
			today = []
			try:
				day = dt.date.today()
				res = orgs.call(lambda c: c.agenda(day, day, today=day), None)
				if res:
					today = TodaysReminders(res)
			except Exception:
				log.exception("notifications: could not build today")
			self.today = today


notice = None

# Rebuild today's list when the server re-reads a file or (re)connects.
# Saves come in bursts, so wait for them to settle.
REBUILD_DELAY = 3.0
_rebuildTimer = None
_rebuildLock  = threading.Lock()

def ScheduleRebuild(delay=REBUILD_DELAY):
	global _rebuildTimer
	with _rebuildLock:
		if _rebuildTimer is not None:
			_rebuildTimer.cancel()
		_rebuildTimer = threading.Timer(delay, _Rebuild)
		_rebuildTimer.daemon = True
		_rebuildTimer.start()

def _Rebuild():
	global _rebuildTimer
	with _rebuildLock:
		_rebuildTimer = None
	if notice is not None:
		try:
			notice.BuildToday()
		except Exception:
			log.exception("notifications: rebuild failed")

def OnServerEvent(ev):
	if ev.get("kind") == "reload":
		ScheduleRebuild()

def OnServerState(state):
	if state == orgs.CONNECTED:
		ScheduleRebuild(0.5)

def Setup():
	global notice
	checkPeriod = sets.Get("noticePeriod",1)*60
	if(checkPeriod < 60):
		checkPeriod = 60
	notice = NotificationSystem(interval=timedelta(seconds=checkPeriod))
	notice.start()
	orgs.on_event(OnServerEvent)
	orgs.on_state(OnServerState)
	log.debug("NOTIFICATION SYSTEM IS UP AND RUNNING: " + str(checkPeriod))

def plugin_unloaded():
	global _rebuildTimer
	orgs.off_event(OnServerEvent)
	orgs.off_state(OnServerState)
	with _rebuildLock:
		if _rebuildTimer is not None:
			_rebuildTimer.cancel()
			_rebuildTimer = None

def Get():
	return notice   

r"""
/* SDOC: Reference::Commands::Notifications
*** Org Agenda Rebuild Notices
    Command: =org_rebuild_notifications=  Keys: none

    Rebuilds today's reminder list from the orgs server's agenda now
    (starting the notification thread if it is not running), instead of
    waiting for the next periodic rebuild. Needs the orgs server.
EDOC */
"""

class OrgRebuildNotificationsCommand(sublime_plugin.TextCommand):
    def run(self, edit):
    	global notice
    	if(notice == None):
    		Setup()
    	sublime.set_timeout_async(notice.BuildToday, 0)

