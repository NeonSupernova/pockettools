"""Electrical math: wire sizing (voltage drop + ampacity), fuses, Ohm's law.

Pure Python, no UI, so it can be tested off-device.

Wire sizing, the usual way:
1. Current: I = P / V (or measured).
2. Voltage-drop size: CM = K * I * (2L) / Vdrop. 2L because current goes out
   and back. K is the resistivity of the conductor in ohm-circular-mil per
   foot: ~10.8 copper, ~17.0 aluminium (near room temperature; hot wire is
   worse, ~12.9 Cu at 75 C).
3. Ampacity size: from a table (no formula). The wire must carry the fuse
   rating, since the fuse is what protects the wire.
4. Use whichever is thicker. At 12 V, voltage drop nearly always decides.

AWG: d(mils) = 5 * 92 ** ((36 - n) / 39), CM = d**2. n = 0, -1, -2, -3 are
1/0, 2/0, 3/0, 4/0. Anchors: 10 AWG ~ 1 mOhm/ft copper; resistance doubles
every 3 gauges.
"""
import math

K = {'copper': 10.8, 'aluminum': 17.0}

# Sizes people can actually buy, thin -> thick.
SIZES = [22, 20, 18, 16, 14, 12, 10, 8, 6, 4, 3, 2, 1, 0, -1, -2, -3]

# Single conductor in free air (chassis wiring). Optimistic for harness bundles
# and hot engine bays: apply a derate.
CHASSIS_CU = {22: 7, 20: 11, 18: 16, 16: 22, 14: 32, 12: 41, 10: 55, 8: 73, 6: 101,
              4: 135, 3: 158, 2: 181, 1: 211, 0: 245, -1: 283, -2: 328, -3: 380}

# NEC Table 310.16, 75 C column, with the 240.4(D) small-conductor limits
# (14 Cu 15 A, 12 Cu 20 A, 10 Cu 30 A; 12 Al 15 A, 10 Al 25 A). 18/16 AWG
# aren't building branch-circuit sizes, so they're absent.
NEC75_CU = {14: 15, 12: 20, 10: 30, 8: 50, 6: 65, 4: 85, 3: 100, 2: 115, 1: 130,
            0: 150, -1: 175, -2: 200, -3: 230}
NEC75_AL = {12: 15, 10: 25, 8: 40, 6: 50, 4: 65, 3: 75, 2: 90, 1: 100,
            0: 120, -1: 135, -2: 155, -3: 180}

TABLES = {
    'chassis': ('Chassis (free air)', {'copper': CHASSIS_CU,
                                       # no common published Al chassis table: ~78% of Cu
                                       'aluminum': dict((n, int(a * 0.78)) for n, a in CHASSIS_CU.items())}),
    'nec': ('NEC 310.16 75°C', {'copper': NEC75_CU, 'aluminum': NEC75_AL}),
}

# Blade (ATO/mini) then MAXI / bolt-down sizes.
FUSES = [1, 2, 3, 4, 5, 7.5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 70, 80, 100, 125,
         150, 175, 200, 225, 250, 300, 350, 400]


def label(n):
    return '%d/0' % (1 - n) if n <= 0 else str(n)


def diameter_mils(n):
    return 5.0 * 92 ** ((36 - n) / 39.0)


def cmils(n):
    return diameter_mils(n) ** 2


def awg_from_cm(cm):
    """Fractional AWG for a circular-mil area (36 - 39*log92(d/5))."""
    return 36 - 39 * math.log(math.sqrt(cm) / 5.0, 92)


def ohms_per_ft(n, material='copper'):
    return K[material] / cmils(n)


def drop(current, one_way_ft, n, material='copper'):
    """Volts lost in the round trip (2L) of gauge n."""
    return current * ohms_per_ft(n, material) * 2 * one_way_ft


def required_cm(current, one_way_ft, vdrop, material='copper'):
    return K[material] * current * 2 * one_way_ft / vdrop


def size_for_cm(cm):
    """Thinnest buyable size with at least cm circular mils (None: thicker than 4/0)."""
    for n in SIZES:
        if cmils(n) >= cm:
            return n
    return None


def ampacity(n, table='chassis', material='copper', derate=1.0):
    a = TABLES[table][1][material].get(n)
    return None if a is None else a * derate


def size_for_amps(amps, table='chassis', material='copper', derate=1.0):
    for n in SIZES:
        a = ampacity(n, table, material, derate)
        if a is not None and a >= amps:
            return n
    return None


def fuse_for(current, factor=1.25):
    need = current * factor
    for f in FUSES:
        if f >= need - 1e-9:
            return f
    return None


def thicker(a, b):
    """Thicker of two gauges (smaller AWG number); None means 'beyond the table'."""
    if a is None or b is None:
        return None
    return min(a, b)


def size_wire(volts, one_way_ft, drop_pct, current=None, watts=None, material='copper',
              table='chassis', derate=1.0, fuse_factor=1.25):
    """Everything needed to pick a wire. Pass current or watts."""
    if current is None:
        if not watts or not volts:
            raise ValueError('need current, or watts and volts')
        current = watts / float(volts)
    if current <= 0 or volts <= 0 or one_way_ft <= 0 or drop_pct <= 0:
        raise ValueError('current, volts, length and drop % must be positive')
    vdrop = volts * drop_pct / 100.0
    cm = required_cm(current, one_way_ft, vdrop, material)
    by_drop = size_for_cm(cm)
    fuse = fuse_for(current, fuse_factor)
    by_amps = size_for_amps(fuse if fuse else current, table, material, derate)
    pick = thicker(by_drop, by_amps)
    if by_drop is None or by_amps is None:
        decides = 'beyond 4/0: split the run or raise the voltage'
    elif by_drop == by_amps:
        decides = 'both limits agree'
    elif pick == by_drop:
        decides = 'voltage drop decides'
    else:
        decides = 'ampacity decides'
    return {'current': current, 'watts': current * volts, 'volts': volts, 'one_way_ft': one_way_ft,
            'drop_pct': drop_pct, 'vdrop_max': vdrop, 'cm_needed': cm, 'awg_exact': awg_from_cm(cm),
            'by_drop': by_drop, 'fuse': fuse, 'by_amps': by_amps, 'pick': pick, 'decides': decides,
            'material': material, 'table': table, 'derate': derate}


def gauge_rows(r, span=3):
    """Comparison rows around the pick: a few thinner and a few thicker sizes."""
    ref = r['pick'] if r['pick'] is not None else SIZES[-1]
    i = SIZES.index(ref)
    out = []
    for n in SIZES[max(0, i - span):i + span]:
        v = drop(r['current'], r['one_way_ft'], n, r['material'])
        amp = ampacity(n, r['table'], r['material'], r['derate'])
        out.append({'awg': n, 'cm': cmils(n), 'mohm_ft': 1000 * ohms_per_ft(n, r['material']),
                    'vdrop': v, 'pct': 100 * v / r['volts'], 'v_load': r['volts'] - v,
                    'loss_w': v * r['current'], 'amp': amp,
                    'amp_ok': amp is not None and r['fuse'] is not None and amp >= r['fuse'],
                    'drop_ok': v <= r['vdrop_max'] + 1e-9, 'pick': n == r['pick']})
    return out


def ohms_law(v=None, i=None, r=None, p=None):
    """Give exactly two of V, I, R, P; returns all four."""
    given = dict((k, x) for k, x in (('v', v), ('i', i), ('r', r), ('p', p)) if x is not None)
    if len(given) != 2:
        raise ValueError('enter exactly two of V, I, R, P')
    if any(x < 0 for x in given.values()):
        raise ValueError('values must be positive')
    k = ''.join(sorted(given))
    if k == 'iv':
        v, i = given['v'], given['i']
        r, p = (v / i if i else float('inf')), v * i
    elif k == 'rv':
        v, r = given['v'], given['r']
        i = v / r
        p = v * i
    elif k == 'pv':
        v, p = given['v'], given['p']
        i = p / v
        r = v / i if i else float('inf')
    elif k == 'ir':
        i, r = given['i'], given['r']
        v = i * r
        p = v * i
    elif k == 'ip':
        i, p = given['i'], given['p']
        v = p / i
        r = v / i
    else:  # 'pr'
        p, r = given['p'], given['r']
        i = math.sqrt(p / r)
        v = i * r
    return {'v': v, 'i': i, 'r': r, 'p': p}


def meter(clamp_amps, loops=1, v_battery=None, v_load=None, one_way_ft=None, inrush_amps=None,
          material='copper'):
    """Turn clamp-meter and voltmeter readings into wiring facts."""
    if loops < 1:
        raise ValueError('loops must be 1 or more')
    i = clamp_amps / float(loops)
    out = {'current': i, 'loops': loops}
    if inrush_amps is not None:
        out['inrush'] = inrush_amps / float(loops)
        out['inrush_ratio'] = out['inrush'] / i if i else None
    if v_battery is not None and v_load is not None:
        d = v_battery - v_load
        out['vdrop'] = d
        out['pct'] = 100 * d / v_battery if v_battery else None
        out['load_watts'] = v_load * i
        out['loss_w'] = d * i
        if i > 0 and d > 0:
            rt = d / i
            out['r_circuit'] = rt
            if one_way_ft:
                per_ft = rt / (2 * one_way_ft)
                out['mohm_ft'] = per_ft * 1000
                out['awg_equiv'] = awg_from_cm(K[material] / per_ft)
    return out
