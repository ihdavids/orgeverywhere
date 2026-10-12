import sublime
import sublime_plugin
import re
import os
from   OrgEverywhere.orgparse.sublimenode import * 
import OrgEverywhere.orgutil.util as util
from OrgEverywhere.orgutil.util import *
import logging
import OrgEverywhere.orgdb as db
import OrgEverywhere.asettings as sets
import OrgEverywhere.orgextension as ext
from collections import defaultdict
import struct
import urllib.request
import urllib.parse
import yaml
import OrgEverywhere.orgneovi as nvi
import OrgEverywhere.pymitter as evt
from   OrgEverywhere.orgplist import *
import OrgEverywhere.orguniqueview as uview
import OrgEverywhere.orgs.session as orgs
import OrgEverywhere.orgs.ui as orgsui
import subprocess
import webbrowser

try:
    import importlib
except ImportError:
    pass

log = logging.getLogger(__name__)

# This is entirely copied from the wonderful OrgMode plugin
# found on package control. The orgmode plugin has a great base
# resolver system. I have copied it and will be extending it
# somewhat. OrgEverywhere is not compatible with orgmode
# so I have had to consume it here rather than just recommend
# you use it.


def find_all_links(view):
    links = view.find_by_selector("orgmode.link")
    return links

def extract_link(view):
    pt    = view.sel()[0].begin()
    links = find_all_links(view)
    for link in links:
        if(link.contains(pt)):
            return link
    return None


DEFAULT_LINK_RESOLVERS = [
    'internal',
    'http',
    'https',
    'prompt',
    'jira',
    'email',
    'file',
]

available_resolvers = ext.find_extension_modules('orgresolver', DEFAULT_LINK_RESOLVERS)
linkre              = re.compile(r"\[\[([^\[\]]+)\]\s*(\[[^\[\]]*\])?\]")


# Returns the url from the full link
def extract_link_url(str):
    m = linkre.search(str)
    return m.group(1) if m is not None else str

def extract_link_url_from_region(view, region):
    return extract_link_url(view.substr(region))

def is_region_link(view, region):
    return 'orgmode.link' in view.scope_name(region.end())

def get_link_region_at(view):
    if(is_region_link(view, view.sel()[0])):
        return extract_link(view)
    return None

def find_image_file(view, url):
    # ABS
    if(os.path.isabs(url)):
        return url
    # Relative
    if(view != None):
        curDir = os.path.dirname(view.file_name())
        filename = os.path.join(curDir, url)
        if(os.path.isfile(filename)):
            return filename
    # In search path
    searchHere = sets.Get("imageSearchPath",[])
    for direc in searchHere:
        filename = os.path.join(direc, url)
        if(os.path.isfile(filename)):
            return filename

    searchHere = db.Get().OrgDirs()
    for direc in searchHere:
        filename = os.path.join(direc, "images", url) 
        if(os.path.isfile(filename)):
            return filename

RE_TARGET = re.compile(r'<<(?P<target>[^>]+)>>')
RE_NAMED = re.compile(r'[#][+]NAME[:]\s*(?P<target>.+)')
def CreateLink(view):
    fn = view.file_name()
    # Org Files have a LOT more potential for making links!
    if(util.isPotentialOrgFile(fn)):
        r = view.curRow()
        line = view.getLine(r)
        linet = RE_TARGET.match(line)
        namet = RE_NAMED.match(line)
        link = None
        # have target on this line?
        if(linet):
           link = "[[file:{0}::{1}][{1}]]".format(view.file_name(),linet.group('target'))
        # have named object on this line?
        if(link == None and namet):
           link = "[[file:{0}::{1}][{1}]]".format(view.file_name(),namet.group('target'))
        n = db.Get().AtInView(view)
        if(link == None and n and not n.is_root()):
            p  = n.get_property("ID")
            cp = n.get_property("CUSTOM_ID")
            if(p):
               link = "[[file:{0}::#{1}][{2}]]".format(view.file_name(),p,n.heading)
            # Have custom id?
            elif(cp):
               link = "[[file:{0}::#{1}][{2}]]".format(view.file_name(),cp,n.heading)
            # Am near a heading?
            else:
               link = "[[file:{0}::*{1}][{1}]]".format(view.file_name(),n.heading)
        # okay then just use row,col
        if(link == None):
            r,c = view.curRowCol()
            link = "[[file:{0}::{1},{2}][{3}]]".format(view.file_name(),r,c,os.path.basename(view.file_name()))
        return link
    else:
        # Other file types only have line and column
        r,c = view.curRowCol()
        link = "[[{0}::{1}::{2}][{3}]]".format(fn,r,c,os.path.basename(fn))
        return link

r"""
/* SDOC: Reference::Commands::Links
*** Org Open Link
    Command: =org_open_link=  Keys: =enter= on a link (when the completion popup is not showing)

    Follows the link under the cursor. A link whose protocol the local
    resolvers do not know (anything but =file=, =id=, =http=, =https=,
    =mailto=, =email=, =cmd=, =prompt=, and =jira= / =j= once
    =resolver.jira.url= is set) is offered to the orgs server first, which
    resolves it with its =linkProtocols= config: to a url (opened in the
    browser; anything but http(s) asks first) or a command (always asks
    before running it). When the server does not know it either, or is not
    connected, the local resolvers in =linkResolvers= are tried in order:

    - internal: =[[id:...]]=, =[[#custom-id]]=, =[[*Heading]]=, a =<<target>>=
      or =#+NAME:= in this file. Ids are looked up on the orgs server.
    - http / https: opened with the system opener.
    - prompt: =cmd:DIR= / =prompt:DIR= opens a terminal there.
    - jira: =jira:KEY= / =j:KEY= opens =resolver.jira.url= with the key.
    - email: =mailto:ADDR/SUBJECT= opens your mail program (=email:= links are
      not implemented).
    - file: =file:path=, with =::row::col=, =::#custom-id=, =::*heading= or
      =::text= (target, name or heading); relative to the file, =~= and
      environment variables expanded. Files matching =file_exclude_patterns=
      open in the system's application instead of Sublime.

    An unresolvable link shows an error dialog.
EDOC */
"""

r"""
/* SDOC: Reference::Settings::Links
*** Link and image settings
    | Setting                   | Default | Effect                                                                                                  |
    |---------------------------+---------+---------------------------------------------------------------------------------------------------------|
    | =imageSearchPath=         | =[]=    | Folders inline images are looked for in (after the file's own folder).                                 |
    | =linkFindUseRoamTags=     | =true=  | Org Link To File shows each file's =#+ROAM_TAGS:= in the list.                                          |
    | =insertRoamTagToFileLink= | =true=  | Org Link To File puts the roam tags in the link description too; =false= uses the bare title.          |
    | =backlinksUpdate=         | =true=  | Keep the Backlinks view current when files are saved or re-read by the server.                         |
    | =startup=                 | =["showall", "noinlineimages"]= | Global =#+STARTUP:=; =inlineimages= shows images when a file opens (folding values are read elsewhere). |
EDOC */
"""

class OrgOpenLinkCommand(sublime_plugin.TextCommand):
    def resolve(self, content):
        for resolver in self.resolvers:
            result = resolver.resolve(content)
            if result is not None:
                return resolver, result
        return None, None

    def is_valid_scope(self, region):
        return is_region_link(self.view, region)

    def extract_content(self, region):
        return extract_link_url_from_region(self.view, region)

    def run(self, edit):
        # reload our resolvers if they are not loaded.
        #if(not hasattr(self, "resolvers")):
        wanted_resolvers = sets.Get("linkResolvers", DEFAULT_LINK_RESOLVERS)
        self.resolvers   = [available_resolvers[name].Resolver(self.view) for name in wanted_resolvers]

        # This is goofy. File loads get resolve called that calls extract.
        # extact may launch the file into sublime. IF we return a path
        # sublime will call start on the file.
        view = self.view
        for sel in view.sel():
            if not self.is_valid_scope(sel):
                continue
            region = extract_link(view) #view.extract_scope(sel.end())
            content = self.extract_content(region)
            if ServerShouldResolve(content):
                # orgs may know this protocol (linkProtocols in its config):
                # ask it first, fall back to the local resolvers.
                ResolveViaServer(view, content, lambda content=content: self.resolve_local(content))
                continue
            self.resolve_local(content)

    def resolve_local(self, content):
        resolver, newcontent = self.resolve(content)
        if newcontent is None:
            log.error(" Could not resolve link:\n%s" % content)
            sublime.error_message('Could not resolve link:\n%s' % content)
            return
        resolver.execute(newcontent)


# Protocols the local resolvers know how to follow. Anything else is offered
# to the orgs server first (its linkProtocols setting).
LOCAL_LINK_PROTOCOLS = set(['file', 'id', 'http', 'https', 'mailto', 'email', 'cmd', 'prompt', 'jira', 'j'])
RE_LINK_PROTOCOL = re.compile(r'^(?P<proto>[a-zA-Z][a-zA-Z0-9+.-]+):')
JIRA_SANDBOX_URL = 'http://sandbox.onjira.com/browse/%s'


def LinkProtocol(content):
    m = RE_LINK_PROTOCOL.match(content.strip())
    return m.group('proto').lower() if m else None


def ServerShouldResolve(content):
    if not orgs.connected():
        return False
    proto = LinkProtocol(content)
    if not proto:
        return False
    local = set(LOCAL_LINK_PROTOCOLS)
    # The jira resolver's default url is a placeholder: let the server's
    # linkProtocols win unless the user configured one.
    if sets.Get("resolver.jira.url", JIRA_SANDBOX_URL) == JIRA_SANDBOX_URL:
        local.discard('jira')
        local.discard('j')
    return proto not in local


def ResolveViaServer(view, content, fallback):
    def done(res):
        if not res or not res.get("Ok"):
            fallback()
            return
        if res.get("Url"):
            url = res["Url"]
            scheme = (urllib.parse.urlparse(url).scheme or "").lower()
            if scheme not in ("http", "https") and not sublime.ok_cancel_dialog(
                    "The orgs server resolved\n\n{0}\n\nto\n\n{1}\n\nOpen it?".format(content, url), "Open"):
                return
            sublime.status_message("Opening " + url)
            webbrowser.open(url)
        elif res.get("Command"):
            cmd = res["Command"]
            if isinstance(cmd, str) or not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd):
                sublime.error_message("orgs sent an unusable link command:\n{0}".format(cmd))
                return
            line = res.get("CommandLine") or " ".join(cmd)
            # The server picks the program: the user has to agree to run it.
            if not sublime.ok_cancel_dialog(
                    "The orgs server wants to run this command for the link\n\n{0}\n\n{1}\n\nRun it?".format(
                        content, subprocess.list2cmdline(cmd)), "Run"):
                return
            cwd = None
            if view and view.file_name():
                cwd = os.path.dirname(view.file_name())
            sublime.status_message("Running: " + line)
            try:
                subprocess.Popen(cmd, cwd=cwd)
            except OSError as e:
                sublime.error_message("Could not run link command:\n{0}\n\n{1}".format(line, e))
        else:
            fallback()

    def failed(e):
        log.info("orgs could not resolve %s: %s", content, e)
        fallback()
    # When not connected orgs.run calls failed() itself.
    orgs.run(lambda c: c.resolve_link(content.strip()), done, on_error=failed, what="link")

r"""
/* SDOC: Reference::Commands::Links
*** Org Create Link
    Command: =org_create_link=  Keys: =alt+o y l=; NeoVintageous normal mode: =space y l=

    Puts a link to where the cursor is on the clipboard (and the
    NeoVintageous register), to paste somewhere else. In an org file it
    links, in this order: a =<<target>>= or =#+NAME:= on the line, the
    heading's =ID=, its =CUSTOM_ID=, the heading by title, else the row and
    column. In any other file it links the row and column.
EDOC */
"""

class OrgCreateLinkCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        link = CreateLink(self.view)
        sublime.set_clipboard(link)
        nvi.TestAndSetClip(self.view, link)



# global magic
VIEWS_WITH_IMAGES = set()

# Stolen from:
# https://github.com/renerocksai/sublime_zk/blob/master/sublime_zk.py
# The excellent work in that system showed a way of using
# Phantoms to show images inline. This gives us a part of one of Org Modes
# Most powerful features which is babel modes ability to make diagrams in
# documents.
class ImageHandler:
    Phantoms = defaultdict(set)
    Cache    = {}

    @staticmethod
    def save_cache():
       user_settings_path = os.path.join(sublime.packages_path(), "User","OrgEverywhere_image_cache.yaml")
       f = open(user_settings_path,"w")
       data = yaml.dump(ImageHandler.Cache, f)
       f.close() 

    @staticmethod
    def load_cache():
        user_settings_path = os.path.join(sublime.packages_path(), "User","OrgEverywhere_image_cache.yaml")
        if(os.path.isfile(user_settings_path)):
            stream = open(user_settings_path, 'r')
            ImageHandler.Cache = yaml.load(stream, Loader=yaml.SafeLoader)
            stream.close()

    @staticmethod
    def show_image(region, view, max_width=1024):
        width = -1
        height = -1
        node = db.Get().AtRegion(view,region)
        if(node):
            attr = node.get_comment("ORG_ATTR",None)
            if(attr):
                params = PList.createPList(attr)
                try:
                    width = int(params.Get('width',-1))
                    height = int(params.Get('height',-1))
                except:
                    log.error("Could not extract width and height from plist / ORG_ATTR comment")
        # If we already have this image then exit out
        if view.id() in ImageHandler.Phantoms and str(region) in ImageHandler.Phantoms[view.id()]:
            return
        url    = extract_link_url_from_region(view, region)
        # We can only handle links to images this way.
        if not util.is_image(url):
            return
        level  = db.Get().GetIndentForRegion(view, region)
        indent = "&nbsp;" * (level * 2)
        if url.startswith('http') or url.startswith('https'):
            img = url
            local_filename = None
            if(url in ImageHandler.Cache and os.path.isfile(ImageHandler.Cache[url])):
                local_filename = ImageHandler.Cache[url]
                log.debug("Loaded from cache: " + url)
            else:
                log.debug("Downloaded: " + url)
                local_filename, headers = urllib.request.urlretrieve(url)
                ImageHandler.Cache[url] = local_filename
                ImageHandler.save_cache()
            size = ImageHandler.get_image_size(local_filename)
            ttype = None
            if None != size:
                w, h, ttype = size
                if ttype and ttype == 'svg':
                    view.erase_phantoms(str(region))
                    html_img = util.get_as_string(img)
                    print(html_img)
                    view.add_phantom(str(region), region, html_img, sublime.LAYOUT_BLOCK)
                    ImageHandler.Phantoms[view.id()].add(str(region))
                    return
                FMT = u'''
                    {}<img src="data:image/{}" class="centerImage" {}>
                '''
            img = ttype + ";base64," + util.get_as_base64(img)
        elif url.startswith("file:"):
            url = url.replace("file:","")
            log.debug("FILE: " + url)
            FMT = '''
                {}<img src="file://{}" class="centerImage" {}>
            '''
            img  = find_image_file(view, url)
            if(img):
                size = ImageHandler.get_image_size(img)
                if(width > 0):
                    size = [width, size[1], size[2]]
                if(height > 0):
                    size = [size[0], height, size[2]]
            else:
                size = (100,100,"png")
        else:
            log.debug("local file: " + url)
            FMT = '''
                {}<img src="file://{}" class="centerImage" {}>
            '''
            img  = find_image_file(view, url)
            log.debug("local file2: " + url)
            size = ImageHandler.get_image_size(img)
            if(width > 0):
                size = [width, size[1], size[2]]
            if(height > 0):
                size = [size[0], height, size[2]]
        if not size:
            return
        w, h, t = size
        line_region = view.line(region)
        imgattr = ImageHandler.check_imgattr(view, line_region, region)
        if not imgattr:
            if w > max_width:
                m = max_width / w
                h *= m
                w = max_width
            imgattr = 'width="{}" height="{}"'.format(w, h)

        view.erase_phantoms(str(region))
        html_img = FMT.format(indent, img, imgattr)
        view.add_phantom(str(region), region, html_img, sublime.LAYOUT_BLOCK)
        ImageHandler.Phantoms[view.id()].add(str(region))

    @staticmethod
    def hide_image(region, view):
        try:
            view.erase_phantoms(str(region))
            ImageHandler.Phantoms[view.id()].remove(str(region))
        except:
            pass

    @staticmethod
    def show_image_at(view, max_width=1024):
        reg = get_link_region_at(view)
        if(reg):
            ImageHandler.show_image(reg, view)

    @staticmethod
    def hide_image_at(view, max_width=1024):
        reg = get_link_region_at(view)
        if(reg):
            ImageHandler.hide_image(reg, view)

    @staticmethod
    def show_images(view, max_width=1024):
        global VIEWS_WITH_IMAGES
        skip = 0

        while True:
            imageRegions = view.find_by_selector('orgmode.link')[skip:]
            skip += 1
            if not imageRegions:
                break
            region = imageRegions[0]
            ImageHandler.show_image(region, view, max_width)
        VIEWS_WITH_IMAGES.add(view.id())

    @staticmethod
    def check_imgattr(view, line_region, link_region=None):
        # find attrs for this link
        full_line = view.substr(line_region)
        link_till_eol = full_line[link_region.a - line_region.a:]
        # find attr if present
        m = re.match(r'.*\)\{(.*)\}', link_till_eol)
        if m:
            return m.groups()[0]

    @staticmethod
    def hide_images(view, edit):
        for rel_p in ImageHandler.Phantoms[view.id()]:
            view.erase_phantoms(rel_p)
        del ImageHandler.Phantoms[view.id()]
        skip = 0
        while True:
            img_regs = view.find_by_selector('orgmode.link.href')[skip:]
            skip += 1
            if not img_regs:
                break
            region = img_regs[0]
            rel_p = view.substr(region)
            if(util.is_image(rel_p)):
                pass
                #line_region = view.line(region)
                #line_str = view.substr(line_region)
                #view.replace(edit, line_region, line_str.strip())
        VIEWS_WITH_IMAGES.discard(view.id())

    @staticmethod
    def get_image_size(img):
        """
        Determine the image type of img and return its size.
        """
        #print("IMG: " + img)
        try:
            with open(img, 'rb') as f:
                head = f.read(24)
                ttype = None

                if b'<svg' in head:
                    ttype = 'svg'
                    width, height = (100,100)
                    return width, height, ttype
                if len(head) != 24:
                    return
                if head.startswith(b'\x89PNG\r\n\x1a\n'):
                    ttype = "png"
                    check = struct.unpack('>i', head[4:8])[0]
                    if check != 0x0d0a1a0a:
                        return
                    width, height = struct.unpack('>ii', head[16:24])
                elif head[:6] in (b'GIF87a', b'GIF89a'):
                    ttype = "gif"
                    width, height = struct.unpack('<HH', head[6:10])
                elif head.startswith(b'\xff\xd8'):
                    ttype = "jpeg"
                    try:
                        f.seek(0)  # Read 0xff next
                        size = 2
                        ftype = 0
                        while not 0xc0 <= ftype <= 0xcf:
                            f.seek(size, 1)
                            byte = f.read(1)
                            while ord(byte) == 0xff:
                                byte = f.read(1)
                            ftype = ord(byte)
                            size = struct.unpack('>H', f.read(2))[0] - 2
                        # SOFn block
                        f.seek(1, 1)  # skip precision byte.
                        height, width = struct.unpack('>HH', f.read(4))
                    except Exception:
                        return
                else:
                    return
                return width, height, ttype
        except:
            return 100,100,'png'

r"""
/* SDOC: Reference::Commands::Links
*** Redisplay inline images
    Command: =org_cycle_images=  Keys: none  (no palette entry)  Args: =onDone=

    Hides and then shows again every inline image in the view (Org Hide
    Images then Org Show Images), keeping the cursor; for after an image
    file changed on disk.
EDOC */
"""

class OrgCycleImagesCommand(sublime_plugin.TextCommand):
    def OnDone(self):
        self.view.sel().clear()
        self.view.sel().add(self.cursor)
        evt.EmitIf(self.onDone)

    def OnShown(self):
        self.OnDone()

    def OnHidden(self):
        self.view.run_command("org_show_images",{"onDone": evt.Make(self.OnShown)})

    def run(self, edit, onDone=None):
        self.onDone = onDone
        self.cursor = self.view.sel()[0]
        self.view.run_command("org_hide_images",{"onDone": evt.Make(self.OnHidden)})

r"""
/* SDOC: Reference::Commands::Links
*** Org Show Images
    Command: =org_show_images=  Keys: none  Args: =onDone=

    Shows every image link in the view inline, below its line, scaled to at
    most 1024 pixels wide. Local images are looked for relative to the file,
    then in =imageSearchPath=, then in an =images= folder of each org
    directory; =http(s)= images are downloaded once and cached (the cache
    list is =Packages/User/OrgEverywhere_image_cache.yaml=). A =#+ORG_ATTR:=
    with =:width= / =:height= above the link, or ={width=... height=...}=
    after it, sets the size. =#+STARTUP: inlineimages= (or =startup=) shows
    them when the file opens.
EDOC */
"""

class OrgShowImagesCommand(sublime_plugin.TextCommand):
    def run(self, edit,onDone=None):
        ImageHandler.show_images(self.view)
        evt.EmitIf(onDone)

r"""
/* SDOC: Reference::Commands::Links
*** Org Hide Images
    Command: =org_hide_images=  Keys: none  Args: =onDone=

    Removes every inline image from the view.
EDOC */
"""

class OrgHideImagesCommand(sublime_plugin.TextCommand):
    def run(self, edit,onDone=None):
        ImageHandler.hide_images(self.view, edit)
        evt.EmitIf(onDone)

r"""
/* SDOC: Reference::Commands::Links
*** Org Show Image / Org Hide Image
    Command: =org_show_image=, =org_hide_image=  Keys: none

    Shows or hides the inline image of the link under the cursor only.
EDOC */
"""

class OrgShowImageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        ImageHandler.show_image_at(self.view)

class OrgHideImageCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        ImageHandler.hide_image_at(self.view)

# ON LOAD HANDLER: if startup is set we show or hide the images.
#+STARTUP: inlineimages
#+STARTUP: noinlineimages
def get_show_images_default():
    return sets.Get("startup",["noinlineimages"])

def get_image_startup(node):
    startupDefault = get_show_images_default()
    return node.startup(startupDefault)

def onShutdown():
    ImageHandler.save_cache()

def onLoad(view):
    ImageHandler.load_cache()
    file = db.Get().FindInfo(view)
    if(file):
        startup = get_image_startup(file.org[0])
        if(Startup.inlineimages in startup):
            ImageHandler.show_images(view)




RE_HEADER_COMMENT = re.compile(r'^\s*#\+(?P<key>TITLE|ROAM_TAGS)\s*:\s*(?P<value>.*)$', re.IGNORECASE)
RE_FIRST_HEADING = re.compile(r'^\*+\s')


def FileHeader(filename, maxLines=500):
    """(title, roam tags) from the comments above a file's first heading.
    Reads only the top of the file, no full parse."""
    titles = []
    tags = []
    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= maxLines or RE_FIRST_HEADING.match(line):
                    break
                m = RE_HEADER_COMMENT.match(line)
                if m:
                    val = m.group('value').strip()
                    if m.group('key').upper() == "TITLE":
                        titles.append(val)
                    else:
                        tags.append(val)
    except (IOError, OSError):
        pass
    return " ".join(titles).strip(), " ".join(tags).strip()


r"""
/* SDOC: Reference::Commands::Links
*** Org Link To File
    Command: =org_link_to_file=  Keys: none

    Inserts a =[[file:...][title]]= link to another org file at the cursor,
    relative to this file. The quick panel lists every org file the orgs
    server knows, with its =#+TITLE:= and, with =linkFindUseRoamTags=, its
    =#+ROAM_TAGS:= in parentheses. The description is the title (with the
    roam tags when =insertRoamTagToFileLink= is on), or the file name.
    Needs the orgs server.
EDOC */
"""

class OrgLinkToFileCommand(sublime_plugin.TextCommand):
    def on_done(self, index, modifiers=None):
        if(index >= 0):
            f = self.files[index]
            link = self.view.MakeRelativeToMe(f[0])
            desc = os.path.basename(link)
            if(len(f) > 1):
                desc = f[1]
                includeRoamTag = sets.Get("insertRoamTagToFileLink", True)
                if includeRoamTag is False:
                    desc = self.rawTitles[f[0]]
            indent = ""
            node = db.Get().AtInView(self.view)
            if(node):
                indent = node.indent()
            data = r"{indent}[[file:{link}][{desc}]]".format(indent=indent, link=link, desc=desc)
            self.view.run_command("org_internal_insert", {"location": self.view.sel()[0].begin(), "text": data})

    def run(self, edit):
        useRoamTags = sets.Get("linkFindUseRoamTags", True)

        # The server's file list; titles come from the top of each file.
        def collect(c):
            files = []
            rawTitles = {}
            for filename in sorted(c.files(), key=lambda x: x.lower()):
                title, tags = FileHeader(filename)
                rawTitles[filename] = title
                if(useRoamTags and tags != ""):
                    title = "(" + tags + ") " + title
                if(title != ""):
                    files.append([filename, title])
                else:
                    files.append([filename])
            return files, rawTitles

        def show(res):
            self.files, self.rawTitles = res
            if not self.files:
                sublime.status_message("orgs: no org files")
                return
            self.view.window().show_quick_panel(self.files, self.on_done, -1, -1)
        orgs.run(collect, show, what="files")


# ------------------------------------------------------------------
# Backlinks: links in other files that point at this one, from orgs.

def FetchBacklinks(c, filename):
    """(worker thread) The server's link dicts pointing into filename."""
    res = c.links(filename)
    return [l for l in (res.get("In") or []) if l.get("From")]


def BacklinkLabel(l):
    return l.get("Desc") or l.get("Raw") or ""


def BacklinkDetail(l, titles=None):
    frm = l.get("From") or {}
    fn = frm.get("Filename") or ""
    title = (titles or {}).get(fn) or orgsui.short_file(fn)
    olp = " / ".join(frm.get("Olp") or []) or frm.get("Headline") or ""
    return "{0}:{1}  {2}".format(title, (frm.get("Line") or 0) + 1, olp).strip()


def BuildBacklinksDisplay(links, titles=None):
    """Text for the Backlinks view from the server's link dicts."""
    if not links:
        return "0 Backlinks"
    titles = titles or {}
    out = str(len(links)) + " Backlinks\n"
    byFile = {}
    for l in links:
        byFile.setdefault(l["From"].get("Filename") or "", []).append(l)

    def fileTitle(fn):
        return titles.get(fn) or orgsui.short_file(fn)
    for fn in sorted(byFile.keys(), key=lambda x: fileTitle(x).lower()):
        out += "\n* {}\n".format(fileTitle(fn))
        for l in sorted(byFile[fn], key=lambda x: x["From"].get("Line") or 0):
            frm = l["From"]
            desc = BacklinkLabel(l).replace("[", "(").replace("]", ")")
            out += "  - [[file:{}::{}][{}]]".format(fn, (frm.get("Line") or 0) + 1, desc)
            olp = " / ".join(frm.get("Olp") or []) or frm.get("Headline") or ""
            if olp:
                out += "  :: " + olp
            if l.get("Broken"):
                out += "  (broken)"
            out += "\n"
    return out


def BacklinksText(c, filename):
    """(worker thread) Fetch and format the Backlinks view for filename."""
    links = FetchBacklinks(c, filename)
    titles = {}
    for l in links:
        fn = l["From"].get("Filename")
        if fn and fn not in titles:
            titles[fn] = FileHeader(fn)[0]
    return BuildBacklinksDisplay(links, titles)


BACKLINKS_VIEW = "Backlinks"
_backlinks = {"gen": 0, "subject": None}


def RenderBacklinks(text, curview=None):
    uv = uview.UniqueView.Get(BACKLINKS_VIEW, curview=curview)
    text = text + "\n"
    if uv.view.substr(sublime.Region(0, uv.view.size())) == text:
        return
    uv.view.run_command("org_remove_all_folds")
    uv.view.set_read_only(False)
    uv.view.run_command("org_internal_replace", {"start": 0, "end": uv.view.size(), "text": text})
    uv.view.set_read_only(True)
    uv.view.run_command("org_fold_all_links")
    uv.view.sel().clear()


def RequestBacklinksUpdate(filename, delay=300):
    """Refresh the Backlinks view for filename soon (debounced, async)."""
    _backlinks["subject"] = filename
    _backlinks["gen"] += 1
    gen = _backlinks["gen"]

    def fetch():
        if gen != _backlinks["gen"] or not orgs.connected() or not uview.UniqueView.IsShowing(BACKLINKS_VIEW):
            return

        def done(text):
            if gen == _backlinks["gen"] and uview.UniqueView.IsShowing(BACKLINKS_VIEW):
                RenderBacklinks(text)

        def failed(e):
            log.debug("backlinks update failed: %s", e)
        orgs.run(lambda c: BacklinksText(c, filename), done, on_error=failed)
    sublime.set_timeout(fetch, delay)


def UpdateBacklinksForDisplay(view):
    """Called when an org view is activated: follow it in the Backlinks view."""
    if not uview.UniqueView.IsShowing(BACKLINKS_VIEW):
        return
    fn = view.file_name() if view else None
    if not fn or not util.isPotentialOrgFile(fn):
        return
    if fn == _backlinks["subject"]:
        return
    RequestBacklinksUpdate(fn)


def OnServerEvent(ev):
    # Any file may have gained or lost a link to the one on show.
    if ev.get("kind") == "reload" and _backlinks["subject"] and sets.Get("backlinksUpdate", True):
        RequestBacklinksUpdate(_backlinks["subject"], 800)


class BacklinksEventListener(sublime_plugin.EventListener):
    def on_post_save_async(self, view):
        # Covers servers without an event stream; the reload event does the rest.
        if (_backlinks["subject"] and sets.Get("backlinksUpdate", True)
                and util.isPotentialOrgFile(view.file_name())):
            RequestBacklinksUpdate(_backlinks["subject"], 1000)


def plugin_loaded():
    orgs.on_event(OnServerEvent)


def plugin_unloaded():
    orgs.off_event(OnServerEvent)


r"""
/* SDOC: Reference::Commands::Links
*** Org Jump To Backlink
    Command: =org_jump_to_backlinks=  Keys: none

    Lists the links in other files that point at the current file (from the
    orgs server's link index): their description, and the file title, line
    and heading they are in. Picking one jumps there. Needs the orgs server.
EDOC */
"""

class OrgJumpToBacklinksCommand(sublime_plugin.TextCommand):
    def on_done(self, index, modifiers=None):
        if(index >= 0):
            frm = self.links[index]["From"]
            orgsui.goto(self.view.window(), frm.get("Filename"), frm.get("Line") or 0)

    def run(self, edit):
        fn = self.view.file_name()
        if not fn:
            sublime.status_message("Backlinks: this buffer has no file")
            return

        def collect(c):
            links = FetchBacklinks(c, fn)
            titles = {}
            for l in links:
                f = l["From"].get("Filename")
                if f and f not in titles:
                    titles[f] = FileHeader(f)[0]
            return links, titles

        def show(res):
            self.links, titles = res
            if not self.links:
                sublime.status_message("No backlinks to " + os.path.basename(fn))
                return
            items = [[BacklinkLabel(l), BacklinkDetail(l, titles)] for l in self.links]
            self.view.window().show_quick_panel(items, self.on_done, -1, -1)
        orgs.run(collect, show, what="backlinks")


r"""
/* SDOC: Reference::Commands::Links
*** Org Show Backlinks
    Command: =org_show_backlinks=  Keys: none

    Opens a "Backlinks" view beside the file: the links pointing at it,
    grouped by source file (by title), each an org link to its line with
    the heading path, broken links marked. While it is showing it follows
    the org file you switch to, and refreshes when files are saved or the
    server re-reads them (=backlinksUpdate=). Needs the orgs server.
EDOC */
"""

class OrgShowBacklinksCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if not fn:
            sublime.status_message("Backlinks: this buffer has no file")
            return
        view = self.view
        _backlinks["subject"] = fn
        _backlinks["gen"] += 1
        orgs.run(lambda c: BacklinksText(c, fn), lambda text: RenderBacklinks(text, curview=view), what="backlinks")


r"""
/* SDOC: Reference::Commands::Links
*** Org Search Links
    Command: =org_search_links=  Keys: none

    Lists every link in every org file the orgs server knows (description
    and target, with file, line and heading) and jumps to the one you pick,
    previewing as you move. Needs the orgs server.
EDOC */
"""

class OrgSearchLinksCommand(sublime_plugin.TextCommand):
    def close_preview(self):
        # Only close a preview we opened, never a view that was already open.
        if(self.transientview is not None):
            if self.transientview.id() not in self.openViews and self.transientview.is_valid():
                self.transientview.close()
            self.transientview = None

    def on_done(self, index, modifiers=None):
        self.close_preview()
        if(index >= 0):
            l = self.lobjs[index]
            orgsui.goto(self.view.window(), l.get("Filename"), l.get("Line") or 0)

    def on_highlighted(self, index, modifiers=None):
        self.close_preview()
        if(index >= 0):
            l = self.lobjs[index]
            self.transientview = orgsui.goto(self.view.window(), l.get("Filename"), l.get("Line") or 0, transient=True)

    def run(self, edit):
        self.transientview = None
        window = self.view.window()

        def show(res):
            self.lobjs = (res or {}).get("Links") or []
            if not self.lobjs:
                sublime.status_message("orgs: no links found")
                return
            self.links = []
            for l in self.lobjs:
                desc = l["Desc"] + " : " if l.get("Desc") else ""
                where = "{0}:{1}  {2}".format(orgsui.short_file(l.get("Filename")), (l.get("Line") or 0) + 1, l.get("Heading") or "")
                self.links.append([desc + (l.get("Raw") or ""), where.strip()])
            self.openViews = set(v.id() for v in window.views())
            window.show_quick_panel(self.links, self.on_done, -1, -1, self.on_highlighted)
        orgs.run(lambda c: c.all_links(), show, what="links")
