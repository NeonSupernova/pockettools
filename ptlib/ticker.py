"""Main-thread repeating timer built on ui.delay.

Each start() bumps a generation counter so a stale chain from an earlier
start/stop cycle can never keep firing. Never uses ui.cancel_delays(), which
would kill every app's timers.
"""
import logging

import ui

_log = logging.getLogger('pt.ticker')


class Ticker(object):
    def __init__(self, interval, fn):
        self.interval = interval
        self.fn = fn
        self.active = False
        self._gen = 0

    def start(self):
        if self.active:
            return
        self.active = True
        self._gen += 1
        self._schedule(self._gen)

    def stop(self):
        self.active = False
        self._gen += 1

    def _schedule(self, gen):
        ui.delay(lambda: self._tick(gen), self.interval)

    def _tick(self, gen):
        if not self.active or gen != self._gen:
            return
        try:
            self.fn()
        except Exception:
            _log.exception('tick %s failed', getattr(self.fn, '__qualname__', self.fn))
        if self.active and gen == self._gen:
            self._schedule(gen)
