"""Classify: on-device image classification and object detection.

Camera or Photo, then the image goes through Apple's Vision framework
(ptlib/vision.py), locally with no network:
- Labels: whole-image classification against Vision's built-in taxonomy
  (~1,300 classes), best first, with confidence bars.
- Objects: detected regions (salient objects, animals, people, faces) drawn
  on the photo; unlabelled regions are named by classifying their crop.
Tap a box or an object row to highlight it. Copy puts the results on the
clipboard. Tag hands the photo and its detections to Tagger, where they can
be corrected, confirmed and saved.
"""
import time

import ui
import console
import clipboard
from objc_util import on_main_thread

from ptlib import theme, log
from ptlib.imgcanvas import ImageCanvas

_log = log.get('classify')
TITLE = 'Classify'
TOP_LABELS = 25


@log.instrument
class ClassifyView(ui.View):
    def __init__(self):
        super().__init__()
        self.background_color = theme.BG
        self.img = None
        self.labels = []
        self.objects = []
        self.busy = False
        self.elapsed = 0.0
        self.canvas = ImageCanvas(self, editable=False)
        self.canvas.placeholder = 'Camera or Photo to classify an image'
        self.summary = theme.label('', 14, theme.TEXT, align=ui.ALIGN_LEFT, lines=2)
        self.seg = ui.SegmentedControl()
        self.seg.segments = ['Labels', 'Objects']
        self.seg.selected_index = 0
        self.seg.tint_color = theme.AMBER
        self.seg.action = lambda s: self.tv.reload()
        self.copy_btn = theme.button('Copy', self.on_copy, size=14)
        self.tv = theme.table(self, 46)
        for v in (self.canvas, self.summary, self.seg, self.copy_btn, self.tv):
            self.add_subview(v)
        self.right_button_items = [ui.ButtonItem(title='Camera', action=self.on_camera),
                                   ui.ButtonItem(title='Photo', action=self.on_photo),
                                   ui.ButtonItem(title='Tag', action=self.on_tag)]
        from ptlib import vision
        if not vision.available():
            self.summary.text = 'Vision image classification needs iOS 13 or later.'

    def layout(self):
        W, H, p = self.width, self.height, 8
        canvas_h = max(180, min(H * 0.45, W * 1.1))
        self.canvas.frame = (p, p, W - 2 * p, canvas_h)
        y = p + canvas_h + p
        self.summary.frame = (p + 4, y, W - 2 * p - 8, 36)
        y += 36 + p
        self.seg.frame = (p, y, W - 3 * p - 70, 32)
        self.copy_btn.frame = (W - p - 70, y, 70, 32)
        y += 32 + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def refresh(self):
        if self.busy:
            self.summary.text = 'Classifying…'
        elif self.img is not None:
            top = ', '.join('%s %d%%' % (l, round(c * 100)) for l, c in self.labels[:3]) or 'no confident label'
            self.summary.text = '%s\n%d object%s · %dx%d · %.2f s on device' % (
                top, len(self.objects), '' if len(self.objects) == 1 else 's',
                self.img.size[0], self.img.size[1], self.elapsed)
        self.canvas.set_needs_display()
        self.tv.reload()

    # ---- input ----
    @log.bg
    def on_camera(self, sender):
        import photos
        img = photos.capture_image()
        if img is not None:
            self._run(img)

    @log.bg
    def on_photo(self, sender):
        import photos
        try:
            asset = photos.pick_asset(title='Pick a photo to classify')
        except Exception:
            _log.exception('pick_asset failed')
            return
        if asset is not None:
            img = asset.get_image()
            if img is not None:
                self._run(img)

    def _run(self, img):
        """Background thread: full classify + detect pass."""
        from ptlib import vision
        self._set_busy(True)
        t0 = time.time()
        try:
            img = vision.prepare(img)
            labels = vision.classify(img, top=TOP_LABELS, min_conf=0.01)
            objects = vision.label_objects(img, vision.detect(img))
        except Exception:
            _log.exception('classify failed')
            self._set_busy(False)
            console.hud_alert('Classification failed (see Logs)', 'error', 1.5)
            return
        elapsed = time.time() - t0
        _log.info('classified %dx%d in %.2fs: %s | %d objects', img.size[0], img.size[1], elapsed,
                  ', '.join('%s %.2f' % lc for lc in labels[:5]), len(objects))
        self._show(img, labels, objects, elapsed, vision.to_ui_image(img))

    @on_main_thread
    def _set_busy(self, busy):
        self.busy = busy
        self.refresh()

    @on_main_thread
    def _show(self, img, labels, objects, elapsed, ui_img):
        self.img, self.labels, self.objects, self.elapsed = img, labels, objects, elapsed
        self.busy = False
        boxes = [{'box': o['box'], 'tag': o['label'], 'conf': o['conf'], 'confirmed': False} for o in objects]
        self.canvas.set_image(ui_img, img.size, boxes)
        self.refresh()

    # ---- canvas callbacks ----
    def box_selected(self, idx):
        if idx is not None:
            self.seg.selected_index = 1
        self.tv.reload()

    def box_added(self, idx):
        pass

    def box_changed(self, idx):
        pass

    # ---- actions ----
    def on_copy(self, sender):
        if self.img is None:
            console.hud_alert('Nothing classified yet', 'error', 0.8)
            return
        lines = ['Labels:'] + ['  %-28s %5.1f%%' % (l, c * 100) for l, c in self.labels]
        lines.append('Objects:')
        for i, o in enumerate(self.objects):
            x, y, w, h = o['box']
            lines.append('  %d %-20s %5.1f%%  %s  box %d,%d %dx%d' % (i + 1, o['label'], o['conf'] * 100, o['kind'],
                                                                   x, y, w, h))
        clipboard.set('\n'.join(lines))
        console.hud_alert('Copied')

    def on_tag(self, sender):
        if self.img is None:
            console.hud_alert('Classify a photo first', 'error', 0.8)
            return
        from ptapps import tagger
        boxes = [{'box': tuple(o['box']), 'tag': o['label'], 'conf': round(o['conf'], 3), 'confirmed': False,
                  'source': 'auto:' + o['kind'], 'suggest': [[o['label'], round(o['conf'], 3)]]}
                 for o in self.objects]
        v = tagger.EditorView(None, img=self.img, boxes=boxes)
        v.meta['labels'] = [[l, round(c, 3)] for l, c in self.labels[:8]]
        self.navigation_view.push_view(v)

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.labels) if self.seg.selected_index == 0 else len(self.objects)

    def tableview_cell_for_row(self, tv, section, row):
        if self.seg.selected_index == 0:
            label, conf = self.labels[row]
            cell = theme.cell(label, '%.1f%%' % (conf * 100), style='value1')
            bar = ui.View(background_color=theme.SERIES[0] if row == 0 else theme.PANEL_HI)
            bar.frame = (16, 40, max(2, (tv.width - 32) * conf), 4)
            bar.corner_radius = 2
            cell.content_view.add_subview(bar)
            return cell
        o = self.objects[row]
        cell = theme.cell('%d  %s' % (row + 1, o['label']), '%s · %.0f%%' % (o['kind'], o['conf'] * 100),
                          style='value1')
        if row == self.canvas.selected:
            cell.background_color = theme.PANEL_HI
            cell.text_label.text_color = theme.AMBER
        return cell

    def tableview_did_select(self, tv, section, row):
        if self.seg.selected_index == 1:
            self.canvas.select(None if self.canvas.selected == row else row)
        tv.reload()


def make_view():
    return ClassifyView()
