"""Grade tracker: multiple classes, weighted or points-based grading, history graphs,
and semester/cumulative GPA projection.

Class modes
  weighted: each assignment's weight is its % of the final grade.
            Basis = 100 (or the sum of weights, if you entered more than 100).
  points:   weight is ignored; each assignment counts its possible points.
            Basis = total possible points entered so far (add planned items
            with no score to make projections meaningful).

For each class:
  current = sum(w * pct) / sum(w graded)            -- grade on work so far
  floor   = sum(w * pct) / basis                    -- if every remaining item is 0
  ceiling = (sum(w * pct) + remaining) / basis      -- if every remaining item is 100%
  needed(cutoff) = (cutoff * basis - sum(w * pct)) / remaining
"""
import datetime
import time
import uuid

import ui
import console
import dialogs
from objc_util import on_main_thread

from ptlib import theme, storage, log
from ptlib.graph import TimeGraph

_log = log.get('grades')
TITLE = 'Grades'
APP = 'grades'
DEFAULT_SCALE = [[90, 'A', 4.0], [80, 'B', 3.0], [70, 'C', 2.0], [60, 'D', 1.0], [0, 'F', 0.0]]


# ---------------- pure logic (no ui) ----------------

def _today():
    return datetime.date.today().isoformat()


def new_class(name, credits=3.0, mode='weighted'):
    return {'id': uuid.uuid4().hex[:8], 'name': name, 'credits': credits, 'mode': mode, 'assignments': []}


def new_assignment(name, weight, earned, possible, date):
    return {'id': uuid.uuid4().hex[:8], 'name': name, 'weight': weight, 'earned': earned,
            'possible': possible, 'date': date}


def _eff_weight(c, a):
    return float(a.get('possible') or 0) if c.get('mode') == 'points' else float(a.get('weight') or 0)


def _graded(a):
    return a.get('earned') is not None and (a.get('possible') or 0) > 0


def letter(pct, scale):
    for cut, name, pts in scale:
        if pct is not None and pct >= cut:
            return name, pts
    return scale[-1][1], scale[-1][2]


def class_stats(c, scale):
    acc = 0.0
    w_graded = 0.0
    w_all = 0.0
    hist = []
    for a in sorted(c['assignments'], key=lambda a: (a.get('date') or '', a.get('name') or '')):
        w = _eff_weight(c, a)
        w_all += w
        if _graded(a) and w > 0:
            pct = float(a['earned']) / float(a['possible'])
            acc += w * pct
            w_graded += w
            d = datetime.date.fromisoformat(a.get('date') or _today())
            hist.append((d.toordinal(), 100.0 * acc / w_graded))
    if c.get('mode') == 'points':
        basis = w_all
    else:
        basis = max(100.0, w_all)
    remaining = max(0.0, basis - w_graded)
    s = {'graded_w': w_graded, 'basis': basis, 'remaining': remaining, 'history': hist,
         'current': None, 'floor': None, 'ceiling': None, 'needed': []}
    if w_graded > 0:
        s['current'] = 100.0 * acc / w_graded
    if basis > 0:
        s['floor'] = 100.0 * acc / basis
        s['ceiling'] = 100.0 * (acc + remaining) / basis
        if remaining > 0:
            for cut, name, _ in scale:
                if cut <= 0:
                    continue
                need = 100.0 * (cut / 100.0 * basis - acc) / remaining
                s['needed'].append((name, need))
    return s


def gpa(classes, scale, which='current', prior_gpa=None, prior_credits=0.0):
    """which: 'current' | 'floor' | 'ceiling'. Classes with no grade yet are skipped."""
    pts = 0.0
    cr = 0.0
    for c in classes:
        v = class_stats(c, scale)[which]
        if v is None or not c.get('credits'):
            continue
        pts += float(c['credits']) * letter(v, scale)[1]
        cr += float(c['credits'])
    sem = pts / cr if cr else None
    cum = None
    if prior_gpa is not None and prior_credits:
        tot = cr + float(prior_credits)
        cum = (pts + float(prior_gpa) * float(prior_credits)) / tot if tot else None
    return sem, cum


def parse_scale(txt):
    """'90 A 4, 80 B 3, 0 F 0' -> sorted scale; raises ValueError."""
    out = []
    for part in txt.split(','):
        bits = part.split()
        if not bits:
            continue
        if len(bits) != 3:
            raise ValueError(part)
        out.append([float(bits[0]), bits[1], float(bits[2])])
    if not out:
        raise ValueError('empty')
    out.sort(key=lambda r: -r[0])
    if out[-1][0] > 0:
        out.append([0, 'F', 0.0])
    return out


def scale_str(scale):
    return ', '.join('%g %s %g' % tuple(r) for r in scale)


def _num(s):
    s = (s or '').strip()
    return float(s) if s else None


def _fmt(v, nd=1):
    return '--' if v is None else ('%.' + str(nd) + 'f') % v


# ---------------- ui ----------------

class Store(object):
    def __init__(self):
        d = storage.load(APP, 'data.json') or {}
        self.classes = d.get('classes', [])
        self.scale = d.get('scale', DEFAULT_SCALE)
        self.prior_gpa = d.get('prior_gpa')
        self.prior_credits = d.get('prior_credits', 0.0)

    def save(self):
        storage.save(APP, 'data.json', {'classes': self.classes, 'scale': self.scale,
                                        'prior_gpa': self.prior_gpa, 'prior_credits': self.prior_credits})


def _bands(scale):
    return [(r[0], r[1]) for r in scale if r[0] > 0]


@log.instrument
class GradesView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_color = theme.BG
        self.store = Store()
        self.summary = theme.label('', 14, theme.TEXT, align=ui.ALIGN_LEFT, lines=3)
        self.graph = TimeGraph(caption='Running grade %')
        self.tv = theme.table(self, 60)
        for v in (self.summary, self.graph, self.tv):
            self.add_subview(v)
        self.right_button_items = [ui.ButtonItem(title='Add', action=self.on_add),
                                   ui.ButtonItem(title='Scale', action=self.on_settings)]
        self.refresh()

    def layout(self):
        W, H, p = self.width, self.height, 8
        self.summary.frame = (p + 4, p, W - 2 * p - 4, 58)
        gh = min(220, max(150, H * 0.3))
        self.graph.frame = (p, 70, W - 2 * p, gh)
        y = 70 + gh + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def refresh(self):
        st = self.store
        cur, cum = gpa(st.classes, st.scale, 'current', st.prior_gpa, st.prior_credits)
        lo, cum_lo = gpa(st.classes, st.scale, 'floor', st.prior_gpa, st.prior_credits)
        hi, cum_hi = gpa(st.classes, st.scale, 'ceiling', st.prior_gpa, st.prior_credits)
        credits = sum(float(c.get('credits') or 0) for c in st.classes)
        lines = ['Semester GPA %s   (%g cr)' % (_fmt(cur, 2), credits),
                 'Range if rest is 0%%…100%%: %s – %s' % (_fmt(lo, 2), _fmt(hi, 2))]
        if cum is not None:
            lines.append('Cumulative %s   (%s – %s)' % (_fmt(cum, 2), _fmt(cum_lo, 2), _fmt(cum_hi, 2)))
        else:
            lines.append('Set prior GPA/credits under Scale for cumulative')
        self.summary.text = '\n'.join(lines)
        self.graph.series = [(c['name'], theme.SERIES[i % len(theme.SERIES)], class_stats(c, st.scale)['history'])
                             for i, c in enumerate(st.classes)]
        self.graph.bands = _bands(st.scale)
        self.graph.set_needs_display()
        self.tv.reload()

    def save(self):
        self.store.save()
        self.refresh()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self.save()

    @log.bg
    def on_add(self, sender):
        r = dialogs.form_dialog(title='New class', fields=[
            {'type': 'text', 'key': 'name', 'title': 'Name', 'placeholder': 'CS 303'},
            {'type': 'number', 'key': 'credits', 'title': 'Credits', 'value': '3'},
            {'type': 'switch', 'key': 'points', 'title': 'Points-based (no weights)', 'value': False},
        ])
        if not r or not r['name'].strip():
            return
        try:
            cr = float(r['credits'] or 0)
        except ValueError:
            console.hud_alert('Bad credits', 'error')
            return
        self._apply(lambda: self.store.classes.append(
            new_class(r['name'].strip(), cr, 'points' if r['points'] else 'weighted')))

    @log.bg
    def on_settings(self, sender):
        st = self.store
        r = dialogs.form_dialog(title='Scale & history', fields=[
            {'type': 'text', 'key': 'scale', 'title': 'Scale (min letter pts, ...)', 'value': scale_str(st.scale)},
            {'type': 'number', 'key': 'pgpa', 'title': 'Prior cumulative GPA',
             'value': '' if st.prior_gpa is None else '%g' % st.prior_gpa},
            {'type': 'number', 'key': 'pcr', 'title': 'Prior credits', 'value': '%g' % (st.prior_credits or 0)},
        ])
        if not r:
            return
        try:
            scale = parse_scale(r['scale'])
            pg = _num(r['pgpa'])
            pc = _num(r['pcr']) or 0.0
        except ValueError:
            console.hud_alert('Could not parse', 'error')
            return

        def f():
            st.scale, st.prior_gpa, st.prior_credits = scale, pg, pc
        self._apply(f)

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.store.classes)

    def tableview_cell_for_row(self, tv, section, row):
        c = self.store.classes[row]
        s = class_stats(c, self.store.scale)
        if s['current'] is None:
            title = c['name']
        else:
            title = '%s   %.1f%% %s' % (c['name'], s['current'], letter(s['current'], self.store.scale)[0])
        pct_done = 100.0 * s['graded_w'] / s['basis'] if s['basis'] else 0
        detail = '%g cr · %.0f%% graded · range %s–%s' % (
            float(c.get('credits') or 0), pct_done, _fmt(s['floor'], 0), _fmt(s['ceiling'], 0))
        cell = theme.cell(title, detail, fg=theme.SERIES[row % len(theme.SERIES)])
        cell.accessory_type = 'disclosure_indicator'
        return cell

    def tableview_did_select(self, tv, section, row):
        c = self.store.classes[row]
        v = ClassView(self, c, theme.SERIES[row % len(theme.SERIES)])
        self.navigation_view.push_view(v)

    def tableview_can_delete(self, tv, section, row):
        return True

    def tableview_delete(self, tv, section, row):
        c = self.store.classes.pop(row)
        storage.save(APP, 'deleted_%s_%d.json' % (c['id'], int(time.time())), c)  # recoverable
        self.save()


@log.instrument
class ClassView(ui.View):
    def __init__(self, root, c, color):
        super().__init__()
        self.background_color = theme.BG
        self.root = root
        self.c = c
        self.color = color
        self.name = c['name']
        self.summary = theme.label('', 13, theme.TEXT, align=ui.ALIGN_LEFT, lines=4, mono=True)
        self.graph = TimeGraph(caption='Running grade %')
        self.tv = theme.table(self, 56)
        for v in (self.summary, self.graph, self.tv):
            self.add_subview(v)
        self.right_button_items = [ui.ButtonItem(title='Add', action=self.on_add),
                                   ui.ButtonItem(title='Edit', action=self.on_edit)]
        self.refresh()

    @property
    def rows(self):
        return sorted(self.c['assignments'], key=lambda a: (a.get('date') or '', a.get('name') or ''),
                      reverse=True)

    def layout(self):
        W, H, p = self.width, self.height, 8
        self.summary.frame = (p + 4, p, W - 2 * p - 4, 72)
        gh = min(200, max(140, H * 0.28))
        self.graph.frame = (p, 84, W - 2 * p, gh)
        y = 84 + gh + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def refresh(self):
        scale = self.root.store.scale
        s = class_stats(self.c, scale)
        cur = s['current']
        lines = ['Now %s%% %s   %s' % (_fmt(cur), letter(cur, scale)[0] if cur is not None else '',
                                       'points' if self.c.get('mode') == 'points' else 'weighted'),
                 'Floor %s%%  Ceiling %s%%' % (_fmt(s['floor']), _fmt(s['ceiling']))]
        if s['remaining'] > 0 and s['needed']:
            reach = ['%s:%s' % (n, 'done' if v <= 0 else ('%.0f' % v if v <= 100 else 'x'))
                     for n, v in s['needed']]
            lines.append('Need on rest  ' + '  '.join(reach))
        else:
            lines.append('No remaining weight entered')
        self.summary.text = '\n'.join(lines)
        self.graph.series = [(self.c['name'], self.color, s['history'])]
        self.graph.bands = _bands(scale)
        self.graph.set_needs_display()
        self.tv.reload()

    def save(self):
        self.root.store.save()
        self.root.refresh()
        self.refresh()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self.save()

    def _form(self, a=None):
        points = self.c.get('mode') == 'points'
        a = a or {}
        try:
            d = datetime.date.fromisoformat(a.get('date') or _today())
        except ValueError:
            d = datetime.date.today()
        fields = [{'type': 'text', 'key': 'name', 'title': 'Assignment', 'value': a.get('name', '')}]
        if not points:
            fields.append({'type': 'number', 'key': 'weight', 'title': 'Weight (% of grade)',
                           'value': '%g' % a['weight'] if a.get('weight') is not None else ''})
        fields += [
            {'type': 'number', 'key': 'earned', 'title': 'Earned (blank = not graded)',
             'value': '%g' % a['earned'] if a.get('earned') is not None else ''},
            {'type': 'number', 'key': 'possible', 'title': 'Possible',
             'value': '%g' % a['possible'] if a.get('possible') is not None else '100'},
            {'type': 'date', 'key': 'date', 'title': 'Date',
             'value': datetime.datetime.combine(d, datetime.time(12))},
        ]
        r = dialogs.form_dialog(title='Edit assignment' if a else 'New assignment', fields=fields)
        if not r:
            return None
        try:
            weight = 0.0 if points else (_num(r.get('weight')) or 0.0)
            earned = _num(r['earned'])
            possible = _num(r['possible']) or 100.0
        except ValueError:
            console.hud_alert('Numbers only', 'error')
            return None
        dv = r.get('date')
        date = dv.date().isoformat() if isinstance(dv, datetime.datetime) else (
            dv.isoformat() if isinstance(dv, datetime.date) else _today())
        return new_assignment(r['name'].strip() or 'Untitled', weight, earned, possible, date)

    @log.bg
    def on_add(self, sender):
        a = self._form()
        if a is not None:
            self._apply(lambda: self.c['assignments'].append(a))

    @log.bg
    def _edit_assignment(self, a):
        new = self._form(a)
        if new is not None:
            new['id'] = a['id']
            self._apply(lambda: a.update(new))

    @log.bg
    def on_edit(self, sender):
        c = self.c
        r = dialogs.form_dialog(title='Class', fields=[
            {'type': 'text', 'key': 'name', 'title': 'Name', 'value': c['name']},
            {'type': 'number', 'key': 'credits', 'title': 'Credits', 'value': '%g' % float(c.get('credits') or 0)},
            {'type': 'switch', 'key': 'points', 'title': 'Points-based (no weights)',
             'value': c.get('mode') == 'points'},
        ])
        if not r:
            return
        try:
            cr = float(r['credits'] or 0)
        except ValueError:
            console.hud_alert('Bad credits', 'error')
            return

        def f():
            c['name'] = r['name'].strip() or c['name']
            c['credits'] = cr
            c['mode'] = 'points' if r['points'] else 'weighted'
        self._apply(f)

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.c['assignments'])

    def tableview_cell_for_row(self, tv, section, row):
        a = self.rows[row]
        w = '' if self.c.get('mode') == 'points' else ' · w %g%%' % float(a.get('weight') or 0)
        date = (a.get('date') or '')[5:].replace('-', '/')
        if _graded(a):
            pct = 100.0 * a['earned'] / a['possible']
            title = '%s   %.1f%%' % (a['name'], pct)
            detail = '%g/%g%s · %s' % (a['earned'], a['possible'], w, date)
            fg = theme.TEXT
        else:
            title = a['name']
            detail = 'planned · /%g%s · %s' % (a['possible'], w, date)
            fg = theme.MUTED
        return theme.cell(title, detail, fg=fg)

    def tableview_did_select(self, tv, section, row):
        self._edit_assignment(self.rows[row])

    def tableview_can_delete(self, tv, section, row):
        return True

    def tableview_delete(self, tv, section, row):
        aid = self.rows[row]['id']
        self.c['assignments'] = [a for a in self.c['assignments'] if a['id'] != aid]
        self.save()


def make_view():
    return GradesView()
