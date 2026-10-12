import os


def _norm(p):
    return p.replace('\\', '/').rstrip('/')


class PathMap:
    """Maps between the paths the server reports and local paths.

    A server running on this machine sees the same paths we do and needs no
    map. A remote server (or one in a container) reports its own paths, so
    orgsPathMap holds {"/server/prefix": "/local/prefix"} pairs.
    """

    def __init__(self, mapping=None):
        self.pairs = []
        for remote, local in (mapping or {}).items():
            self.pairs.append((_norm(remote), _norm(os.path.expanduser(local))))
        # Longest prefix wins.
        self.pairs.sort(key=lambda x: -len(x[0]))

    @staticmethod
    def _swap(path, frm, to):
        p = path.replace('\\', '/')
        if p == frm or p.startswith(frm + '/'):
            return to + p[len(frm):]
        return None

    def to_local(self, path):
        if not path:
            return path
        for remote, local in self.pairs:
            r = PathMap._swap(path, remote, local)
            if r is not None:
                return os.path.normpath(r)
        return path

    def to_server(self, path):
        if not path:
            return path
        for remote, local in sorted(self.pairs, key=lambda x: -len(x[1])):
            r = PathMap._swap(path, local, remote)
            if r is not None:
                return r
        return path

    @staticmethod
    def same(a, b):
        if not a or not b:
            return False
        a = os.path.normcase(os.path.normpath(a))
        b = os.path.normcase(os.path.normpath(b))
        if a == b:
            return True
        # macOS: /tmp vs /private/tmp and friends.
        try:
            return os.path.realpath(a) == os.path.realpath(b)
        except Exception:
            return False
