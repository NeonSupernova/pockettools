"""UIImpactFeedbackGenerator wrapper. Call from the main thread (button actions are)."""
try:
    from objc_util import ObjCClass
    _Gen = ObjCClass('UIImpactFeedbackGenerator')
except Exception:
    _Gen = None

_cache = {}


def tap(style=1):
    """style: 0 light, 1 medium, 2 heavy."""
    if _Gen is None:
        return
    try:
        g = _cache.get(style)
        if g is None:
            g = _Gen.alloc().initWithStyle_(style)
            _cache[style] = g
        g.impactOccurred()
    except Exception:
        pass
