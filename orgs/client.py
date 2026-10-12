"""A REST client for the orgs server.

Pure python on purpose: nothing in here imports sublime, so it can be tested
against a running server from the command line, and it runs on the python 3.8
plugin host as well as anything newer. It uses urllib rather than requests so
that it does not depend on Package Control dependencies installing cleanly.

Conventions of the orgs api worth knowing (see ~/dev/orgs/internal/app/orgs):

- JSON keys are Go field names: Ok, Msg, Hash, Headline, Filename, LineNum...
- Many handlers report failure as HTTP 200 with {"Ok": false, "Msg": ...} or
  a bare json string. check() turns both into OrgsError.
- A heading hash is base64. In a url *path* it must be url-safe base64 encoded
  a second time (hash_path); in query params and json bodies it is sent raw.
- Hashes change when a file changes. Look one up right before using it.
- LineNum and row positions are 0 based.
"""
import base64
import datetime
import hashlib
import http.client
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .paths import PathMap

log = logging.getLogger(__name__)

CLIENT_HASH_PREFIX = "sha256c:"
TOKEN_REFRESH_MARGIN = datetime.timedelta(minutes=2)


class OrgsError(Exception):
    def __init__(self, msg, status=None, body=None):
        super(OrgsError, self).__init__(msg)
        self.msg = msg
        self.status = status
        self.body = body


class Unavailable(OrgsError):
    """The server could not be reached at all."""


class AuthRequired(OrgsError):
    """The server wants a login and we could not get one."""


class Timeout(OrgsError):
    """The server took too long to answer. Not a lost connection: a slow
    export or search must not take the whole session offline. The request
    may still have been carried out."""


def hash_path(h):
    """Encode a heading hash for use as a url path segment."""
    return base64.urlsafe_b64encode(h.encode('utf-8')).decode('ascii')


def client_hash(user, salt, password):
    """The salted password hash the server accepts in place of the password.

    Mirrors common.ClientHash in orgs (internal/common/keystore.go). The
    plain password never leaves this machine and is never stored; this hash
    is what we keep in the keychain when asked to remember a login.
    """
    raw = "orgs-client-v1:" + user + ":" + salt + ":" + password
    return CLIENT_HASH_PREFIX + hashlib.sha256(raw.encode('utf-8')).hexdigest()


_FRAC = re.compile(r'\.(\d+)')


def parse_time(s):
    """Parse a Go RFC3339(Nano) time. Returns an aware datetime or None."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    # python 3.8 takes exactly 3 or 6 fractional digits; go trims zeros
    # and goes to 9.
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        d = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.year <= 1:
        # Go's zero time: "not set".
        return None
    return d


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def is_failure(res):
    return isinstance(res, dict) and res.get('Ok') is False


def check(res, what="request"):
    """Raise OrgsError for the ways the server reports failure in a 200."""
    if is_failure(res):
        raise OrgsError(res.get('Msg') or (what + " failed"), body=res)
    if isinstance(res, str):
        raise OrgsError(res, body=res)
    if res is None:
        raise OrgsError(what + ": empty answer from the server")
    return res


def check_ok(res, what="request"):
    """For endpoints answering a Result ({"Ok", "Msg"}): anything but
    Ok:true is a failure. Many handlers answer an error as {} (a go error
    encodes to an empty object), which check() would let through."""
    check(res, what)
    if not (isinstance(res, dict) and res.get('Ok') is True):
        raise OrgsError(what + " failed" + (": " + res["Msg"] if isinstance(res, dict) and res.get("Msg") else
                                            " (the heading may have changed; try again)"), body=res)
    return res


def target(filename, id, type="hash", lvl=0):
    return {"Filename": filename or "", "Id": id or "", "Type": type, "Lvl": lvl}


class Token:
    def __init__(self, token, expires, user=None):
        self.token = token
        self.expires = expires
        self.user = user

    def valid(self, margin=datetime.timedelta(0)):
        if not self.token:
            return False
        if self.expires is None:
            return True
        return now_utc() + margin < self.expires

    def to_dict(self):
        return {"token": self.token,
                "expires": self.expires.isoformat() if self.expires else None,
                "user": self.user}

    @staticmethod
    def from_dict(d):
        if not d or not d.get("token"):
            return None
        return Token(d["token"], parse_time(d.get("expires")), d.get("user"))


class OrgsClient:
    """One server connection.

    store: a creds.SecretStore (or None). Tokens are kept there per server url
           so a restart does not need a new login while the token is good.
    remember: also keep the salted password hash so an expired session can
              log back in silently. Only honoured when the store is secure.
    on_auth_needed: callable(client, reason) -> bool, run on the calling
              (worker) thread when the server answers 401. It should get a
              login done (prompt the user) and return True if it did.
    """

    def __init__(self, url, verify=True, cafile=None, timeout=15.0,
                 store=None, remember=True, path_map=None, on_auth_needed=None):
        self.url = (url or "").rstrip('/')
        self.timeout = timeout
        self.store = store
        self.remember = remember
        self.paths = path_map or PathMap()
        self.on_auth_needed = on_auth_needed
        self.on_token_changed = None
        self._token = None
        self._auth_lock = threading.RLock()
        self._refresh_timer = None
        self._closed = False
        self._cancelled_at = 0.0
        self._stored_user = None
        self.auth_disabled = False   # server runs with noAuth
        if self.url.startswith("https"):
            if verify:
                self._ssl = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
            else:
                self._ssl = ssl._create_unverified_context()
        else:
            self._ssl = None
        self._load_token()

    # ------------------------------------------------------------------
    # Transport

    def _open(self, method, path, params=None, body=None, raw_body=None,
              headers=None, auth=True, timeout=None):
        url = self.url + path
        if params:
            pairs = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    for x in v:
                        pairs.append((k, str(x)))
                elif isinstance(v, bool):
                    pairs.append((k, 't' if v else 'f'))
                else:
                    pairs.append((k, str(v)))
            if pairs:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)
        data = None
        hdrs = {"Accept": "application/json"}
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode('utf-8')
            hdrs["Content-Type"] = "application/json; charset=utf-8"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        tok = self._token
        if auth and tok and tok.token:
            # Unredirected: urllib copies ordinary headers to wherever a
            # redirect points, another host included.
            req.add_unredirected_header("Authorization", "Bearer " + tok.token)
        return urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ssl)

    def _do(self, method, path, params=None, body=None, raw_body=None, raw=False,
            auth=True, timeout=None, headers=None):
        try:
            with self._open(method, path, params, body, raw_body, headers, auth, timeout) as resp:
                data = resp.read()
                ctype = resp.headers.get('Content-Type', '')
                return resp.status, data, ctype
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b''
            return e.code, data, e.headers.get('Content-Type', '') if e.headers else ''
        except urllib.error.URLError as e:
            # Connecting failed (refused, no route, dns, tls, connect timeout).
            raise Unavailable("orgs server at {0} is unreachable: {1}".format(self.url, e.reason))
        except socket.timeout:
            # Connected, but the answer did not come in time.
            raise Timeout("orgs server at {0} did not answer {1} {2} in time".format(self.url, method, path))
        except (ConnectionError, ssl.SSLError, http.client.HTTPException, OSError) as e:
            raise Unavailable("orgs server at {0} dropped the connection: {1}".format(self.url, e))

    @staticmethod
    def _decode(data, ctype, raw):
        if raw:
            return data
        if not data:
            return None
        text = data.decode('utf-8', errors='replace')
        try:
            return json.loads(text)
        except ValueError:
            return text

    def request(self, method, path, params=None, body=None, raw_body=None,
                raw=False, auth=True, timeout=None, headers=None, _retry=True):
        if auth and not self.auth_disabled:
            self._maybe_refresh()
        sent = self._token
        status, data, ctype = self._do(method, path, params, body, raw_body, raw, auth, timeout, headers)
        if status == 401 and auth:
            # Only drop the token this request used: another thread may have
            # logged in again while it was in flight.
            self.drop_token(sent)
            if _retry and self._reauthenticate("Session expired or login required"):
                return self.request(method, path, params, body, raw_body, raw, auth, timeout, headers, _retry=False)
            raise AuthRequired("orgs login required", status=401)
        res = self._decode(data, ctype, raw)
        if status >= 400:
            msg = res.get('Msg') if isinstance(res, dict) else res
            if isinstance(res, dict) and not msg:
                msg = res.get('error') or res.get('msg')
            raise OrgsError("{0} {1}: {2}".format(method, path, msg or status), status=status, body=res)
        return res

    def get(self, path, params=None, **kw):
        return self.request("GET", path, params=params, **kw)

    def post(self, path, body=None, params=None, **kw):
        return self.request("POST", path, params=params, body=body if body is not None else {}, **kw)

    def delete(self, path, body=None, params=None, **kw):
        return self.request("DELETE", path, params=params, body=body, **kw)

    def close(self):
        self._closed = True
        self._cancel_refresh()

    @property
    def closed(self):
        return self._closed

    # ------------------------------------------------------------------
    # Auth

    def _store_key(self):
        return "orgs:" + self.url

    def _load_token(self):
        if not self.store:
            return
        try:
            d = self.store.get(self._store_key())
            if d:
                self._stored_user = d.get("user")
                self._token = Token.from_dict(d)
                self._schedule_refresh()
        except Exception as e:
            log.warning("orgs: could not read stored login: %s", e)

    def _save(self, extra=None):
        if not self.store:
            return
        try:
            old = self.store.get(self._store_key()) or {}
            d = dict(old)
            if self._token:
                d.update(self._token.to_dict())
            else:
                d.pop("token", None)
                d.pop("expires", None)
            if extra:
                d.update(extra)
            if not (self.remember and self.store.secure):
                d.pop("hash", None)
            if any(d.get(k) for k in ("token", "hash")):
                self.store.set(self._store_key(), d)
            else:
                self.store.delete(self._store_key())
        except Exception as e:
            log.warning("orgs: could not store login: %s", e)

    def _set_token(self, tok, persist=True, extra=None):
        self._token = tok
        if persist:
            self._save(extra)
        self._schedule_refresh()
        if self.on_token_changed:
            try:
                self.on_token_changed(self)
            except Exception:
                log.exception("orgs: token change callback")

    @property
    def user(self):
        return self.cached_user()

    def cached_user(self):
        """The user name we know, without asking the secret store (which can
        block on a locked keychain)."""
        if self._token and self._token.user:
            return self._token.user
        return self._stored_user

    def drop_token(self, tok):
        """Forget tok if it is still the current token."""
        with self._auth_lock:
            if tok is not None and self._token is tok:
                self._set_token(None, persist=True)

    def note_login_cancelled(self):
        self._cancelled_at = time.time()

    @property
    def token(self):
        return self._token

    def logged_in(self):
        return self.auth_disabled or bool(self._token and self._token.valid())

    def remembered_hash(self):
        if not self.store or not self.store.secure or not self.remember:
            return None, None
        d = self.store.get(self._store_key()) or {}
        return d.get("user"), d.get("hash")

    def salt(self, user):
        res = self.request("GET", "/salt", params={"user": user}, auth=False)
        if not isinstance(res, dict) or "salt" not in res:
            raise OrgsError("this does not look like an orgs server (no salt)")
        return res["salt"]

    def login(self, user, password):
        """Log in with a typed password. The password itself is not kept."""
        salt = self.salt(user)
        return self.login_hash(user, client_hash(user, salt, password))

    def login_hash(self, user, chash):
        status, data, ctype = self._do("POST", "/login", body={"username": user, "password": chash}, auth=False)
        if status == 401:
            raise AuthRequired("login failed: wrong user name or password", status=401)
        res = self._decode(data, ctype, False)
        if status >= 400 or not isinstance(res, dict) or not res.get("token"):
            raise OrgsError("login failed ({0}): {1}".format(status, res), status=status)
        tok = Token(res["token"], parse_time(res.get("ExpiresAt")), user)
        self._stored_user = user
        self._cancelled_at = 0.0
        self._set_token(tok, extra={"user": user, "hash": chash})
        return tok

    def logout(self, forget=True):
        self._cancel_refresh()
        self._token = None
        if self.store:
            try:
                if forget:
                    self.store.delete(self._store_key())
                else:
                    self._save()
            except Exception as e:
                log.warning("orgs: could not clear stored login: %s", e)
        if self.on_token_changed:
            self.on_token_changed(self)

    def refresh(self):
        status, data, ctype = self._do("POST", "/refresh", body={})
        if status != 200:
            return False
        res = self._decode(data, ctype, False)
        if not isinstance(res, dict) or not res.get("token"):
            return False
        user = self._token.user if self._token else None
        self._set_token(Token(res["token"], parse_time(res.get("ExpiresAt")), user))
        return True

    def _maybe_refresh(self):
        t = self._token
        if t and t.valid() and not t.valid(TOKEN_REFRESH_MARGIN):
            with self._auth_lock:
                if self._token is t:
                    try:
                        self.refresh()
                    except OrgsError:
                        pass

    def _reauthenticate(self, reason):
        with self._auth_lock:
            if self._token and self._token.valid():
                # Another thread logged in while we waited for the lock.
                return True
            user, chash = self.remembered_hash()
            if user and chash:
                try:
                    self.login_hash(user, chash)
                    return True
                except AuthRequired:
                    # The password changed; the stored hash is useless.
                    self._save({"hash": None})
                except OrgsError as e:
                    log.warning("orgs: silent login failed: %s", e)
            if time.time() - self._cancelled_at < 15.0:
                # The user just said no; calls that were queued behind that
                # prompt must not each open another one.
                return False
            if self.on_auth_needed:
                try:
                    return bool(self.on_auth_needed(self, reason))
                except Exception:
                    log.exception("orgs: login prompt failed")
            return False

    def _cancel_refresh(self):
        if self._refresh_timer:
            self._refresh_timer.cancel()
            self._refresh_timer = None

    def _schedule_refresh(self):
        self._cancel_refresh()
        t = self._token
        if self._closed or not t or not t.expires or not t.valid():
            return
        remaining = (t.expires - now_utc()).total_seconds()
        delay = max(remaining * 0.75, 10.0)
        if delay >= remaining:
            return

        def fire():
            if self._closed or self._token is not t:
                return
            try:
                if not self.refresh():
                    log.info("orgs: token refresh refused, will log in again when needed")
            except OrgsError as e:
                log.info("orgs: token refresh failed: %s", e)
        self._refresh_timer = threading.Timer(delay, fire)
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def check_session(self):
        """Is the server reachable, and does it accept us?

        Returns "ok", "login" (reachable but wants a login) or raises
        Unavailable. Detects servers running with noAuth.
        """
        status, data, ctype = self._do("GET", "/status")
        if status == 200:
            if not (self._token and self._token.token):
                self.auth_disabled = True
            return "ok"
        if status == 401:
            if self._token:
                self._set_token(None)
            return "login"
        raise OrgsError("unexpected status {0} from /status".format(status), status=status)

    # ------------------------------------------------------------------
    # Paths

    def local(self, path):
        return self.paths.to_local(path)

    def remote(self, path):
        return self.paths.to_server(path)

    def _localize(self, items, *keys):
        """Map server paths in a list of dicts to local paths, in place."""
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    for k in keys:
                        if it.get(k):
                            it[k] = self.local(it[k])
        return items

    # ------------------------------------------------------------------
    # Files

    def files(self):
        return [self.local(f) for f in (self.get("/files") or [])]

    def find_file(self, name):
        res = self.get("/findfile", {"filename": name})
        if isinstance(res, dict) and res.get("Ok"):
            return self.local(res.get("Msg"))
        return None

    def file_text(self, filename):
        res = check(self.get("/orgfile", {"filename": self.remote(filename)}), "read file")
        return res.get("Msg", "")

    def headings(self, filename):
        """Every heading of a file in document order (Todo dicts)."""
        res = self.get("/filecontents/headings", {"filename": self.remote(filename)})
        return self._localize(res or [], "Filename")

    def new_file(self, filename, title="", template=""):
        res = self.post("/file", {"filename": self.remote(filename), "title": title, "template": template})
        if isinstance(res, list) and res:
            return self.local(res[0])
        check(res, "create file")
        return None

    def new_file_templates(self):
        return self.get("/newtemplates") or []

    def dirs(self):
        return [self.local(d) for d in (self.get("/dirs") or [])]

    def reformat(self, filenames):
        return check_ok(self.post("/reformat", [self.remote(f) for f in filenames]), "reformat")

    def text_search(self, query, ignore_case=True, max=200, file=None):
        res = self.get("/files/search", {"q": query, "ignoreCase": ignore_case, "max": max,
                                         "file": self.remote(file) if file else None})
        check(res, "search")
        for f in res.get("Files") or []:
            f["Filename"] = self.local(f.get("Filename"))
        return res

    def history(self, filename, limit=100):
        return check(self.get("/history", {"file": self.remote(filename), "limit": limit}), "history")

    def history_text(self, filename, rev):
        return check(self.get("/history/text", {"file": self.remote(filename), "rev": rev}), "history")

    def reload_config(self):
        return self.post("/reloadconfig")

    def addresses(self):
        return self.get("/addresses")

    # ------------------------------------------------------------------
    # Headings

    def lookup_hash(self, filename, row):
        res = self.get("/lookuphash", {"filename": self.remote(filename), "pos": row})
        if isinstance(res, str) and res and not res.startswith("Failed") and " " not in res:
            return res
        return None

    def by_hash(self, h):
        res = self.get("/hash/" + hash_path(h))
        if isinstance(res, dict) and res.get("Hash"):
            res["Filename"] = self.local(res.get("Filename"))
            return res
        return None

    def by_id(self, id):
        """Look up a heading by :ID: or :CUSTOM_ID:."""
        res = self.get("/id/" + urllib.parse.quote(id, safe=''))
        if isinstance(res, dict) and res.get("Hash"):
            res["Filename"] = self.local(res.get("Filename"))
            return res
        return None

    def full(self, h):
        return check(self.get("/todofull/" + hash_path(h)), "heading")

    def html(self, h, theme=None):
        return check(self.get("/todohtml/" + hash_path(h), {"theme": theme}), "heading")

    def body(self, h):
        return check(self.get("/body/" + hash_path(h)), "body")

    def logbook(self, h):
        return self.get("/logbook/" + hash_path(h))

    def search(self, query):
        res = self.get("/search", {"query": query})
        check(res, "search")
        return self._localize(res or [], "Filename")

    @staticmethod
    def _day(d):
        if d is None or isinstance(d, str):
            return d
        return "{0:04d}-{1:02d}-{2:02d}".format(d.year, d.month, d.day)

    def agenda(self, frm=None, to=None, query=None, today=None, closed=False):
        """What lands on each day from frm to to (inclusive): an
        AgendaResult {Ok, Msg, From, To, Today, Nodes, Days}. Each day's
        Entries point into Nodes by index. Dates are date objects or
        YYYY-MM-DD strings; frm defaults to today on the server."""
        res = check(self.get("/agenda", {"from": self._day(frm), "to": self._day(to), "query": query or None,
                                         "today": self._day(today), "closed": closed or None}), "agenda")
        self._localize(res.get("Nodes") or [], "Filename")
        return res

    def agenda_nodes(self, query=None):
        """The headings a query finds, with everything the agenda reads
        about them (AgendaNode), for list views."""
        res = check(self.get("/agenda/nodes", {"query": query or None}), "agenda")
        return self._localize(res.get("Nodes") or [], "Filename")

    def filters(self):
        return self.get("/filters") or {}

    def tag_groups(self):
        return self.get("/taggroups") or {}

    def properties(self):
        return self.get("/properties") or []

    def all_tags(self):
        return self.get("/alltags") or []

    def todo_states(self, h=None):
        if h:
            return self.get("/status/" + hash_path(h)) or {}
        return self.get("/status") or {}

    def stored_queries(self):
        return self.get("/ext/queries") or []

    def save_query(self, name, query):
        return check_ok(self.post("/ext/query", {"name": name, "query": query}), "save query")

    def delete_query(self, name):
        return check_ok(self.delete("/ext/query", params={"name": name}), "delete query")

    # ------------------------------------------------------------------
    # Heading edits. All take a raw hash.

    def set_status(self, h, value, note=None):
        body = {"Hash": h, "Value": value}
        if note:
            body["Note"] = note
        return check_ok(self.post("/status/change", body), "change status")

    def set_headline(self, h, value):
        return check_ok(self.post("/headline/change", {"Hash": h, "Value": value}), "change headline")

    def set_body(self, h, value):
        return check_ok(self.post("/body/change", {"Hash": h, "Value": value}), "change body")

    def toggle_tag(self, h, tag):
        # Not /tags: it writes the whole document back, re-indenting drawers
        # and moving planning lines in every other heading of the file.
        # /heading/parts edits the one line.
        t = self.by_hash(h)
        if t is None:
            raise OrgsError("toggle tag failed (the heading may have changed; try again)")
        tags = list(t.get("Tags") or [])
        if tag in tags:
            tags.remove(tag)
        else:
            tags.append(tag)
        return self.set_tags(h, tags)

    def set_tags(self, h, tags):
        return check_ok(self.post("/heading/parts", {"Hash": h, "Tags": list(tags)}), "set tags")

    def set_priority(self, h, priority):
        return check_ok(self.post("/heading/parts", {"Hash": h, "Priority": priority or ""}), "set priority")

    def set_property(self, h, name, value):
        return check_ok(self.post("/property", {"Hash": h, "Name": name, "Value": value}), "set property")

    def set_date(self, h, name, value):
        """name: SCHEDULED, DEADLINE, CLOSED or TIMESTAMP. value: "<2024-01-15 Mon>"."""
        if not value:
            return check_ok(self.delete("/date/change", body={"Hash": h, "Name": name}), "clear date")
        return check_ok(self.post("/date/change", {"Hash": h, "Name": name, "Value": value}), "change date")

    def set_checkbox(self, h, index, done, text=""):
        return check_ok(self.post("/checklist", {"Hash": h, "Index": index, "Text": text, "Done": done}), "checklist")

    def delete_heading(self, tgt):
        return check_ok(self.post("/delete", tgt), "delete")

    def update_plugin(self, name, tgt):
        return check(self.post("/update", {"Name": name, "Target": tgt}), name)

    # ------------------------------------------------------------------
    # Capture, refile, archive

    def capture_templates(self):
        return self.get("/capture/templates") or []

    def capture(self, template, headline="", content="", tags=None, props=None, priority=""):
        return check_ok(self.post("/capture", {
            "Template": template,
            "NewNode": {"Headline": headline, "Content": content, "Tags": list(tags or []),
                        "Props": dict(props or {}), "Priority": priority or ""}}), "capture")

    def refile_targets(self, depth=3, files=None):
        res = check(self.get("/refile/targets", {"depth": depth,
                                                 "file": [self.remote(f) for f in files] if files else None}),
                    "refile targets")
        for t in res.get("Targets") or []:
            t["Filename"] = self.local(t.get("Filename"))
        return res.get("Targets") or []

    def refile_files(self):
        """The configured refileTargets as "file|H1|H2" strings."""
        return self.get("/refilefiles") or []

    def refile(self, frm, to):
        return check_ok(self.post("/refile", {"FromId": frm, "ToId": to}), "refile")

    def copy(self, frm, to):
        return check_ok(self.post("/copy", {"FromId": frm, "ToId": to}), "copy")

    def move(self, op, frm_list, to, create=False):
        return check(self.post("/move", {"Op": op, "From": frm_list, "To": to, "Create": create}), op)

    def archive(self, tgt):
        return check_ok(self.post("/archive", tgt), "archive")

    # ------------------------------------------------------------------
    # Clocking

    def clock(self):
        res = self.get("/clock") or {}
        if res.get("Target") and res["Target"].get("Filename"):
            res["Target"]["Filename"] = self.local(res["Target"]["Filename"])
        return res

    def clock_in(self, tgt):
        return check_ok(self.post("/clockin", tgt), "clock in")

    def clock_out(self):
        return check_ok(self.post("/clockout"), "clock out")

    def clock_report(self, block="today"):
        res = self.get("/clockreport", {"block": block}) or {}
        if res.get("error"):
            raise OrgsError(res["error"])
        for e in res.get("entries") or []:
            e["filename"] = self.local(e.get("filename"))
        return res

    # ------------------------------------------------------------------
    # Markers, day pages, habits

    def set_marker(self, name, tgt):
        return check_ok(self.post("/setexclusivemarker", {"Name": name, "ToId": tgt}), "set marker")

    def marker(self, name):
        res = self.get("/exclusivemarker", {"name": name})
        if isinstance(res, list):
            return self._localize(res, "Filename")
        return []

    @staticmethod
    def daypage_date(d):
        # The server parses YYYY-DD-MM (go layout 2006-02-01).
        return "{0:04d}-{1:02d}-{2:02d}".format(d.year, d.day, d.month)

    def daypage(self, d):
        res = self.get("/daypage/" + self.daypage_date(d))
        if isinstance(res, list):
            return [self.local(p) for p in res]
        return []

    def create_daypage(self):
        res = self.post("/daypage")
        if isinstance(res, list):
            return [self.local(p) for p in res]
        check(res, "create day page")
        return []

    def daypage_increment(self):
        res = self.get("/daypage/increment")
        return res if isinstance(res, int) else 1

    def habits(self, days=21):
        res = check(self.get("/habits", {"days": max(7, days)}), "habits")
        self._localize(res.get("Habits") or [], "Filename")
        return res

    def habit_untick(self, h):
        return check(self.post("/habits/untick", {"Hash": h}), "untick")

    # ------------------------------------------------------------------
    # Links

    def links(self, filename):
        res = check(self.get("/links", {"filename": self.remote(filename)}), "links")
        for k in ("In", "Out", "Internal"):
            for l in res.get(k) or []:
                for end in ("From", "To"):
                    if l.get(end) and l[end].get("Filename"):
                        l[end]["Filename"] = self.local(l[end]["Filename"])
        return res

    def all_links(self, service=None, file=None):
        res = self.get("/links/all", {"service": service, "file": self.remote(file) if file else None}) or {}
        self._localize(res.get("Links") or [], "Filename", "ToFilename")
        return res

    def resolve_link(self, link):
        return self.get("/links/resolve", {"link": link})

    # ------------------------------------------------------------------
    # Export

    def exporters(self):
        res = self.get("/exporters") or {}
        return res.get("Names") or []

    def export(self, kind, filename, **opts):
        params = {"query": self.remote(filename)}
        params.update(opts)
        res = check(self.get("/file/" + kind, params, timeout=120), "export " + kind)
        return res.get("Msg", "")

    def pdf(self, filename, refresh=False):
        return self.get("/pdf", {"filename": self.remote(filename), "refresh": refresh or None},
                        raw=True, timeout=300)

    def themes(self, kind="html"):
        res = self.get("/" + kind + "/themes") or {}
        return res.get("Themes") or []

    def tangle(self, filename, write=False):
        return self.get("/tangle", {"filename": self.remote(filename), "write": write or None})

    # ------------------------------------------------------------------
    # Tables, code, dynamic blocks

    def table_names(self):
        res = self.get("/tablenames") or {}
        return res.get("NamedTables") or []

    def table_random_row(self, name):
        return check(self.get("/tablerandomget", {"name": name}), "random row").get("Msg", "")

    def tables(self, filename=None):
        res = check(self.get("/tables", {"filename": self.remote(filename) if filename else None}), "tables")
        return self._localize(res.get("Tables") or [], "Filename")

    def babel_exec(self, filename, line, all=False, nowrite=False):
        return check(self.post("/babel/exec", {"Filename": self.remote(filename), "Line": line,
                                               "All": all, "NoWrite": nowrite}), "execute")

    def dblocks_update(self, filename, line=0, all=False):
        return check(self.post("/dblocks/update", {"Filename": self.remote(filename), "Line": line,
                                                   "All": all}), "dynamic blocks")

    # ------------------------------------------------------------------
    # Records, drill, crypt, attachments

    def records(self, type=None, q=None):
        return self._localize(self.get("/records", {"type": type, "q": q}) or [], "Filename")

    def contacts(self, q=None):
        return self._localize(self.get("/contacts", {"q": q}) or [], "Filename")

    def birthdays(self, frm=None, to=None):
        return self._localize(self.get("/records/birthdays", {"from": frm, "to": to}) or [], "Filename")

    def drill_decks(self):
        return self.get("/ext/drill/decks") or []

    def drill_cards(self, deck=None, query=None):
        res = self.get("/drill/cards", {"deck": deck, "query": query, "format": "org"}) or {}
        if isinstance(res, dict):
            self._localize(res.get("cards") or [], "file")
        return res

    def drill_review(self, h, quality, deck=None):
        return self.post("/drill/review", {"hash": h, "quality": quality, "deck": deck or ""})

    def crypt(self, op, h, passphrase, write=True):
        return check(self.post("/crypt/" + op, {"hash": h, "passphrase": passphrase, "write": write}), op)

    def crypt_lock(self):
        return self.post("/crypt/lock")

    def attachments(self, h):
        return check(self.get("/attachments", {"hash": h}), "attachments")

    def attachment_url(self, h, name):
        return self.url + "/attachment/" + hash_path(h) + "/" + urllib.parse.quote(name)

    # ------------------------------------------------------------------
    # Events

    def open_events(self, kinds=None):
        """Open the server sent event stream. Caller reads lines from it."""
        params = {"kinds": ",".join(kinds)} if kinds else None
        return self._open("GET", "/events", params=params, headers={"Accept": "text/event-stream"},
                          timeout=60)
