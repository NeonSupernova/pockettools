"""pockettools launcher.

One Pythonista process hosts every tool: a NavigationView whose root lists the
apps; tapping one imports its module and pushes make_view().

Lifecycle: pushed views get no pop callback, so a ticker watches for "launcher
root back on screen and the app view off it" and calls the app's stop().
Closing the launcher (Close button) stops the active app via will_close().

Diagnostics: everything logs to data/logs/pockettools.log; hard crashes dump a
Python stack to data/logs/crash.log (faulthandler). If the previous session
died, the launcher says so on start. Both files are viewable under Logs.

Updates: on launch a background thread checks GitHub (ptlib/updater.py) and
installs a new version if there is one; it applies on the next launch. The
Update button checks on demand. Set AUTO_UPDATE = False to pin a version.

Deep link: pass an app key as argv, e.g. from Shortcuts' "Run Pythonista
Script" with argument `ble`, or pythonista3://pockettools/launcher.py?action=run&argv=ble
"""
import importlib
import os
import sys
import threading
from ctypes import Structure, c_double

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ui  # noqa: E402
import console  # noqa: E402
import clipboard  # noqa: E402

from ptlib import log  # noqa: E402  (set up before anything else can fail)

log.setup()
_log = log.get('launcher')

AUTO_UPDATE = True
DEV_RELOAD = True  # re-import modules on each open so edits apply without restarting Pythonista
# ptlib.log is deliberately absent: reloading it would orphan faulthandler's open file.
LIB = ('ptlib.theme', 'ptlib.storage', 'ptlib.ticker', 'ptlib.haptics', 'ptlib.graph')
APPS = [
    ('volley', 'Volleyball', 'ptapps.volleyball', 'Score, serve/rotation, timeouts, subs, player stats'),
    ('count', 'Counter', 'ptapps.counter', 'Named tally counters'),
    ('grades', 'Grades', 'ptapps.grades', 'Weighted grades, history graphs, GPA projection'),
    ('net', 'Network Scan', 'ptapps.netscan', 'Subnet sweep, open ports, latency graph'),
    ('ble', 'BLE Scan', 'ptapps.blescan', 'Advertisers, live RSSI graph, adv data'),
]


def _load(modname):
    if DEV_RELOAD:
        for m in LIB:  # dependency order: theme before graph
            if m in sys.modules:
                importlib.reload(sys.modules[m])
        if modname in sys.modules:
            _log.info('reload %s', modname)
            return importlib.reload(sys.modules[modname])
    _log.info('import %s', modname)
    return importlib.import_module(modname)


from ptlib import theme  # noqa: E402
from ptlib.ticker import Ticker  # noqa: E402
from ptlib import updater  # noqa: E402


class _Insets(Structure):
    _fields_ = [('top', c_double), ('left', c_double), ('bottom', c_double), ('right', c_double)]


def safe_insets(view):
    # Explicit restype: never let objc_util guess a struct return type. A wrong
    # guess on a struct-returning objc_msgSend corrupts registers/stack and is
    # a hard crash, not an exception.
    try:
        ins = view.objc_instance.safeAreaInsets(restype=_Insets, argtypes=[])
        return ins.top, ins.bottom
    except Exception:
        _log.exception('safeAreaInsets failed; falling back')
        return 20.0, 0.0


@log.instrument
class LogView(ui.View):
    def __init__(self):
        super().__init__()
        self.name = 'Logs'
        self.background_color = theme.BG
        self.seg = ui.SegmentedControl()
        self.seg.segments = ['Log', 'Crash dumps']
        self.seg.selected_index = 0
        self.seg.action = lambda s: self.refresh()
        self.seg.tint_color = theme.AMBER
        self.tv = ui.TextView()
        self.tv.editable = False
        self.tv.font = (theme.MONO, 10)
        self.tv.text_color = theme.TEXT
        self.tv.background_color = theme.BG
        self.add_subview(self.seg)
        self.add_subview(self.tv)
        self.right_button_items = [ui.ButtonItem(title='Clear', action=self.on_clear),
                                   ui.ButtonItem(title='Copy', action=self.on_copy)]
        self.refresh()

    def layout(self):
        p = 8
        self.seg.frame = (p, p, self.width - 2 * p, 32)
        self.tv.frame = (0, 48, self.width, self.height - 48)

    def refresh(self):
        path = log.LOG_PATH if self.seg.selected_index == 0 else log.CRASH_PATH
        self.tv.text = log.tail(path, 400)
        # newest at the bottom; jump there
        try:
            n = len(self.tv.text)
            self.tv.selected_range = (n, n)
        except Exception:
            pass

    def on_copy(self, sender):
        clipboard.set(self.tv.text)
        console.hud_alert('Copied')

    @log.bg
    def on_clear(self, sender):
        try:
            console.alert('Clear logs', 'Truncate both log files?', 'Clear')
        except KeyboardInterrupt:
            return
        log.clear()
        ui.delay(self.refresh, 0.1)


@log.instrument
class Launcher(ui.View):
    def __init__(self, shell):
        super().__init__()
        self.shell = shell
        self.name = 'Tools'
        self.background_color = theme.BG
        self.tv = theme.table(self, 64)
        self.tv.frame = self.bounds
        self.tv.flex = 'WH'
        self.add_subview(self.tv)
        self.active = None
        self.watch = Ticker(0.5, self._watch)
        self.right_button_items = [ui.ButtonItem(title='Logs', action=self.open_logs),
                                   ui.ButtonItem(title='Update', action=self.on_update)]

    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(APPS)

    def tableview_cell_for_row(self, tv, section, row):
        _, title, _, desc = APPS[row]
        c = theme.cell(title, desc)
        c.text_label.font = (theme.BOLD, 17)
        c.accessory_type = 'disclosure_indicator'
        return c

    def tableview_did_select(self, tv, section, row):
        tv.reload()  # clear selection highlight
        self.open(row)

    def open(self, idx):
        self.stop_active()
        key, title, modname, _ = APPS[idx]
        _log.info('open %s: loading module', key)
        try:
            mod = _load(modname)
            _log.info('open %s: make_view', key)
            v = mod.make_view()
        except Exception:
            _log.exception('open %s failed', key)
            console.hud_alert('%s failed to load (see Logs)' % title, 'error', 2)
            return
        v.name = title
        self.active = v
        _log.info('open %s: push_view', key)
        self.shell.nav.push_view(v)
        _log.info('open %s: pushed', key)
        self.watch.start()

    def open_key(self, key):
        for i, app in enumerate(APPS):
            if app[0] == key:
                self.open(i)
                return
        _log.warning('unknown deep-link key %r', key)

    @log.bg
    def on_update(self, sender):
        console.hud_alert('Checking\u2026', 'success', 0.6)
        status, detail = updater.check()
        if status == 'updated':
            console.hud_alert('Installed %s: close and reopen' % detail, 'success', 2.5)
        elif status == 'current':
            console.hud_alert('Up to date (%s)' % detail, 'success', 1.2)
        else:
            console.hud_alert('Update failed: see Logs', 'error', 2)

    def open_logs(self, sender):
        self.shell.nav.push_view(LogView())

    def _watch(self):
        v = self.active
        if v is not None and self.on_screen and not v.on_screen:
            _log.info('app view left screen; stopping %s', v.name)
            self.stop_active()

    def stop_active(self):
        v, self.active = self.active, None
        self.watch.stop()
        if v is not None and hasattr(v, 'stop'):
            try:
                v.stop()
            except Exception:
                _log.exception('stop() failed for %s', v.name)


class Shell(ui.View):
    """Container so will_close fires (NavigationView can't be subclassed) and the
    nav bar can be pushed below the status bar / above the home indicator."""

    def __init__(self):
        super().__init__()
        self.background_color = theme.BG
        self.launcher = Launcher(self)
        self.nav = ui.NavigationView(self.launcher)
        for attr, val in (('bar_tint_color', theme.PANEL), ('title_color', theme.TEXT),
                          ('tint_color', theme.AMBER)):
            try:
                setattr(self.nav, attr, val)
            except Exception:
                _log.warning('NavigationView has no %s', attr)
        self.add_subview(self.nav)
        self.launcher.left_button_items = [ui.ButtonItem(title='Close', action=lambda s: self.close())]

    def layout(self):
        top, bottom = safe_insets(self)
        self.nav.frame = (0, top, self.width, self.height - top - bottom)
        _log.debug('shell layout %.0fx%.0f insets top=%.0f bottom=%.0f', self.width, self.height, top, bottom)

    def will_close(self):
        _log.info('shell closing')
        self.launcher.stop_active()
        log.clean_shutdown()


def _auto_update():
    status, detail = updater.check()
    if status == 'updated':
        console.hud_alert('Updated to %s: applies next launch' % detail, 'success', 2.5)


def main():
    crashed = log.begin_session()
    _log.info('version %s', (updater.current_version() or 'unknown')[:7])
    if AUTO_UPDATE:
        threading.Thread(target=_auto_update, name='updater', daemon=True).start()
    _log.info('building shell')
    shell = Shell()
    _log.info('presenting')
    shell.present('fullscreen', hide_title_bar=True)
    _log.info('presented')
    ui.delay(shell.layout, 0.3)  # safe-area insets are only valid once in a window
    if crashed:
        ui.delay(lambda: console.hud_alert('Last session did not close cleanly: see Logs', 'error', 2.5), 0.8)
    if len(sys.argv) > 1:
        key = sys.argv[1].strip().lower()
        ui.delay(lambda: shell.launcher.open_key(key), 0.6)


if __name__ == '__main__':
    main()
