# -*- coding: utf-8 -*-
import os
import sys
import subprocess
import sublime


r"""
/* SDOC: Reference::Settings::Links
*** Link resolver settings
    =linkResolvers= (default =["internal", "http", "https", "prompt",
    "jira", "email", "file"]=) is the order Org Open Link tries its local
    resolvers in; a resolver module of your own in =Packages/User/orgresolver=
    can be named here too. Each resolver reads its own settings:

    - =resolver.abstract.commands=: the opener command per platform
      (=darwin=, =win32=, =linux=); default =open=, =cmd /C start=,
      =xdg-open=. Setting it replaces the default of every resolver, the
      prompt resolver's terminal command included.
    - =resolver.abstract.arg_list_wrapper= (default =[]=): a command the
      opener command line is wrapped in.
    - =resolver.http.pattern= / =resolver.http.url= (default
      ~^(http):(?P<url>.+)$~ and =http:%s=), and the same for =https=: what
      such a link is and the url opened.
    - =resolver.jira.pattern= (default ~^(jira|j):(?P<issue>.+)$~, group
      =issue=) and =resolver.jira.url= (default
      =http://sandbox.onjira.com/browse/%s=). While the url is that
      placeholder, jira links go to the orgs server first.
    - =resolver.email.pattern= / =resolver.email.url= (default =mailto:%s=):
      =mailto:= links.
    - =resolver.prompt.pattern= (default ~^(cmd:|prompt:)(?P<path>.+)$~) and
      =resolver.prompt.path= (default =%s=, on Linux
      =--working-directory=%s=): links that open a terminal in a folder.
    - =resolver.local_file.pattern=: how a file link splits into the path and
      its =::= search (default in =orgresolver/file.py=). The internal
      resolver reads the same key with a different default, so setting it
      changes both.
    - =resolver.local_file.force_into_sublime=: file patterns always opened
      in Sublime even when =file_exclude_patterns= matches (default text,
      code and image extensions). A list, or a comma separated string with
      no quotes or spaces around the patterns.
EDOC */
"""

DEFAULT_OPEN_LINK_COMMANDS = dict(
    # Standard universal can opener for OSX.
    darwin=['open'],
    win32=['cmd', '/C', 'start'],
    linux=['xdg-open'],
)


class AbstractLinkResolver(object):

    def __init__(self, view):
        self.view = view
        self.settings      = sublime.load_settings('OrgEverywhere.sublime-settings')
        self.link_commands = self.settings.get('resolver.abstract.commands', DEFAULT_OPEN_LINK_COMMANDS)

    def extract(self, content):
        return content

    def replace(self, content):
        return content

    def resolve(self, content):
        match = self.extract(content)
        if not match:
            return None
        return self.replace(match)

    def get_link_command(self):
        platform = sys.platform
        for key, val in self.link_commands.items():
            if key in platform:
                return val
        return None

    def execute(self, content):

        if os.path.isfile(content) or os.path.isdir(content):
            content = os.path.normpath(content)
        else:
            print('{} does not exist!'.format(content))

        command = self.get_link_command()
        if not command:
            sublime.error_message(
                'Could not get link opener command.\nPlatform not yet supported.')
            return None

        if sys.version_info[0] < 3:
            content = content.encode(sys.getfilesystemencoding())

        cmd = command + [content]
        arg_list_wrapper = self.settings.get(
            "resolver.abstract.arg_list_wrapper", [])
        if arg_list_wrapper:
            cmd = arg_list_wrapper + [' '.join(cmd)]
            source_filename = '\"' + self.view.file_name() + '\"'
            cmd += [source_filename]
            if sys.platform != 'win32':
                cmd += ['--origin', source_filename, '--quiet']

        print('*****')
        print(repr(content), content)
        print(cmd)
        sublime.status_message('Executing: %s' % cmd)

        if sys.platform != 'win32':
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            stdout, stderr = process.communicate()
            if stdout:
                stdout = str(stdout, sys.getfilesystemencoding())
                sublime.status_message(stdout)
            if stderr:
                stderr = str(stderr, sys.getfilesystemencoding())
                sublime.error_message(stderr)
        else:     
            os.startfile(content)


class AbstractRegexLinkResolver(AbstractLinkResolver):

    def __init__(self, view):
        self.view = view
        self.settings      = sublime.load_settings('OrgEverywhere.sublime-settings')
        self.link_commands = self.settings.get('resolver.abstract.commands', DEFAULT_OPEN_LINK_COMMANDS)
        self.regex         = None

    def extract(self, content):
        if self.regex is None:
            return content
        match = self.regex.match(content)
        return match

    def replace(self, match):
        return match.groups()[1]
