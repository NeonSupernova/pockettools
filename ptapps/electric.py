"""Electrical calculators: wire gauge, Ohm's law, clamp-meter readings.

Wire: enter power or current, system voltage, one-way run length and the
allowed drop. It sizes for voltage drop (CM = K*I*2L/Vdrop) and for ampacity
(the wire must carry the fuse, fuse = next standard size >= 1.25 x current),
recommends the thicker of the two, and lists neighbouring gauges with their
drop, voltage at the load, heat lost and ampacity. Presets cover common
12 V loads (blower motor on high/low, truck blower, fuel pump, lights) and a
120 V branch circuit.

Ohm: fill any two of V, I, R, P.

Meter: clamp reading (and loops through the jaws), battery and load
voltages, run length: gives the real current, how much the existing wiring
drops, its resistance and what gauge it behaves like.

All maths lives in ptlib/elec.py. Fields autosave.
"""
import ui
import console
import dialogs
import clipboard
from objc_util import on_main_thread

from ptlib import theme, storage, log, elec

_log = log.get('electric')
TITLE = 'Electrical'
APP = 'electric'

PRESETS = [
    ('Car blower motor, high (15 A, 10 ft)', {'watts': '', 'amps': '15', 'volts': '12', 'feet': '10', 'drop': '3'}),
    ('Car blower motor, low (5 A)', {'watts': '', 'amps': '5', 'volts': '12', 'feet': '10', 'drop': '3'}),
    ('Truck/SUV blower, high (25 A)', {'watts': '', 'amps': '25', 'volts': '12', 'feet': '10', 'drop': '3'}),
    ('Electric fuel pump (8 A, 15 ft)', {'watts': '', 'amps': '8', 'volts': '12', 'feet': '15', 'drop': '3'}),
    ('Headlights 2 × 55 W (8 ft)', {'watts': '110', 'amps': '', 'volts': '12', 'feet': '8', 'drop': '3'}),
    ('Radiator fan (25 A, 6 ft)', {'watts': '', 'amps': '25', 'volts': '12', 'feet': '6', 'drop': '3'}),
    ('120 V 15 A circuit, 50 ft (NEC)', {'watts': '', 'amps': '15', 'volts': '120', 'feet': '50', 'drop': '3',
                                         'table': 1}),
]

WIRE_NOTES = """
Notes
• Size for running current. Inrush (2–3× for motors)
  lasts milliseconds: use a slow-blow fuse, not
  thicker wire.
• Chassis ratings assume one wire in free air. In a
  bundle or a hot engine bay, set derate to ~70%.
• K = 10.8 Cu / 17.0 Al is near room temperature;
  hot wire has ~20% more resistance.
• At 12 V, 3% is 0.36 V; motors tolerate ~5%,
  lights and electronics want ≤ 3%."""

METER_NOTES = """
Clamp meter checklist
• Needs DC current (Hall-effect). AC-only clamps
  read nothing on DC.
• Zero it with the jaws closed, then clamp ONE
  conductor; both wires cancel to ~0.
• Engine running; read each fan speed. MAX/INRUSH
  mode catches start-up current.
• Low currents: loop the wire N times through the
  jaws and enter N.
• Volts at the battery and at the motor (on high)
  show what the existing wiring drops."""


def _num(s):
    s = (s or '').strip().replace(',', '.')
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError('"%s" is not a number' % s)


def _fmt(x, nd=2):
    if x is None:
        return '-'
    if x == float('inf'):
        return '∞'
    return ('%.' + str(nd) + 'f') % x


class _FieldDelegate(object):
    def __init__(self, cb):
        self.cb = cb

    def textfield_did_change(self, tf):
        self.cb()

    def textfield_should_return(self, tf):
        tf.end_editing()
        return True


@log.instrument
class FormTab(ui.View):
    """Grid of numeric fields + segmented controls over a monospace result view."""

    def __init__(self, key, fields, segs, compute, state):
        super().__init__()
        self.key, self.compute, self.state = key, compute, state
        saved = state.setdefault(key, {})
        self.background_color = theme.BG
        self.delegate = _FieldDelegate(self.changed)
        self.fields, self.labels, self.segs = {}, {}, {}
        for fkey, title, placeholder in fields:
            lb = theme.label(title, 11, theme.MUTED, align=ui.ALIGN_LEFT)
            tf = ui.TextField()
            tf.text = saved.get(fkey, '')
            tf.placeholder = placeholder
            tf.keyboard_type = ui.KEYBOARD_DECIMAL_PAD
            tf.clear_button_mode = 'while_editing'
            tf.background_color = theme.PANEL
            tf.text_color = theme.TEXT
            tf.tint_color = theme.AMBER
            tf.font = (theme.MONO, 17)
            tf.bordered = False
            tf.corner_radius = 8
            tf.delegate = self.delegate
            self.fields[fkey], self.labels[fkey] = tf, lb
            self.add_subview(lb)
            self.add_subview(tf)
        self.order = [f[0] for f in fields]
        for skey, options in segs:
            sc = ui.SegmentedControl()
            sc.segments = options
            sc.selected_index = min(saved.get(skey, 0), len(options) - 1)
            sc.tint_color = theme.AMBER
            sc.action = lambda s: self.changed()
            self.segs[skey] = sc
            self.add_subview(sc)
        self.seg_order = [s[0] for s in segs]
        self.out = ui.TextView()
        self.out.editable = False
        self.out.font = (theme.MONO, 12)
        self.out.text_color = theme.TEXT
        self.out.background_color = theme.BG
        self.add_subview(self.out)
        self.recompute()

    def layout(self):
        W, p = self.width, 8
        col = (W - 3 * p) / 2.0
        y = p
        for i, k in enumerate(self.order):
            c = i % 2
            if i and c == 0:
                y += 54
            x = p + c * (col + p)
            self.labels[k].frame = (x + 2, y, col - 4, 14)
            self.fields[k].frame = (x, y + 15, col, 34)
        y += 54 + 2
        for k in self.seg_order:
            self.segs[k].frame = (p, y, W - 2 * p, 30)
            y += 30 + p
        self.out.frame = (p, y, W - 2 * p, max(0, self.height - y))

    def values(self):
        return dict((k, tf.text) for k, tf in self.fields.items())

    def seg(self, k):
        return self.segs[k].selected_index

    def apply(self, vals):
        for k, v in vals.items():
            if k in self.fields:
                self.fields[k].text = v
            elif k in self.segs:
                self.segs[k].selected_index = v
        self.changed()

    def end_editing(self):
        for tf in self.fields.values():
            tf.end_editing()

    def changed(self):
        saved = self.state[self.key]
        saved.update(self.values())
        for k in self.segs:
            saved[k] = self.seg(k)
        storage.save(APP, 'state.json', self.state)
        self.recompute()

    def recompute(self):
        try:
            self.out.text = self.compute(self)
        except ValueError as e:
            self.out.text = 'Check inputs: %s' % e
        except ZeroDivisionError:
            self.out.text = 'Check inputs: division by zero'


# ---- calculators ----
def wire_text(t):
    v = t.values()
    watts, amps = _num(v['watts']), _num(v['amps'])
    volts, feet, pct = _num(v['volts']), _num(v['feet']), _num(v['drop'])
    derate = (_num(v['derate']) or 100.0) / 100.0
    if volts is None or feet is None or pct is None or (watts is None and amps is None):
        return ('Enter power OR current, plus volts, one-way length\n'
                'and allowed drop %. Presets: top right.\n' + WIRE_NOTES)
    material = ('copper', 'aluminum')[t.seg('material')]
    table = ('chassis', 'nec')[t.seg('table')]
    r = elec.size_wire(volts, feet, pct, current=amps, watts=None if amps is not None else watts,
                       material=material, table=table, derate=derate)
    src = '  (%s W / %s V)' % (_fmt(watts, 0), _fmt(volts, 1)) if amps is None else '  (%s W)' % _fmt(r['watts'], 0)
    tname = elec.TABLES[table][0]
    lines = [
        'Current   %s A%s' % (_fmt(r['current']), src),
        'Max drop  %s V  (%s%% of %s V)' % (_fmt(r['vdrop_max'], 3), _fmt(pct, 1), _fmt(volts, 1)),
        'Wire run  %s ft round trip (2 × %s ft)' % (_fmt(2 * feet, 0), _fmt(feet, 0)),
        'By drop   %s CM = %s AWG → %s' % (format(int(round(r['cm_needed'])), ','), _fmt(r['awg_exact'], 1),
                                           elec.label(r['by_drop']) + ' AWG' if r['by_drop'] is not None else '> 4/0'),
        'Fuse      %s A  (≥ 1.25 × %s A)' % (_fmt(r['fuse'], 1).rstrip('0').rstrip('.') if r['fuse'] else '> 400',
                                             _fmt(r['current'], 1)),
        'By amps   carry %s A → %s' % (_fmt(r['fuse'] or r['current'], 0),
                                       elec.label(r['by_amps']) + ' AWG' if r['by_amps'] is not None else 'none in table'),
        '          %s%s' % (tname, ', derated %d%%' % round(derate * 100) if derate != 1 else ''),
        '─' * 44,
        ('USE  %s AWG %s' % (elec.label(r['pick']), material)) if r['pick'] is not None else 'USE  thicker than 4/0',
        '     %s' % r['decides'],
        '─' * 44,
        'AWG  mΩ/ft  drop V    %   V@load  loss W  amps',
    ]
    for row in elec.gauge_rows(r):
        amp = '-' if row['amp'] is None else '%d' % row['amp']
        lines.append('%s%-4s %5.2f  %6.3f %5.1f%s %6.2f  %6.1f  %4s%s' % (
            '►' if row['pick'] else ' ', elec.label(row['awg']), row['mohm_ft'], row['vdrop'], row['pct'],
            ' ' if row['drop_ok'] else '!', row['v_load'], row['loss_w'], amp, '✓' if row['amp_ok'] else '✗'))
    lines.append('! = over the allowed drop   ✗ = below fuse rating')
    return '\n'.join(lines) + '\n' + WIRE_NOTES


def ohm_text(t):
    v = t.values()
    vals = dict((k, _num(v[k])) for k in ('v', 'i', 'r', 'p'))
    filled = [k for k, x in vals.items() if x is not None]
    if len(filled) != 2:
        return ('Fill exactly two of V, I, R, P (%d filled).\n\n'
                'V = I·R    P = V·I = I²·R = V²/R\n'
                'Example: 180 W blower at 12 V → 15 A, 0.8 Ω.' % len(filled))
    r = elec.ohms_law(**vals)
    return '\n'.join([
        'Voltage     %s V' % _fmt(r['v'], 3),
        'Current     %s A' % _fmt(r['i'], 3),
        'Resistance  %s Ω  (%s mΩ)' % (_fmt(r['r'], 4), _fmt(r['r'] * 1000 if r['r'] != float('inf') else None, 1)),
        'Power       %s W' % _fmt(r['p'], 2),
        '',
        'from %s' % ' and '.join(k.upper() for k in sorted(filled)),
    ])


def meter_text(t):
    v = t.values()
    clamp = _num(v['clamp'])
    if clamp is None:
        return 'Enter the clamp reading (A).\n' + METER_NOTES
    loops = int(_num(v['loops']) or 1)
    material = ('copper', 'aluminum')[t.seg('material')]
    m = elec.meter(clamp, loops, _num(v['vbat']), _num(v['vload']), _num(v['feet']), _num(v['inrush']), material)
    lines = ['Current      %s A%s' % (_fmt(m['current']), '  (%s ÷ %d loops)' % (_fmt(clamp, 2), loops) if loops > 1 else '')]
    if 'inrush' in m:
        lines.append('Inrush       %s A  = %s× running' % (_fmt(m['inrush'], 1), _fmt(m['inrush_ratio'], 1)))
    if 'vdrop' in m:
        lines += [
            'Wiring drop  %s V  (%s%%)' % (_fmt(m['vdrop'], 3), _fmt(m['pct'], 1)),
            'Load power   %s W at the motor' % _fmt(m['load_watts'], 1),
            'Lost in wire %s W' % _fmt(m['loss_w'], 1),
        ]
        if 'r_circuit' in m:
            lines.append('Circuit R    %s mΩ (wire+connectors+switch)' % _fmt(m['r_circuit'] * 1000, 1))
        if 'mohm_ft' in m:
            lines.append('Per foot     %s mΩ/ft → behaves like %s AWG' % (_fmt(m['mohm_ft'], 2), _fmt(m['awg_equiv'], 1)))
        if m.get('pct') is not None and m['pct'] > 5:
            lines.append('\nOver 5%: look for a corroded ground, tired connector or resistor pack before blaming wire size.')
        elif m.get('vdrop') is not None and m['vdrop'] <= 0:
            lines.append('\nNo drop measured: check meter leads / readings.')
    else:
        lines.append('\nAdd battery and motor volts to see the wiring drop.')
    return '\n'.join(lines) + '\n' + METER_NOTES


@log.instrument
class ElectricView(ui.View):
    def __init__(self):
        super().__init__()
        self.background_color = theme.BG
        self.state = storage.load(APP, 'state.json') or {}
        self.state.setdefault('wire', {'volts': '12', 'feet': '10', 'drop': '3', 'derate': '100', 'amps': '15'})
        self.tabs = [
            FormTab('wire', [('watts', 'Power (W)', 'or current'), ('amps', 'Current (A)', 'or power'),
                             ('volts', 'System volts', '12'), ('feet', 'Length one way (ft)', '10'),
                             ('drop', 'Allowed drop (%)', '3'), ('derate', 'Ampacity derate (%)', '100')],
                    [('material', ['Copper', 'Aluminum']), ('table', ['Chassis / auto', 'NEC 310.16 75°C'])],
                    wire_text, self.state),
            FormTab('ohm', [('v', 'Voltage V', ''), ('i', 'Current I (A)', ''),
                            ('r', 'Resistance R (Ω)', ''), ('p', 'Power P (W)', '')], [], ohm_text, self.state),
            FormTab('meter', [('clamp', 'Clamp reading (A)', ''), ('loops', 'Loops through jaws', '1'),
                              ('vbat', 'Volts at battery', '14.2'), ('vload', 'Volts at motor', '13.5'),
                              ('feet', 'Length one way (ft)', '10'), ('inrush', 'Inrush / MAX (A)', 'optional')],
                    [('material', ['Copper', 'Aluminum'])], meter_text, self.state),
        ]
        self.seg = ui.SegmentedControl()
        self.seg.segments = ['Wire gauge', "Ohm's law", 'Clamp meter']
        self.seg.selected_index = self.state.get('tab', 0)
        self.seg.tint_color = theme.AMBER
        self.seg.action = self.on_tab
        self.add_subview(self.seg)
        for t in self.tabs:
            self.add_subview(t)
        self.right_button_items = [ui.ButtonItem(title='Presets', action=self.on_presets),
                                   ui.ButtonItem(title='Copy', action=self.on_copy),
                                   ui.ButtonItem(title='⌨︎↓', action=self.on_hide_kb)]
        self.on_tab(None)

    def layout(self):
        p = 8
        self.seg.frame = (p, p, self.width - 2 * p, 32)
        for t in self.tabs:
            t.frame = (0, 48, self.width, self.height - 48)

    def on_tab(self, sender):
        i = self.seg.selected_index
        for j, t in enumerate(self.tabs):
            t.hidden = j != i
            t.end_editing()
        if self.state.get('tab') != i:
            self.state['tab'] = i
            storage.save(APP, 'state.json', self.state)

    def on_hide_kb(self, sender):
        for t in self.tabs:
            t.end_editing()

    def on_copy(self, sender):
        clipboard.set(self.tabs[self.seg.selected_index].out.text)
        console.hud_alert('Copied')

    @log.bg
    def on_presets(self, sender):
        names = [n for n, _ in PRESETS]
        choice = dialogs.list_dialog('Wire presets', names)
        if choice is None:
            return
        vals = dict(PRESETS[names.index(choice)][1])
        vals.setdefault('table', 0)
        self._apply_preset(vals)

    @on_main_thread
    def _apply_preset(self, vals):
        self.seg.selected_index = 0
        self.on_tab(None)
        self.tabs[0].apply(vals)


def make_view():
    return ElectricView()
