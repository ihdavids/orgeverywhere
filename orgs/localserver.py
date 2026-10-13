"""A local orgs server that OrgEverywhere downloads, sets up and runs.

Pure python, like client.py: nothing here imports sublime, so the whole life
cycle (download, config, start, stop, users) can be tested from a command
line. orgsserver.py is the sublime side: the commands, the dialogs, and when
to start or update.

Everything lives in one folder (data_dir()): the orgs binary, its orgs.yaml,
the keystore, a self-signed certificate, the log and a little state.json.
The server is started detached, so it outlives the Sublime that started it
and is shared by every window; a Sublime that finds it not answering starts
it again.

It serves https and nothing else, on the loopback only, with a certificate
=orgs cert= makes beside it. OrgEverywhere trusts that one certificate for
that one address - not every self-signed one.

The orgs command line beside it reads that server's orgs.yaml, keys and all,
so it makes its own token and nobody is asked for a password.

The same server set up by the vscode plugin (vscode-org-everywhere) works the
same way; each keeps its own copy, with its own login.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile

from .client import client_hash, parse_time

log = logging.getLogger(__name__)

ORGS_REPO = "ihdavids/orgs"
# The account OrgEverywhere makes in the keystore and logs in with.
ORGS_USER = "sublimetext"
DEFAULT_PORT = 8010
# How long a server gets to read its org files and start listening.
START_TIMEOUT = 90.0
# Where the random password for ORGS_USER is kept (creds store key).
PASSWORD_KEY = "orgs-local-server"
USER_AGENT = "org-everywhere-sublime"


# ----------------------------------------------------------------------
# The parts with no state: release names, checksums, versions, the yaml.

def asset_name(tag, platform=None, machine=None):
    """goreleaser's name for the archive (.goreleaser.yaml in orgs):
    orgs_0.1.0_darwin_arm64.tar.gz, orgs_0.1.0_windows_amd64.zip. None for a
    machine there is no build for."""
    platform = platform or sys.platform
    machine = (machine or _machine()).lower()
    if platform == "darwin":
        osname = "darwin"
    elif platform.startswith("linux"):
        osname = "linux"
    elif platform == "win32":
        osname = "windows"
    else:
        return None
    cpu = {"x86_64": "amd64", "amd64": "amd64", "x64": "amd64",
           "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not cpu:
        return None
    return "orgs_{0}_{1}_{2}.{3}".format(re.sub(r"^v", "", tag), osname, cpu,
                                         "zip" if osname == "windows" else "tar.gz")


def _machine():
    import platform
    m = platform.machine()
    # Sublime's x64 build under Rosetta says x86_64 on an arm mac; the arm64
    # build of orgs is the one that runs natively there.
    if sys.platform == "darwin" and m == "x86_64":
        try:
            out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5).stdout.strip()
            if out == b"1":
                return "arm64"
        except Exception:
            pass
    return m


def binary_name(platform=None):
    return "orgs.exe" if (platform or sys.platform) == "win32" else "orgs"


def parse_checksums(text):
    """checksums.txt: "<sha256>  <file name>" a line."""
    sums = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S+)$", line.strip())
        if m:
            sums[m.group(2)] = m.group(1).lower()
    return sums


def _version_parts(v):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return [int(m.group(i)) for i in (1, 2, 3)] if m else None


def is_newer(a, b):
    """Whether release tag a is newer than b. Something that is not a version
    is never newer, and anything is newer than nothing installed."""
    pa = _version_parts(a)
    if not pa:
        return False
    pb = _version_parts(b or "")
    if not pb:
        return True
    return pa > pb


def _q(s):
    # A yaml double-quoted string is a json string.
    return json.dumps(s)


def _https_settings(port, crt, key):
    return [
        ("port", str(port)),
        ("tlsport", str(port)),
        ("allowHttps", "true"),
        ("httpsOnly", "true"),
        ("localhostOnly", "true"),
        ("servercrt", _q(crt)),
        ("serverkey", _q(key)),
    ]


def orgs_yaml(port, org_dirs, keystore, salt, jws, jwe, day_page_path, tls=None):
    """The orgs.yaml for a server only this machine's user talks to: https
    only with a self-signed certificate (tls = (crt, key)), on the loopback,
    logins against its own keystore, babel off. Without tls it is http - for
    an orgs too old to make a certificate."""
    if tls:
        listen = ["  {0}: {1}".format(k, v) for k, v in _https_settings(port, tls[0], tls[1])]
    else:
        listen = ["  port: {0}".format(port), "  allowHttps: false", "  localhostOnly: true"]
    return "\n".join([
        "# Written by OrgEverywhere (Org: Set Up a Local orgs Server), and yours to edit.",
        "# Saving it from Sublime reloads the server; a change to the port, orgDirs or",
        "# the keystore needs Org: Restart Local orgs Server. Every setting is described at",
        "# https://github.com/{0}".format(ORGS_REPO),
        "",
        "# Where the orgs command line finds the server.",
        "url: " + _q("{0}://localhost:{1}".format("https" if tls else "http", port)),
        "server:",
    ] + listen + [
        "  keystore: " + _q(keystore),
        "  orgSalt: " + _q(salt),
        "  orgJWS: " + _q(jws),
        "  orgJWE: " + _q(jwe),
        "  orgDirs:",
    ] + ["    - " + _q(d) for d in org_dirs] + [
        "  dayPagePath: " + _q(day_page_path),
        "",
    ])


def _server_block(lines, server_at):
    """The lines that are direct settings of the server: block starting at
    server_at, and how far they are indented."""
    indent = -1
    children = []
    for i in range(server_at + 1, len(lines)):
        line = lines[i]
        if re.match(r"^\s*(#|$)", line):
            continue
        lead = len(line) - len(line.lstrip())
        if lead == 0:
            break
        if indent < 0:
            indent = lead
        if lead == indent:
            children.append(i)
    return (2 if indent < 0 else indent), children


def _find_server(lines):
    for i, l in enumerate(lines):
        if re.match(r"^server:\s*(#.*)?$", l):
            return i
    return -1


def with_https(yaml, port, crt, key):
    """An existing orgs.yaml switched to https only (the settings orgs_yaml
    writes for it), keeping everything else in it as it was."""
    lines = yaml.splitlines()
    url = "url: " + _q("https://localhost:{0}".format(port))
    url_at = next((i for i, l in enumerate(lines) if l.startswith("url:")), -1)
    if url_at >= 0:
        lines[url_at] = url
    else:
        lines.insert(0, url)
    server_at = _find_server(lines)
    if server_at < 0:
        lines.append("server:")
        server_at = len(lines) - 1
    indent, children = _server_block(lines, server_at)
    added = 0
    for k, v in _https_settings(port, crt, key):
        line = " " * indent + "{0}: {1}".format(k, v)
        at = next((i for i in children if re.match(r"^\s*" + k + ":", lines[i])), None)
        if at is not None:
            lines[at] = line
        else:
            # In order, at the top of server:, ahead of what was there.
            lines.insert(server_at + 1 + added, line)
            added += 1
            children = [i + 1 for i in children]
    return "\n".join(lines) + "\n"


def server_value(yaml, key):
    """A setting directly under server: in an orgs.yaml, unquoted, or None.
    Line based, which is enough for the file orgs_yaml writes and most
    anything somebody edits it into."""
    lines = yaml.splitlines()
    server_at = _find_server(lines)
    if server_at < 0:
        return None
    rx = re.compile(r"^\s*" + re.escape(key) +
                    r":\s*(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|([^#]*?))\s*(#.*)?$")
    for i in _server_block(lines, server_at)[1]:
        m = rx.match(lines[i])
        if m:
            if m.group(1) is not None:
                return json.loads('"' + m.group(1) + '"')
            return m.group(2) if m.group(2) is not None else m.group(3)
    return None


def config_listen(yaml):
    """(scheme, port) the server this orgs.yaml describes answers on: https
    on its tlsport when it serves nothing else, http on its port otherwise.
    port is None when the file does not say."""
    if server_value(yaml, "httpsOnly") == "true":
        v = server_value(yaml, "tlsport")
        return "https", (int(v) if v and v.isdigit() else 443)
    v = server_value(yaml, "port")
    return "http", (int(v) if v and v.isdigit() else None)


def is_local_url(url, port, scheme="http"):
    """Whether url is this machine on port, by scheme."""
    m = re.match(r"^(https?)://(localhost|127(?:\.\d{1,3}){3}|\[::1\]):(\d+)/*$", (url or "").strip(), re.I)
    return bool(m) and m.group(1).lower() == scheme and int(m.group(3)) == port


def username_problem(name):
    """Why orgs would refuse this as a user name (common.ValidUsername), or
    None when it would not."""
    if not name:
        return "a user needs a name"
    if len(name) > 64:
        return "that name is longer than 64 characters"
    if re.search(r"\s", name):
        return "a user name cannot have whitespace in it"
    if re.search(r"[:/?#\[\]@]", name):
        return "a user name cannot contain any of  : / ? # [ ] @"
    return None


def new_salt():
    """A fresh salt for a new user, as orgs' common.NewSalt makes one."""
    return secrets.token_hex(16)


def _random(nbytes):
    return base64.b64encode(secrets.token_bytes(nbytes)).decode("ascii")


def data_dir():
    """Where the local server lives: the OS's place for an app's own data,
    not Packages/User (which people sync, and which would carry the keys)."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "OrgEverywhere", "orgs")


VSCODE_EXTENSION = "iorg.vscode-org-everywhere"
VSCODE_APPS = ("Code", "Code - Insiders", "VSCodium", "Cursor")


def vscode_dirs():
    """Where the vscode plugin (vscode-org-everywhere) keeps the local server
    it sets up: its globalStorage, in each VS Code that might have it."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(base, app, "User", "globalStorage", VSCODE_EXTENSION, "orgs") for app in VSCODE_APPS]


def vscode_servers(dirs=None):
    """The local servers the vscode plugin has set up on this machine."""
    return [s for s in (VSCodeServer(d) for d in (dirs or vscode_dirs())) if s.installed()]


# ----------------------------------------------------------------------
# Downloads

def _urlopen(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _is_cert_error(e):
    reason = getattr(e, "reason", e)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason)


def _curl():
    return shutil.which("curl")


def fetch_text(url, timeout=15.0):
    """GET url as text. Sublime's python does not always find the system's
    certificate authorities; curl, which does, is the fallback."""
    try:
        with _urlopen(url, timeout) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise OSError("{0} answered {1}".format(url, e.code))
    except (urllib.error.URLError, ssl.SSLError) as e:
        if not (_is_cert_error(e) and _curl()):
            raise OSError("{0}: {1}".format(url, getattr(e, "reason", e)))
    out = _run([_curl(), "-fsSL", "--max-time", str(int(timeout)), "-A", USER_AGENT, url])
    return out


def download(url, dest, progress=None):
    """Stream url to dest; return the sha256 of what was written.
    progress(got, total) as it goes."""
    h = hashlib.sha256()
    try:
        with _urlopen(url, 60.0) as r, open(dest, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
        return h.hexdigest()
    except urllib.error.HTTPError as e:
        raise OSError("{0} answered {1}".format(url, e.code))
    except (urllib.error.URLError, ssl.SSLError) as e:
        if not (_is_cert_error(e) and _curl()):
            raise OSError("{0}: {1}".format(url, getattr(e, "reason", e)))
    _run([_curl(), "-fsSL", "-A", USER_AGENT, "-o", dest, url], timeout=600)
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class Release:
    def __init__(self, tag, page, assets):
        self.tag = tag
        self.page = page
        self.assets = assets   # name -> download url


def latest_release():
    data = json.loads(fetch_text("https://api.github.com/repos/{0}/releases/latest".format(ORGS_REPO)))
    return Release(data.get("tag_name"), data.get("html_url"),
                   dict((a["name"], a["browser_download_url"]) for a in data.get("assets") or []))


def _run(args, cwd=None, timeout=60):
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        raise OSError("{0} {1}: took longer than {2}s".format(os.path.basename(args[0]), args[1] if len(args) > 1 else "", timeout))
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
        # "orgs user: ..." - the program and its subcommand, not the paths.
        sub = next((a for a in args[1:] if not a.startswith("-") and not os.path.isabs(a)), "")
        raise OSError("{0} {1}: {2}".format(os.path.basename(args[0]), sub, err).replace("  ", " "))
    return p.stdout.decode("utf-8", "replace")


def _extract_binary(archive, into, name):
    """Pull the one binary out of a release archive (by name, wherever it is
    in it). Nothing else is unpacked, so a hostile path cannot land
    anywhere."""
    dest = os.path.join(into, name)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if os.path.basename(info.filename) == name and not info.is_dir():
                    with z.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    return dest
    else:
        with tarfile.open(archive, "r:gz") as t:
            for m in t.getmembers():
                if os.path.basename(m.name) == name and m.isfile():
                    with t.extractfile(m) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    return dest
    raise OSError("{0} has no {1} in it".format(os.path.basename(archive), name))


# ----------------------------------------------------------------------
# The server

class LocalServer:
    def __init__(self, directory=None):
        self.dir = directory or data_dir()

    # Paths
    def path(self, *p):
        return os.path.join(self.dir, *p)

    @property
    def bin(self):
        return self.path(binary_name())

    @property
    def config(self):
        return self.path("orgs.yaml")

    @property
    def crt(self):
        return self.path("server.crt")

    @property
    def key(self):
        return self.path("server.key")

    @property
    def log_file(self):
        return self.path("server.log")

    @property
    def keystore(self):
        return self.path("keystore.yaml")

    def installed(self):
        return os.path.exists(self.bin) and os.path.exists(self.config)

    # State: the installed version, when github was last asked, a skipped
    # release, whether setting one up has been offered.
    def state(self):
        try:
            with open(self.path("state.json"), "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def set_state(self, **kw):
        d = self.state()
        d.update(kw)
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path(".state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, self.path("state.json"))

    @property
    def version(self):
        return self.state().get("version")

    # Where it answers
    def listen(self):
        try:
            with open(self.config, "r", encoding="utf-8") as f:
                scheme, port = config_listen(f.read())
            return scheme, port or DEFAULT_PORT
        except Exception:
            return "http", DEFAULT_PORT

    @property
    def https(self):
        return self.listen()[0] == "https"

    @property
    def url(self):
        scheme, port = self.listen()
        return "{0}://localhost:{1}".format(scheme, port)

    def is_managed(self, url):
        """Whether url is this server, however the address is spelled."""
        if not self.installed():
            return False
        scheme, port = self.listen()
        return is_local_url((url or "").rstrip("/"), port, scheme)

    def cafile(self):
        """The certificate to trust for this server, when it has one."""
        return self.crt if self.https and os.path.exists(self.crt) else None

    def is_up(self):
        """Whether an orgs server answers on our port: /status is 200, or
        401/403 when it wants a login. Anything else there is not one.

        The certificate is not checked: nothing is sent but the question,
        and right after a renewal the running server still has the old one -
        checking it would call the server down and it would never be
        restarted onto the new one."""
        ctx = ssl._create_unverified_context() if self.https else None
        try:
            with urllib.request.urlopen(self.url + "/status", timeout=2, context=ctx) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code in (401, 403)
        except Exception:
            return False

    # Command line
    def cli(self, args, timeout=60):
        """This server's own orgs as a command line on its config - for
        `orgs user`, which edits the keystore file rather than asking the
        server, and `orgs cert`."""
        return _run([self.bin, "-config", self.config] + list(args), cwd=self.dir, timeout=timeout)

    # Releases
    def fetch_binary(self, release, report=None):
        """Download, verify and unpack the release's binary into a staging
        folder, leaving anything running alone. Returns the staged binary."""
        report = report or (lambda msg: None)
        name = asset_name(release.tag)
        if not name or name not in release.assets:
            raise OSError("orgs {0} has no build for {1}/{2}".format(release.tag, sys.platform, _machine()))
        sums_url = release.assets.get("checksums.txt")
        if not sums_url:
            raise OSError("orgs {0} has no checksums.txt to check the download against".format(release.tag))
        staging = self.path("download")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        want = parse_checksums(fetch_text(sums_url)).get(name)
        if not want:
            raise OSError("checksums.txt does not list " + name)
        archive = os.path.join(staging, name)
        shown = [-1]

        def progress(got, total):
            pct = int(got * 100 / total) if total else 0
            if pct // 5 != shown[0]:
                shown[0] = pct // 5
                report("downloading orgs {0}: {1}% of {2:.0f} MB".format(release.tag, pct, total / 1e6))
        got = download(release.assets[name], archive, progress)
        if got != want:
            raise OSError("the download of {0} does not match its checksum - not installing it".format(name))
        report("unpacking")
        unpacked = os.path.join(staging, "x")
        os.makedirs(unpacked)
        b = _extract_binary(archive, unpacked, binary_name())
        os.chmod(b, 0o755)
        return b

    def place_binary(self, staged, tag):
        os.replace(staged, self.bin)
        shutil.rmtree(self.path("download"), ignore_errors=True)
        self.set_state(version=tag)

    # Config
    @staticmethod
    def free_port(preferred=DEFAULT_PORT):
        for p in (preferred, 0):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
            except OSError:
                continue
            finally:
                s.close()
        raise OSError("no free port")

    def make_certificate(self, force=False):
        """A self-signed certificate for this machine, from orgs itself. force
        replaces one already there. False when this orgs cannot make one."""
        try:
            self.cli(["cert", "-crt", self.crt, "-key", self.key] + (["-force"] if force else []))
        except OSError as e:
            log.info("orgs: no certificate: %s", e)
            return False
        return os.path.exists(self.crt) and os.path.exists(self.key)

    def certificate_expires(self):
        """When the certificate runs out (aware datetime), or None."""
        try:
            out = self.cli(["cert", "-json", "-crt", self.crt, "-key", self.key])
            return parse_time(json.loads(out).get("expires"))
        except Exception:
            return None

    def configure(self, org_dir):
        """Write a fresh orgs.yaml, certificate and keystore for org_dir, and
        add ORGS_USER as an administrator with a random password. Returns
        that password, for the caller to keep."""
        port = self.free_port()
        args = dict(port=port, org_dirs=[org_dir], keystore=self.keystore, salt=_random(24),
                    jws=_random(32), jwe=_random(32), day_page_path=os.path.join(org_dir, "daypages"))
        # https only, unless this orgs cannot make the certificate for it:
        # then http until an update that can (upgrade_to_https).
        self._write_config(orgs_yaml(**args))
        if self.make_certificate(force=True):
            self._write_config(orgs_yaml(tls=(self.crt, self.key), **args))
        # A fresh keystore each time: its salt is the one just written.
        try:
            os.remove(self.keystore)
        except OSError:
            pass
        password = _random(24)
        # -password because there is no terminal to be asked on. It is on
        # this process's command line for the moment orgs takes to hash it.
        self.cli(["user", "add", ORGS_USER, "-admin", "-password", password])
        return password

    def _write_config(self, text):
        fd = os.open(self.config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    def upgrade_to_https(self):
        """A server on http (set up by an orgs that could not make a
        certificate) moves to https only once its orgs can. The caller
        restarts it. False when it cannot yet, or is https already."""
        if not self.installed() or self.https:
            return False
        # Not forced: another Sublime doing the same at the same moment gets
        # the same pair rather than one the other replaced.
        if not self.make_certificate():
            return False
        _, port = self.listen()
        with open(self.config, "r", encoding="utf-8") as f:
            yaml = f.read()
        self._write_config(with_https(yaml, port, self.crt, self.key))
        return True

    # Running
    def _pid(self):
        try:
            with open(self.path("orgs.pid")) as f:
                pid = int(f.read().strip())
            return pid if pid > 0 else None
        except Exception:
            return None

    @staticmethod
    def _is_orgs(pid):
        """Whether pid is still an orgs - a pid file can outlive its process
        and the number be handed to something else."""
        try:
            if sys.platform == "win32":
                out = _run(["tasklist", "/FI", "PID eq {0}".format(pid), "/NH"])
            else:
                out = _run(["ps", "-p", str(pid), "-o", "comm="])
            return re.search(r"\borgs(\.exe)?\b", out, re.I) is not None
        except OSError:
            return False

    def _last_lines(self, n=5):
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                return "\n".join(f.read().rstrip().splitlines()[-n:])
        except Exception:
            return ""

    def start(self):
        """Start the server unless one already answers, and wait until it
        does. Raises OSError when it does not come up."""
        if self.is_up():
            return
        lock = self.path("start.lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Another Sublime (or vscode on the same folder) is starting it.
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                age = 0
            if age > START_TIMEOUT:
                try:
                    os.remove(lock)
                except OSError:
                    pass
                return self.start()
            end = time.time() + START_TIMEOUT
            while time.time() < end:
                if self.is_up():
                    return
                time.sleep(0.5)
            raise OSError("orgs did not start (something else was starting it)")
        try:
            scheme, port = self.listen()
            # -port as well as server.port: orgs serve's own -port default of
            # 8010 wins over the config's otherwise. -tlsport likewise.
            args = [self.bin, "-config", self.config, "serve", "-port", str(port)]
            if scheme == "https":
                args += ["-tlsport", str(port)]
            kw = {}
            if sys.platform == "win32":
                kw["creationflags"] = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
            else:
                kw["start_new_session"] = True
            with open(self.log_file, "wb") as out:
                child = subprocess.Popen(args, cwd=self.dir, stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=out, close_fds=True, **kw)
            end = time.time() + START_TIMEOUT
            while True:
                code = child.poll()
                if code is not None:
                    raise OSError("orgs stopped as it started (exit {0}):\n{1}".format(code, self._last_lines()))
                if self.is_up():
                    break
                if time.time() > end:
                    raise OSError("orgs did not answer on {0} within {1:.0f}s - see its log".format(self.url, START_TIMEOUT))
                time.sleep(0.5)
            with open(self.path("orgs.pid"), "w") as f:
                f.write(str(child.pid))
        finally:
            os.close(fd)
            try:
                os.remove(lock)
            except OSError:
                pass

    def stop(self):
        """Stop the server this started. True when nothing answers after."""
        pid = self._pid()
        if pid and self._is_orgs(pid):
            try:
                if sys.platform == "win32":
                    _run(["taskkill", "/PID", str(pid), "/F"])
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            end = time.time() + 10
            while time.time() < end and self.is_up():
                time.sleep(0.25)
        try:
            os.remove(self.path("orgs.pid"))
        except OSError:
            pass
        return not self.is_up()

    def with_stopped(self, fn):
        """fn() with the server stopped, started again after if it was
        running. For edits to its keystore: the server reads it only when it
        starts, and writes it back on each login, which would put back what
        fn took out. Returns (fn's result, whether it was restarted)."""
        was_up = self.is_up()
        if was_up and not self.stop():
            raise OSError("the running orgs would not stop")
        try:
            return fn(), was_up
        finally:
            if was_up:
                self.start()

    # Users
    def users(self):
        """The keystore's users: [{Name, Admin, Password, LastLogin}]."""
        try:
            rows = json.loads(self.cli(["user", "ls", "-json"]))
            return rows if isinstance(rows, list) else []
        except Exception as e:
            log.info("orgs: user ls: %s", e)
            return []


class VSCodeServer(LocalServer):
    """A local server the vscode plugin set up and runs. Sublime only
    connects to it: it trusts that server's certificate for that server's
    address, and logs in like any server (the plugin's password stays in
    VS Code)."""

    def installed(self):
        return os.path.exists(self.config)

    def cafile(self):
        try:
            with open(self.config, "r", encoding="utf-8") as f:
                crt = server_value(f.read(), "servercrt")
        except OSError:
            crt = None
        crt = crt or self.crt
        return crt if self.https and os.path.exists(crt) else None


def add_user_body(name, password, admin=False):
    """The body of POST /users/add: the password does not travel, a hash of
    it under a fresh salt does, as a login does."""
    salt = new_salt()
    return {"username": name, "salt": salt, "password": client_hash(name, salt, password), "admin": bool(admin)}
