"""BLE advertisement scanner with live RSSI graph.

Bypasses Pythonista's cb module (no RSSI, no adv data) and drives
CBCentralManager directly through objc_util. Delegate callbacks only, no
ObjC blocks, which sidesteps the ObjCBlock fragility. The manager is created
with a nil queue, so callbacks land on the main thread: no locking against
the UI timer, but keep the handler cheap.

RSSI refresh: AllowDuplicates delivers every advertisement, which in a busy
room is thousands of bridge crossings per second on the main thread (that flood
crashed it). Instead, scan without duplicates and restart the scan every
RESCAN seconds: each restart re-reports every device once with a fresh RSSI,
roughly 1/100th of the callback volume. The graph carries the last value
forward between reports.

Peripheral IDs are per-phone UUIDs assigned by iOS, not MAC addresses.
"""
import time
from collections import deque, OrderedDict

import ui
import console
from objc_util import ObjCClass, ObjCInstance, create_objc_class, ns, load_framework, nsdata_to_bytes

from ptlib import theme, log
from ptlib.ticker import Ticker
from ptlib.graph import LineGraph

_log = log.get('blescan')
TITLE = 'BLE Scan'
TICK = 0.5
HISTORY = 120            # samples -> 60 s
RESCAN = 2.0             # s between scan restarts (fresh RSSI per device)
HOLD = 3.0               # s to carry an RSSI forward between reports
STALE = 10.0             # s without adverts -> dimmed, dropped from auto graph
FORGET = 90.0            # s -> removed (unless pinned)
MAX_PINS = len(theme.SERIES)
STATES = {0: 'unknown', 1: 'resetting', 2: 'unsupported', 3: 'unauthorized', 4: 'powered off', 5: 'on'}
COMPANIES = {0x004C: 'Apple', 0x0006: 'Microsoft', 0x0075: 'Samsung', 0x00E0: 'Google',
             0x0059: 'Nordic', 0x02E5: 'Espressif', 0x000D: 'TI'}

_log.info('load_framework CoreBluetooth')
load_framework('CoreBluetooth')
CBCentralManager = ObjCClass('CBCentralManager')
NSObject = ObjCClass('NSObject')

_handler = [None]        # active Scanner; callbacks route here
_delegate_cls = [None]
_keepalive = []          # IMP functions: never let the GC near anything the ObjC runtime calls


def _get_delegate_class():
    if _delegate_cls[0] is not None:
        return _delegate_cls[0]

    def centralManagerDidUpdateState_(_self, _cmd, central):
        h = _handler[0]
        if h is not None:
            h.on_state(ObjCInstance(central))

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(_self, _cmd, central, peripheral, adv, rssi):
        h = _handler[0]
        if h is not None:
            h.on_discover(ObjCInstance(peripheral), ObjCInstance(adv), ObjCInstance(rssi))

    _keepalive.extend([centralManagerDidUpdateState_,
                       centralManager_didDiscoverPeripheral_advertisementData_RSSI_])
    centralManagerDidUpdateState_.encoding = 'v@:@'
    centralManager_didDiscoverPeripheral_advertisementData_RSSI_.encoding = 'v@:@@@@'
    name = 'PTBLEDelegate%d' % (int(time.time() * 1000) % 1000000000)
    _log.info('create_objc_class %s', name)
    _delegate_cls[0] = create_objc_class(
        name, NSObject,
        methods=[centralManagerDidUpdateState_,
                 centralManager_didDiscoverPeripheral_advertisementData_RSSI_])
    _keepalive.append(_delegate_cls[0])
    _log.info('delegate class created')
    return _delegate_cls[0]


def _adv(adv, key):
    try:
        return adv.objectForKey_(key)
    except Exception:
        return None


class Device(object):
    def __init__(self, uid):
        self.uid = uid
        self.name = None
        self.rssi = None
        self.last_seen = 0.0
        self.count = 0
        self.window = []
        self.series = deque(maxlen=HISTORY)
        self.mfr = None
        self.mfr_bytes = b''
        self.connectable = None
        self.tx = None
        self.services = ()

    def update(self, p, adv, rssi):
        self.rssi = rssi
        self.last_seen = time.time()
        self.count += 1
        self.window.append(rssi)
        self._parse(p, adv)  # cheap now: one report per device per RESCAN

    def _parse(self, p, adv):
        try:
            n = _adv(adv, 'kCBAdvDataLocalName')
            if n is None:
                n = p.name()
            if n is not None and str(n):
                self.name = str(n)
            m = _adv(adv, 'kCBAdvDataManufacturerData')
            if m is not None:
                b = nsdata_to_bytes(m)
                self.mfr_bytes = b
                if len(b) >= 2:
                    self.mfr = b[0] | (b[1] << 8)
            c = _adv(adv, 'kCBAdvDataIsConnectable')
            if c is not None:
                self.connectable = bool(c.boolValue())
            t = _adv(adv, 'kCBAdvDataTxPowerLevel')
            if t is not None:
                self.tx = int(t.intValue())
            s = _adv(adv, 'kCBAdvDataServiceUUIDs')
            if s is not None:
                self.services = tuple(str(s.objectAtIndex_(i).UUIDString()) for i in range(int(s.count())))
        except Exception:
            pass

    def roll(self, now):
        w = self.window
        if w:
            v = sum(w) / float(len(w))
        elif self.rssi is not None and now - self.last_seen <= HOLD:
            v = self.rssi
        else:
            v = None
        self.series.append(v)
        self.window = []

    def label(self):
        return self.name or ('Unnamed ' + self.uid[:4])

    def company(self):
        if self.mfr is None:
            return None
        return COMPANIES.get(self.mfr, '0x%04X' % self.mfr)

    def detail(self, now):
        parts = ['%d dBm' % self.rssi if self.rssi is not None else '--']
        if self.company():
            parts.append(self.company())
        if self.connectable:
            parts.append('conn')
        if self.services:
            parts.append('%d svc' % len(self.services))
        age = now - self.last_seen
        if age > 2:
            parts.append('%ds ago' % age)
        return ' · '.join(parts)

    def info(self):
        lines = ['UUID (iOS-assigned, not MAC):', self.uid,
                 'RSSI %s dBm, %d adverts' % (self.rssi, self.count)]
        if self.tx is not None:
            lines.append('TX power %d dBm' % self.tx)
        if self.connectable is not None:
            lines.append('Connectable: %s' % ('yes' if self.connectable else 'no'))
        if self.mfr is not None:
            lines.append('Manufacturer %s' % self.company())
            lines.append(self.mfr_bytes[:24].hex(' ') + (' …' if len(self.mfr_bytes) > 24 else ''))
        if self.services:
            lines.append('Services: ' + ', '.join(self.services))
        return '\n'.join(lines)


class Scanner(object):
    def __init__(self):
        self.devices = OrderedDict()
        self.central = None
        self.delegate = None
        self.state = None
        self.scanning = False
        self.want_scan = False
        self.last_scan = 0.0

    def start(self):
        _handler[0] = self
        self.want_scan = True
        if self.central is None:
            cls = _get_delegate_class()
            _log.info('alloc delegate')
            self.delegate = cls.alloc().init()
            _log.info('alloc CBCentralManager')
            # nil queue -> main queue; scanning starts in on_state once powered on
            self.central = CBCentralManager.alloc().initWithDelegate_queue_(self.delegate, None)
            _log.info('CBCentralManager ready, awaiting state')
        elif self.state == 5:
            self._scan()

    def _scan(self):
        opts = ns({'kCBScanOptionAllowDuplicates': False})
        self.central.scanForPeripheralsWithServices_options_(None, opts)
        self.scanning = True
        self.last_scan = time.time()

    def rescan(self):
        """Restart the scan so every device is reported again with a fresh RSSI."""
        if self.scanning and self.central is not None and time.time() - self.last_scan >= RESCAN:
            self.central.stopScan()
            self._scan()

    def stop(self):
        self.want_scan = False
        if self.central is not None and self.scanning:
            self.central.stopScan()
        self.scanning = False

    def shutdown(self):
        _log.info('scanner shutdown')
        self.stop()
        if self.central is not None:
            self.central.setDelegate_(None)
        self.central = None
        self.delegate = None
        if _handler[0] is self:
            _handler[0] = None

    def on_state(self, central):
        self.state = int(central.state())
        _log.info('central state %s (%s)', self.state, STATES.get(self.state))
        if self.state == 5 and self.want_scan:
            _log.info('scanForPeripherals (no duplicates, rescan every %.0fs)', RESCAN)
            self._scan()
        elif self.state != 5:
            self.scanning = False

    def on_discover(self, p, adv, rssi):
        r = int(rssi.intValue())
        if r >= 127 or r == 0:  # 127 = RSSI unavailable
            return
        uid = str(p.identifier().UUIDString())
        d = self.devices.get(uid)
        if d is None:
            d = self.devices[uid] = Device(uid)
            _log.debug('new device %s rssi %d', uid[:8], r)
        d.update(p, adv, r)


@log.instrument
class BLEView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_color = theme.BG
        self.scanner = Scanner()
        self.pinned = []
        self.rows = []
        self.colors = {}
        self.status = theme.label('Starting…', 13, theme.MUTED, align=ui.ALIGN_LEFT)
        self.graph = LineGraph(caption='RSSI (dBm)', y_min=-100, y_max=-30, capacity=HISTORY)
        self.tv = theme.table(self, 56)
        for v in (self.status, self.graph, self.tv):
            self.add_subview(v)
        self.scan_item = ui.ButtonItem(title='Stop', action=self.toggle)
        self.right_button_items = [self.scan_item]
        self.ticker = Ticker(TICK, self.tick, self)
        self.scanner.start()
        self.ticker.start()

    def layout(self):
        W, H, p = self.width, self.height, 8
        self.status.frame = (p + 2, 4, W - 2 * p, 20)
        gh = min(220, max(150, H * 0.3))
        self.graph.frame = (p, 26, W - 2 * p, gh)
        y = 26 + gh + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def toggle(self, sender):
        if self.scanner.want_scan:
            self.scanner.stop()
            self.scan_item.title = 'Scan'
        else:
            self.scanner.start()
            self.scan_item.title = 'Stop'

    def tick(self):
        now = time.time()
        sc = self.scanner
        sc.rescan()
        for uid in list(sc.devices):
            d = sc.devices[uid]
            if now - d.last_seen > FORGET and uid not in self.pinned:
                del sc.devices[uid]
                continue
            d.roll(now)
        self.rows = sorted(sc.devices.values(),
                           key=lambda d: (now - d.last_seen > STALE, -(d.rssi if d.rssi is not None else -999)))
        if self.pinned:
            shown = [sc.devices[u] for u in self.pinned if u in sc.devices]
        else:
            shown = [d for d in self.rows if now - d.last_seen <= STALE][:4]
        self.colors = {}
        series = []
        for i, d in enumerate(shown):
            c = theme.SERIES[i % len(theme.SERIES)]
            self.colors[d.uid] = c
            series.append((d.label(), c, d.series))
        self.graph.series = series
        self.graph.set_needs_display()
        active = sum(1 for d in self.rows if now - d.last_seen <= STALE)
        st = sc.state
        if st == 3:
            self.status.text = 'Bluetooth access denied: Settings › Pythonista'
        elif st is not None and st != 5:
            self.status.text = 'Bluetooth %s' % STATES.get(st, st)
        else:
            mode = 'scanning' if sc.scanning else 'stopped'
            pin = ' · %d pinned' % len(self.pinned) if self.pinned else ' · tap rows to pin'
            self.status.text = '%s · %d active / %d seen%s' % (mode, active, len(self.rows), pin)
        self.tv.reload()

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.rows)

    def tableview_cell_for_row(self, tv, section, row):
        if row >= len(self.rows):
            return ui.TableViewCell()
        d = self.rows[row]
        now = time.time()
        stale = now - d.last_seen > STALE
        fg = self.colors.get(d.uid, theme.MUTED if stale else theme.TEXT)
        title = ('★ ' if d.uid in self.pinned else '') + d.label()
        c = theme.cell(title, d.detail(now), fg=fg)
        c.accessory_type = 'detail_button'
        return c

    def tableview_did_select(self, tv, section, row):
        if row >= len(self.rows):
            return
        uid = self.rows[row].uid
        if uid in self.pinned:
            self.pinned.remove(uid)
        elif len(self.pinned) < MAX_PINS:
            self.pinned.append(uid)
        else:
            console.hud_alert('Max %d pinned' % MAX_PINS, 'error', 0.8)
        self.tick()

    def tableview_accessory_button_tapped(self, tv, section, row):
        if row < len(self.rows):
            d = self.rows[row]
            self._show_info(d.label(), d.info())

    @log.bg
    def _show_info(self, title, msg):
        try:
            console.alert(title, msg, 'OK', hide_cancel_button=True)
        except KeyboardInterrupt:
            pass

    def stop(self):
        self.ticker.stop()
        self.scanner.shutdown()


def make_view():
    return BLEView()
