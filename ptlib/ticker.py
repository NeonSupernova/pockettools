"""Main-thread timers driven by Pythonista's native View.update() machinery.

The previous implementation re-armed itself with a recursive ui.delay() from
inside each callback. Crash dumps from three separate sessions (BLE twice,
and Volleyball with no BLE loaded) all died on the main thread with that
ui.delay lambda as the only Python frame, i.e. inside Pythonista's native
delay dispatch, not in our code. So nothing here uses ui.delay.

Instead each Ticker owns a 1x1 transparent subview of its host whose
update_interval Pythonista drives natively. Pythonista only calls update()
while the view is on screen, so timers pause automatically when their app is
covered or popped, and resume when it comes back.
"""
import logging

import ui

_log = logging.getLogger('pt.ticker')


class _TickView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fn = None

    def update(self):
        fn = self.fn
        if fn is None:
            return
        try:
            fn()
        except Exception:
            _log.exception('tick %s failed', getattr(fn, '__qualname__', fn))


class Ticker(object):
    def __init__(self, interval, fn, host):
        self.interval = interval
        self.fn = fn
        self.active = False
        self.view = _TickView(frame=(0, 0, 1, 1))
        self.view.touch_enabled = False
        self.view.background_color = None
        self.view.update_interval = 0
        host.add_subview(self.view)

    def start(self):
        self.active = True
        self.view.fn = self.fn
        self.view.update_interval = self.interval

    def stop(self):
        self.active = False
        self.view.update_interval = 0
        self.view.fn = None


def after(host, seconds, fn):
    """One-shot: run fn once on the main thread after ~seconds (while host is on screen)."""
    t = None

    def fire():
        t.stop()
        fn()
    t = Ticker(seconds, fire, host)
    t.start()
    return t
