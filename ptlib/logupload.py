"""Automatic log upload to a GitHub branch, so logs can be read off-device.

Each upload writes one file, logs/<device>.log, on a dedicated branch
(default `device-logs`) via the GitHub contents API. The file holds a short
header, the tail of crash.log and the tail of pockettools.log. Every upload
is a commit, so the branch history keeps every earlier report too.

When it uploads (only once a token is configured and uploads are enabled):
- on launch (the previous session's tail, including any crash dump),
- a few seconds after any ERROR/CRITICAL record (debounced, rate-limited),
- on clean shutdown,
- on demand from the Logs screen.

Setup: Logs -> Upload -> Settings. Use a fine-grained GitHub token limited to
the one repository with "Contents: read and write". The token lives only in
data/logupload/config.json (data/ is gitignored and never touched by the
updater) and is never written to the log. The branch is created from main on
the first upload if it doesn't exist. Point `repo` at a private repository if
the code repository is public: logs include app activity such as scanned
hosts and BLE device names.
"""
import base64
import json
import logging
import platform
import re
import threading
import time
import urllib.error
import urllib.request

from ptlib import storage

APP = 'logupload'
DEFAULT_REPO = 'NeonSupernova/pockettools'
DEFAULT_BRANCH = 'device-logs'
LOG_LINES = 1500
CRASH_LINES = 300
MIN_INTERVAL = 60.0   # seconds between error-triggered uploads
DEBOUNCE = 8.0        # wait for the rest of a traceback burst before uploading
TIMEOUT = 15

_log = logging.getLogger('pt.logupload')
_lock = threading.Lock()
_state = {'last': 0.0, 'timer': None, 'status': 'never', 'handler': None}


# ---- config ----
def config():
    c = storage.load(APP, 'config.json') or {}
    c.setdefault('token', '')
    c.setdefault('repo', DEFAULT_REPO)
    c.setdefault('branch', DEFAULT_BRANCH)
    c.setdefault('device', default_device())
    c.setdefault('enabled', True)
    return c


def save_config(c):
    storage.save(APP, 'config.json', c)


def configured(c=None):
    c = c or config()
    return bool(c.get('token')) and bool(c.get('enabled'))


def default_device():
    name = platform.node() or 'device'
    return re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'device'


def status():
    return _state['status']


# ---- GitHub API ----
class _HTTP(Exception):
    def __init__(self, code, body):
        Exception.__init__(self, 'HTTP %d' % code)
        self.code = code
        self.body = body


def _api(c, method, path, body=None):
    url = 'https://api.github.com/repos/%s/%s' % (c['repo'], path)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + c['token'],
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'pockettools-logupload',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw.decode('utf-8')) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            msg = ''
        raise _HTTP(e.code, msg)


def _ensure_branch(c):
    try:
        _api(c, 'GET', 'git/ref/heads/%s' % c['branch'])
        return
    except _HTTP as e:
        if e.code != 404:
            raise
    base = _api(c, 'GET', 'git/ref/heads/main')['object']['sha']
    _api(c, 'POST', 'git/refs', {'ref': 'refs/heads/%s' % c['branch'], 'sha': base})
    _log.info('created branch %s', c['branch'])


def _existing_sha(c, path):
    try:
        return _api(c, 'GET', 'contents/%s?ref=%s' % (path, c['branch'])).get('sha')
    except _HTTP as e:
        if e.code == 404:
            return None
        raise


# ---- report ----
def _tail(path, n):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return ''.join(f.readlines()[-n:])
    except OSError:
        return '(missing)\n'


def build_report(reason):
    from ptlib import log  # late: log imports storage, avoid import cycles at load time
    try:
        from ptlib import updater
        version = (updater.current_version() or 'unknown')[:7]
    except Exception:
        version = 'unknown'
    head = [
        'pockettools log report',
        'reason:   %s' % reason,
        'uploaded: %s' % time.strftime('%Y-%m-%d %H:%M:%S %z'),
        'version:  %s' % version,
        'device:   %s (%s, %s)' % (config()['device'], platform.machine(), platform.platform()),
        'prev_crashed: %s' % log.previous_session_crashed(),
        '',
    ]
    return ('\n'.join(head)
            + '===== crash.log (last %d lines) =====\n' % CRASH_LINES + _tail(log.CRASH_PATH, CRASH_LINES)
            + '\n===== pockettools.log (last %d lines) =====\n' % LOG_LINES + _tail(log.LOG_PATH, LOG_LINES))


# ---- upload ----
def upload(reason='manual'):
    """Blocking. Returns (ok, detail). Never raises."""
    c = config()
    if not c.get('token'):
        return False, 'no token configured'
    if not _lock.acquire(blocking=False):
        return False, 'upload already running'
    try:
        _state['last'] = time.time()
        path = 'logs/%s.log' % c['device']
        content = base64.b64encode(build_report(reason).encode('utf-8')).decode('ascii')
        _ensure_branch(c)
        body = {'message': 'logs: %s %s %s' % (c['device'], reason, time.strftime('%Y-%m-%d %H:%M:%S')),
                'content': content, 'branch': c['branch']}
        sha = _existing_sha(c, path)
        if sha:
            body['sha'] = sha
        _api(c, 'PUT', 'contents/%s' % path, body)
        _state['status'] = 'ok %s (%s)' % (time.strftime('%H:%M:%S'), reason)
        _log.info('uploaded logs to %s@%s:%s (%s)', c['repo'], c['branch'], path, reason)
        return True, '%s@%s' % (c['repo'], c['branch'])
    except _HTTP as e:
        detail = 'HTTP %d %s' % (e.code, e.body[:120])
        hint = {401: 'bad or expired token', 403: 'token lacks Contents write',
                404: 'repo not found or token has no access'}.get(e.code)
        if hint:
            detail = 'HTTP %d: %s' % (e.code, hint)
        _state['status'] = 'failed: ' + detail
        _log.warning('log upload failed: %s', detail)  # WARNING, not ERROR: must not re-trigger
        return False, detail
    except Exception as e:  # offline, DNS, timeouts
        _state['status'] = 'failed: %s' % e
        _log.warning('log upload failed: %s', e)
        return False, str(e)
    finally:
        _lock.release()


def upload_async(reason='auto'):
    if not configured():
        return None
    t = threading.Thread(target=upload, args=(reason,), name='logupload', daemon=True)
    t.start()
    return t


def _fire():
    _state['timer'] = None
    upload('error')


class _ErrorTrigger(logging.Handler):
    """Schedules a debounced upload when an error is logged anywhere under pt.*."""

    def emit(self, record):
        if record.name.startswith('pt.logupload') or not configured():
            return
        if _state['timer'] is not None:
            return
        wait = max(DEBOUNCE, MIN_INTERVAL - (time.time() - _state['last']))
        t = threading.Timer(wait, _fire)
        t.daemon = True
        t.name = 'logupload-timer'
        _state['timer'] = t
        t.start()


def install(root):
    """Attach the error trigger to the pt root logger once (idempotent across reruns)."""
    for h in root.handlers:
        if getattr(h, '_pt_logupload', False):
            return
    h = _ErrorTrigger(level=logging.ERROR)
    h._pt_logupload = True
    root.addHandler(h)
