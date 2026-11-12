"""Photo canvas with labelled boxes: draw, select, move and resize them.

Boxes are dicts with 'box': (x, y, w, h) in image pixels (origin top-left)
plus 'tag'/'label', 'confirmed' and optional 'conf'. The canvas keeps a
reference to the owner's list and edits boxes in place.

Gestures (editable=True):
- drag on empty photo: draw a new box (owner.box_added(index) when released)
- tap a box: select it (owner.box_selected(index or None))
- drag inside the selected box: move it; drag its corner handle: resize
"""
import ui

from ptlib import theme

HANDLE = 26   # pt: touch target of the resize handle
MIN_BOX = 14  # pt: smaller drags count as taps


def box_text(b):
    t = b.get('tag') or b.get('label') or '?'
    if b.get('conf') is not None and not b.get('confirmed', True):
        t += ' %d%%' % round(100 * b['conf'])
    return t


class ImageCanvas(ui.View):
    def __init__(self, owner=None, editable=True):
        super().__init__()
        self.owner = owner
        self.editable = editable
        self.background_color = theme.PANEL
        self.corner_radius = 10
        self.image = None       # ui.Image (display copy, may be downscaled)
        self.size = (1, 1)      # pixel size of the full image the boxes refer to
        self.boxes = []
        self.selected = None
        self.placeholder = 'No photo'
        self._drag = None

    # ---- coordinates ----
    def _fit(self):
        iw, ih = self.size
        s = min(self.width / float(iw), self.height / float(ih))
        return s, (self.width - iw * s) / 2.0, (self.height - ih * s) / 2.0

    def to_view(self, box):
        s, ox, oy = self._fit()
        x, y, w, h = box
        return (ox + x * s, oy + y * s, w * s, h * s)

    def to_image(self, x, y):
        s, ox, oy = self._fit()
        iw, ih = self.size
        return (min(iw, max(0.0, (x - ox) / s)), min(ih, max(0.0, (y - oy) / s)))

    def set_image(self, ui_image, size, boxes):
        self.image, self.size, self.boxes = ui_image, size, boxes
        self.selected = None
        self.set_needs_display()

    def select(self, idx):
        self.selected = idx
        self.set_needs_display()

    # ---- drawing ----
    def draw(self):
        if self.image is None:
            ui.draw_string(self.placeholder, rect=(0, self.height / 2.0 - 12, self.width, 24),
                           font=(theme.FONT, 15), color=theme.MUTED, alignment=ui.ALIGN_CENTER)
            return
        s, ox, oy = self._fit()
        iw, ih = self.size
        self.image.draw(ox, oy, iw * s, ih * s)
        order = [i for i in range(len(self.boxes)) if i != self.selected]
        if self.selected is not None and self.selected < len(self.boxes):
            order.append(self.selected)  # selected box on top
        for i in order:
            self._draw_box(i, self.boxes[i])

    def _draw_box(self, i, b):
        x, y, w, h = self.to_view(b['box'])
        sel = i == self.selected
        color = theme.AMBER if sel else (theme.GREEN if b.get('confirmed') else theme.CYAN)
        p = ui.Path.rect(x, y, w, h)
        p.line_width = 3 if sel else 2
        if not b.get('confirmed') and not sel:
            p.set_line_dash([6, 4])
        ui.set_color(color)
        p.stroke()
        text = '%d %s' % (i + 1, box_text(b))
        tw = min(max(40, 7.2 * len(text) + 10), max(40, self.width - x))
        ty = y - 20 if y >= 20 else y
        ui.set_color(color)
        ui.Path.rounded_rect(x, ty, tw, 20, 4).fill()
        ui.draw_string(text, rect=(x + 5, ty + 3, tw - 8, 16), font=(theme.BOLD, 12),
                       color=theme.BG, line_break_mode=ui.LB_TRUNCATE_TAIL)
        if sel and self.editable:
            ui.set_color(theme.AMBER)
            ui.Path.oval(x + w - 9, y + h - 9, 18, 18).fill()

    # ---- touches ----
    def _hit(self, x, y):
        """Smallest box containing the point (so nested boxes stay reachable)."""
        best, area = None, None
        for i, b in enumerate(self.boxes):
            bx, by, bw, bh = self.to_view(b['box'])
            if bx <= x <= bx + bw and by <= y <= by + bh and (area is None or bw * bh < area):
                best, area = i, bw * bh
        return best

    def touch_began(self, t):
        if self.image is None:
            return
        x, y = t.location
        mode = 'new'
        if self.editable and self.selected is not None and self.selected < len(self.boxes):
            bx, by, bw, bh = self.to_view(self.boxes[self.selected]['box'])
            if abs(x - (bx + bw)) < HANDLE and abs(y - (by + bh)) < HANDLE:
                mode = 'resize'
            elif bx <= x <= bx + bw and by <= y <= by + bh:
                mode = 'move'
        self._drag = {'mode': mode, 'start': (x, y), 'moved': False,
                      'orig': tuple(self.boxes[self.selected]['box']) if mode != 'new' else None,
                      'new': None}

    def touch_moved(self, t):
        d = self._drag
        if d is None or not self.editable:
            return
        x, y = t.location
        sx, sy = d['start']
        if not d['moved'] and abs(x - sx) < 6 and abs(y - sy) < 6:
            return
        d['moved'] = True
        s, _, _ = self._fit()
        if d['mode'] == 'new':
            ax, ay = self.to_image(sx, sy)
            bx, by = self.to_image(x, y)
            d['new'] = (min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay))
            if d.get('idx') is None:
                self.boxes.append({'box': d['new'], 'tag': '', 'confirmed': False, 'source': 'manual'})
                d['idx'] = len(self.boxes) - 1
                self.selected = d['idx']
            self.boxes[d['idx']]['box'] = d['new']
        else:
            ox, oy, ow, oh = d['orig']
            dx, dy = (x - sx) / s, (y - sy) / s
            iw, ih = self.size
            b = self.boxes[self.selected]
            if d['mode'] == 'move':
                nx = min(max(0.0, ox + dx), iw - ow)
                ny = min(max(0.0, oy + dy), ih - oh)
                b['box'] = (nx, ny, ow, oh)
            else:
                b['box'] = (ox, oy, min(max(8.0, ow + dx), iw - ox), min(max(8.0, oh + dy), ih - oy))
        self.set_needs_display()

    def touch_ended(self, t):
        d, self._drag = self._drag, None
        if d is None:
            return
        x, y = t.location
        if not d['moved']:
            idx = self._hit(x, y)
            self.selected = idx
            self.set_needs_display()
            if self.owner is not None:
                self.owner.box_selected(idx)
            return
        if not self.editable:
            return
        if d['mode'] == 'new':
            s, _, _ = self._fit()
            nb = d.get('idx')
            if nb is not None and (self.boxes[nb]['box'][2] * s < MIN_BOX or self.boxes[nb]['box'][3] * s < MIN_BOX):
                del self.boxes[nb]
                self.selected = None
                self.set_needs_display()
                return
            if nb is not None and self.owner is not None:
                self.owner.box_added(nb)
        elif self.owner is not None:
            self.owner.box_changed(self.selected)
        self.set_needs_display()
