"""Volleyball in-game tracker.

Rally scoring to 25 (deciding set to 15), win by 2, best of 5 or 3.
Serve and home rotation: rot[0] is position 1 (right back / server). A team
that wins a rally on the opponent's serve gains serve; if that team is home,
the home lineup rotates clockwise (rot shifts left by one).

Stats are recorded for home players. Kill/Ace/Block also award the point to
home, Err awards it to away, Dig is stat-only. Every mutation snapshots state
for undo and autosaves, so a crash or killed app loses nothing.
"""
import json
import time

import ui
import console
import dialogs
import clipboard
from objc_util import on_main_thread

from ptlib import theme, storage, haptics, log

_log = log.get('volleyball')
TITLE = 'Volleyball'
APP = 'volleyball'
TEAMS = ('home', 'away')
STATS = ('kill', 'ace', 'block', 'dig', 'error')
STAT_LABEL = {'kill': 'Kill', 'ace': 'Ace', 'block': 'Block', 'dig': 'Dig', 'error': 'Err'}
STAT_ABBR = {'kill': 'K', 'ace': 'A', 'block': 'B', 'dig': 'D', 'error': 'E'}
POINT_FOR_HOME = ('kill', 'ace', 'block')
POINT_FOR_AWAY = ('error',)
GRID = (3, 2, 1, 4, 5, 0)  # court slots (front L->R, back L->R) -> rot index


def other(team):
    return 'away' if team == 'home' else 'home'


def _num_key(p):
    return (0, int(p), '') if p.isdigit() else (1, 0, p)


class Match(object):
    def __init__(self, home='Home', away='Away', lineup=None, best_of=5, home_serves=True):
        self.home = home
        self.away = away
        self.best_of = best_of
        self.lineup = [str(p) for p in (lineup or ['1', '2', '3', '4', '5', '6'])]
        self.rot = list(self.lineup)
        self.score = {'home': 0, 'away': 0}
        self.sets = []
        self.set_no = 1
        self.first_serve = 'home' if home_serves else 'away'
        self.serving = self.first_serve
        self.timeouts = {'home': 0, 'away': 0}
        self.subs = {'home': 0, 'away': 0}
        self.stats = {}
        self.log = []
        self.finished = False

    def to_dict(self):
        return json.loads(json.dumps(self.__dict__))

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.__dict__.update(d)
        return m

    def name(self, team):
        return self.home if team == 'home' else self.away

    def target(self):
        return 15 if self.set_no >= self.best_of else 25

    def sets_won(self, team):
        i = 0 if team == 'home' else 1
        return sum(1 for s in self.sets if s[i] > s[1 - i])

    def started(self):
        return bool(self.log)

    def rotate(self):
        self.rot = self.rot[1:] + self.rot[:1]

    def point(self, team, note=None):
        if self.finished:
            return None
        if self.serving != team:
            self.serving = team
            if team == 'home':
                self.rotate()
        self.score[team] += 1
        self._log('%s point%s' % (self.name(team), ' (%s)' % note if note else ''))
        return self._check_set()

    def stat(self, player, kind):
        if self.finished:
            return None
        s = self.stats.setdefault(player, {})
        s[kind] = s.get(kind, 0) + 1
        note = '#%s %s' % (player, kind)
        if kind in POINT_FOR_HOME:
            return self.point('home', note)
        if kind in POINT_FOR_AWAY:
            return self.point('away', note)
        self._log(note)
        return None

    def timeout(self, team):
        if self.timeouts[team] >= 2:
            return False
        self.timeouts[team] += 1
        self._log('Timeout %s (%d/2)' % (self.name(team), self.timeouts[team]))
        return True

    def sub(self, idx, player):
        old = self.rot[idx]
        self.rot[idx] = player
        self.subs['home'] += 1
        self._log('Sub #%s for #%s (P%d)' % (player, old, idx + 1))

    def set_lineup(self, players):
        self.lineup = list(players)
        self.rot = list(players)
        self._log('Lineup ' + ' '.join(players))

    def toggle_serve(self):
        self.serving = other(self.serving)
        if self.score['home'] == 0 and self.score['away'] == 0:
            self.first_serve = self.serving

    def _check_set(self):
        h, a = self.score['home'], self.score['away']
        if max(h, a) < self.target() or abs(h - a) < 2:
            return None
        winner = 'home' if h > a else 'away'
        self.sets.append([h, a])
        self._log('Set %d to %s %d-%d' % (self.set_no, self.name(winner), h, a))
        if self.sets_won(winner) > self.best_of // 2:
            self.finished = True
            msg = '%s wins %d-%d' % (self.name(winner), self.sets_won(winner),
                                     self.sets_won(other(winner)))
            self._log('Match: ' + msg)
            return msg
        self.set_no += 1
        self.score = {'home': 0, 'away': 0}
        self.timeouts = {'home': 0, 'away': 0}
        self.subs = {'home': 0, 'away': 0}
        self.first_serve = other(self.first_serve)
        self.serving = self.first_serve
        return 'Set %d: %s %d-%d' % (self.set_no - 1, self.name(winner), h, a)

    def _log(self, text):
        self.log.append([self.set_no, self.score['home'], self.score['away'], text])

    def summary(self):
        out = ['%s vs %s' % (self.home, self.away)]
        sets = ', '.join('%d-%d' % tuple(s) for s in self.sets) or '-'
        out.append('Sets: %s  (%d-%d)' % (sets, self.sets_won('home'), self.sets_won('away')))
        if not self.finished:
            out.append('Now: set %d, %d-%d' % (self.set_no, self.score['home'], self.score['away']))
        out.append('')
        out.append('%-6s' % 'Player' + ''.join('%4s' % STAT_ABBR[k] for k in STATS) + '%5s' % 'Pts')
        tot = dict((k, 0) for k in STATS)
        for p in sorted(self.stats, key=_num_key):
            s = self.stats[p]
            pts = sum(s.get(k, 0) for k in POINT_FOR_HOME)
            out.append('%-6s' % ('#' + p) + ''.join('%4d' % s.get(k, 0) for k in STATS) + '%5d' % pts)
            for k in STATS:
                tot[k] += s.get(k, 0)
        out.append('%-6s' % 'Team' + ''.join('%4d' % tot[k] for k in STATS)
                   + '%5d' % sum(tot[k] for k in POINT_FOR_HOME))
        out.append('')
        out.append('Log (newest first):')
        for set_no, h, a, text in reversed(self.log[-80:]):
            out.append('S%d %2d-%-2d %s' % (set_no, h, a, text))
        return '\n'.join(out)


@log.instrument
class VolleyballView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_color = theme.BG
        d = storage.load(APP, 'current.json')
        self.m = Match.from_dict(d) if d else Match()
        self.undo_stack = []
        self.selected = None
        self.sub_mode = False
        self.right_button_items = [ui.ButtonItem(title='Stats', action=self.show_stats),
                                   ui.ButtonItem(title='New', action=self.new_match)]
        self.score_btn, self.team_lbl, self.serve_lbl, self.to_btn = {}, {}, {}, {}
        for t in TEAMS:
            fg = theme.CYAN if t == 'home' else theme.AMBER
            b = theme.button('0', lambda s, t=t: self.on_point(t), fg=fg, size=84)
            self.team_lbl[t] = theme.label('', 15, theme.MUTED, bold=True)
            self.serve_lbl[t] = theme.label('', 13, theme.AMBER, bold=True)
            b.add_subview(self.team_lbl[t])
            b.add_subview(self.serve_lbl[t])
            self.score_btn[t] = b
            self.add_subview(b)
            self.to_btn[t] = theme.button('TO', lambda s, t=t: self.on_timeout(t), size=15)
            self.add_subview(self.to_btn[t])
        self.info = theme.label('', 14, theme.TEXT)
        self.sub_btn = theme.button('Sub', self.on_sub_mode, size=15)
        self.net_lbl = theme.label('NET', 10, theme.MUTED, bold=True)
        for v in (self.info, self.sub_btn, self.net_lbl):
            self.add_subview(v)
        self.player_btn, self.pos_lbl = {}, {}
        for idx in range(6):
            b = theme.button('', lambda s, i=idx: self.on_player(i), size=24)
            lb = theme.label('P%d' % (idx + 1), 10, theme.MUTED, align=ui.ALIGN_LEFT)
            b.add_subview(lb)
            self.player_btn[idx], self.pos_lbl[idx] = b, lb
            self.add_subview(b)
        self.stat_btn = {}
        for k in STATS:
            fg = theme.RED if k == 'error' else theme.TEXT
            b = theme.button(STAT_LABEL[k], lambda s, k=k: self.on_stat(k), bg=theme.PANEL_HI, fg=fg, size=16)
            self.stat_btn[k] = b
            self.add_subview(b)
        self.bottom = [theme.button('Undo', self.on_undo, size=15),
                       theme.button('Serve ⇄', self.on_serve, size=15),
                       theme.button('Lineup', self.on_lineup, size=15)]
        for b in self.bottom:
            self.add_subview(b)
        self.refresh()

    # ---- layout / render ----
    def layout(self):
        W, H, p = self.width, self.height, 8
        info_h, row_h, cell_h, net_h, stat_h, bot_h = 22, 40, 54, 14, 48, 44
        fixed = p * 8 + info_h + row_h + net_h + 2 * cell_h + 6 + stat_h + bot_h
        score_h = max(110, min(300, H - fixed))
        y = p
        half = (W - 3 * p) / 2.0
        for i, t in enumerate(TEAMS):
            self.score_btn[t].frame = (p + i * (half + p), y, half, score_h)
            self.team_lbl[t].frame = (6, 6, half - 12, 20)
            self.serve_lbl[t].frame = (6, score_h - 24, half - 12, 18)
        y += score_h + p
        self.info.frame = (p, y, W - 2 * p, info_h)
        y += info_h + p
        third = (W - 4 * p) / 3.0
        for i, b in enumerate((self.to_btn['home'], self.sub_btn, self.to_btn['away'])):
            b.frame = (p + i * (third + p), y, third, row_h)
        y += row_h + p
        self.net_lbl.frame = (p, y, W - 2 * p, net_h)
        y += net_h
        for slot, idx in enumerate(GRID):
            r, c = divmod(slot, 3)
            self.player_btn[idx].frame = (p + c * (third + p), y + r * (cell_h + 6), third, cell_h)
            self.pos_lbl[idx].frame = (6, 3, 30, 12)
        y += 2 * cell_h + 6 + p
        sw = (W - (len(STATS) + 1) * p) / float(len(STATS))
        for i, k in enumerate(STATS):
            self.stat_btn[k].frame = (p + i * (sw + p), y, sw, stat_h)
        y += stat_h + p
        bw = (W - (len(self.bottom) + 1) * p) / float(len(self.bottom))
        for i, b in enumerate(self.bottom):
            b.frame = (p + i * (bw + p), y, bw, bot_h)

    def refresh(self):
        m = self.m
        for t in TEAMS:
            self.score_btn[t].title = str(m.score[t])
            self.team_lbl[t].text = '%s · %d' % (m.name(t), m.sets_won(t))
            self.serve_lbl[t].text = '● SERVING' if m.serving == t and not m.finished else ''
            self.to_btn[t].title = '%s TO %d/2' % (m.name(t)[:5], m.timeouts[t])
        done = '  '.join('%d-%d' % tuple(s) for s in m.sets)
        head = 'FINAL' if m.finished else 'Set %d (to %d)' % (m.set_no, m.target())
        self.info.text = (head + '   ' + done).strip()
        self.sub_btn.title = 'Pick player' if self.sub_mode else 'Sub (%d)' % m.subs['home']
        self.sub_btn.background_color = theme.AMBER if self.sub_mode else theme.PANEL
        self.sub_btn.tint_color = theme.BG if self.sub_mode else theme.TEXT
        for idx, b in self.player_btn.items():
            sel = idx == self.selected
            b.title = '#' + m.rot[idx]
            b.background_color = theme.CYAN if sel else theme.PANEL
            b.tint_color = theme.BG if sel else theme.TEXT
            b.border_color = theme.AMBER
            b.border_width = 2 if (idx == 0 and m.serving == 'home') else 0
        for b in self.stat_btn.values():
            b.alpha = 1.0 if self.selected is not None else 0.45

    # ---- state plumbing ----
    def save(self):
        storage.save(APP, 'current.json', self.m.to_dict())

    def _mutate(self, fn):
        self.undo_stack.append(json.dumps(self.m.__dict__))
        del self.undo_stack[:-300]
        msg = fn()
        self.save()
        self.refresh()
        if isinstance(msg, str):
            console.hud_alert(msg, 'success', 1.6)
        return msg

    @on_main_thread
    def _apply(self, fn):
        self._mutate(fn)

    # ---- actions ----
    def on_point(self, team):
        haptics.tap(1)
        self._mutate(lambda: self.m.point(team))

    def on_stat(self, kind):
        if self.selected is None:
            console.hud_alert('Tap a player first', 'error', 0.8)
            return
        player = self.m.rot[self.selected]
        self.selected = None
        haptics.tap(1)
        self._mutate(lambda: self.m.stat(player, kind))

    def on_timeout(self, team):
        if self.m.timeouts[team] >= 2:
            console.hud_alert('No timeouts left', 'error', 0.8)
            return
        self._mutate(lambda: self.m.timeout(team) and None)

    def on_player(self, idx):
        if self.sub_mode:
            self.sub_mode = False
            self.refresh()
            self._prompt_sub(idx)
            return
        self.selected = None if self.selected == idx else idx
        self.refresh()

    def on_sub_mode(self, sender):
        self.sub_mode = not self.sub_mode
        self.selected = None
        self.refresh()

    def on_serve(self, sender):
        self._mutate(self.m.toggle_serve)

    def on_undo(self, sender):
        if not self.undo_stack:
            console.hud_alert('Nothing to undo', 'error', 0.8)
            return
        self.m = Match.from_dict(json.loads(self.undo_stack.pop()))
        self.selected = None
        self.save()
        self.refresh()
        console.hud_alert('Undone', 'success', 0.6)

    @log.bg
    def _prompt_sub(self, idx):
        try:
            n = console.input_alert('Substitution', 'In for #%s (P%d)' % (self.m.rot[idx], idx + 1), '', 'Sub')
        except KeyboardInterrupt:
            return
        n = n.strip().lstrip('#')
        if n:
            self._apply(lambda: self.m.sub(idx, n))

    @log.bg
    def on_lineup(self, sender):
        try:
            txt = console.input_alert('Lineup', 'Six numbers in serve order (P1 first)',
                                      ', '.join(self.m.rot), 'Set')
        except KeyboardInterrupt:
            return
        players = [x.strip().lstrip('#') for x in txt.replace(',', ' ').split() if x.strip()]
        if len(players) != 6:
            console.hud_alert('Need exactly 6', 'error')
            return
        self._apply(lambda: self.m.set_lineup(players))

    @log.bg
    def new_match(self, sender):
        m = self.m
        fields = [
            {'type': 'text', 'key': 'home', 'title': 'Home', 'value': m.home},
            {'type': 'text', 'key': 'away', 'title': 'Away', 'value': 'Away'},
            {'type': 'text', 'key': 'lineup', 'title': 'Lineup (P1..P6)', 'value': ', '.join(m.lineup)},
            {'type': 'switch', 'key': 'bo3', 'title': 'Best of 3', 'value': m.best_of == 3},
            {'type': 'switch', 'key': 'home_serves', 'title': 'Home serves first', 'value': True},
        ]
        r = dialogs.form_dialog(title='New match', fields=fields)
        if not r:
            return
        players = [x.strip().lstrip('#') for x in r['lineup'].replace(',', ' ').split() if x.strip()]
        if len(players) != 6:
            console.hud_alert('Lineup needs 6 players', 'error')
            return
        self._start(Match(r['home'] or 'Home', r['away'] or 'Away', players,
                          3 if r['bo3'] else 5, bool(r['home_serves'])))

    @on_main_thread
    def _start(self, match):
        if self.m.started():
            storage.save(APP, 'match_%d.json' % int(time.time()), self.m.to_dict())
        self.m = match
        self.undo_stack = []
        self.selected = None
        self.sub_mode = False
        self.save()
        self.refresh()

    def show_stats(self, sender):
        v = ui.View(background_color=theme.BG)
        v.name = 'Stats'
        tv = ui.TextView(frame=v.bounds, flex='WH')
        tv.editable = False
        tv.font = (theme.MONO, 13)
        tv.text_color = theme.TEXT
        tv.background_color = theme.BG
        tv.text = self.m.summary()
        v.add_subview(tv)

        def copy(s):
            clipboard.set(tv.text)
            console.hud_alert('Copied')
        v.right_button_items = [ui.ButtonItem(title='Copy', action=copy)]
        nav = self.navigation_view
        if nav is not None:
            nav.push_view(v)
        else:
            v.present('sheet')

    def stop(self):
        self.save()


def make_view():
    return VolleyballView()
