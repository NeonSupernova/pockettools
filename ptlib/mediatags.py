"""Write title/artist/album/... and cover art into media files with mutagen.

yt-dlp's own metadata and thumbnail embedding go through ffmpeg, which iOS
doesn't have. mutagen is pure Python and covers the containers that matter:
MP4 family (m4a, mp4, m4v, mov), MP3 (ID3), Ogg Opus/Vorbis and FLAC.
WebM/Matroska is not supported by mutagen, so .webm downloads can't be tagged.
"""
import base64
import os

TAGGABLE = ('m4a', 'mp4', 'm4v', 'mov', 'm4b', 'mp3', 'ogg', 'oga', 'opus', 'flac')

FIELDS = ('title', 'artist', 'album', 'date', 'genre', 'track', 'comment')


def supported(ext):
    return (ext or '').lower().lstrip('.') in TAGGABLE


def _image_kind(data):
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    return None


def to_cover(data):
    """Cover art must be JPEG or PNG; convert anything else (webp) with PIL."""
    if not data:
        return None
    if _image_kind(data):
        return data
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert('RGB')
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=90)
        return out.getvalue()
    except Exception:
        return None


def _track(v):
    try:
        return int(str(v).split('/')[0])
    except (TypeError, ValueError):
        return None


def write(path, tags, cover=None):
    """tags: dict with any of FIELDS (empty values skipped). cover: image bytes."""
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    if not supported(ext):
        raise ValueError('.%s files cannot be tagged' % ext)
    tags = dict((k, str(v).strip()) for k, v in tags.items() if k in FIELDS and v not in (None, ''))
    tags = dict((k, v) for k, v in tags.items() if v)
    cover = to_cover(cover)
    if ext in ('m4a', 'mp4', 'm4v', 'mov', 'm4b'):
        _mp4(path, tags, cover)
    elif ext == 'mp3':
        _mp3(path, tags, cover)
    elif ext == 'flac':
        _flac(path, tags, cover)
    else:
        _ogg(path, tags, cover)


def _mp4(path, tags, cover):
    from mutagen.mp4 import MP4, MP4Cover
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    keys = {'title': '\xa9nam', 'artist': '\xa9ART', 'album': '\xa9alb', 'date': '\xa9day',
            'genre': '\xa9gen', 'comment': '\xa9cmt'}
    for k, atom in keys.items():
        if k in tags:
            f.tags[atom] = [tags[k]]
    if 'artist' in tags:
        f.tags['aART'] = [tags['artist']]
    n = _track(tags.get('track'))
    if n:
        f.tags['trkn'] = [(n, 0)]
    if cover:
        fmt = MP4Cover.FORMAT_PNG if _image_kind(cover) == 'png' else MP4Cover.FORMAT_JPEG
        f.tags['covr'] = [MP4Cover(cover, imageformat=fmt)]
    f.save()


def _mp3(path, tags, cover):
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TDRC, TCON, TRCK, COMM, APIC
    try:
        f = ID3(path)
    except ID3NoHeaderError:
        f = ID3()
    frames = {'title': TIT2, 'artist': TPE1, 'album': TALB, 'date': TDRC, 'genre': TCON, 'track': TRCK}
    for k, cls in frames.items():
        if k in tags:
            f.setall(cls.__name__, [cls(encoding=3, text=tags[k])])
    if 'comment' in tags:
        f.setall('COMM', [COMM(encoding=3, lang='eng', desc='', text=tags['comment'])])
    if cover:
        mime = 'image/png' if _image_kind(cover) == 'png' else 'image/jpeg'
        f.setall('APIC', [APIC(encoding=3, mime=mime, type=3, desc='Cover', data=cover)])
    f.save(path)


_VORBIS = {'title': 'title', 'artist': 'artist', 'album': 'album', 'date': 'date', 'genre': 'genre',
           'track': 'tracknumber', 'comment': 'comment'}


def _picture(cover):
    from mutagen.flac import Picture
    p = Picture()
    p.type = 3
    p.mime = 'image/png' if _image_kind(cover) == 'png' else 'image/jpeg'
    p.desc = 'Cover'
    p.data = cover
    return p


def _flac(path, tags, cover):
    from mutagen.flac import FLAC
    f = FLAC(path)
    for k, key in _VORBIS.items():
        if k in tags:
            f[key] = [tags[k]]
    if cover:
        f.clear_pictures()
        f.add_picture(_picture(cover))
    f.save()


def _ogg(path, tags, cover):
    import mutagen
    f = mutagen.File(path)
    if f is None:
        raise ValueError('not a recognised Ogg file')
    if f.tags is None:
        f.add_tags()
    for k, key in _VORBIS.items():
        if k in tags:
            f[key] = [tags[k]]
    if cover:
        f['metadata_block_picture'] = [base64.b64encode(_picture(cover).write()).decode('ascii')]
    f.save()
