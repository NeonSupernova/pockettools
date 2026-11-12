"""Tagger: photograph something, box the objects in it, tag them, keep a labelled library.

Library (root): every saved photo with a thumbnail, its tags and box count.
Camera / Photo start a new one; Tags filters the library by tag.

Editor:
- Drag on the photo to box an object. The crop is classified on-device
  (ptlib/vision.py) and a tag picker opens with those suggestions first,
  then your recent tags, then "Type a tag".
- Detect proposes boxes automatically (salient objects, animals, people,
  faces), each pre-labelled; they stay dashed until confirmed with Tag.
- Tap a box (on the photo or in the list) to select; drag it to move, drag
  its corner dot to resize. Delete removes the selected box.
- Changes autosave as soon as there's something to keep. More: save an
  annotated copy to Photos, copy the annotations as JSON, or label the
  whole photo.

Storage: data/tagger/<id>/image.jpg (upright, longest side <= 2048 px),
thumb.jpg and meta.json; boxes are pixel (x, y, w, h) in image.jpg, origin
top-left. Deleted items move to data/tagger/_deleted/.
"""
import io
import json
import os
import shutil
import time

import ui
import console
import dialogs
import clipboard
from objc_util import on_main_thread

from ptlib import theme, storage, haptics, log
from ptlib.imgcanvas import ImageCanvas

_log = log.get('tagger')
TITLE = 'Tagger'
APP = 'tagger'
MAX_RECENT = 30


# ---- storage ----
def _root():
    d = os.path.join(storage.BASE, APP)
    os.makedirs(d, exist_ok=True)
    return d


def item_dir(iid):
    return os.path.join(_root(), iid)


def list_items():
    out = []
    for name in os.listdir(_root()):
        meta = storage.load(APP, os.path.join(name, 'meta.json')) if not name.startswith('_') else None
        if meta:
            out.append(meta)
    out.sort(key=lambda m: -m.get('updated', 0))
    return out


def load_image(iid):
    from PIL import Image
    img = Image.open(os.path.join(item_dir(iid), 'image.jpg'))
    img.load()
    return img


def recent_tags():
    return storage.load(APP, 'tags.json', []) or []


def remember_tag(tag):
    tags = [t for t in recent_tags() if t != tag]
    tags.insert(0, tag)
    storage.save(APP, 'tags.json', tags[:MAX_RECENT])


def tag_summary(boxes):
    counts = {}
    for b in boxes:
        t = b.get('tag') or '?'
        counts[t] = counts.get(t, 0) + 1
    return ', '.join('%s ×%d' % (t, n) if n > 1 else t for t, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def annotated(img, boxes, max_side=None):
    """PIL copy of img with boxes and tags burned in (for sharing / Photos / thumbnails)."""
    from PIL import ImageDraw
    out = img.copy()
    k = 1.0
    if max_side and max(out.size) > max_side:
        out.thumbnail((max_side, max_side))
        k = out.size[0] / float(img.size[0])
    d = ImageDraw.Draw(out)
    lw = max(2, int(max(out.size) / 400))
    for i, b in enumerate(boxes):
        x, y, w, h = [v * k for v in b['box']]
        color = (76, 217, 123) if b.get('confirmed') else (63, 208, 224)
        d.rectangle([x, y, x + w, y + h], outline=color, width=lw)
        text = '%d %s' % (i + 1, b.get('tag') or '?')
        ty = y - 14 * lw if y > 14 * lw else y
        d.rectangle([x, ty, x + 7 * lw * len(text) + 4, ty + 12 * lw], fill=color)
        d.text((x + 2, ty + 1), text, fill=(16, 19, 23))
    return out


def export_json(meta):
    keep = ('box', 'tag', 'confirmed', 'source', 'conf')
    return json.dumps({
        'id': meta['id'], 'created': meta.get('created'), 'size': meta.get('size'),
        'labels': meta.get('labels', []),
        'objects': [dict((k, b[k]) for k in keep if k in b) for b in meta.get('boxes', [])],
    }, indent=1)


# ---- library ----
@log.instrument
class TaggerView(ui.View):
    def __init__(self):
        super().__init__()
        self.background_color = theme.BG
        self.items = []
        self.filter = None
        self.thumbs = {}
        self.tv = theme.table(self, 72)
        self.tv.frame = self.bounds
        self.tv.flex = 'WH'
        self.add_subview(self.tv)
        self.right_button_items = [ui.ButtonItem(title='Camera', action=self.on_camera),
                                   ui.ButtonItem(title='Photo', action=self.on_photo),
                                   ui.ButtonItem(title='Tags', action=self.on_tags)]
        self.refresh()

    def refresh(self):
        items = list_items()
        if self.filter:
            items = [m for m in items if any(b.get('tag') == self.filter for b in m.get('boxes', []))]
        self.items = items
        self.tv.reload()

    @on_main_thread
    def refresh_main(self):
        self.refresh()

    def _thumb(self, meta):
        key = (meta['id'], meta.get('updated'))
        im = self.thumbs.get(key)
        if im is None:
            p = os.path.join(item_dir(meta['id']), 'thumb.jpg')
            try:
                with open(p, 'rb') as f:
                    im = ui.Image.from_data(f.read())
            except OSError:
                im = None
            self.thumbs[key] = im
        return im

    # ---- new photo ----
    @log.bg
    def on_camera(self, sender):
        import photos
        _log.info('capture_image')
        img = photos.capture_image()
        if img is not None:
            self._open_new(img)

    @log.bg
    def on_photo(self, sender):
        import photos
        try:
            asset = photos.pick_asset(title='Pick a photo to tag')
        except Exception:
            _log.exception('pick_asset failed')
            return
        if asset is None:
            return
        img = asset.get_image()
        if img is not None:
            self._open_new(img)

    def _open_new(self, img):
        from ptlib import vision
        console.show_activity()
        try:
            img = vision.prepare(img)
        finally:
            console.hide_activity()
        self._push_editor(img)

    @on_main_thread
    def _push_editor(self, img):
        self.navigation_view.push_view(EditorView(self, img=img))

    @log.bg
    def on_tags(self, sender):
        counts = {}
        for m in list_items():
            for b in m.get('boxes', []):
                t = b.get('tag')
                if t:
                    counts[t] = counts.get(t, 0) + 1
        items = ['All photos'] + ['%s (%d)' % (t, n) for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        choice = dialogs.list_dialog('Filter by tag', items)
        if choice is None:
            return
        self.filter = None if choice == 'All photos' else choice.rsplit(' (', 1)[0]
        self.refresh_main()

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.items) or 1

    def tableview_cell_for_row(self, tv, section, row):
        if not self.items:
            msg = 'Nothing tagged "%s"' % self.filter if self.filter else 'No tagged photos yet'
            return theme.cell(msg, 'Camera or Photo to start, then drag boxes around objects', fg=theme.MUTED)
        m = self.items[row]
        boxes = m.get('boxes', [])
        title = tag_summary(boxes) or 'untagged'
        when = time.strftime('%b %d %H:%M', time.localtime(m.get('created', 0)))
        cell = theme.cell(title, '%s · %d object%s' % (when, len(boxes), '' if len(boxes) == 1 else 's'))
        cell.text_label.font = (theme.BOLD, 16)
        im = self._thumb(m)
        if im is not None:
            cell.image_view.image = im
        cell.accessory_type = 'disclosure_indicator'
        return cell

    def tableview_did_select(self, tv, section, row):
        if not self.items:
            return
        m = self.items[row]
        try:
            img = load_image(m['id'])
        except Exception:
            _log.exception('load %s failed', m['id'])
            console.hud_alert('Could not open photo (see Logs)', 'error')
            return
        self.navigation_view.push_view(EditorView(self, img=img, meta=m))

    def tableview_can_delete(self, tv, section, row):
        return bool(self.items)

    def tableview_delete(self, tv, section, row):
        m = self.items[row]
        trash = os.path.join(_root(), '_deleted')
        os.makedirs(trash, exist_ok=True)
        try:
            shutil.move(item_dir(m['id']), os.path.join(trash, m['id']))  # recoverable
        except OSError:
            _log.exception('delete %s failed', m['id'])
        self.refresh()


# ---- editor ----
@log.instrument
class EditorView(ui.View):
    def __init__(self, library=None, img=None, meta=None, boxes=None):
        """library: TaggerView to refresh after saves (may be None when opened
        from Classify). img: prepared PIL image. meta: saved item, or None for
        a new photo. boxes: initial proposals for a new photo."""
        super().__init__()
        self.name = 'Tag objects'
        self.background_color = theme.BG
        self.library = library
        self.img = img
        self.meta = meta or {'id': None, 'created': time.time(), 'boxes': list(boxes or []),
                             'size': list(img.size), 'labels': []}
        self.boxes = self.meta['boxes']
        self.busy = False
        self.canvas = ImageCanvas(self, editable=True)
        self.info = theme.label('', 13, theme.MUTED, align=ui.ALIGN_LEFT, lines=2)
        self.tv = theme.table(self, 44)
        self.btns = [theme.button('Detect', self.on_detect, size=15),
                     theme.button('Tag', self.on_tag, size=15, fg=theme.GREEN),
                     theme.button('Delete', self.on_delete, size=15, fg=theme.RED),
                     theme.button('Save', self.on_save, size=15, bg=theme.PANEL_HI, fg=theme.AMBER)]
        for v in [self.canvas, self.info, self.tv] + self.btns:
            self.add_subview(v)
        self.right_button_items = [ui.ButtonItem(title='More', action=self.on_more)]
        from ptlib import vision
        self.canvas.set_image(vision.to_ui_image(img), img.size, self.boxes)
        self.refresh()

    def layout(self):
        W, H, p = self.width, self.height, 8
        bot_h, info_h = 44, 34
        list_h = max(88, min(176, H * 0.22))
        canvas_h = max(160, H - (p * 5 + bot_h + info_h + list_h))
        self.canvas.frame = (p, p, W - 2 * p, canvas_h)
        y = p + canvas_h + p
        self.info.frame = (p + 4, y, W - 2 * p - 8, info_h)
        y += info_h + p
        self.tv.frame = (0, y, W, list_h)
        y += list_h + p
        bw = (W - (len(self.btns) + 1) * p) / float(len(self.btns))
        for i, b in enumerate(self.btns):
            b.frame = (p + i * (bw + p), y, bw, bot_h)

    def refresh(self):
        sel = self.canvas.selected
        if self.busy:
            self.info.text = 'Working…'
        elif not self.boxes:
            self.info.text = 'Drag on the photo to box an object, or tap Detect to find them automatically.'
        elif sel is None:
            pending = sum(1 for b in self.boxes if not b.get('confirmed'))
            self.info.text = '%d object%s%s. Tap a box to select it.' % (
                len(self.boxes), '' if len(self.boxes) == 1 else 's',
                ', %d unconfirmed (dashed)' % pending if pending else '')
        else:
            b = self.boxes[sel]
            sug = ', '.join('%s %d%%' % (l, round(c * 100)) for l, c in b.get('suggest', [])[:3])
            self.info.text = 'Box %d: %s%s\n%s' % (sel + 1, b.get('tag') or 'untagged',
                                                   '' if b.get('confirmed') else ' (unconfirmed)',
                                                   ('Looks like: ' + sug) if sug else 'Drag to move, corner dot to resize')
        for b in self.btns[1:3]:
            b.alpha = 1.0 if sel is not None else 0.45
        self.btns[0].alpha = 0.45 if self.busy else 1.0
        self.canvas.set_needs_display()
        self.tv.reload()

    # ---- persistence ----
    def save(self):
        """Write image (first time), thumbnail and meta. Creates the item lazily."""
        from ptlib import vision
        m = self.meta
        first = m['id'] is None
        if first:
            m['id'] = time.strftime('%Y%m%d-%H%M%S') + '-%03d' % int((time.time() % 1) * 1000)
            os.makedirs(item_dir(m['id']), exist_ok=True)
            with open(os.path.join(item_dir(m['id']), 'image.jpg'), 'wb') as f:
                f.write(vision.jpeg_bytes(self.img, 90))
        m['updated'] = time.time()
        m['size'] = list(self.img.size)
        th = annotated(self.img, self.boxes, max_side=240)
        b = io.BytesIO()
        th.save(b, 'JPEG', quality=80)
        with open(os.path.join(item_dir(m['id']), 'thumb.jpg'), 'wb') as f:
            f.write(b.getvalue())
        storage.save(APP, os.path.join(m['id'], 'meta.json'), m)
        if first:
            _log.info('new item %s', m['id'])
        if self.library is not None:
            self.library.refresh_main()

    def _changed(self):
        self.save()
        self.refresh()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self._changed()

    @on_main_thread
    def _set_busy(self, busy):
        self.busy = busy
        self.refresh()

    # ---- canvas callbacks ----
    def box_selected(self, idx):
        self.refresh()

    def box_changed(self, idx):
        if idx is not None:
            self.boxes[idx].pop('suggest', None)
            self._changed()
            self._suggest_then_tag(idx, prompt=False)

    def box_added(self, idx):
        haptics.tap(0)
        self._changed()
        self._suggest_then_tag(idx, prompt=True)

    @log.bg
    def _suggest_then_tag(self, idx, prompt):
        from ptlib import vision
        if idx >= len(self.boxes):
            return
        b = self.boxes[idx]
        self._set_busy(True)
        try:
            sug = vision.classify(vision.crop(self.img, b['box']), top=5, min_conf=0.02)
        except Exception:
            _log.exception('classify crop failed')
            sug = []
        finally:
            self._set_busy(False)

        def f():
            b['suggest'] = [[l, round(c, 3)] for l, c in sug]
            if not b.get('confirmed') and sug:
                b['tag'], b['conf'] = sug[0][0], round(sug[0][1], 3)
        self._apply(f)
        if prompt:
            self._pick_tag(idx)

    # ---- actions ----
    def on_tag(self, sender):
        if self.canvas.selected is None:
            console.hud_alert('Tap a box first', 'error', 0.8)
            return
        self._pick_tag_bg(self.canvas.selected)

    @log.bg
    def _pick_tag_bg(self, idx):
        self._pick_tag(idx)

    def _pick_tag(self, idx):
        """Runs on a background thread (dialogs block)."""
        if idx >= len(self.boxes):
            return
        b = self.boxes[idx]
        typed = '✎ Type a tag…'
        items, values = [typed], [None]
        if b.get('tag') and not b.get('confirmed'):
            items.append('✓ Keep "%s"' % b['tag'])
            values.append(b['tag'])
        seen = set(values)
        for l, c in b.get('suggest', []):
            if l not in seen:
                items.append('%s  (%d%%)' % (l, round(c * 100)))
                values.append(l)
                seen.add(l)
        for t in recent_tags():
            if t not in seen:
                items.append(t)
                values.append(t)
                seen.add(t)
        choice = dialogs.list_dialog('Tag box %d' % (idx + 1), items)
        if choice is None:
            return
        tag = values[items.index(choice)]
        if tag is None:
            try:
                tag = console.input_alert('Tag box %d' % (idx + 1), 'What is it?', b.get('tag') or '', 'Tag').strip()
            except KeyboardInterrupt:
                return
        if not tag:
            return
        remember_tag(tag)

        def f():
            b['tag'] = tag
            b['confirmed'] = True
            b['conf'] = None if b.get('source') == 'manual' else b.get('conf')
        self._apply(f)

    def on_delete(self, sender):
        idx = self.canvas.selected
        if idx is None:
            console.hud_alert('Tap a box first', 'error', 0.8)
            return
        del self.boxes[idx]
        self.canvas.select(None)
        self._changed()

    @log.bg
    def on_detect(self, sender):
        from ptlib import vision
        if self.busy:
            return
        self._set_busy(True)
        try:
            found = vision.label_objects(self.img, vision.detect(self.img))
        except Exception:
            _log.exception('detect failed')
            found = None
        finally:
            self._set_busy(False)
        if found is None:
            console.hud_alert('Detection failed (see Logs)', 'error')
            return
        new = [f for f in found if all(vision.iou(f['box'], b['box']) < 0.6 for b in self.boxes)]
        if not new:
            console.hud_alert('Nothing new found', 'error', 1.2)
            return

        def f():
            for o in new:
                self.boxes.append({'box': tuple(o['box']), 'tag': o['label'], 'conf': round(o['conf'], 3),
                                   'confirmed': False, 'source': 'auto:' + o['kind'],
                                   'suggest': [[o['label'], round(o['conf'], 3)]]})
        self._apply(f)
        console.hud_alert('Found %d: tap each, then Tag to confirm' % len(new), 'success', 1.6)

    def on_save(self, sender):
        if not self.boxes:
            console.hud_alert('Box at least one object first', 'error', 1)
            return
        self.save()
        console.hud_alert('Saved', 'success', 0.8)
        nav = self.navigation_view
        if nav is not None:
            nav.pop_view()

    @log.bg
    def on_more(self, sender):
        opts = ['Label whole photo', 'Save annotated copy to Photos', 'Copy annotations (JSON)']
        choice = dialogs.list_dialog('More', opts)
        if choice == opts[0]:
            self._label_whole()
        elif choice == opts[1]:
            import photos
            from ptlib import vision
            p = os.path.join(storage.BASE, APP, '_export.jpg')
            with open(p, 'wb') as f:
                f.write(vision.jpeg_bytes(annotated(self.img, self.boxes), 92))
            ok = photos.create_image_asset(p)
            console.hud_alert('Saved to Photos' if ok else 'Photos refused (permission?)',
                              'success' if ok else 'error', 1.4)
        elif choice == opts[2]:
            if self.meta['id'] is None:
                self.save()
            clipboard.set(export_json(self.meta))
            console.hud_alert('Copied JSON', 'success', 0.8)

    def _label_whole(self):
        from ptlib import vision
        self._set_busy(True)
        try:
            labels = vision.classify(self.img, top=8, min_conf=0.05)
        finally:
            self._set_busy(False)
        self._apply(lambda: self.meta.__setitem__('labels', [[l, round(c, 3)] for l, c in labels]))
        console.hud_alert(', '.join(l for l, _ in labels[:4]) or 'No confident labels', 'success', 2)

    # ---- object list ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.boxes)

    def tableview_cell_for_row(self, tv, section, row):
        b = self.boxes[row]
        ok = b.get('confirmed')
        detail = 'tagged' if ok else ('%d%% guess' % round(100 * b['conf']) if b.get('conf') else 'unconfirmed')
        if str(b.get('source', '')).startswith('auto:'):
            detail += ' · ' + b['source'][5:]
        cell = theme.cell('%d  %s' % (row + 1, b.get('tag') or 'untagged'), detail, style='value1',
                          fg=theme.GREEN if ok else theme.CYAN)
        if row == self.canvas.selected:
            cell.background_color = theme.PANEL_HI
            cell.text_label.text_color = theme.AMBER
        return cell

    def tableview_did_select(self, tv, section, row):
        self.canvas.select(None if self.canvas.selected == row else row)
        self.refresh()

    def tableview_can_delete(self, tv, section, row):
        return True

    def tableview_delete(self, tv, section, row):
        del self.boxes[row]
        self.canvas.select(None)
        self._changed()


def make_view():
    return TaggerView()
