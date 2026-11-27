"""Downloader: a yt-dlp front end.

Paste a link (Paste button, or paste into the field) and the options appear
once yt-dlp has read the page: title/thumbnail, Video or Audio, a format
picker filled from what the site actually offers, file name, and metadata
(title, artist, album, year, genre, track, comment, cover art) pre-filled
from the page. Download, then the iOS "Save to…" sheet asks where it goes.

No ffmpeg on iOS, so nothing can be merged or converted:
- Video lists formats that already carry sound first; video-only streams are
  listed last and marked "no audio".
- Audio lists audio-only streams as the site serves them (m4a preferred).
- Metadata is written with mutagen (pure Python) into m4a/mp4/mov, mp3,
  ogg/opus and flac. WebM can't be tagged; the picker says so.
- yt-dlp's ffmpeg-based fixups are turned off.

yt-dlp and mutagen are installed on first use from PyPI (ptlib/pydeps.py):
the newest wheel this Python accepts, SHA-256 checked. On Pythonista's
Python 3.10 that is yt-dlp 2026.10.21 (later releases need 3.11). Update
them from the "yt-dlp" button.

Single videos only (playlists: the first entry). Some sites, YouTube
especially, need a JavaScript runtime for full format lists; iOS has none, so
those may offer fewer formats or fail with the error yt-dlp reports.
"""
import os
import re
import shutil
import threading
import time
import urllib.request

import ui
import console
import dialogs
import clipboard
from objc_util import on_main_thread

from ptlib import theme, storage, log, pydeps, mediatags

_log = log.get('ytdl')
TITLE = 'Downloader'
APP = 'ytdl'
URL_RE = re.compile(r'https?://\S+', re.I)


# ---- pure helpers (testable without yt-dlp) ----
def _size(f, duration):
    s = f.get('filesize') or f.get('filesize_approx')
    if not s and f.get('tbr') and duration:
        s = f['tbr'] * 1000 / 8.0 * duration
    return s


def human_size(n):
    if not n:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%.0f %s' if unit in ('B', 'KB') else '%.1f %s') % (n, unit)
        n /= 1024.0


def human_time(s):
    if not s:
        return ''
    s = int(s)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


def _codec(c):
    return None if c in (None, 'none') else c.split('.')[0]


def classify(f):
    """'muxed' (video+audio), 'video' (video only), 'audio' (audio only), or None (skip)."""
    v, a = f.get('vcodec'), f.get('acodec')
    if f.get('format_note') == 'storyboard' or (f.get('ext') in ('mhtml',)):
        return None
    if v == 'none' and a == 'none':
        return None
    if v == 'none':
        return 'audio'
    if a == 'none':
        return 'video'
    if v is None and a is None and f.get('ext') in ('m4a', 'mp3', 'opus', 'ogg', 'aac', 'flac', 'wav'):
        return 'audio'
    return 'muxed'  # both present, or unknown (single-file sites usually serve muxed)


def split_formats(info):
    """info dict -> (video options, audio options), best first. Each option is a dict with
    id, label, ext, kind, size, taggable, hls."""
    dur = info.get('duration')
    fmts = info.get('formats') or [dict(info, format_id=info.get('format_id') or 'best')]
    video, audio = [], []
    for f in fmts:
        kind = classify(f)
        if kind is None or not f.get('format_id'):
            continue
        ext = f.get('ext') or '?'
        hls = 'm3u8' in (f.get('protocol') or '')
        size = _size(f, dur)
        o = {'id': str(f['format_id']), 'ext': ext, 'kind': kind, 'size': size, 'hls': hls,
             'taggable': mediatags.supported(ext) and not hls, 'height': f.get('height') or 0,
             'abr': f.get('abr') or f.get('tbr') or 0, 'tbr': f.get('tbr') or 0}
        if kind == 'audio':
            bits = [ext, _codec(f.get('acodec')), '%d kbps' % o['abr'] if o['abr'] else None]
        else:
            res = '%dp' % o['height'] if o['height'] else (f.get('format_note') or f.get('resolution') or 'video')
            fps = f.get('fps')
            bits = [res + ('%d' % fps if fps and fps > 30 else ''), ext,
                    '/'.join(x for x in (_codec(f.get('vcodec')), _codec(f.get('acodec'))) if x) or None]
        if size:
            bits.append(human_size(size))
        if hls:
            bits.append('HLS')
        if kind == 'video':
            bits.append('⚠ no audio')
        o['label'] = ' · '.join(b for b in bits if b)
        (audio if kind == 'audio' else video).append(o)
    video.sort(key=lambda o: (o['kind'] != 'muxed', o['hls'], -o['height'], -o['tbr']))
    audio.sort(key=lambda o: (o['hls'], -o['abr'], o['ext'] != 'm4a'))
    return video, audio


def default_choice(options, mode):
    if not options:
        return None
    if mode == 'audio':
        for o in options:
            if o['ext'] == 'm4a' and not o['hls']:
                return o
        return options[0]
    muxed = [o for o in options if o['kind'] == 'muxed']
    for o in muxed:
        if o['ext'] == 'mp4' and not o['hls']:
            return o
    return (muxed or options)[0]


def default_meta(info):
    date = info.get('release_year') or (info.get('release_date') or info.get('upload_date') or '')[:4]
    cats = info.get('categories') or []
    return {
        'title': info.get('track') or info.get('title') or '',
        'artist': info.get('artist') or info.get('creator') or info.get('uploader') or info.get('channel') or '',
        'album': info.get('album') or '',
        'date': str(date or ''),
        'genre': info.get('genre') or (cats[0] if cats else ''),
        'track': str(info.get('track_number') or ''),
        'comment': info.get('webpage_url') or info.get('original_url') or '',
    }


def safe_name(s, limit=120):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', s or '').strip().strip('.')
    s = re.sub(r'\s+', ' ', s)
    return (s[:limit].rstrip() or 'download')


def best_thumb(info):
    """Prefer a large JPEG/PNG thumbnail (taggable as cover art without conversion)."""
    thumbs = [t for t in (info.get('thumbnails') or []) if t.get('url')]
    if not thumbs:
        return info.get('thumbnail')

    def score(t):
        u = t['url'].split('?')[0].lower()
        jpg = u.endswith(('.jpg', '.jpeg', '.png'))
        return (jpg, (t.get('width') or 0) * (t.get('height') or 0), t.get('preference') or 0)
    return max(thumbs, key=score)['url']


# ---- yt-dlp glue ----
class _YLog(object):
    def debug(self, msg):
        if not msg.startswith('[debug] '):
            _log.debug(msg)

    def info(self, msg):
        _log.info(msg)

    def warning(self, msg):
        _log.warning('yt-dlp: %s', msg)

    def error(self, msg):
        _log.warning('yt-dlp error: %s', msg)  # surfaced to the user by the caller


class Cancelled(Exception):
    pass


def _dir(*parts):
    d = os.path.join(storage.BASE, APP, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def base_opts():
    return {'quiet': True, 'no_warnings': False, 'noplaylist': True, 'logger': _YLog(),
            'socket_timeout': 20, 'cachedir': _dir('cache'), 'fixup': 'never',
            'retries': 3, 'fragment_retries': 5, 'noprogress': True}


def _clean_error(e):
    msg = str(e)
    msg = re.sub(r'\x1b\[[0-9;]*m', '', msg)
    msg = re.split(r';? ?please report this issue|\(caused by |; please report|Confirm you are on the latest', msg)[0]
    return msg.replace('ERROR: ', '').strip().rstrip(';').strip()


# ---- UI ----
class _UrlDelegate(object):
    def __init__(self, owner):
        self.owner = owner

    def textfield_did_change(self, tf):
        self.owner.url_changed()

    def textfield_should_return(self, tf):
        tf.end_editing()
        self.owner.fetch(force=True)
        return True


@log.instrument
class DownloaderView(ui.View):
    def __init__(self):
        super().__init__()
        self.background_color = theme.BG
        self.info = None
        self.fetched_url = None
        self.video, self.audio = [], []
        self.choice = {'video': None, 'audio': None}
        self.thumb_bytes = None
        self.busy = None          # 'install' | 'fetch' | 'download'
        self.cancel = False
        self.last_file = None
        self.timer = None
        self.ytdlp_version = None
        self._last_progress = 0

        # url row (always visible)
        self.url = ui.TextField()
        self.url.placeholder = 'Paste a video or audio link'
        self.url.keyboard_type = ui.KEYBOARD_URL
        self.url.autocapitalization_type = ui.AUTOCAPITALIZE_NONE
        self.url.autocorrection_type = False
        self.url.clear_button_mode = 'while_editing'
        self.url.delegate = _UrlDelegate(self)
        self._style_field(self.url)
        self.paste_btn = theme.button('Paste', self.on_paste, size=15, bg=theme.PANEL_HI, fg=theme.AMBER)
        self.status = theme.label('', 13, theme.MUTED, align=ui.ALIGN_LEFT, lines=3)
        for v in (self.url, self.paste_btn, self.status):
            self.add_subview(v)

        # options (shown after a link is read)
        self.scroll = ui.ScrollView()
        self.scroll.background_color = theme.BG
        self.add_subview(self.scroll)
        sv = self.scroll
        self.thumb = ui.ImageView()
        self.thumb.content_mode = ui.CONTENT_SCALE_ASPECT_FILL
        self.thumb.background_color = theme.PANEL
        self.thumb.corner_radius = 8
        self.thumb.clips_to_bounds = True
        self.title_lbl = theme.label('', 15, theme.TEXT, bold=True, align=ui.ALIGN_LEFT, lines=2)
        self.sub_lbl = theme.label('', 12, theme.MUTED, align=ui.ALIGN_LEFT, lines=2)
        self.mode = ui.SegmentedControl()
        self.mode.segments = ['Video', 'Audio']
        self.mode.selected_index = storage.load(APP, 'prefs.json', {}).get('mode', 0)
        self.mode.tint_color = theme.AMBER
        self.mode.action = self.on_mode
        self.fmt_hdr = self._hdr('FORMAT')
        self.fmt_btn = theme.button('', self.on_format, size=14, bold=False)
        self.fmt_btn.alignment = ui.ALIGN_LEFT
        self.fmt_note = theme.label('', 12, theme.AMBER, align=ui.ALIGN_LEFT, lines=2)
        self.name_hdr = self._hdr('FILE NAME')
        self.name_field = ui.TextField()
        self._style_field(self.name_field)
        self.meta_hdr = self._hdr('METADATA')
        self.meta_switch = ui.Switch()
        self.meta_switch.value = True
        self.meta_switch.action = lambda s: self.relayout()
        self.meta_switch_lbl = theme.label('Write tags into the file', 14, theme.TEXT, align=ui.ALIGN_LEFT)
        self.meta_fields = {}
        self.meta_labels = {}
        for key, title in (('title', 'Title'), ('artist', 'Artist'), ('album', 'Album'), ('date', 'Year'),
                           ('genre', 'Genre'), ('track', 'Track #'), ('comment', 'Comment')):
            tf = ui.TextField()
            self._style_field(tf, size=14)
            if key in ('date', 'track'):
                tf.keyboard_type = ui.KEYBOARD_NUMBER_PAD
            self.meta_fields[key] = tf
            self.meta_labels[key] = theme.label(title, 12, theme.MUTED, align=ui.ALIGN_LEFT)
        self.cover_switch = ui.Switch()
        self.cover_switch.value = True
        self.cover_lbl = theme.label('Embed thumbnail as cover art', 14, theme.TEXT, align=ui.ALIGN_LEFT)
        self.dl_btn = theme.button('Download', self.on_download, size=17, bg=theme.AMBER, fg=theme.BG)
        self.progress_bg = ui.View(background_color=theme.PANEL)
        self.progress_bg.corner_radius = 4
        self.progress_fg = ui.View(background_color=theme.CYAN)
        self.progress_fg.corner_radius = 4
        self.progress_bg.add_subview(self.progress_fg)
        self.save_btn = theme.button('Save again…', self.on_save_again, size=15)
        self.share_btn = theme.button('Share', self.on_share, size=15)
        for v in ([self.thumb, self.title_lbl, self.sub_lbl, self.mode, self.fmt_hdr, self.fmt_btn, self.fmt_note,
                   self.name_hdr, self.name_field, self.meta_hdr, self.meta_switch, self.meta_switch_lbl]
                  + [x for k in self.meta_fields for x in (self.meta_labels[k], self.meta_fields[k])]
                  + [self.cover_switch, self.cover_lbl, self.dl_btn, self.progress_bg, self.save_btn, self.share_btn]):
            sv.add_subview(v)

        self.right_button_items = [ui.ButtonItem(title='yt-dlp', action=self.on_tools)]
        self.refresh()
        threading.Thread(target=self._install_worker, args=(False,), name='ytdl-install', daemon=True).start()

    # ---- small view helpers ----
    def _style_field(self, tf, size=16):
        tf.background_color = theme.PANEL
        tf.text_color = theme.TEXT
        tf.tint_color = theme.AMBER
        tf.font = (theme.FONT, size)
        tf.bordered = False
        tf.corner_radius = 8

    def _hdr(self, text):
        return theme.label(text, 11, theme.MUTED, bold=True, align=ui.ALIGN_LEFT)

    @property
    def mode_key(self):
        return ('video', 'audio')[self.mode.selected_index]

    @property
    def options(self):
        return self.video if self.mode_key == 'video' else self.audio

    @property
    def current(self):
        return self.choice.get(self.mode_key)

    # ---- layout ----
    def layout(self):
        W, p = self.width, 8
        self.url.frame = (p, p, W - 3 * p - 70, 40)
        self.paste_btn.frame = (W - p - 70, p, 70, 40)
        self.status.frame = (p + 4, 52, W - 2 * p - 8, 40)
        self.scroll.frame = (0, 96, W, self.height - 96)
        self.relayout()

    def relayout(self):
        W, p = self.width, 8
        y = p
        inner = W - 2 * p
        has = self.info is not None
        self.scroll.hidden = not has
        if not has:
            return
        th = 72
        self.thumb.frame = (p, y, th * 16 / 9.0, th)
        tx = p + th * 16 / 9.0 + p
        self.title_lbl.frame = (tx, y, W - tx - p, 40)
        self.sub_lbl.frame = (tx, y + 40, W - tx - p, 32)
        y += th + 12
        self.mode.frame = (p, y, inner, 32)
        y += 32 + 12
        self.fmt_hdr.frame = (p + 2, y, inner, 14)
        y += 16
        self.fmt_btn.frame = (p, y, inner, 40)
        y += 40 + 4
        self.fmt_note.hidden = not self.fmt_note.text
        if self.fmt_note.text:
            self.fmt_note.frame = (p + 2, y, inner - 4, 32)
            y += 34
        y += 6
        self.name_hdr.frame = (p + 2, y, inner, 14)
        y += 16
        self.name_field.frame = (p, y, inner, 36)
        y += 36 + 12
        cur = self.current
        taggable = bool(cur and cur['taggable'])
        self.meta_hdr.frame = (p + 2, y, inner, 14)
        y += 18
        self.meta_switch.frame = (W - p - 51, y, 51, 31)
        self.meta_switch_lbl.frame = (p + 2, y, inner - 60, 31)
        self.meta_switch.enabled = taggable
        self.meta_switch_lbl.text = 'Write tags into the file' if taggable else (
            'Tags: not possible for .%s' % cur['ext'] if cur else 'Tags')
        self.meta_switch_lbl.text_color = theme.TEXT if taggable else theme.MUTED
        y += 31 + 8
        show_meta = taggable and self.meta_switch.value
        half = (inner - p) / 2.0
        pairs = [('title',), ('artist',), ('album',), ('date', 'track'), ('genre',), ('comment',)]
        for k in self.meta_fields:
            self.meta_fields[k].hidden = self.meta_labels[k].hidden = not show_meta
        for sw in (self.cover_switch, self.cover_lbl):
            sw.hidden = not show_meta
        if show_meta:
            for row in pairs:
                w = inner if len(row) == 1 else half
                for i, k in enumerate(row):
                    x = p + i * (half + p)
                    self.meta_labels[k].frame = (x + 2, y, w, 14)
                    self.meta_fields[k].frame = (x, y + 15, w, 34)
                y += 15 + 34 + 6
            self.cover_switch.frame = (W - p - 51, y, 51, 31)
            self.cover_lbl.frame = (p + 2, y, inner - 60, 31)
            y += 31 + 8
        y += 6
        self.dl_btn.frame = (p, y, inner, 48)
        y += 48 + 8
        self.progress_bg.frame = (p, y, inner, 8)
        y += 8 + 12
        done = self.last_file is not None and os.path.exists(self.last_file)
        self.save_btn.hidden = self.share_btn.hidden = not done
        if done:
            self.save_btn.frame = (p, y, half, 40)
            self.share_btn.frame = (2 * p + half, y, half, 40)
            y += 40 + 8
        self.scroll.content_size = (W, y + 260)  # room to scroll fields above the keyboard

    def refresh(self):
        cur = self.current
        if self.info is not None:
            self.fmt_btn.title = '  %s   ▾' % (cur['label'] if cur else 'No %s formats on this page' % self.mode_key)
            note = ''
            if cur is None and self.mode_key == 'audio' and self.video:
                note = 'This site serves no separate audio. Choose Video.'
            elif cur and cur['kind'] == 'video':
                note = 'This stream has no sound, and there is no ffmpeg to add it. Pick a format without "no audio".'
            elif cur and cur['hls']:
                note = 'HLS stream: saved as MPEG-TS data; some players and tagging won\'t handle it.'
            self.fmt_note.text = note
        busy = self.busy
        self.dl_btn.title = 'Cancel' if busy == 'download' else 'Download'
        self.dl_btn.background_color = theme.RED if busy == 'download' else theme.AMBER
        self.dl_btn.enabled = busy in (None, 'download') and cur is not None
        self.dl_btn.alpha = 1.0 if self.dl_btn.enabled else 0.5
        self.paste_btn.enabled = busy not in ('install', 'download')
        self.relayout()

    # ---- status / progress (thread-safe) ----
    @on_main_thread
    def set_status(self, text, color=None):
        self.status.text = text
        self.status.text_color = color or theme.MUTED

    @on_main_thread
    def set_progress(self, frac):
        frac = max(0.0, min(1.0, frac or 0.0))
        w = self.progress_bg.width
        self.progress_fg.frame = (0, 0, w * frac, self.progress_bg.height)

    @on_main_thread
    def set_busy(self, what):
        self.busy = what
        self.refresh()

    # ---- install ----
    def _install_worker(self, update):
        self.set_busy('install')
        try:
            if update:
                v, changed = pydeps.install('yt-dlp', self.set_status)
                pydeps.drop_modules('yt_dlp')
                mv, _ = pydeps.install('mutagen', self.set_status)
                pydeps.drop_modules('mutagen')
                msg = ('Updated to yt-dlp %s' if changed else 'yt-dlp %s is the newest for this Python') % v
            else:
                v = pydeps.ensure('yt-dlp', self.set_status)
                pydeps.ensure('mutagen', self.set_status)
                msg = None
            import yt_dlp  # verifies the wheel imports on this Python
            _log.info("yt-dlp %s imported", yt_dlp.version.__version__)
            self.ytdlp_version = v
            self.set_status(msg or 'yt-dlp %s ready. Paste a link.' % v)
        except Exception as e:
            _log.exception('yt-dlp install failed')
            self.set_status('Could not set up yt-dlp: %s' % _clean_error(e), theme.RED)
        finally:
            self.set_busy(None)
            if self.ytdlp_version and not update and URL_RE.search(self.url.text or ''):
                self._fetch_main(False)

    # ---- link handling ----
    def on_paste(self, sender):
        text = clipboard.get() or ''
        m = URL_RE.search(text)
        if not m:
            console.hud_alert('No link on the clipboard', 'error', 1)
            return
        self.url.text = m.group(0)
        self.fetch(force=True)

    def url_changed(self):
        """Typing/pasting in the field: fetch once it settles on a new link."""
        if self.timer is not None:
            self.timer.cancel()
        text = (self.url.text or '').strip()
        if not URL_RE.fullmatch(text) or text == self.fetched_url:
            return
        self.timer = threading.Timer(0.7, lambda: self._fetch_main(False))
        self.timer.daemon = True
        self.timer.start()

    @on_main_thread
    def _fetch_main(self, force):
        self.fetch(force)

    def fetch(self, force=False):
        m = URL_RE.search(self.url.text or '')
        if not m:
            return
        url = m.group(0)
        if self.busy in ('download', 'fetch', 'install') or (url == self.fetched_url and not force):
            return
        if not self.ytdlp_version:
            self.set_status('Still setting up yt-dlp; the link will load when it is ready.')
            return
        self.fetched_url = url
        threading.Thread(target=self._fetch_worker, args=(url,), name='ytdl-fetch', daemon=True).start()

    def _fetch_worker(self, url):
        import yt_dlp
        self.set_busy('fetch')
        self.set_status('Reading %s…' % url[:60])
        t0 = time.time()
        try:
            with yt_dlp.YoutubeDL(dict(base_opts(), skip_download=True)) as y:
                info = y.extract_info(url, download=False)
                if info.get('_type') == 'playlist':
                    entries = [e for e in (info.get('entries') or []) if e]
                    if not entries:
                        raise RuntimeError('playlist has no playable entries')
                    info = entries[0]
                    if info.get('_type') == 'url':
                        info = y.extract_info(info['url'], download=False)
                info = y.sanitize_info(info)
        except Exception as e:
            _log.warning('fetch failed for %s: %s', url, _clean_error(e))
            self.fetched_url = None
            self.set_status('Couldn\'t read that link: %s' % _clean_error(e)[:240], theme.RED)
            self.set_busy(None)
            return
        video, audio = split_formats(info)
        _log.info('fetched %s in %.1fs: %s, %d video / %d audio formats', info.get('extractor_key'),
                  time.time() - t0, info.get('title'), len(video), len(audio))
        thumb = None
        turl = best_thumb(info)
        if turl:
            try:
                req = urllib.request.Request(turl, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    thumb = r.read(4 * 1024 * 1024)
            except Exception as e:
                _log.info('thumbnail fetch failed: %s', e)
        self._show(info, video, audio, thumb)

    @on_main_thread
    def _show(self, info, video, audio, thumb):
        self.info, self.video, self.audio, self.thumb_bytes = info, video, audio, thumb
        self.choice = {'video': default_choice(video, 'video'), 'audio': default_choice(audio, 'audio')}
        if self.choice[self.mode_key] is None:  # this page only has the other kind
            other = 'audio' if self.mode_key == 'video' else 'video'
            if self.choice[other] is not None:
                self.mode.selected_index = ('video', 'audio').index(other)
        self.last_file = None
        self.title_lbl.text = info.get('title') or info.get('id') or ''
        bits = [info.get('uploader') or info.get('channel'), human_time(info.get('duration')),
                info.get('extractor_key')]
        self.sub_lbl.text = ' · '.join(b for b in bits if b)
        try:
            self.thumb.image = ui.Image.from_data(thumb) if thumb else None
        except Exception:
            self.thumb.image = None
        meta = default_meta(info)
        for k, tf in self.meta_fields.items():
            tf.text = meta.get(k, '')
        self.name_field.text = safe_name(info.get('title') or info.get('id'))
        self.set_progress(0)
        self.busy = None
        self.status.text = '%d video and %d audio formats found.' % (len(video), len(audio))
        self.status.text_color = theme.MUTED
        self.refresh()

    # ---- options ----
    def on_mode(self, sender):
        prefs = storage.load(APP, 'prefs.json', {}) or {}
        prefs['mode'] = self.mode.selected_index
        storage.save(APP, 'prefs.json', prefs)
        self.refresh()

    @log.bg
    def on_format(self, sender):
        opts = self.options
        if not opts:
            console.hud_alert('No %s formats on this page' % self.mode_key, 'error', 1)
            return
        labels = [('✓ ' if o is self.current else '') + o['label'] + ('' if o['taggable'] else '  (no tags)')
                  for o in opts]
        pick = dialogs.list_dialog('%s format' % self.mode_key.title(), labels)
        if pick is None:
            return
        self._choose(opts[labels.index(pick)])

    @on_main_thread
    def _choose(self, o):
        self.choice[self.mode_key] = o
        self.refresh()

    # ---- download ----
    def on_download(self, sender):
        if self.busy == 'download':
            self.cancel = True
            self.set_status('Cancelling…')
            return
        cur = self.current
        if cur is None or self.fetched_url is None:
            return
        for tf in [self.name_field, self.url] + list(self.meta_fields.values()):
            tf.end_editing()
        job = {
            'url': self.fetched_url, 'fmt': cur, 'name': safe_name(self.name_field.text or self.info.get('title')),
            'tags': dict((k, tf.text) for k, tf in self.meta_fields.items())
            if (cur['taggable'] and self.meta_switch.value) else None,
            'cover': self.thumb_bytes if (cur['taggable'] and self.meta_switch.value and self.cover_switch.value) else None,
        }
        self.cancel = False
        threading.Thread(target=self._download_worker, args=(job,), name='ytdl-download', daemon=True).start()

    def _progress(self, d):
        """yt-dlp progress hook. Raising Cancelled is how a download is stopped, so it must
        not go through log.instrument (which would log every cancel as a crash)."""
        if self.cancel:
            raise Cancelled()
        if d.get('status') != 'downloading':
            return
        now = time.time()
        if now - self._last_progress < 0.25:
            return
        self._last_progress = now
        done = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        frac = done / float(total) if total else None
        speed = d.get('speed')
        eta = d.get('eta')
        txt = 'Downloading %s%s%s%s' % (human_size(done), ' of %s' % human_size(total) if total else '',
                                        '  ·  %s/s' % human_size(speed) if speed else '',
                                        '  ·  %s left' % human_time(eta) if eta else '')
        self.set_status(txt)
        if frac is not None:
            self.set_progress(frac)

    _progress._pt_guarded = True  # log.instrument skips it

    def _download_worker(self, job):
        import yt_dlp
        self.set_busy('download')
        self.set_progress(0)
        tmp = _dir('tmp')
        for name in os.listdir(tmp):  # leftovers from earlier runs
            try:
                os.remove(os.path.join(tmp, name))
            except OSError:
                pass
        fmt = job['fmt']
        opts = dict(base_opts(), format=fmt['id'], progress_hooks=[self._progress],
                    outtmpl={'default': os.path.join(tmp, '%(id)s.%(ext)s')},
                    concurrent_fragment_downloads=4)
        _log.info('download %s format %s (%s)', job['url'], fmt['id'], fmt['label'])
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(job['url'], download=True)
                if info.get('_type') == 'playlist':
                    info = next(e for e in info.get('entries') or [] if e)
            reqs = info.get('requested_downloads') or []
            src = reqs[0].get('filepath') if reqs else None
            if not src or not os.path.exists(src):
                cands = [os.path.join(tmp, n) for n in os.listdir(tmp) if not n.endswith(('.part', '.ytdl'))]
                src = max(cands, key=os.path.getsize) if cands else None
            if not src:
                raise RuntimeError('download finished but no file was produced')
        except Exception as e:
            cancelled = self.cancel or isinstance(e, Cancelled) or 'Cancelled' in type(e).__name__
            if cancelled:
                self.set_status('Cancelled.')
            else:
                _log.warning('download failed: %s', _clean_error(e))
                self.set_status('Download failed: %s' % _clean_error(e)[:240], theme.RED)
            self.set_progress(0)
            self.set_busy(None)
            return
        ext = os.path.splitext(src)[1]
        dest = os.path.join(_dir('out'), job['name'] + ext)
        out_dir = os.path.dirname(dest)
        for name in os.listdir(out_dir):  # keep only the newest file around
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass
        shutil.move(src, dest)
        self.set_progress(1)
        note = ''
        if job['tags'] is not None:
            try:
                mediatags.write(dest, job['tags'], job['cover'])
                note = ' Tags written.'
            except Exception as e:
                _log.exception('tagging failed')
                note = ' (Tags not written: %s)' % e
        size = human_size(os.path.getsize(dest))
        _log.info('downloaded %s (%s)%s', os.path.basename(dest), size, note)
        self.set_status('Done: %s (%s).%s Choose where to save it.' % (os.path.basename(dest), size, note), theme.GREEN)
        self._finish(dest)

    @on_main_thread
    def _finish(self, path):
        self.last_file = path
        self.busy = None
        self.refresh()
        self._save(path)

    def _save(self, path):
        from ptlib import savefile

        def done(saved):
            self.set_status(('Saved %s.' if saved else 'Not saved. Use "Save again…" or Share for %s.')
                            % os.path.basename(path), theme.GREEN if saved else theme.MUTED)
        savefile.save(path, done)

    def on_save_again(self, sender):
        if self.last_file and os.path.exists(self.last_file):
            self._save(self.last_file)

    def on_share(self, sender):
        if self.last_file and os.path.exists(self.last_file):
            console.open_in(self.last_file)

    # ---- tools ----
    @log.bg
    def on_tools(self, sender):
        st = pydeps.state()
        y = st.get('yt-dlp', {}).get('version', 'not installed')
        m = st.get('mutagen', {}).get('version', 'not installed')
        import sys
        opts = ['Update yt-dlp and mutagen', 'Copy page info (debug)']
        pick = dialogs.list_dialog('yt-dlp %s · mutagen %s · Python %d.%d' % ((y, m) + sys.version_info[:2]), opts)
        if pick == opts[0]:
            if self.busy:
                console.hud_alert('Busy: try again in a moment', 'error', 1)
                return
            threading.Thread(target=self._install_worker, args=(True,), name='ytdl-update', daemon=True).start()
        elif pick == opts[1]:
            if not self.info:
                console.hud_alert('Read a link first', 'error', 1)
                return
            lines = ['%s  %s' % (self.info.get('extractor_key'), self.info.get('webpage_url'))]
            for o in self.video + self.audio:
                lines.append('%-8s %-6s %s' % (o['id'], o['kind'], o['label']))
            clipboard.set('\n'.join(lines))
            console.hud_alert('Copied')

    def stop(self):
        self.cancel = True
        if self.timer is not None:
            self.timer.cancel()


def make_view():
    return DownloaderView()
