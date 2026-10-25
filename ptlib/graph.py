"""Stateless graphs drawn with ui.Path.

Set the data attribute (.series / .bars), then call set_needs_display().
draw() runs in Python on every redraw, so keep refresh rates around 1-4 Hz;
use the scene module for anything that needs 60 fps.
"""
import datetime

import ui

from ptlib import theme

PAD_L, PAD_R, PAD_T, PAD_B = 44, 10, 26, 10
SMALL = (theme.FONT, 10)


def _panel(view, caption):
    w, h = view.width, view.height
    ui.set_color(theme.PANEL)
    ui.Path.rounded_rect(0, 0, w, h, 10).fill()
    ui.draw_string(caption, rect=(10, 5, w - 20, 16), font=(theme.BOLD, 12), color=theme.TEXT)
    return w, h


def _legend(entries, w, caption):
    """entries: [(label, color)] drawn right after the caption on the top row."""
    x = 18 + ui.measure_string(caption, font=(theme.BOLD, 12))[0]
    for name, color in entries:
        name = (name or '')[:12]
        tw = ui.measure_string(name, font=SMALL)[0]
        if x + tw + 14 > w - 8:
            break
        ui.set_color(color)
        ui.Path.rect(x, 9, 8, 8).fill()
        ui.draw_string(name, rect=(x + 11, 6, tw + 2, 14), font=SMALL, color=theme.MUTED)
        x += tw + 22


def _grid(lo, hi, top, ph, left, pw, fmt, n=4):
    def ymap(v):
        return top + ph - (v - lo) / (hi - lo) * ph
    for i in range(n + 1):
        v = lo + (hi - lo) * i / float(n)
        y = ymap(v)
        ui.set_color(theme.PANEL_HI)
        p = ui.Path()
        p.move_to(left, y)
        p.line_to(left + pw, y)
        p.line_width = 0.5
        p.stroke()
        ui.draw_string(fmt % v, rect=(2, y - 7, left - 6, 14), font=SMALL,
                       color=theme.MUTED, alignment=ui.ALIGN_RIGHT)
    return ymap


def _auto_range(vals, y_min, y_max):
    lo = y_min if y_min is not None else (min(vals) if vals else 0.0)
    hi = y_max if y_max is not None else (max(vals) if vals else 1.0)
    span = (hi - lo) or abs(hi) or 1.0
    if y_min is None:
        lo -= span * 0.1
    if y_max is None:
        hi += span * 0.1
    if hi - lo < 1e-9:
        hi = lo + 1.0
    return lo, hi


def _stroke(points, color, lo, hi):
    """points: [(x, y_screen) or None]; None breaks the line."""
    path = ui.Path()
    path.line_width = 2
    path.line_join_style = ui.LINE_JOIN_ROUND
    down = False
    last = None
    for pt in points:
        if pt is None:
            down = False
            continue
        if down:
            path.line_to(*pt)
        else:
            path.move_to(*pt)
            down = True
        last = pt
    ui.set_color(color)
    path.stroke()
    if last is not None:
        ui.Path.oval(last[0] - 3, last[1] - 3, 6, 6).fill()


class LineGraph(ui.View):
    """Index-based series, right-aligned: newest sample at the right edge."""

    def __init__(self, *args, **kwargs):
        caption = kwargs.pop('caption', '')
        y_min = kwargs.pop('y_min', None)
        y_max = kwargs.pop('y_max', None)
        capacity = kwargs.pop('capacity', 120)
        fmt = kwargs.pop('fmt', '%.0f')
        super().__init__(*args, **kwargs)
        self.caption = caption
        self.y_min = y_min
        self.y_max = y_max
        self.capacity = capacity
        self.fmt = fmt
        self.series = []  # [(label, color, values)], values may contain None

    def draw(self):
        w, h = _panel(self, self.caption)
        pw, ph = w - PAD_L - PAD_R, h - PAD_T - PAD_B
        if pw < 20 or ph < 20:
            return
        _legend([(s[0], s[1]) for s in self.series], w, self.caption)
        vals = [v for _, _, vs in self.series for v in vs if v is not None]
        lo, hi = _auto_range(vals, self.y_min, self.y_max)
        ymap = _grid(lo, hi, PAD_T, ph, PAD_L, pw, self.fmt)
        n = max(2, self.capacity)
        step = pw / float(n - 1)
        for _, color, values in self.series:
            vs = list(values)[-n:]
            off = n - len(vs)
            pts = [None if v is None else (PAD_L + (off + i) * step, ymap(max(lo, min(hi, v))))
                   for i, v in enumerate(vs)]
            _stroke(pts, color, lo, hi)


class TimeGraph(ui.View):
    """XY series where x is a date ordinal (datetime.date.toordinal())."""

    def __init__(self, *args, **kwargs):
        caption = kwargs.pop('caption', '')
        y_min = kwargs.pop('y_min', None)
        y_max = kwargs.pop('y_max', None)
        fmt = kwargs.pop('fmt', '%.0f')
        super().__init__(*args, **kwargs)
        self.caption = caption
        self.y_min = y_min
        self.y_max = y_max
        self.fmt = fmt
        self.bands = []   # [(y_value, label)] dashed reference lines, e.g. letter cutoffs
        self.series = []  # [(label, color, [(x_ordinal, y)])]

    def draw(self):
        w, h = _panel(self, self.caption)
        bottom = 16
        pw, ph = w - PAD_L - PAD_R, h - PAD_T - PAD_B - bottom
        if pw < 20 or ph < 20:
            return
        _legend([(s[0], s[1]) for s in self.series], w, self.caption)
        pts_all = [p for _, _, ps in self.series for p in ps]
        if not pts_all:
            ui.draw_string('No data yet', rect=(0, h / 2 - 8, w, 16), font=SMALL,
                           color=theme.MUTED, alignment=ui.ALIGN_CENTER)
            return
        lo, hi = _auto_range([p[1] for p in pts_all], self.y_min, self.y_max)
        ymap = _grid(lo, hi, PAD_T, ph, PAD_L, pw, self.fmt)
        for yv, name in self.bands:
            if lo < yv < hi:
                y = ymap(yv)
                p = ui.Path()
                p.move_to(PAD_L, y)
                p.line_to(PAD_L + pw, y)
                p.line_width = 0.5
                p.set_line_dash([3, 3])
                ui.set_color(theme.MUTED)
                p.stroke()
                ui.draw_string(name, rect=(PAD_L + pw - 24, y - 12, 22, 12), font=(theme.FONT, 9),
                               color=theme.MUTED, alignment=ui.ALIGN_RIGHT)
        x0 = min(p[0] for p in pts_all)
        x1 = max(p[0] for p in pts_all)
        if x1 == x0:
            x0, x1 = x0 - 1, x1 + 1

        def xmap(x):
            return PAD_L + (x - x0) / float(x1 - x0) * pw
        for i in range(3):
            xo = x0 + (x1 - x0) * i / 2.0
            d = datetime.date.fromordinal(int(round(xo)))
            ui.draw_string('%d/%d' % (d.month, d.day), rect=(xmap(xo) - 20, h - PAD_B - 13, 40, 12),
                           font=(theme.FONT, 9), color=theme.MUTED, alignment=ui.ALIGN_CENTER)
        for _, color, ps in self.series:
            pts = [(xmap(x), ymap(max(lo, min(hi, y)))) for x, y in sorted(ps)]
            _stroke(pts, color, lo, hi)


class BarGraph(ui.View):
    def __init__(self, *args, **kwargs):
        caption = kwargs.pop('caption', '')
        fmt = kwargs.pop('fmt', '%.0f')
        super().__init__(*args, **kwargs)
        self.caption = caption
        self.fmt = fmt
        self.bars = []  # [(label, value, color)]

    def draw(self):
        w, h = _panel(self, self.caption)
        bottom = 14
        pw, ph = w - PAD_L - PAD_R, h - PAD_T - PAD_B - bottom
        if pw < 20 or ph < 20:
            return
        if not self.bars:
            ui.draw_string('No data', rect=(0, h / 2 - 8, w, 16), font=SMALL,
                           color=theme.MUTED, alignment=ui.ALIGN_CENTER)
            return
        hi = max(v for _, v, _ in self.bars) * 1.1 or 1.0
        _grid(0.0, hi, PAD_T, ph, PAD_L, pw, self.fmt, n=3)
        slot = pw / float(len(self.bars))
        bw = max(2.0, slot * 0.7)
        every = max(1, int(16 // slot) + 1)
        for i, (name, v, color) in enumerate(self.bars):
            x = PAD_L + i * slot + (slot - bw) / 2
            bh = v / hi * ph
            ui.set_color(color)
            ui.Path.rect(x, PAD_T + ph - bh, bw, bh).fill()
            if i % every == 0:
                ui.draw_string(str(name), rect=(x - 8, PAD_T + ph + 2, bw + 16, 12),
                               font=(theme.FONT, 9), color=theme.MUTED, alignment=ui.ALIGN_CENTER)
