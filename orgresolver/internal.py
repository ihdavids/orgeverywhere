import re
import os
from fnmatch import fnmatch
import logging
import sublime
from .abstract import AbstractLinkResolver
import OrgEverywhere.orgdb as db
import OrgEverywhere.orgs.ui as orgsui
from OrgEverywhere.orgutil.util import *

PATTERN_SETTING = 'resolver.local_file.pattern'
PATTERN_DEFAULT = r'^(((::(?P<row>\d+))(::(?P<col>\d+))?)|(::\#(?P<cid>[a-zA-Z0-9!$@%&_-]+))|(::\*(?P<heading>.+?)))?\s*$'

log = logging.getLogger(__name__)

FORCE_LOAD_SETTING = 'resolver.local_file.force_into_sublime'

RE_ID_LINK = re.compile(r'^\s*id:(?P<id>.+?)\s*$')


def JumpToId(view, id):
    """Open the heading with this ID or CUSTOM_ID (the server knows where it
    is; an open buffer's row wins, and unsaved ids are found in buffers)."""
    db.Get().JumpToAnyId(id, view.window() if view else None)


class Resolver(AbstractLinkResolver):
    def __init__(self, view):
        super(Resolver, self).__init__(view)
        self.view = view
        get = self.settings.get
        pattern = get(PATTERN_SETTING, PATTERN_DEFAULT)
        self.regex = re.compile(pattern)        

    def is_internal_link(self, filepath):
    	fp = filepath.strip()
    	# Starts with :: this is DEFINETLY an internal link
    	if(len(fp) > 2 and fp.startswith("::")):
    		return True
    	if('.' in fp):
    		return False
    	if('/' in fp):
    		return False
    	if('\\' in fp):
    		return False
    	# File path for sure?
    	if(':' in fp):
    		return False
    	if(os.path.isfile(fp)):
    		return False
    	# Okay, this PROBABLY is an internal link
    	#print('Probably internal link')
    	return True

    def tryMatchHeading(self, heading):
        fi = db.Get().FindInfo(self.view)
        if(fi):
            for n in fi.org:
                if not n.is_root() and n.heading.strip() == heading.strip():
                    orgsui.goto(self.view.window(), self.view.file_name(), n.start_row)
                    return True
        return None

    def tryMatchDirectTarget(self, val):
        fi = db.Get().FindInfo(self.view)
        if(fi):
            if(fi.org.targets):
                if(val in fi.org.targets):
                    tgt = fi.org.targets[val]
                    row = tgt['row'] + 1
                    col = tgt['col']
                    filepath = self.view.file_name()
                    if row:
                        filepath += ':{0}'.format(row)
                    if col:
                        filepath += ':{0}'.format(col)
                    self.view.window().open_file(filepath, sublime.ENCODED_POSITION)
                    return True
        return None

    def tryMatchNamedObject(self, val):
        fi = db.Get().FindInfo(self.view)
        if(fi):
            if(fi.org.names):
                if(val in fi.org.names):
                    tgt = fi.org.names[val]
                    row = tgt['row'] + 1
                    col = 0
                    filepath = self.view.file_name()
                    if row:
                        filepath += ':{0}'.format(row)
                    if col:
                        filepath += ':{0}'.format(col)
                    self.view.window().open_file(filepath, sublime.ENCODED_POSITION)
                    return True
        return None

    def expand_path(self, filepath):
        m = RE_ID_LINK.match(filepath)
        if m:
            JumpToId(self.view, m.group('id'))
            return True
        fp = filepath.strip()
        # [[#custom-id]] and [[*Heading]] in this file
        if len(fp) > 1 and fp[0] == '#':
            JumpToId(self.view, fp[1:])
            return True
        if len(fp) > 1 and fp[0] == '*':
            return self.tryMatchHeading(fp[1:])
        filepath = os.path.expandvars(filepath)
        filepath = os.path.expanduser(filepath)
        if(not self.is_internal_link(filepath)):
            #print("Not an internal link")
            return None

        # Check for standard internal link
        match = self.regex.match(filepath)
        if match:
            row, col, cid, heading = match.group('row'), match.group('col'), match.group('cid'), match.group('heading')
            # The presence of a custom ID means we jump
            # using a different means
            if(cid):
                JumpToId(self.view, cid)
                return True
            if(heading):
                return self.tryMatchHeading(heading)
            return None
        else:
            # Okay now the fun starts. We got an unidentifed link, this could be a:
            # <<>> target
            # A #+NAME "ed" object somewhere
            # a heading somewhere
            #
            # So we have to get creative and go find that.
            if(self.tryMatchDirectTarget(filepath)):
                return True
            if(self.tryMatchNamedObject(filepath)):
                return True
            if(self.tryMatchHeading(filepath)):
                return True
        return None

    def replace(self, content):
        content = self.expand_path(content)
        return content

    def execute(self, content):
        # Nothing to do here!
        return content
        #if content is not True:
        #    return super(Resolver, self).execute(content)
