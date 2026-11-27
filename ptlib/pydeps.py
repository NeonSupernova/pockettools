"""Install pure-Python packages from PyPI at runtime, without pip.

Pythonista has no pip, and some packages (yt-dlp) move faster than any
bundled copy. This picks the newest release of a package whose
requires_python accepts the running interpreter and that ships a pure
`py3-none-any` wheel, downloads it, checks the SHA-256 PyPI publishes,
and puts the wheel itself on sys.path (a wheel is a zip, so zipimport loads
it in place: nothing is unpacked).

Files live in data/pydeps/ (data/ is never touched by the pockettools
updater), and data/pydeps/state.json records what is installed.

Version pinning by interpreter matters: yt-dlp 2026.10.26+ requires Python
3.11, while Pythonista 3.4 runs 3.10, so on 3.10 this lands on 2026.10.21.
"""
import hashlib
import json
import logging
import os
import re
import sys
import urllib.request

from ptlib import storage

APP = 'pydeps'
TIMEOUT = 30
_log = logging.getLogger('pt.pydeps')


def _dir():
    d = os.path.join(storage.BASE, APP)
    os.makedirs(d, exist_ok=True)
    return d


def state():
    return storage.load(APP, 'state.json', {}) or {}


def _save_state(st):
    storage.save(APP, 'state.json', st)


# ---- version handling (enough of PEP 440 for release numbers) ----
def vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', v.split('+')[0]))


def is_final(v):
    return re.fullmatch(r'\d+(\.\d+)*', v) is not None


def python_ok(spec, py=None):
    """Does sys.version_info (or py) satisfy a requires_python spec like '>=3.10,<4'?"""
    if not spec:
        return True
    py = tuple(py or sys.version_info[:3])
    for part in spec.split(','):
        part = part.strip()
        m = re.match(r'(~=|==|!=|<=|>=|<|>)\s*([\d.]+)(\.\*)?$', part)
        if not m:
            continue  # unknown clause: don't block on it
        op, ver, star = m.group(1), vtuple(m.group(2)), m.group(3)
        n = len(ver)
        here = py[:n] if star else py + (0,) * max(0, n - len(py))
        if op == '>=' and not here >= ver:
            return False
        if op == '>' and not here > ver:
            return False
        if op == '<=' and not here <= ver:
            return False
        if op == '<' and not here < ver:
            return False
        if op == '==' and not (here[:n] == ver if star else here == ver):
            return False
        if op == '!=' and (here[:n] == ver if star else here == ver):
            return False
        if op == '~=' and not (here >= ver and here[:n - 1] == ver[:n - 1]):
            return False
    return True


def pick_release(pypi_json, py=None):
    """(version, file dict) of the newest final, non-yanked pure wheel this Python accepts."""
    best = None
    for v, files in pypi_json.get('releases', {}).items():
        if not is_final(v):
            continue
        for f in files:
            if f.get('yanked') or f.get('packagetype') != 'bdist_wheel':
                continue
            if not f['filename'].endswith('-py3-none-any.whl') and not f['filename'].endswith('-py2.py3-none-any.whl'):
                continue
            if not python_ok(f.get('requires_python'), py):
                continue
            if best is None or vtuple(v) > vtuple(best[0]):
                best = (v, f)
    return best


def _get(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'pockettools-pydeps'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


# ---- public API ----
def activate(pkg):
    """Put an installed wheel on sys.path. Returns its version or None."""
    info = state().get(pkg)
    if not info:
        return None
    path = os.path.join(_dir(), info['file'])
    if not os.path.isfile(path):
        return None
    if path not in sys.path:
        sys.path.insert(0, path)
    return info['version']


def install(pkg, progress=None):
    """Download the best wheel for this Python. Returns (version, changed)."""
    say = progress or (lambda s: None)
    say('Checking PyPI for %s…' % pkg)
    meta = json.loads(_get('https://pypi.org/pypi/%s/json' % pkg).decode('utf-8'))
    pick = pick_release(meta)
    if pick is None:
        raise RuntimeError('no %s release supports Python %d.%d' % ((pkg,) + sys.version_info[:2]))
    version, f = pick
    st = state()
    cur = st.get(pkg)
    if cur and cur.get('version') == version and os.path.isfile(os.path.join(_dir(), cur['file'])):
        activate(pkg)
        return version, False
    say('Downloading %s %s (%.1f MB)…' % (pkg, version, f.get('size', 0) / 1e6))
    blob = _get(f['url'])
    digest = hashlib.sha256(blob).hexdigest()
    if digest != f['digests']['sha256']:
        raise RuntimeError('%s download failed its SHA-256 check' % pkg)
    path = os.path.join(_dir(), f['filename'])
    tmp = path + '.tmp'
    with open(tmp, 'wb') as out:
        out.write(blob)
    os.replace(tmp, path)
    if cur and cur.get('file') and cur['file'] != f['filename']:
        old = os.path.join(_dir(), cur['file'])
        if old in sys.path:
            sys.path.remove(old)
        try:
            os.remove(old)
        except OSError:
            pass
    st[pkg] = {'version': version, 'file': f['filename'], 'sha256': digest}
    _save_state(st)
    _log.info('installed %s %s for python %d.%d', pkg, version, *sys.version_info[:2])
    activate(pkg)
    return version, True


def ensure(pkg, progress=None):
    """Installed version, installing on first use."""
    return activate(pkg) or install(pkg, progress)[0]


def drop_modules(prefix):
    """Forget imported modules so a newly installed version loads on next import."""
    for name in [m for m in sys.modules if m == prefix or m.startswith(prefix + '.')]:
        del sys.modules[name]
