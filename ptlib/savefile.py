"""The iOS "Save to…" sheet (UIDocumentPickerViewController in export mode).

save(path, done) shows the system location picker (Files app: On My iPhone,
iCloud Drive, other providers) and copies the file wherever the user picks.
done(saved: bool) runs on the main thread afterwards.

Delegate callbacks only, no ObjC blocks (see blescan.py for why). If the
picker can't be shown, falls back to the share sheet (console.open_in).
"""
import logging
import time

from objc_util import ObjCClass, ObjCInstance, create_objc_class, nsurl, on_main_thread

_log = logging.getLogger('pt.savefile')
_cls = [None]
_keep = []          # picker, delegate and IMPs must outlive the presentation
_pending = {}       # delegate ptr -> callback


def _delegate_class():
    if _cls[0] is not None:
        return _cls[0]

    def documentPicker_didPickDocumentsAtURLs_(_self, _cmd, picker, urls):
        _finish(_self, True)

    def documentPickerWasCancelled_(_self, _cmd, picker):
        _finish(_self, False)

    documentPicker_didPickDocumentsAtURLs_.encoding = 'v@:@@'
    documentPickerWasCancelled_.encoding = 'v@:@'
    _keep.extend([documentPicker_didPickDocumentsAtURLs_, documentPickerWasCancelled_])
    name = 'PTSaveDelegate%d' % (int(time.time() * 1000) % 1000000000)
    _cls[0] = create_objc_class(name, ObjCClass('NSObject'),
                                methods=[documentPicker_didPickDocumentsAtURLs_, documentPickerWasCancelled_],
                                protocols=['UIDocumentPickerDelegate'])
    _keep.append(_cls[0])
    return _cls[0]


def _key(p):
    p = getattr(p, 'ptr', p)            # ObjCInstance -> c_void_p
    return int(getattr(p, 'value', p) or 0)  # c_void_p -> int; ints pass through


def _finish(delegate_ptr, saved):
    cb = _pending.pop(_key(delegate_ptr), None)
    _log.info('save picker finished: %s', 'saved' if saved else 'cancelled')
    if cb is not None:
        try:
            cb(saved)
        except Exception:
            _log.exception('save callback failed')


def _top_controller():
    app = ObjCClass('UIApplication').sharedApplication()
    vc = app.keyWindow().rootViewController()
    while vc.presentedViewController() is not None:
        vc = vc.presentedViewController()
    return vc


@on_main_thread
def save(path, done=None):
    try:
        Picker = ObjCClass('UIDocumentPickerViewController')
        url = nsurl(path)
        try:
            picker = Picker.alloc().initForExportingURLs_asCopy_([url], True)  # iOS 14+
        except Exception:
            picker = Picker.alloc().initWithURL_inMode_(url, 2)  # UIDocumentPickerModeExportToService
        delegate = _delegate_class().alloc().init()
        picker.setDelegate_(delegate)
        _pending[_key(ObjCInstance(delegate))] = done
        _keep.extend([picker, delegate])
        del _keep[:-40]
        _log.info('presenting save picker for %s', path)
        _top_controller().presentViewController_animated_completion_(picker, True, None)
    except Exception:
        _log.exception('save picker unavailable; falling back to share sheet')
        import console
        console.open_in(path)
        if done is not None:
            done(True)
