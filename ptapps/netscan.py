"""Subnet sweep + per-host latency monitor.

iOS gives no raw sockets, so no ICMP. Liveness comes from TCP connects instead:
a SYN answered by SYN/ACK (open) or RST (ECONNREFUSED) proves the host is up.
Each host probes all ports at once with non-blocking sockets multiplexed via
selectors (kqueue on iOS, so no FD_SETSIZE ceiling); a down host costs one
timeout, not one per port. Hosts that drop everything (stealth firewall) and
have none of the probed ports open are invisible to this method.

First run triggers iOS's Local Network permission prompt; allow it and rescan.
"""
import errno
import ipaddress
import selectors
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import ui
import console

from ptlib import theme, log
from ptlib.ticker import Ticker
from ptlib.graph import BarGraph, LineGraph

_log = log.get('netscan')
TITLE = 'Network Scan'
PORTS = [80, 443, 22, 445, 139, 53, 548, 631, 9100, 8080, 62078, 3389, 1883, 502, 5000]
PORT_NAMES = {22: 'ssh', 53: 'dns', 80: 'http', 139: 'netbios', 443: 'https', 445: 'smb',
              502: 'modbus', 548: 'afp', 631: 'ipp', 1883: 'mqtt', 3389: 'rdp', 5000: '5000',
              8080: 'http-alt', 9100: 'jetdirect', 62078: 'apple-sync'}
INPROGRESS = (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN)
MAX_HOSTS = 1024


WINDOW = 64        # sockets in flight at once; stays well under iOS's default 256-fd limit
NAME_WORKERS = 4   # reverse-DNS is blocking-only, so it needs a few threads


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))  # UDP connect sends nothing; just picks a route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def probe_host(ip, ports, timeout):
    """Returns {port: ('open'|'refused', ms)} for ports that answered."""
    sel = selectors.DefaultSelector()
    socks, results = [], {}
    t0 = time.perf_counter()
    try:
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            except OSError:
                break  # out of descriptors: probe what we have
            socks.append(s)
            s.setblocking(False)
            rc = s.connect_ex((ip, port))
            ms = (time.perf_counter() - t0) * 1000.0
            if rc == 0:
                results[port] = ('open', ms)
            elif rc == errno.ECONNREFUSED:
                results[port] = ('refused', ms)
            elif rc in INPROGRESS:
                sel.register(s, selectors.EVENT_WRITE, port)
        deadline = t0 + timeout
        while sel.get_map():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            for key, _ in sel.select(remaining):
                sel.unregister(key.fileobj)
                err = key.fileobj.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                ms = (time.perf_counter() - t0) * 1000.0
                if err == 0:
                    results[key.data] = ('open', ms)
                elif err == errno.ECONNREFUSED:
                    results[key.data] = ('refused', ms)
    finally:
        sel.close()
        for s in socks:
            s.close()
    return results


class Sweep(object):
    """Single-threaded sweep: one kqueue, WINDOW non-blocking connects in flight
    across all (host, port) pairs. No thread pool, no per-host selector, and the
    process fd limit is left alone (raising it inside Pythonista is a risk to
    the host app's own native code)."""

    def __init__(self, net, ports=PORTS, timeout=0.5):
        self.hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
        self.ports = list(ports)
        self.timeout = timeout
        self.total = len(self.hosts)
        self.done = 0
        self.results = {}
        self.lock = threading.Lock()
        self.cancelled = False
        self.running = False
        self.phase = 'idle'
        self.version = 0

    def start(self):
        _log.info('sweep %d hosts x %d ports, window %d', self.total, len(self.ports), WINDOW)
        self.t0 = time.time()
        self.running = True
        self.phase = 'scan'
        threading.Thread(target=self._run, name='sweep', daemon=True).start()

    def _record(self, ip, port, kind, ms):
        with self.lock:
            r = self.results.get(ip)
            if r is None:
                r = self.results[ip] = {'open': [], 'answer': [], 'rtt': ms, 'name': None}
            r['answer'] = sorted(r['answer'] + [port])
            if kind == 'open':
                r['open'] = sorted(r['open'] + [port])
            r['rtt'] = min(r['rtt'], ms)
            self.version += 1

    def _host_finished(self):
        with self.lock:
            self.done += 1
            self.version += 1

    def _run(self):
        try:
            self._scan()
            if not self.cancelled and self.results:
                self.phase = 'names'
                with ThreadPoolExecutor(max_workers=NAME_WORKERS) as ex:
                    for _ in ex.map(self._resolve, list(self.results)):
                        pass
        except Exception:
            _log.exception('sweep failed')
        finally:
            self.phase = 'cancelled' if self.cancelled else 'done'
            _log.info('sweep %s: %d up in %.1fs', self.phase, len(self.results), time.time() - self.t0)
            self.running = False
            with self.lock:
                self.version += 1

    def _scan(self):
        pending = deque((ip, port) for ip in self.hosts for port in self.ports)
        outstanding = dict((ip, len(self.ports)) for ip in self.hosts)
        inflight = {}  # sock -> (ip, port, t0)
        sel = selectors.DefaultSelector()

        def finish(ip):
            outstanding[ip] -= 1
            if outstanding[ip] == 0:
                self._host_finished()

        def drop(s):
            try:
                sel.unregister(s)
            except (KeyError, ValueError):
                pass
            s.close()
            inflight.pop(s, None)

        try:
            while (pending or inflight) and not self.cancelled:
                while pending and len(inflight) < WINDOW:
                    ip, port = pending.popleft()
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    except OSError:
                        pending.appendleft((ip, port))  # out of fds: drain before opening more
                        break
                    s.setblocking(False)
                    t0 = time.perf_counter()
                    rc = s.connect_ex((ip, port))
                    if rc in INPROGRESS:
                        sel.register(s, selectors.EVENT_WRITE)
                        inflight[s] = (ip, port, t0)
                        continue
                    if rc == 0:
                        self._record(ip, port, 'open', (time.perf_counter() - t0) * 1000.0)
                    elif rc == errno.ECONNREFUSED:
                        self._record(ip, port, 'refused', (time.perf_counter() - t0) * 1000.0)
                    s.close()
                    finish(ip)
                if not inflight:
                    continue
                for key, _ in sel.select(0.05):
                    s = key.fileobj
                    ip, port, t0 = inflight[s]
                    err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    ms = (time.perf_counter() - t0) * 1000.0
                    if err == 0:
                        self._record(ip, port, 'open', ms)
                    elif err == errno.ECONNREFUSED:
                        self._record(ip, port, 'refused', ms)
                    drop(s)
                    finish(ip)
                now = time.perf_counter()
                for s, (ip, port, t0) in list(inflight.items()):
                    if now - t0 > self.timeout:
                        drop(s)
                        finish(ip)
        finally:
            for s in list(inflight):
                drop(s)
            sel.close()

    def _resolve(self, ip):
        if self.cancelled:
            return
        try:
            name = socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            return
        with self.lock:
            self.results[ip]['name'] = name
            self.version += 1


def _ip_key(ip):
    return int(ipaddress.ip_address(ip))


@log.instrument
class NetScanView(ui.View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.background_color = theme.BG
        self.ip = local_ip()
        default = str(ipaddress.ip_network(self.ip + '/24', strict=False)) if self.ip else '192.168.1.0/24'
        self.field = ui.TextField()
        self.field.text = default
        self.field.font = (theme.MONO, 15)
        self.field.text_color = theme.TEXT
        self.field.background_color = theme.PANEL
        self.field.bordered = False
        self.field.corner_radius = 8
        self.field.autocorrection_type = False
        self.field.autocapitalization_type = ui.AUTOCAPITALIZE_NONE
        self.field.keyboard_type = getattr(ui, 'KEYBOARD_NUMBERS', ui.KEYBOARD_DEFAULT)
        self.scan_btn = theme.button('Scan', self.toggle_scan, bg=theme.AMBER, fg=theme.BG)
        self.status = theme.label('This device: %s' % (self.ip or 'no route'), 13, theme.MUTED,
                                  align=ui.ALIGN_LEFT)
        self.graph = BarGraph(caption='Connect RTT (ms) by host')
        self.tv = theme.table(self, 56)
        for v in (self.field, self.scan_btn, self.status, self.graph, self.tv):
            self.add_subview(v)
        self.sweep = None
        self.rows = []
        self.seen_version = -1
        self.monitor = None
        self.ticker = Ticker(0.4, self.poll, self)

    def layout(self):
        W, H, p = self.width, self.height, 8
        self.field.frame = (p, p, W - 3 * p - 90, 40)
        self.scan_btn.frame = (W - p - 90, p, 90, 40)
        self.status.frame = (p + 2, 52, W - 2 * p, 18)
        gh = min(180, max(120, H * 0.25))
        self.graph.frame = (p, 74, W - 2 * p, gh)
        y = 74 + gh + p
        self.tv.frame = (0, y, W, max(0, H - y))

    def toggle_scan(self, sender):
        if self.sweep is not None and self.sweep.running:
            self.sweep.cancelled = True
            return
        try:
            net = ipaddress.ip_network(self.field.text.strip(), strict=False)
        except ValueError:
            console.hud_alert('Bad CIDR', 'error')
            return
        if net.version != 4 or net.num_addresses > MAX_HOSTS:
            console.hud_alert('IPv4, /22 or smaller', 'error')
            return
        self.field.end_editing()
        self.rows = []
        self.seen_version = -1
        self.sweep = Sweep(net)
        self.sweep.start()
        self.scan_btn.title = 'Stop'
        self.ticker.start()

    def poll(self):
        sw = self.sweep
        if sw is None:
            return
        with sw.lock:
            version = sw.version
            snap = dict((ip, dict(r)) for ip, r in sw.results.items())
            done = sw.done
        if version != self.seen_version:
            self.seen_version = version
            self.rows = sorted(snap.items(), key=lambda kv: _ip_key(kv[0]))
            self.graph.bars = [(ip.rsplit('.', 1)[-1], r['rtt'], theme.CYAN if r['open'] else theme.MUTED)
                               for ip, r in self.rows]
            self.graph.set_needs_display()
            self.tv.reload()
        up = len(self.rows)
        if sw.phase == 'scan':
            txt = 'Scanning %d/%d · %d up' % (done, sw.total, up)
        elif sw.phase == 'names':
            txt = '%d up · resolving names…' % up
        else:
            txt = '%s · %d up of %d' % ('Stopped' if sw.phase == 'cancelled' else 'Done', up, sw.total)
            if up == 0:
                txt += ' · allow Local Network access?'
        self.status.text = txt
        if not sw.running:
            self.scan_btn.title = 'Scan'
            self.ticker.stop()

    # ---- table ----
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(self.rows)

    def tableview_cell_for_row(self, tv, section, row):
        ip, r = self.rows[row]
        title = ip + ('   ' + r['name'] if r['name'] else '')
        ports = ', '.join(PORT_NAMES.get(p, str(p)) for p in r['open']) or 'up (RST only)'
        if ip == self.ip:
            ports += ' · this device'
        c = theme.cell(title, '%.0f ms · %s' % (r['rtt'], ports))
        c.accessory_type = 'disclosure_indicator'
        return c

    def tableview_did_select(self, tv, section, row):
        if row >= len(self.rows):
            return
        ip, r = self.rows[row]
        ports = r['open'] or r['answer']
        self.monitor = MonitorView(ip, ports, r['name'])
        self.navigation_view.push_view(self.monitor)

    def stop(self):
        if self.sweep is not None:
            self.sweep.cancelled = True
        self.ticker.stop()
        if self.monitor is not None:
            self.monitor.stop()


@log.instrument
class MonitorView(ui.View):
    """Probes one host:port every second; graphs connect RTT, gaps are timeouts."""

    def __init__(self, ip, ports, name=None):
        super().__init__()
        self.background_color = theme.BG
        self.name = name or ip
        self.ip = ip
        self.ports = list(ports)[:5] or [80]
        self.port = self.ports[0]
        self.seg = ui.SegmentedControl()
        self.seg.segments = [PORT_NAMES.get(p, str(p)) for p in self.ports]
        self.seg.selected_index = 0
        self.seg.action = self.on_seg
        self.seg.tint_color = theme.AMBER
        self.graph = LineGraph(caption='', y_min=0, capacity=90)
        self.stats_lbl = theme.label('', 13, theme.TEXT, mono=True, lines=2)
        for v in (self.seg, self.graph, self.stats_lbl):
            self.add_subview(v)
        self.samples = deque(maxlen=90)
        self.busy = False
        self.offscreen = 0
        self._caption()
        self.ticker = Ticker(1.0, self.tick, self)
        self.ticker.start()

    def _caption(self):
        self.graph.caption = '%s:%d RTT (ms)' % (self.ip, self.port)

    def layout(self):
        W, H, p = self.width, self.height, 8
        self.seg.frame = (p, p, W - 2 * p, 32)
        self.graph.frame = (p, 48, W - 2 * p, max(160, H * 0.45))
        self.stats_lbl.frame = (p, self.graph.frame[1] + self.graph.frame[3] + p, W - 2 * p, 40)

    def on_seg(self, sender):
        self.port = self.ports[sender.selected_index]
        self.samples.clear()
        self._caption()

    def _probe(self, port):
        try:
            r = probe_host(self.ip, [port], 1.0).get(port)
            if port == self.port:
                self.samples.append(r[1] if r else None)
        finally:
            self.busy = False

    def tick(self):
        # Self-stopping: once popped off the nav stack, kill the timer.
        if not self.on_screen:
            self.offscreen += 1
            if self.offscreen >= 2:
                self.stop()
            return
        self.offscreen = 0
        if not self.busy:
            self.busy = True
            threading.Thread(target=self._probe, args=(self.port,), daemon=True).start()
        vals = list(self.samples)
        self.graph.series = [('rtt', theme.CYAN, vals)]
        self.graph.set_needs_display()
        ok = [v for v in vals if v is not None]
        if ok:
            self.stats_lbl.text = 'last %s  min %.0f  avg %.0f  max %.0f\nloss %d/%d (%.0f%%)' % (
                '%.0f' % vals[-1] if vals[-1] is not None else '--', min(ok), sum(ok) / len(ok), max(ok),
                len(vals) - len(ok), len(vals), 100.0 * (len(vals) - len(ok)) / len(vals))
        else:
            self.stats_lbl.text = 'waiting…' if not vals else 'no answer (%d tries)' % len(vals)

    def stop(self):
        self.ticker.stop()


def make_view():
    return NetScanView()
