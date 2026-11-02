"""Volleyball in-game tracker.

Rally scoring to 25 (deciding set to 15), win by 2, best of 5 or 3.
Serve and home rotation: rot[0] is position 1 (right back / server). A team
that wins a rally on the opponent's serve gains serve; if that team is home,
the home lineup rotates clockwise (rot shifts left by one).

Teams: rosters (jersey number, name, position) live in data/volleyball/
teams.json and are edited under Team. A match takes a snapshot of the chosen
roster, so court cells, the bench strip, stats and the log show names.

Subs: the bench strip lists every roster player not on court. Each sub pairs
the two players for the rest of the set (they may only swap with each other),
and the court and bench both show that pairing. Pairing and the sub limit are
checked and warned about, never hard-blocked, since leagues differ.

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
DEFAULT_SUB_LIMIT = 6


def other(team):
    return 'away' if team == 'home' else 'home'


def _num_key(p):
    return (0, int(p), '') if p.isdigit() else (1, 0, p)


def _clean_num(s):
    return (s or '').strip().lstrip('#').strip()


# ---- teams / rosters ----
class TeamStore(object):
    """data/volleyball/teams.json: {'teams': [{'id', 'name', 'players': [{'num', 'name', 'pos'}]}]}"""

    def __init__(self):
        d = storage.load(APP, 'teams.json') or {}
        self.teams = d.get('teams', [])

    def save(self):
        storage.save(APP, 'teams.json', {'teams': self.teams})

    def get(self, tid):
        for t in self.teams:
            if t['id'] == tid:
                return t
        return None

    def add(self, name):
        t = {'id': str(int(time.time() * 1000)), 'name': name, 'players': []}
        self.teams.append(t)
        return t


def roster_of(team):
    """Team dict -> match roster snapshot {num: {'name', 'pos'}}."""
    return dict((p['num'], {'name': p.get('name', ''), 'pos': p.get('pos', '')}) for p in team['players'])


class Match(object):
    def __init__(self, home='Home', away='Away', lineup=None, best_of=5, home_serves=True,
                 roster=None, team_id=None, sub_limit=DEFAULT_SUB_LIMIT):
        self.home = home
        self.away = away
        self.best_of = best_of
        self.lineup = [str(p) for p in (lineup or ['1', '2', '3', '4', '5', '6'])]
        self.rot = list(self.lineup)
        self.set_start = list(self.lineup)  # who started the current set (to tell subs from starters)
        self.score = {'home': 0, 'away': 0}
        self.sets = []
        self.set_no = 1
        self.first_serve = 'home' if home_serves else 'away'
        self.serving = self.first_serve
        self.timeouts = {'home': 0, 'away': 0}
        self.subs = {'home': 0, 'away': 0}
        self.sub_limit = sub_limit  # 0 = no limit
        self.partner = {}  # this set: num -> the only num it may swap with
        self.sub_log = []  # [set_no, home, away, in, out]
        self.roster = roster or {}
        self.team_id = team_id
        self.stats = {}
        self.log = []
        self.finished = False

    def to_dict(self):
        return json.loads(json.dumps(self.__dict__))

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.__dict__.update(d)
        if 'set_start' not in d:  # saves from before rosters: treat the current six as starters
            m.set_start = list(m.rot)
        return m

    def name(self, team):
        return self.home if team == 'home' else self.away

    # ---- players ----
    def pname(self, num):
        """'#7 Jane Doe' or '#7' when not on the roster."""
        r = self.roster.get(num)
        return '#%s %s' % (num, r['name']) if r and r.get('name') else '#' + num

    def first(self, num):
        r = self.roster.get(num)
        return (r.get('name') or '').split(' ')[0] if r else ''

    def pos(self, num):
        r = self.roster.get(num)
        return r.get('pos', '') if r else ''

    def bench(self):
        off = set(self.roster) | set(self.partner)
        return sorted((p for p in off if p not in self.rot), key=_num_key)

    def entered_as_sub(self, num):
        return num in self.rot and num not in self.set_start

    def sub_problems(self, idx, player):
        """Reasons this sub breaks the usual rules ([] = clean)."""
        out = self.rot[idx]
        probs = []
        if player in self.rot:
            probs.append('#%s is already on court' % player)
        p_out, p_in = self.partner.get(out), self.partner.get(player)
        if p_out is not None and p_out != player:
            probs.append('#%s can only be replaced by #%s' % (out, p_out))
        if p_in is not None and p_in != out:
            probs.append('#%s can only come back in for #%s' % (player, p_in))
        if self.sub_limit and self.subs['home'] >= self.sub_limit:
            probs.append('sub limit (%d) reached' % self.sub_limit)
        return probs

    # ---- game ----
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
        note = '%s %s' % (self.pname(player), kind)
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
        if old not in self.partner and player not in self.partner:
            self.partner[old] = player
            self.partner[player] = old
        self.sub_log.append([self.set_no, self.score['home'], self.score['away'], player, old])
        self._log('SUB IN %s / OUT %s (P%d)' % (self.pname(player), self.pname(old), idx + 1))
        return 'IN %s\nOUT %s' % (self.pname(player), self.pname(old))

    def set_lineup(self, players):
        self.lineup = list(players)
        self.rot = list(players)
        if self.score['home'] == 0 and self.score['away'] == 0:
            self.set_start = list(players)
            self.partner = {}
        self._log('Lineup ' + ' '.join('#' + p for p in players))

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
        self.partner = {}
        self.set_start = list(self.rot)
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
        out.append('ON COURT  ' + '  '.join('P%d %s' % (i + 1, self.pname(p)) for i, p in enumerate(self.rot)))
        out.append('')
        out.append('%-16s' % 'Player' + ''.join('%4s' % STAT_ABBR[k] for k in STATS) + '%5s' % 'Pts')
        tot = dict((k, 0) for k in STATS)
        for p in sorted(set(self.stats) | set(self.roster), key=_num_key):
            s = self.stats.get(p, {})
            pts = sum(s.get(k, 0) for k in POINT_FOR_HOME)
            out.append('%-16s' % self.pname(p)[:15] + ''.join('%4d' % s.get(k, 0) for k in STATS) + '%5d' % pts)
            for k in STATS:
                tot[k] += s.get(k, 0)
        out.append('%-16s' % 'Team' + ''.join('%4d' % tot[k] for k in STATS)
                   + '%5d' % sum(tot[k] for k in POINT_FOR_HOME))
        out.append('')
        out.append('Substitutions:')
        if not self.sub_log:
            out.append('  none')
        for set_no, h, a, pin, pout in self.sub_log:
            out.append('  S%d %2d-%-2d  IN %-14s OUT %s' % (set_no, h, a, self.pname(pin), self.pname(pout)))
        out.append('')
        out.append('Log (newest first):')
        for set_no, h, a, text in reversed(self.log[-80:]):
            out.append('S%d %2d-%-2d %s' % (set_no, h, a, text))
        return '\n'.join(out)


def _sub_label(parent, size, color, bold=False, align=ui.ALIGN_CENTER):
    lb = theme.label('', size, color, bold=bold, align=align)
    lb.touch_enabled = False
    parent.add_subview(lb)
    return lb


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
        self.sub_out = None  # court index picked to go OUT while in sub mode
        self.right_button_items = [ui.ButtonItem(title='Stats', action=self.show_stats),
                                   ui.ButtonItem(title='New', action=self.new_match),
                                   ui.ButtonItem(title='Team', action=self.show_teams)]
        self.score_btn, self.team_lbl, self.serve_lbl, self.to_btn = {}, {}, {}, {}
        for t in TEAMS:
            fg = theme.CYAN if t == 'home' else theme.AMBER
            b = theme.button('0', lambda s, t=t: self.on_point(t), fg=fg, size=84)
            self.team_lbl[t] = _sub_label(b, 15, theme.MUTED, bold=True)
            self.serve_lbl[t] = _sub_label(b, 13, theme.AMBER, bold=True)
            self.score_btn[t] = b
            self.add_subview(b)
            self.to_btn[t] = theme.button('TO', lambda s, t=t: self.on_timeout(t), size=15)
            self.add_subview(self.to_btn[t])
        self.info = theme.label('', 14, theme.TEXT)
        self.sub_btn = theme.button('Sub', self.on_sub_mode, size=15)
        self.net_lbl = theme.label('NET', 10, theme.MUTED, bold=True)
        for v in (self.info, self.sub_btn, self.net_lbl):
            self.add_subview(v)
        self.player_btn, self.pos_lbl, self.num_lbl, self.name_lbl, self.badge_lbl = {}, {}, {}, {}, {}
        for idx in range(6):
            b = theme.button('', lambda s, i=idx: self.on_player(i), size=24)
            self.pos_lbl[idx] = _sub_label(b, 10, theme.MUTED, align=ui.ALIGN_LEFT)
            self.badge_lbl[idx] = _sub_label(b, 10, theme.AMBER, bold=True, align=ui.ALIGN_RIGHT)
            self.num_lbl[idx] = _sub_label(b, 24, theme.TEXT, bold=True)
            self.name_lbl[idx] = _sub_label(b, 11, theme.MUTED)
            self.player_btn[idx] = b
            self.add_subview(b)
        self.bench_hdr = theme.label('BENCH', 10, theme.MUTED, bold=True, align=ui.ALIGN_LEFT)
        self.bench = ui.ScrollView()
        self.bench.shows_horizontal_scroll_indicator = False
        self.bench.background_color = theme.BG
        self.bench_chips = []
        for v in (self.bench_hdr, self.bench):
            self.add_subview(v)
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
    CHIP_W, CHIP_H = 84, 48

    def layout(self):
        W, H, p = self.width, self.height, 8
        info_h, row_h, cell_h, net_h, hdr_h, stat_h, bot_h = 22, 40, 58, 14, 14, 48, 44
        bench_h = self.CHIP_H
        fixed = p * 9 + info_h + row_h + net_h + 2 * cell_h + 6 + hdr_h + bench_h + stat_h + bot_h
        score_h = max(100, min(280, H - fixed))
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
            self.pos_lbl[idx].frame = (6, 3, 40, 12)
            self.badge_lbl[idx].frame = (third - 66, 3, 60, 12)
            self.num_lbl[idx].frame = (0, 12, third, 26)
            self.name_lbl[idx].frame = (4, 38, third - 8, 15)
        y += 2 * cell_h + 6 + p
        self.bench_hdr.frame = (p, y, W - 2 * p, hdr_h)
        y += hdr_h
        self.bench.frame = (0, y, W, bench_h)
        self._layout_chips()
        y += bench_h + p
        sw = (W - (len(STATS) + 1) * p) / float(len(STATS))
        for i, k in enumerate(STATS):
            self.stat_btn[k].frame = (p + i * (sw + p), y, sw, stat_h)
        y += stat_h + p
        bw = (W - (len(self.bottom) + 1) * p) / float(len(self.bottom))
        for i, b in enumerate(self.bottom):
            b.frame = (p + i * (bw + p), y, bw, bot_h)

    def _layout_chips(self):
        p = 8
        x = p
        for b in self.bench_chips:
            b.frame = (x, 0, self.CHIP_W, self.CHIP_H)
            x += self.CHIP_W + p
        self.bench.content_size = (x, self.CHIP_H)

    def _chip(self, key):
        b = theme.button('', lambda s, k=key: self.on_bench(k), size=15)
        b.chip_top = _sub_label(b, 15, theme.TEXT, bold=True)
        b.chip_bot = _sub_label(b, 10, theme.MUTED)
        b.chip_top.frame = (2, 5, self.CHIP_W - 4, 20)
        b.chip_bot.frame = (2, 26, self.CHIP_W - 4, 16)
        return b

    def _rebuild_bench(self):
        m = self.m
        keys = m.bench() + ['+']
        if [getattr(b, 'chip_key', None) for b in self.bench_chips] != keys:
            for b in self.bench_chips:
                self.bench.remove_subview(b)
            self.bench_chips = []
            for k in keys:
                b = self._chip(k)
                b.chip_key = k
                self.bench.add_subview(b)
                self.bench_chips.append(b)
            self._layout_chips()
        picking = self.sub_mode and self.sub_out is not None
        for b in self.bench_chips:
            k = b.chip_key
            if k == '+':
                b.chip_top.text = '+ #'
                b.chip_bot.text = 'other player'
                ok = True
            else:
                first = m.first(k)
                b.chip_top.text = ('#%s %s' % (k, first))[:11] if first else '#' + k
                partner = m.partner.get(k)
                pos = m.pos(k)
                if partner is not None:
                    b.chip_bot.text = 'only for #%s' % partner
                else:
                    b.chip_bot.text = pos or 'ready'
                ok = not picking or not m.sub_problems(self.sub_out, k)
            if picking:
                b.background_color = theme.GREEN if ok else theme.PANEL
                b.chip_top.text_color = theme.BG if ok else theme.MUTED
                b.chip_bot.text_color = theme.BG if ok else theme.MUTED
                b.alpha = 1.0 if ok else 0.5
            else:
                b.background_color = theme.PANEL
                b.chip_top.text_color = theme.TEXT
                b.chip_bot.text_color = theme.AMBER if m.partner.get(k) else theme.MUTED
                b.alpha = 1.0
        self.bench_hdr.text = ('BENCH: tap who comes IN for %s' % m.pname(m.rot[self.sub_out])) if picking else (
            'BENCH' if m.roster else 'BENCH  (no roster: Team to add players, or + # to sub by number)')
        self.bench_hdr.text_color = theme.GREEN if picking else theme.MUTED

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
        limit = ('/%d' % m.sub_limit) if m.sub_limit else ''
        if self.sub_mode:
            self.sub_btn.title = 'Pick IN ↓' if self.sub_out is not None else 'Tap OUT ↓'
        else:
            self.sub_btn.title = 'Sub %d%s' % (m.subs['home'], limit)
        self.sub_btn.background_color = theme.AMBER if self.sub_mode else theme.PANEL
        self.sub_btn.tint_color = theme.BG if self.sub_mode else (
            theme.RED if m.sub_limit and m.subs['home'] >= m.sub_limit else theme.TEXT)
        for idx, b in self.player_btn.items():
            num = m.rot[idx]
            sel = idx == self.selected
            out = self.sub_mode and idx == self.sub_out
            bg = theme.CYAN if sel else (theme.RED if out else theme.PANEL)
            b.background_color = bg
            on_color = theme.BG if (sel or out) else theme.TEXT
            self.num_lbl[idx].text = '#' + num
            self.num_lbl[idx].text_color = on_color
            name = m.first(num)
            pos = m.pos(num)
            self.name_lbl[idx].text = ' '.join(x for x in (name, pos and '· ' + pos) if x)
            self.name_lbl[idx].text_color = theme.BG if (sel or out) else theme.MUTED
            self.pos_lbl[idx].text = 'P%d' % (idx + 1) + ('  OUT' if out else '')
            self.pos_lbl[idx].text_color = theme.BG if (sel or out) else theme.MUTED
            partner = m.partner.get(num)
            if partner is not None and m.entered_as_sub(num):
                self.badge_lbl[idx].text = 'SUB ⇄#%s' % partner
            elif partner is not None:
                self.badge_lbl[idx].text = '⇄#%s' % partner
            else:
                self.badge_lbl[idx].text = ''
            self.badge_lbl[idx].text_color = theme.BG if (sel or out) else theme.AMBER
            if self.sub_mode and self.sub_out is None:
                b.border_color = theme.AMBER
                b.border_width = 2
            else:
                b.border_color = theme.AMBER
                b.border_width = 2 if (idx == 0 and m.serving == 'home') else 0
        for b in self.stat_btn.values():
            b.alpha = 1.0 if self.selected is not None else 0.45
        self._rebuild_bench()

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
            self.sub_out = None if self.sub_out == idx else idx
            self.refresh()
            return
        self.selected = None if self.selected == idx else idx
        self.refresh()

    def on_sub_mode(self, sender):
        self.sub_mode = not self.sub_mode
        self.sub_out = None
        self.selected = None
        self.refresh()

    def on_bench(self, key):
        m = self.m
        if not (self.sub_mode and self.sub_out is not None):
            if key == '+':
                console.hud_alert('Tap Sub, then the player going OUT', 'error', 1.2)
            else:
                partner = m.partner.get(key)
                msg = m.pname(key) + ('  · can only return for #%s' % partner if partner else '  · on bench')
                console.hud_alert(msg, 'success', 1.4)
            return
        idx = self.sub_out
        if key == '+':
            self._prompt_sub(idx)
            return
        self._try_sub(idx, key)

    def _try_sub(self, idx, player):
        probs = self.m.sub_problems(idx, player)
        if probs:
            self._confirm_sub(idx, player, probs)
        else:
            self._do_sub(idx, player)

    @on_main_thread
    def _do_sub(self, idx, player):
        self.sub_mode = False
        self.sub_out = None
        haptics.tap(1)
        self._mutate(lambda: self.m.sub(idx, player))

    @log.bg
    def _confirm_sub(self, idx, player, probs):
        try:
            console.alert('Sub anyway?', '\n'.join(probs), 'Sub anyway')
        except KeyboardInterrupt:
            return
        self._do_sub(idx, player)

    def on_serve(self, sender):
        self._mutate(self.m.toggle_serve)

    def on_undo(self, sender):
        if not self.undo_stack:
            console.hud_alert('Nothing to undo', 'error', 0.8)
            return
        self.m = Match.from_dict(json.loads(self.undo_stack.pop()))
        self.selected = None
        self.sub_mode = False
        self.sub_out = None
        self.save()
        self.refresh()
        console.hud_alert('Undone', 'success', 0.6)

    @log.bg
    def _prompt_sub(self, idx):
        try:
            n = console.input_alert('Substitution', 'Number coming IN for %s (P%d)'
                                    % (self.m.pname(self.m.rot[idx]), idx + 1), '', 'Sub')
        except KeyboardInterrupt:
            return
        n = _clean_num(n)
        if n:
            self._try_sub(idx, n)

    @log.bg
    def on_lineup(self, sender):
        if len(self.m.roster) >= 6:
            self._pick_lineup(self.m.roster, list(self.m.rot),
                              lambda players: self._apply(lambda: self.m.set_lineup(players)))
            return
        try:
            txt = console.input_alert('Lineup', 'Six numbers in serve order (P1 first)',
                                      ', '.join(self.m.rot), 'Set')
        except KeyboardInterrupt:
            return
        players = [_clean_num(x) for x in txt.replace(',', ' ').split() if x.strip()]
        if len(players) != 6 or len(set(players)) != 6:
            console.hud_alert('Need 6 different numbers', 'error')
            return
        self._apply(lambda: self.m.set_lineup(players))

    @on_main_thread
    def _pick_lineup(self, roster, current, done):
        v = LineupPicker(roster, current, done)
        self.navigation_view.push_view(v)

    @log.bg
    def new_match(self, sender):
        m = self.m
        store = TeamStore()
        team = None
        if store.teams:
            names = [t['name'] for t in store.teams] + ['No roster (numbers only)']
            choice = dialogs.list_dialog('Home team', names)
            if choice is None:
                return
            idx = names.index(choice)
            team = store.teams[idx] if idx < len(store.teams) else None
        roster = roster_of(team) if team else {}
        fields = []
        if team is None:
            fields.append({'type': 'text', 'key': 'home', 'title': 'Home', 'value': m.home})
        fields.append({'type': 'text', 'key': 'away', 'title': 'Away', 'value': 'Away'})
        if len(roster) < 6:
            fields.append({'type': 'text', 'key': 'lineup', 'title': 'Lineup (P1..P6)',
                           'value': ', '.join(m.lineup)})
        fields += [
            {'type': 'number', 'key': 'sub_limit', 'title': 'Subs per set (0 = no limit)',
             'value': str(m.sub_limit if m.sub_limit is not None else DEFAULT_SUB_LIMIT)},
            {'type': 'switch', 'key': 'bo3', 'title': 'Best of 3', 'value': m.best_of == 3},
            {'type': 'switch', 'key': 'home_serves', 'title': 'Home serves first', 'value': True},
        ]
        r = dialogs.form_dialog(title='New match' + (': ' + team['name'] if team else ''), fields=fields)
        if not r:
            return
        try:
            limit = max(0, int(r['sub_limit'] or 0))
        except ValueError:
            limit = DEFAULT_SUB_LIMIT
        home = team['name'] if team else (r['home'] or 'Home')

        def make(players):
            return Match(home, r['away'] or 'Away', players, 3 if r['bo3'] else 5, bool(r['home_serves']),
                         roster=roster, team_id=team['id'] if team else None, sub_limit=limit)
        if len(roster) >= 6:
            self._pick_lineup(roster, [], lambda players: self._start(make(players)))
            return
        players = [_clean_num(x) for x in r['lineup'].replace(',', ' ').split() if x.strip()]
        if len(players) != 6 or len(set(players)) != 6:
            console.hud_alert('Lineup needs 6 different numbers', 'error')
            return
        self._start(make(players))

    @on_main_thread
    def _start(self, match):
        if self.m.started():
            storage.save(APP, 'match_%d.json' % int(time.time()), self.m.to_dict())
        self.m = match
        self.undo_stack = []
        self.selected = None
        self.sub_mode = False
        self.sub_out = None
        self.save()
        self.refresh()

    def show_teams(self, sender):
        self.navigation_view.push_view(TeamsView(self))

    def roster_changed(self, team):
        """Called by the roster editor: keep the live match's snapshot in sync with its team."""
        if self.m.team_id == team['id']:
            self.m.roster = roster_of(team)
            self.m.home = team['name']
            self.save()
            self.refresh()

    def show_stats(self, sender):
        v = ui.View(background_color=theme.BG)
        v.name = 'Stats'
        tv = ui.TextView(frame=v.bounds, flex='WH')
        tv.editable = False
        tv.font = (theme.MONO, 12)
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


@log.instrument
class LineupPicker(ui.View):
    """Tap roster players in serve order (P1 first). Done when six are picked."""

    def __init__(self, roster, current, done):
        super().__init__()
        self.name = 'Lineup'
        self.background_color = theme.BG
        self.roster = roster
        self.nums = sorted(roster, key=_num_key)
        self.picked = [p for p in current if p in roster][:6]
        self.done = done
        self.head = theme.label('', 14, theme.TEXT, align=ui.ALIGN_LEFT, lines=0)
        self.tv = theme.table(self, 52)
        self.add_subview(self.head)
        self.add_subview(self.tv)
        self.done_btn = ui.ButtonItem(title='Done', action=self.on_done)
        self.right_button_items = [self.done_btn, ui.ButtonItem(title='Clear', action=self.on_clear)]
        self.refresh()

    def layout(self):
        p = 8
        self.head.frame = (p + 4, p, self.width - 2 * p - 8, 64)
        self.tv.frame = (0, 80, self.width, self.height - 80)

    def refresh(self):
        n = len(self.picked)
        order = '  '.join('P%d #%s' % (i + 1, x) for i, x in enumerate(self.picked)) or '-'
        self.head.text = ('Tap players in serve order (P1 = server).\n%s\n%s' %
                          (order, 'Ready: tap Done' if n == 6 else 'Next: P%d' % (n + 1)))
        self.done_btn.enabled = n == 6
        self.tv.reload()

    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.nums)

    def tableview_cell_for_row(self, tv, section, row):
        num = self.nums[row]
        r = self.roster[num]
        title = '#%s  %s' % (num, r.get('name') or '')
        cell = theme.cell(title, r.get('pos') or '', style='value1')
        cell.text_label.font = (theme.BOLD, 17)
        if num in self.picked:
            cell.detail_text_label.text = 'P%d' % (self.picked.index(num) + 1)
            cell.detail_text_label.text_color = theme.BG
            cell.text_label.text_color = theme.BG
            cell.background_color = theme.CYAN
        return cell

    def tableview_did_select(self, tv, section, row):
        num = self.nums[row]
        if num in self.picked:
            self.picked.remove(num)
        elif len(self.picked) < 6:
            self.picked.append(num)
        else:
            console.hud_alert('Already 6: tap one to remove', 'error', 1)
        self.refresh()

    def on_clear(self, sender):
        self.picked = []
        self.refresh()

    def on_done(self, sender):
        if len(self.picked) != 6:
            return
        players = list(self.picked)
        self.navigation_view.pop_view()
        self.done(players)


@log.instrument
class TeamsView(ui.View):
    def __init__(self, game):
        super().__init__()
        self.name = 'Teams'
        self.background_color = theme.BG
        self.game = game
        self.store = TeamStore()
        self.tv = theme.table(self, 60)
        self.tv.frame = self.bounds
        self.tv.flex = 'WH'
        self.add_subview(self.tv)
        self.right_button_items = [ui.ButtonItem(title='Add', action=self.on_add)]

    def reload(self):
        self.store = TeamStore()
        self.tv.reload()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self.store.save()
        self.tv.reload()

    @log.bg
    def on_add(self, sender):
        try:
            name = console.input_alert('New team', 'Team name', '', 'Add').strip()
        except KeyboardInterrupt:
            return
        if name:
            self._add(name)

    @on_main_thread
    def _add(self, name):
        t = self.store.add(name)
        self.store.save()
        self.tv.reload()
        self.navigation_view.push_view(RosterView(self, t))

    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.store.teams) or 1

    def tableview_cell_for_row(self, tv, section, row):
        if not self.store.teams:
            return theme.cell('No teams yet', 'Tap Add to create one', fg=theme.MUTED)
        t = self.store.teams[row]
        live = ' · in current match' if self.game.m.team_id == t['id'] else ''
        cell = theme.cell(t['name'], '%d players%s' % (len(t['players']), live))
        cell.text_label.font = (theme.BOLD, 17)
        cell.accessory_type = 'disclosure_indicator'
        return cell

    def tableview_did_select(self, tv, section, row):
        if self.store.teams:
            self.navigation_view.push_view(RosterView(self, self.store.teams[row]))

    def tableview_can_delete(self, tv, section, row):
        return bool(self.store.teams)

    def tableview_delete(self, tv, section, row):
        t = self.store.teams.pop(row)
        storage.save(APP, 'deleted_team_%s_%d.json' % (t['id'], int(time.time())), t)  # recoverable
        self.store.save()
        self.tv.reload()


@log.instrument
class RosterView(ui.View):
    def __init__(self, parent, team):
        super().__init__()
        self.parent_view = parent
        self.team = team
        self.name = team['name']
        self.background_color = theme.BG
        self.tv = theme.table(self, 56)
        self.tv.frame = self.bounds
        self.tv.flex = 'WH'
        self.add_subview(self.tv)
        self.right_button_items = [ui.ButtonItem(title='Add', action=self.on_add),
                                   ui.ButtonItem(title='Rename', action=self.on_rename)]

    @property
    def players(self):
        return sorted(self.team['players'], key=lambda p: _num_key(p['num']))

    def _changed(self):
        self.parent_view.store.save()
        self.parent_view.tv.reload()
        self.parent_view.game.roster_changed(self.team)
        self.tv.reload()

    @on_main_thread
    def _apply(self, fn):
        fn()
        self._changed()

    def _form(self, p=None):
        p = p or {}
        r = dialogs.form_dialog(title='Edit player' if p else 'New player', fields=[
            {'type': 'number', 'key': 'num', 'title': 'Jersey #', 'value': p.get('num', '')},
            {'type': 'text', 'key': 'name', 'title': 'Name', 'value': p.get('name', ''),
             'autocapitalization': ui.AUTOCAPITALIZE_WORDS},
            {'type': 'text', 'key': 'pos', 'title': 'Position (S, OH, MB, OPP, L, DS)',
             'value': p.get('pos', ''), 'autocapitalization': ui.AUTOCAPITALIZE_ALL},
        ])
        if not r:
            return None
        num = _clean_num(r['num'])
        if not num:
            console.hud_alert('Jersey number required', 'error')
            return None
        if any(q['num'] == num and q is not p for q in self.team['players']):
            console.hud_alert('#%s is already on this team' % num, 'error')
            return None
        return {'num': num, 'name': r['name'].strip(), 'pos': r['pos'].strip().upper()}

    @log.bg
    def on_add(self, sender):
        new = self._form()
        if new:
            self._apply(lambda: self.team['players'].append(new))

    @log.bg
    def _edit(self, p):
        new = self._form(p)
        if new:
            self._apply(lambda: p.update(new))

    @log.bg
    def on_rename(self, sender):
        try:
            name = console.input_alert('Rename team', '', self.team['name'], 'Save').strip()
        except KeyboardInterrupt:
            return
        if name:
            def f():
                self.team['name'] = name
                self.name = name
            self._apply(f)

    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.team['players']) or 1

    def tableview_cell_for_row(self, tv, section, row):
        if not self.team['players']:
            return theme.cell('No players yet', 'Tap Add: jersey number, name, position', fg=theme.MUTED)
        p = self.players[row]
        cell = theme.cell('#%-3s %s' % (p['num'], p.get('name') or ''), p.get('pos') or '', style='value1')
        cell.text_label.font = (theme.BOLD, 18)
        return cell

    def tableview_did_select(self, tv, section, row):
        if self.team['players']:
            self._edit(self.players[row])

    def tableview_can_delete(self, tv, section, row):
        return bool(self.team['players'])

    def tableview_delete(self, tv, section, row):
        p = self.players[row]
        self.team['players'] = [q for q in self.team['players'] if q is not p]
        self._changed()


def make_view():
    return VolleyballView()
