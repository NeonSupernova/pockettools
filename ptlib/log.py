"""Logging + crash capture for pockettools.

Python logging alone can't see a hard crash: a bad objc_msgSend/ctypes call
kills the process before any handler runs, taking the Pythonista console
with it. Three layers cover that:

1. data/logs/pockettools.log: FileHandler flushes every record, so the
   last line written is in the kernel's page cache and survives a process
   death. Breadcrumbs logged before risky native calls pinpoint the culprit.
2. data/logs/crash.log: faulthandler dumps every thread's Python stack on
   SIGSEGV/SIGBUS/SIGABRT/SIGILL/SIGFPE, giving the exact Python line that
   made the fatal native call.
3. A session marker file: present at startup means the previous session
   never closed cleanly (crash, or Pythonista killed from the app switcher).

Uncaught exceptions in the main thread, worker threads, ui callbacks
(instrument) and background actions (bg) are logged with tracebacks.
"""
import faulthandler
import functools
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback

import ui

from ptlib import storage

DIR = os.path.join(storage.BASE, 'logs')
LOG_PATH = os.path.join(DIR, 'pockettools.log')
CRASH_PATH = os.path.join(DIR, 'crash.log')
MARKER = os.path.join(DIR, 'session.running')

root = logging.getLogger('pt')
_state = {'setup': False, 'crash_file': None, 'prev_crashed': False}


def get(name):
    return root.getChild(name)


def setup():
    """Idempotent: safe to call on every launch and after module reloads."""
    if _state['setup'] or root.handlers:
        _state['setup'] = True
        return root
    os.makedirs(DIR, exist_ok=True)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=256 * 1024, backupCount=3, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s] %(name)s: %(message)s',
                                      '%H:%M:%S'))
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
    ch.setLevel(logging.WARNING)
    root.addHandler(ch)

    try:
        f = open(CRASH_PATH, 'a', encoding='utf-8')
        f.write('\n=== session %s pid %d ===\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), os.getpid()))
        f.flush()
        faulthandler.enable(file=f, all_threads=True)
        _state['crash_file'] = f  # must stay open for faulthandler
    except Exception:
        root.exception('faulthandler unavailable')

    def excepthook(t, v, tb):
        root.critical('uncaught exception', exc_info=(t, v, tb))
        sys.__excepthook__(t, v, tb)
    sys.excepthook = excepthook

    def thread_hook(args):
        root.critical('uncaught exception in thread %s', args.thread.name if args.thread else '?',
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_hook
    _state['setup'] = True
    return root


def begin_session():
    """Call on every launch. Pythonista keeps the interpreter (and this module)
    alive between runs, so setup() returns early on re-runs; the marker check
    must not."""
    setup()
    _state['prev_crashed'] = os.path.exists(MARKER)
    with open(MARKER, 'w') as f:
        f.write(str(os.getpid()))
    root.info('---- session start, python %s, prev_crashed=%s', sys.version.split()[0], _state['prev_crashed'])
    return _state['prev_crashed']


def previous_session_crashed():
    return _state['prev_crashed']


def clean_shutdown():
    root.info('---- clean shutdown')
    try:
        os.remove(MARKER)
    except OSError:
        pass


def _guard(fn, label, reraise=True):
    log = get('cb')

    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            log.exception('%s raised', label)
            if reraise:
                raise
    wrapper._pt_guarded = True
    return wrapper


def bg(fn):
    """Drop-in for @ui.in_background that logs exceptions raised in the background thread."""
    return ui.in_background(_guard(fn, fn.__qualname__, reraise=False))


SKIP = {'__init__', '__new__', '__del__', '__getattr__', '__setattr__'}


def instrument(cls):
    """Class decorator: wrap every plain method so exceptions in ui callbacks
    (layout, draw, tableview_*, button actions) land in the log with a traceback."""
    for name, fn in list(vars(cls).items()):
        if name in SKIP or (name.startswith('__') and name.endswith('__')):
            continue
        if isinstance(fn, (staticmethod, classmethod, property)) or not callable(fn):
            continue
        if getattr(fn, '_pt_guarded', False) or getattr(fn, '__wrapped__', None) is not None:
            continue  # already guarded (bg) or wrapped by on_main_thread / in_background
        setattr(cls, name, _guard(fn, '%s.%s' % (cls.__name__, name)))
    return cls


def tail(path, n=300):
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return ''.join(f.readlines()[-n:])
    except OSError:
        return '(no %s yet)' % os.path.basename(path)


def clear():
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            h.acquire()
            try:
                h.stream.seek(0)
                h.stream.truncate()
            finally:
                h.release()
    f = _state['crash_file']
    if f is not None:
        f.seek(0)
        f.truncate()
        f.flush()
    else:
        open(CRASH_PATH, 'w').close()
    root.info('logs cleared')


def format_exc():
    return traceback.format_exc()
