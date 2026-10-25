"""Named tally counters. Tap a row to select, tap the selected row to rename, swipe to delete."""
import ui
import console
from objc_util import on_main_thread

from ptlib import theme, storage, haptics, log

_log = log.get('counter')
TITLE = 'Counter'
APP = 'counter'


@log.instrument
class CounterView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_color = theme.BG
        self.d = storage.load(APP, 'counters.json') or {'counters': [{'name': 'Count', 'value': 0}], 'sel': 0}
        self.name_lbl = theme.label('', 18, theme.MUTED, bold=True)
        self.value_lbl = theme.label('0', 96, theme.CYAN, bold=True)
        self.plus = theme.button('+', lambda s: self.bump(1), bg=theme.PANEL_HI, fg=theme.AMBER, size=72)
        self.minus = theme.button('−', lambda s: self.bump(-1), size=32)
        self.reset_btn = theme.button('Reset', self.on_reset, fg=theme.RED)
        self.tv = theme.table(self, 48)
        for v in (self.name_lbl, self.value_lbl, self.plus, self.minus, self.reset_btn, self.tv):
            self.add_subview(v)
        self.right_button_items = [ui.ButtonItem(title='Add', action=self.on_add)]
        self.refresh()

    @property
    def cur(self):
        cs = self.d['counters']
        self.d['sel'] = max(0, min(self.d.get('sel', 0), len(cs) - 1))
        return cs[self.d['sel']]

    def layout(self):
        W, H, p = self.width, self.height, 8
        y = p
        self.name_lbl.frame = (p, y, W - 2 * p, 24)
        y += 24
        self.value_lbl.frame = (p, y, W - 2 * p, 110)
        y += 110 + p
        ph = max(120, H * 0.26)
        self.plus.frame = (p, y, W - 2 * p, ph)
        y += ph + p
        half = (W - 3 * p) / 2.0
        self.minus.frame = (p, y, half, 50)
        self.reset_btn.frame = (2 * p + half, y, half, 50)
        y += 50 + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def refresh(self):
        c = self.cur
        self.name_lbl.text = c['name']
        self.value_lbl.text = str(c['value'])
        self.tv.reload()

    def save(self):
        storage.save(APP, 'counters.json', self.d)

    def bump(self, n):
        self.cur['value'] += n
        haptics.tap(1 if n > 0 else 0)
        self.save()
        self.refresh()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self.save()
        self.refresh()

    @log.bg
    def on_reset(self, sender):
        try:
            console.alert('Reset', 'Reset "%s" to 0?' % self.cur['name'], 'Reset')
        except KeyboardInterrupt:
            return
        self._apply(lambda: self.cur.__setitem__('value', 0))

    @log.bg
    def on_add(self, sender):
        try:
            name = console.input_alert('New counter', '', '', 'Add').strip()
        except KeyboardInterrupt:
            return
        if name:
            def f():
                self.d['counters'].append({'name': name, 'value': 0})
                self.d['sel'] = len(self.d['counters']) - 1
            self._apply(f)

    @log.bg
    def _rename(self, row):
        c = self.d['counters'][row]
        try:
            name = console.input_alert('Rename', '', c['name'], 'Save').strip()
        except KeyboardInterrupt:
            return
        if name:
            self._apply(lambda: c.__setitem__('name', name))

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.d['counters'])

    def tableview_cell_for_row(self, tv, section, row):
        c = self.d['counters'][row]
        cell = theme.cell(c['name'], str(c['value']), style='value1')
        if row == self.d['sel']:
            cell.accessory_type = 'checkmark'
            cell.text_label.text_color = theme.AMBER
        return cell

    def tableview_did_select(self, tv, section, row):
        if row == self.d['sel']:
            self._rename(row)
        else:
            self.d['sel'] = row
            self.save()
            self.refresh()

    def tableview_can_delete(self, tv, section, row):
        return len(self.d['counters']) > 1

    def tableview_delete(self, tv, section, row):
        del self.d['counters'][row]
        if self.d['sel'] >= row:
            self.d['sel'] = max(0, self.d['sel'] - 1)
        self.save()
        self.refresh()

    def stop(self):
        self.save()


def make_view():
    return CounterView()
