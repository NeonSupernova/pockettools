"""Self-update from GitHub.

Flow (runs in a background thread on launch, or on demand):
1. GET /repos/<repo>/commits/<branch> with Accept: vnd.github.sha -> head SHA.
   Sends If-None-Match with the last ETag; a 304 doesn't count against the
   60/hour anonymous rate limit, so checking on every launch is free.
2. Same SHA as data/update/state.json -> done.
3. Download the zip pinned to that exact SHA (no race with a later push),
   unpack in memory, reject anything unsafe (absolute paths, '..', odd file
   types, oversized), and compile() every .py. A syntax error upstream aborts
   the update instead of bricking the launcher.
4. Back up the current files to data/update/backup/, then write each new file
   to <name>.new and os.replace() it into place (per-file atomic). Files that
   were in the previous manifest but vanished upstream are removed.
5. Record SHA, ETag and manifest. Running code is already in memory, so the
   new version takes effect on the next launch.

data/ is never touched. Set AUTO_UPDATE = False in launcher.py to pin a version.
"""
import io
import logging
import os
import shutil
import urllib.error
import urllib.request
import zipfile

from ptlib import storage

REPO = 'NeonSupernova/pockettools'
BRANCH = 'main'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_APP = 'update'
ALLOWED_EXT = ('.py', '.md', '.txt', '.json')
MAX_FILE = 512 * 1024
MAX_TOTAL = 8 * 1024 * 1024
TIMEOUT = 6

_log = logging.getLogger('pt.update')


class UpdateError(Exception):
    pass


def _state():
    return storage.load(STATE_APP, 'state.json') or {}


def _save_state(st):
    storage.save(STATE_APP, 'state.json', st)


def current_version():
    return _state().get('sha')


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=dict({'User-Agent': 'pockettools-updater'}, **(headers or {})))
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def remote_head(etag=None):
    """Returns (sha, etag), or (None, etag) when unchanged since etag (HTTP 304)."""
    h = {'Accept': 'application/vnd.github.sha'}
    if etag:
        h['If-None-Match'] = etag
    try:
        with _get('https://api.github.com/repos/%s/commits/%s' % (REPO, BRANCH), h) as r:
            return r.read().decode('ascii').strip(), r.headers.get('ETag')
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise UpdateError('GitHub API HTTP %d' % e.code)


def _download(sha):
    url = 'https://codeload.github.com/%s/zip/%s' % (REPO, sha)
    with _get(url) as r:
        data = r.read(MAX_TOTAL + 1)
    if len(data) > MAX_TOTAL:
        raise UpdateError('archive too large')
    return data


def _unpack(blob):
    """zip bytes -> {relpath: bytes}, validated and compiled."""
    files = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.split('/')[1:]  # strip "<repo>-<sha>/"
            rel = '/'.join(parts)
            if not rel:
                continue
            if rel.startswith('/') or '..' in parts or '\\' in rel or rel.startswith('data/'):
                raise UpdateError('unsafe path %r' % rel)
            if not rel.endswith(ALLOWED_EXT):
                continue  # silently skip images, CI config, etc.
            if info.file_size > MAX_FILE:
                raise UpdateError('%s too large' % rel)
            total += info.file_size
            if total > MAX_TOTAL:
                raise UpdateError('archive too large')
            body = z.read(info)
            if rel.endswith('.py'):
                try:
                    compile(body.decode('utf-8'), rel, 'exec')
                except (SyntaxError, UnicodeDecodeError) as e:
                    raise UpdateError('upstream %s does not compile: %s' % (rel, e))
            files[rel] = body
    if 'launcher.py' not in files:
        raise UpdateError('archive has no launcher.py')
    return files


def _apply(files, old_manifest):
    backup = os.path.join(storage.BASE, STATE_APP, 'backup')
    shutil.rmtree(backup, ignore_errors=True)
    for rel in set(files) | set(old_manifest):
        src = os.path.join(ROOT, rel)
        if os.path.isfile(src):
            dst = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    for rel, body in sorted(files.items()):
        dst = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.new'
        with open(tmp, 'wb') as f:
            f.write(body)
        os.replace(tmp, dst)
    for rel in set(old_manifest) - set(files):
        try:
            os.remove(os.path.join(ROOT, rel))
            _log.info('removed %s (gone upstream)', rel)
        except OSError:
            pass


def check(force=False):
    """Returns (status, detail). status: 'updated' | 'current' | 'error'."""
    st = _state()
    try:
        sha, etag = remote_head(None if force else st.get('etag'))
        if sha is None or (sha == st.get('sha') and not force):
            if etag:
                st['etag'] = etag
                _save_state(st)
            _log.info('up to date (%s)', (st.get('sha') or '?')[:7])
            return 'current', (st.get('sha') or '?')[:7]
        _log.info('update %s -> %s', (st.get('sha') or 'none')[:7], sha[:7])
        files = _unpack(_download(sha))
        _apply(files, st.get('manifest', []))
        _save_state({'sha': sha, 'etag': etag, 'manifest': sorted(files)})
        _log.info('applied %s (%d files)', sha[:7], len(files))
        return 'updated', sha[:7]
    except UpdateError as e:
        _log.warning('update aborted: %s', e)
        return 'error', str(e)
    except (urllib.error.URLError, OSError, ValueError, zipfile.BadZipFile) as e:
        _log.info('update check failed: %s', e)  # offline is normal; keep quiet
        return 'error', str(e)
