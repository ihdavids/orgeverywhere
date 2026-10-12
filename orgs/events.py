"""Listens to the orgs /events stream (server sent events).

Each event is a line `data: {"kind": ..., "file": ..., ...}` followed by a
blank line; `: beat` comments arrive every 20 seconds. Kinds: hello, reload
(a file was re-read), clockin, clockout.

The listener runs on its own daemon thread and reconnects with backoff. It
also keeps per file reload counters so a caller that just saved a file can
wait until the server has re-read it (wait_for_reload).
"""
import json
import logging
import os
import threading
import time
import urllib.error

log = logging.getLogger(__name__)


class EventListener:
    def __init__(self, client, on_event=None, on_unauthorized=None):
        self.client = client
        self.on_event = on_event
        self.on_unauthorized = on_unauthorized
        self._gen = 0
        self._stop = threading.Event()
        self._thread = None
        self._cond = threading.Condition()
        self._reloads = {}       # normalized local path -> (count, time of last)
        self.connected = False

    def start(self):
        if self._thread and self._thread.is_alive() and not self._stop.is_set():
            return
        # A stopped thread can still be blocked reading the old stream for
        # up to a heartbeat; it notices the new generation and exits, and a
        # fresh thread takes over now.
        self._gen += 1
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._gen, self._stop),
                                        name="orgs-events", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.connected = False

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.realpath(path)) if path else ""

    def reload_count(self, path):
        with self._cond:
            return self._reloads.get(self._key(path), (0, 0.0))[0]

    def wait_for_reload(self, path, after_count, timeout=2.0, since=None):
        """Block until the server reports re-reading path more than
        after_count times, the last of them at or after `since` (a time.time()
        stamp, so a reload of an earlier save does not count). Returns False
        on timeout or when the stream is down."""
        if not self.connected:
            return False
        key = self._key(path)
        deadline = time.time() + timeout
        with self._cond:
            while True:
                count, at = self._reloads.get(key, (0, 0.0))
                if count > after_count and (since is None or at >= since):
                    return True
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cond.wait(left)

    def _dispatch(self, ev):
        if ev.get("kind") == "reload" and ev.get("file"):
            local = self.client.local(ev["file"])
            ev["file"] = local
            with self._cond:
                k = self._key(local)
                count = self._reloads.get(k, (0, 0.0))[0]
                self._reloads[k] = (count + 1, time.time())
                self._cond.notify_all()
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                log.exception("orgs: event handler")

    def _run(self, gen, stop):
        backoff = 1.0
        while not stop.is_set() and gen == self._gen:
            try:
                if not self.client.logged_in():
                    # Nothing to listen with yet; check back.
                    stop.wait(5.0)
                    continue
                resp = self.client.open_events()
                self.connected = True
                backoff = 1.0
                data = []
                with resp:
                    while not stop.is_set() and gen == self._gen:
                        line = resp.readline()
                        if not line:
                            break
                        line = line.decode('utf-8', 'replace').rstrip('\r\n')
                        if line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif line == "":
                            if data:
                                try:
                                    self._dispatch(json.loads("\n".join(data)))
                                except ValueError:
                                    pass
                                data = []
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    # The server no longer takes our token (restarted with new
                    # keys, revoked...). The local expiry says it is fine, so
                    # without this the stream would retry forever.
                    self.client.drop_token(self.client.token)
                    if self.on_unauthorized:
                        try:
                            self.on_unauthorized(self.client)
                        except Exception:
                            log.exception("orgs: unauthorized handler")
                else:
                    log.debug("orgs: event stream refused: %s", e)
            except Exception as e:
                # Timeouts and dropped connections land here.
                log.debug("orgs: event stream dropped: %s", e)
            if gen == self._gen:
                self.connected = False
            if stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)
