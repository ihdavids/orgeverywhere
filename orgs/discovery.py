"""Finding a running orgs server.

/salt is the one orgs specific route that answers without a login, so it is
the probe: a 200 with {"salt": ...} is an orgs server, anything else is not.
"""
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.request

DEFAULT_CANDIDATES = [
    "http://localhost:8010",   # orgs serve default http port
    "https://localhost:443",   # orgs serve default https port (vscode default)
]


class ProbeResult:
    def __init__(self, url, ok, msg="", cert_problem=False):
        self.url = url
        self.ok = ok
        self.msg = msg
        self.cert_problem = cert_problem

    def __repr__(self):
        return "ProbeResult({0}, ok={1}, {2})".format(self.url, self.ok, self.msg)


def probe(url, verify=True, cafile=None, timeout=2.0):
    url = url.rstrip('/')
    ctx = None
    if url.startswith("https"):
        if verify:
            ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
        else:
            ctx = ssl._create_unverified_context()
    req = urllib.request.Request(url + "/salt?user=orgeverywhere-probe", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode('utf-8', 'replace')
            try:
                j = json.loads(body)
                if isinstance(j, dict) and "salt" in j:
                    return ProbeResult(url, True, "orgs server")
            except ValueError:
                pass
            return ProbeResult(url, False, "answered, but is not an orgs server")
    except urllib.error.HTTPError as e:
        return ProbeResult(url, False, "HTTP {0}: not an orgs server".format(e.code))
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return ProbeResult(url, False, "certificate not trusted (self signed? set orgsNoVerify or orgsCaFile)",
                               cert_problem=True)
        return ProbeResult(url, False, str(reason))
    except (socket.timeout, ConnectionError, http.client.HTTPException, OSError) as e:
        # Also a non-http service on the port, or a server dying mid answer.
        return ProbeResult(url, False, str(e) or type(e).__name__)


def discover(candidates=None, verify=True, cafile=None, timeout=2.0):
    """Probe candidates in order. Returns (first good url or None, [results]).
    cafile is a path, or a function of the url giving the one to trust for it."""
    results = []
    for url in candidates or DEFAULT_CANDIDATES:
        r = probe(url, verify, cafile(url) if callable(cafile) else cafile, timeout)
        results.append(r)
        if r.ok:
            return r.url, results
    return None, results
