"""Where orgs logins are kept between sessions.

The secret stores (macOS keychain, Windows credential manager, the freedesktop
secret service via secret-tool) are used when present. Values are json, base64
encoded so the token's own quotes never meet a shell or the keychain's command
parser, and passed on stdin rather than argv so they do not show up in a
process listing.

What is stored, per server url:
  user, token, expires - always (a token is short lived and bearer only)
  hash                 - the salted password hash, only when the store is
                         secure and orgsRememberLogin is on, so an expired
                         session can log back in without asking.
The typed password itself is never stored anywhere.

FileStore is the fallback when no secret store exists. It is marked insecure,
so the client never puts the password hash in it; it holds only the token, in
a file only the user can read (the orgs CLI keeps its token the same way).
"""
import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading

log = logging.getLogger(__name__)

SERVICE = "OrgEverywhere.orgs"

_file_lock = threading.Lock()


def _enc(d):
    return base64.b64encode(json.dumps(d).encode('utf-8')).decode('ascii')


def _dec(s):
    if not s:
        return None
    try:
        return json.loads(base64.b64decode(s.strip()).decode('utf-8'))
    except Exception:
        return None


def _run(args, stdin=None, timeout=10):
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(args, input=stdin.encode('utf-8') if stdin is not None else None,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, **kw)
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


class SecretStore:
    name = "none"
    secure = False

    def get(self, key):
        return None

    def set(self, key, value):
        pass

    def delete(self, key):
        pass


class MacKeychain(SecretStore):
    name = "macOS Keychain"
    secure = True
    BIN = "/usr/bin/security"

    @classmethod
    def available(cls):
        return sys.platform == "darwin" and os.path.exists(cls.BIN)

    @staticmethod
    def _q(s):
        # security -i takes a shell like command line; our values are base64
        # or urls, but quote anyway.
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def get(self, key):
        code, out, err = _run([self.BIN, "find-generic-password", "-s", SERVICE, "-a", key, "-w"])
        if code != 0:
            return None
        return _dec(out)

    def set(self, key, value):
        cmd = "add-generic-password -U -s {0} -a {1} -l {2} -w {3}\n".format(
            self._q(SERVICE), self._q(key), self._q("OrgEverywhere orgs login"), self._q(_enc(value)))
        code, out, err = _run([self.BIN, "-i"], stdin=cmd)
        if code != 0 or "error" in err.lower():
            raise OSError("keychain write failed: " + err.strip())

    def delete(self, key):
        _run([self.BIN, "delete-generic-password", "-s", SERVICE, "-a", key])


class SecretTool(SecretStore):
    name = "Secret Service (secret-tool)"
    secure = True

    @staticmethod
    def available():
        if not sys.platform.startswith("linux"):
            return False
        for d in os.environ.get("PATH", "/usr/bin").split(os.pathsep):
            if os.path.exists(os.path.join(d, "secret-tool")):
                return SecretTool._works()
        return False

    @staticmethod
    def _works():
        # Present but no running keyring (headless box) is common.
        try:
            code, out, err = _run(["secret-tool", "lookup", "service", SERVICE, "account", "__probe__"], timeout=5)
            return code in (0, 1) and "not" not in err.lower()
        except Exception:
            return False

    def get(self, key):
        code, out, err = _run(["secret-tool", "lookup", "service", SERVICE, "account", key])
        return _dec(out) if code == 0 else None

    def set(self, key, value):
        code, out, err = _run(["secret-tool", "store", "--label=OrgEverywhere orgs login",
                               "service", SERVICE, "account", key], stdin=_enc(value))
        if code != 0:
            raise OSError("secret-tool store failed: " + err.strip())

    def delete(self, key):
        _run(["secret-tool", "clear", "service", SERVICE, "account", key])


class WinCredMan(SecretStore):
    name = "Windows Credential Manager"
    secure = True

    @staticmethod
    def available():
        return sys.platform == "win32"

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)), ("Persist", wintypes.DWORD),
                        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        self.CREDENTIAL = CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CredReadW = adv.CredReadW
        self.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.CredWriteW = adv.CredWriteW
        self.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.CredDeleteW = adv.CredDeleteW
        self.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.CredFree = adv.CredFree

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def _target(self, key):
        return SERVICE + ":" + key

    def get(self, key):
        c = self.ctypes
        p = c.POINTER(self.CREDENTIAL)()
        if not self.CredReadW(self._target(key), self.CRED_TYPE_GENERIC, 0, c.byref(p)):
            return None
        try:
            blob = c.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize)
            return _dec(blob.decode('utf-8'))
        finally:
            self.CredFree(p)

    def set(self, key, value):
        c = self.ctypes
        blob = _enc(value).encode('utf-8')
        cred = self.CREDENTIAL()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = self._target(key)
        cred.UserName = "orgs"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = c.cast(c.create_string_buffer(blob, len(blob)), c.POINTER(c.c_char))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        if not self.CredWriteW(c.byref(cred), 0):
            raise OSError("CredWrite failed: {0}".format(c.get_last_error()))

    def delete(self, key):
        self.CredDeleteW(self._target(key), self.CRED_TYPE_GENERIC, 0)


class FileStore(SecretStore):
    name = "file (no OS keychain found)"
    secure = False

    def __init__(self, directory):
        self.path = os.path.join(directory, "orgs_logins.json")

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, d):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # A fresh 0600 temp file per write (mkstemp), then an atomic rename:
        # concurrent writers never share a half written file.
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".orgs_logins.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key):
        with _file_lock:
            return self._read().get(key)

    def set(self, key, value):
        # Never let a password hash reach a plain file.
        value = dict(value)
        value.pop("hash", None)
        with _file_lock:
            d = self._read()
            d[key] = value
            self._write(d)

    def delete(self, key):
        with _file_lock:
            d = self._read()
            if key in d:
                del d[key]
                self._write(d)


r"""
/* SDOC: Reference::Settings::Connection
*** orgs login settings
    | Setting                    | Default  | Effect                                                                                                    |
    |----------------------------+----------+-----------------------------------------------------------------------------------------------------------|
    | =orgsRememberLogin=        | =true=   | Also keep a salted hash of the password (never the password) so an expired session logs back in without asking. Only in a secure store. |
    | =orgsCredentialStore=      | ="auto"= | ="auto"= uses the OS secret store (macOS Keychain, Windows Credential Manager, or =secret-tool= on Linux) and falls back to a file. ="file"= forces the file. |
    | =orgsUser=                 | not set  | The user name offered at the login prompt (after the last one used).                                      |
    | =orgsPromptLoginOnStartup= | =true=   | Ask for a login at startup when the server wants one; =false= waits until a command needs the server.     |

    The session token (and its user and expiry) is always stored, per server
    url. The file store is a file only you can read, under Sublime's cache
    directory (=OrgEverywhere/=); it is treated as insecure, so the password
    hash is never put in it. Org: Log out of orgs deletes what is stored.
EDOC */
"""

def best_store(fallback_dir, prefer=None):
    """The most secure store this machine offers.

    prefer: "file" forces the file store (orgsCredentialStore setting).
    """
    if prefer != "file":
        try:
            if MacKeychain.available():
                return MacKeychain()
            if WinCredMan.available():
                return WinCredMan()
            if SecretTool.available():
                return SecretTool()
        except Exception as e:
            log.warning("orgs: secret store unavailable, using a file: %s", e)
    return FileStore(fallback_dir)
