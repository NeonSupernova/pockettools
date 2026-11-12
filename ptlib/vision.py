"""On-device image classification and object detection via Apple's Vision framework.

Everything runs locally (Neural Engine/GPU), no network and no API key.
Requests are created without completion handlers and run synchronously with
performRequests:error:, so no ObjC blocks are involved (see blescan.py for why
that matters). Struct returns (boundingBox -> CGRect) always pass an explicit
restype: a wrong guess on a struct-returning objc_msgSend is a hard crash.

Call these from a background thread; a full classify + detect pass on a 2048px
image takes a fraction of a second on recent iPhones.

Coordinates: Vision boxes are normalized with the origin bottom-left. Every
box returned here is converted to pixels in the PIL image passed in, origin
top-left: (x, y, w, h).
"""
import io
import logging
import threading
from ctypes import c_bool, c_float, c_void_p

from objc_util import ObjCClass, CGRect, ns, load_framework

_log = logging.getLogger('pt.vision')
_lock = threading.Lock()  # one Vision pass at a time keeps memory bounded

load_framework('Vision')


def _cls(name):
    try:
        return ObjCClass(name)
    except Exception:
        _log.info('Vision class %s unavailable on this iOS', name)
        return None


VNImageRequestHandler = _cls('VNImageRequestHandler')
VNClassifyImageRequest = _cls('VNClassifyImageRequest')                       # iOS 13
VNSaliencyRequest = _cls('VNGenerateObjectnessBasedSaliencyImageRequest')     # iOS 13
VNAnimalsRequest = _cls('VNRecognizeAnimalsRequest')                          # iOS 13
VNHumansRequest = _cls('VNDetectHumanRectanglesRequest')                      # iOS 13
VNFacesRequest = _cls('VNDetectFaceRectanglesRequest')                        # iOS 11


def available():
    return VNImageRequestHandler is not None and VNClassifyImageRequest is not None


def pretty(identifier):
    return str(identifier).replace('_', ' ')


# ---- images ----
def prepare(img, max_side=2048):
    """PIL image -> upright RGB, longest side <= max_side. Use the result for
    both display and Vision so box coordinates line up."""
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode != 'RGB':
        img = img.convert('RGB')
    w, h = img.size
    s = float(max_side) / max(w, h)
    if s < 1:
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), getattr(Image, 'LANCZOS', 1))
    return img


def jpeg_bytes(img, quality=90):
    b = io.BytesIO()
    img.save(b, 'JPEG', quality=quality)
    return b.getvalue()


def to_ui_image(img, max_side=1200):
    """PIL -> ui.Image, downscaled for cheap redraws."""
    import ui
    from PIL import Image
    w, h = img.size
    s = float(max_side) / max(w, h)
    if s < 1:
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), getattr(Image, 'BILINEAR', 2))
    return ui.Image.from_data(jpeg_bytes(img, 85))


def crop(img, box, pad=0.08):
    """Crop box (x, y, w, h) from img with a little context around it."""
    x, y, w, h = box
    px, py = w * pad, h * pad
    W, H = img.size
    l, t = max(0, int(x - px)), max(0, int(y - py))
    r, b = min(W, int(x + w + px)), min(H, int(y + h + py))
    if r - l < 2 or b - t < 2:
        return img
    return img.crop((l, t, r, b))


# ---- Vision plumbing ----
def _perform(img, requests):
    if VNImageRequestHandler is None:
        raise RuntimeError('Vision framework unavailable')
    data = ns(jpeg_bytes(img))
    handler = VNImageRequestHandler.alloc().initWithData_options_(data, ns({}))
    ok = handler.performRequests_error_(ns(list(requests)), None,
                                        restype=c_bool, argtypes=[c_void_p, c_void_p])
    if not ok:
        raise RuntimeError('Vision performRequests failed')


def _box(obs, size):
    r = obs.boundingBox(restype=CGRect, argtypes=[])
    W, H = size
    x, w = r.origin.x * W, r.size.width * W
    h = r.size.height * H
    y = (1.0 - r.origin.y - r.size.height) * H
    return (x, y, w, h)


def _conf(obs):
    return float(obs.confidence(restype=c_float, argtypes=[]))


def _results(req):
    res = req.results()
    return [res.objectAtIndex_(i) for i in range(res.count())] if res is not None else []


# ---- public API ----
def classify(img, top=10, min_conf=0.01):
    """Whole-image labels: [(label, confidence 0..1)], best first."""
    with _lock:
        req = VNClassifyImageRequest.alloc().init()
        _log.debug('classify %dx%d', img.size[0], img.size[1])
        _perform(img, [req])
        out = []
        for obs in _results(req):
            c = _conf(obs)
            if c >= min_conf:
                out.append((pretty(obs.identifier()), c))
    out.sort(key=lambda t: -t[1])
    return out[:top]


def detect(img, max_objects=8):
    """Object proposals: [{'box', 'kind', 'label', 'conf'}]. kind is one of
    animal, person, face (labelled by Vision) or object (salient region,
    unlabelled; classify a crop of it to name it)."""
    reqs = {}
    for kind, cls in (('object', VNSaliencyRequest), ('animal', VNAnimalsRequest),
                      ('person', VNHumansRequest), ('face', VNFacesRequest)):
        if cls is not None:
            reqs[kind] = cls.alloc().init()
    if not reqs:
        return []
    found = []
    with _lock:
        _log.debug('detect %dx%d with %s', img.size[0], img.size[1], sorted(reqs))
        _perform(img, list(reqs.values()))
        if 'animal' in reqs:
            for obs in _results(reqs['animal']):
                labels = obs.labels()
                label, conf = 'animal', _conf(obs)
                if labels is not None and labels.count():
                    best = labels.objectAtIndex_(0)
                    label, conf = pretty(best.identifier()).lower(), _conf(best)
                found.append({'box': _box(obs, img.size), 'kind': 'animal', 'label': label, 'conf': conf})
        if 'person' in reqs:
            for obs in _results(reqs['person']):
                found.append({'box': _box(obs, img.size), 'kind': 'person', 'label': 'person', 'conf': _conf(obs)})
        if 'face' in reqs:
            for obs in _results(reqs['face']):
                found.append({'box': _box(obs, img.size), 'kind': 'face', 'label': 'face', 'conf': _conf(obs)})
        if 'object' in reqs:
            for sal in _results(reqs['object']):
                objs = sal.salientObjects()
                for i in range(objs.count() if objs is not None else 0):
                    o = objs.objectAtIndex_(i)
                    found.append({'box': _box(o, img.size), 'kind': 'object', 'label': None, 'conf': _conf(o)})
    # drop salient regions that just duplicate a labelled detection
    labelled = [f for f in found if f['kind'] != 'object']
    out = list(labelled)
    for f in found:
        if f['kind'] == 'object' and all(iou(f['box'], g['box']) < 0.5 for g in labelled):
            out.append(f)
    W, H = img.size
    out = [f for f in out if f['box'][2] * f['box'][3] >= 0.002 * W * H]
    return out[:max_objects]


def label_objects(img, objects):
    """Fill in label/conf for unlabelled 'object' detections by classifying a crop."""
    for f in objects:
        if f['label']:
            continue
        top = classify(crop(img, f['box']), top=1, min_conf=0.0)
        if top:
            f['label'], f['conf'] = top[0]
        else:
            f['label'] = 'object'
    return objects


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0
