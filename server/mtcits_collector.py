#!/usr/bin/env python3
"""Live MTC buses from MTC's own commuter site (commuter.mtcbusits.in, "Track a bus"), politely.

    mtcits_collector.py --stops route_stops.json --timings timings.json --stop-dist stop_dist.json \
        --archive DIR --out vehicles.json [--rate 2] [--watch watch.json] [--model model.json]

The site is open to everyone, no login. Its gateway answers, per route direction, every bus on
it: fleet number, operator, service type, position, heading, GPS time, and for every stop MTC's
own predicted arrival, the scheduled time and the actual time once passed:

  SearchRoute_v2 {"routetext": x}            route directions matching x, with active_fleets
  SearchByRouteDetails_v4 {"routeid": dir}   the buses on that direction, stop by stop

There is no all-fleet call, so which direction to ask next is chosen as in the TGSRTC collector
(tsrtc/live.py): every request goes where the map is most wrong.
  - Each bus's GPS report interval and clock lag are learnt from its successive fixes; a direction
    is not worth asking until at least one of its buses should have a new report.
  - Of those, the direction asked is the one whose buses' positions are most wrong now:
    sum over its buses of (speed + AGEW m/s) x age of the fix (speed from its last two fixes, at
    least FLOOR). Busy, fast directions are refreshed most; quiet ones still within MAXGAP.
  - A direction that answers nothing newer backs off (30 s, 1, 2, 4, 8, 15 min).
  - Directions a rider has open on the map (--watch, from report_api.py) are asked every
    HOT_EVERY s, at most HOT_MAX of them.
  - Which directions have buses at all is re-read every DISCOVER s (36 small searches).
--rate is a ceiling, not a target: at night, with few buses out, far fewer requests are made.
A 429 pauses the collector for 10 minutes; a 5xx for 30 s, doubling while they continue.

Each bus is matched to one of our workings (route number, position, heading, as for Chalo) and
MTC's predicted arrivals are moved onto that working's stops by position. The output and the
obs/speed archive are live_collector.py's, so the map, Traffic layer and dashboard carry on.
Also kept in --archive:
  stops-YYYY-MM-DD.jsonl   each stop a bus passed, once: {t, reg, route, trip (scheduled start),
                           st (MTC station id), sch, act} -> stop-level punctuality
  trips-YYYY-MM-DD.jsonl   each operated trip once, with MTC's schedule at every stop: {t, dir, route,
                           start, end, reg, service, stops: [[station, sch arr, sch dep]]}
                           -> the timetable (MTC's own timetable API fails server-side)
  fleet.json               fleet number -> vehicle id, service type, operator, GPS device, last route
  directions.json          each direction's MTC stop list as last seen (for validate_routes_mtcits.py)
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import re
import string
import sys
import threading
import time
import urllib.error
import ssl
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_collector import ETA_KEEP, IST, Collector, log  # noqa: E402
from chalo_collector import ChaloCollector, norm  # noqa: E402
import nowcast  # noqa: E402

BASE = 'https://mobilegatewayapi.mtcbusits.in/passangermobileapi/WebAPI/'
HEAD = {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*', 'lan': 'en',
        'deviceType': 'WEB', 'Origin': 'https://commuter.mtcbusits.in', 'Referer': 'https://commuter.mtcbusits.in/',
        'User-Agent': 'Mozilla/5.0 (MTCMap/1.0; +https://maps.reclaimchennai.city/mapsv2/; civic transit map)'}
DISCOVER = 600       # s between re-reads of which directions have buses
MAXGAP = 600         # s: every direction with buses is asked at least this often
MAXGAP_LIVE = 240    # s: ... and one with a bus that is transmitting (a fix under LIVE_FIX old) this often
LIVE_FIX = 600       # s
SAME_CAP_LIVE = 90   # s: longest 'same answer' backoff while a bus is transmitting (MTC had not got its next fix yet)
MINGAP = 20          # s: never ask one direction twice within this
MAX_V = 12.0         # m/s: fastest a bus is taken to get from its fix to a stop (~43 km/h, stop to stop)
FWD_M = 40           # m forward along its working between two fixes: driving its trip
EARLY_M = 400        # m along its working: a bus this far from its first stop before its start time has left early
FLOOR = 1.0          # m/s a bus is assumed to drift, at least (it may have started moving)
AGEW = 1.5           # m/s added for every bus: an old fix is itself a cost (riders read its age)
CAD0, SLACK = 20.0, 3.0
DEPOT_M, DEPOT_CORE, DEPOT_STILL = 350, 150, 300   # in a depot: within 350 m of one (yards are big), standing
# (within 40 m) for 5 min, 10 min beyond 150 m of its middle
HOT_EVERY, HOT_MAX = 15, 16
GONE = 1800          # s: a bus not re-reported this long leaves the map
# Buses with GPS but no trip in MTC's system (its Track a Bus shows "N/A" to "N/A") are on no
# direction's answer; they are read one by one (VehicleTripDetails_v2), at --sweep-rate:
LOOSE_MOVING = 45    # s: re-ask a trip-less bus that is moving this often (at most)
LOOSE_STILL = 180    # s: ... one with a fresh fix that is standing (at 600, buses that set off for the
                     # depot sat 5-7 km from their GPS for 10 min)
LOOSE_IDLE = 5400    # s: ... any other (offline, no route number, at a depot): round robin
LOOSE_FRESH = 900    # s: a trip-less fix older than this is not shown
# Chalo (the Chennai One app's backend, production.zophop.com, one route variant a call) carries the same
# bus GPS, often seconds fresher, plus positions for some buses MTC lists offline and route numbers for
# buses MTC has none for. Its route labels disagree with MTC's trip on ~25% of buses, so MTC's route,
# trip and ETAs win; Chalo moves a bus only with a newer fix (and within CHALO_NEAR of MTC's), or
# shows one MTC has no fresh position for.
CHALO_NEAR = 3000    # m
CHALO_FRESH = 600    # s: a Chalo-only position older than this is not shown (Chalo keeps hours-old ones)
VIDS_EVERY = 6 * 3600  # s between re-reads of the whole fleet's vehicle ids (ListVehicles 0-9)
DEPART = (-120, 900) # s around a bus's scheduled trip start when it is about to leave a terminus
DEPART_V = 8.0       # m/s assumed for a standing bus in that window: its departure is news (riders wait for it)


def call(path, body, timeout=40):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), HEAD)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=TLS) as r:
            return r.status, json.loads(r.read() or b'null')
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


# one TLS context for every request: urlopen() built a new one each call and reloaded the CA bundle, 61% of
# the collector's CPU (py-spy, 17 Nov), which starved the Chalo workers under the GIL (1.6 of 4 req/s)
TLS = ssl.create_default_context()


def ist(s, fmt):
    try:
        return int(dt.datetime.strptime(s, fmt).replace(tzinfo=IST).timestamp())
    except (TypeError, ValueError):
        return None


def fix_still(s):
    """Seconds the bus has stood within 40 m of one place, up to its latest fix."""
    return s['t'] - s['still'][2] if s.get('still') and 't' in s else 0


def hav(la1, lo1, la2, lo2):
    p = math.pi / 180
    a = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(min(1.0, a)))


def flags_of(stype, rno, known):
    s = (stype or '').upper()
    # MTC's service type, when there is one, is the whole truth: flags remembered from the archive
    # (last write wins, and Chennai One's labels once put 'a' on non-AC Deluxe J1374 and 'x' on
    # Ordinary J1614) are only for a bus with no type at all. MTC has no diesel AC bus: every AC is EV.
    f = set() if s else set(known) - set('unho')
    f |= {'a'} if 'AC' in s.replace('(', ' ').split() or s.startswith('AC') else set()
    f |= {'p', 'e', 'a', 'l'} if 'PREMIUM' in s else set()
    f |= {'e'} if 'EV' in s else set()
    f |= {'x'} if 'EXP' in s else set()
    # low floor with a ramp: MTC's legend marks LF, every EV (AC EV, Deluxe EV) and Premium accessible
    f |= {'l'} if 'LOW FLOOR' in s or 'LF' in s.split() or 'EV' in s or 'PREMIUM' in s else set()
    f |= {'d'} if 'DELUXE' in s else set()       # MTC's Deluxe fare class (diesel or EV)
    f |= {'s'} if 'SMALL' in s else set()        # Small Bus (Ordinary / Express)
    f |= {'w'} if 'LADIES' in s else set()       # Ladies Only
    if re.match(r'P\d', rno or ''):
        f |= {'p', 'e', 'l'}                     # MTC Premium: the OHM-run Switch EiV12 fleet, electric AC
    return ''.join(sorted(f))


class ItsCollector(ChaloCollector):
    def __init__(self, a):
        Collector.__init__(self, a)             # not ChaloCollector's: no Chalo route list here
        rs = json.load(open(a.stops))
        self.by_sn = {}
        for c, r in rs.items():
            self.by_sn.setdefault(norm(r['sn']), []).append(c)
        self.stop_ids = {c: [s[0] for s in r['d'][0]['s']] for c, r in rs.items()}
        self.fleet = self.load_fleet(a.archive)
        self.pause_until = 0
        self.model = nowcast.Model(a.model) if a.model else None
        self.mlock = threading.Lock()
        self.track = {}
        self.day = None
        self.dirs = {}       # direction id -> {'n': active buses, 'rno': route no, 'polled', 'same', 'busy'}
        self.bus = {}        # fleet no -> {t, lat, lon, v, cad, lag, dir}
        self.passed = {}     # (reg, trip start, station) already archived today
        self.reg = {}        # fleet registry
        self.fleet_path = os.path.join(a.archive, 'fleet.json') if a.archive else ''
        if self.fleet_path and os.path.exists(self.fleet_path):
            self.reg = json.load(open(self.fleet_path))
        self.slock = threading.Lock()
        self.stmap = {}      # (direction, code) -> {station id: our stop index}
        self.sent = [0, time.time()]
        self.dir_t = collections.deque(maxlen=5000)   # times of direction requests: the sweep takes what they leave
        self.diag = collections.Counter()
        self.far = []                           # samples of buses whose MTC next stop was far from their GPS
        self.depot_path = os.path.join(os.path.dirname(os.path.abspath(a.archive or '.')), 'depots.json')
        self.depots, self.depot_mtime = [], 0
        self.vids ={r: v['vid'] for r, v in self.reg.items() if v.get('vid')}   # fleet no -> vehicle id
        self.loose = {}      # fleet no -> {'asked', 'every'}: the trip-less sweep's own schedule
        self.cvar = {}       # Chalo variant id -> {'v': variant, 'polled', 'n', 'empty', 'w'}
        if getattr(a, 'chalo_routes', ''):
            self.cvar = {v['route_id']: {'v': v, 'polled': 0, 'n': 1, 'empty': 0, 'w': 1.0}
                         for v in json.load(open(a.chalo_routes))}

    def discover(self):
        found = {}
        for x in list(string.digits) + list(string.ascii_uppercase):
            c, j = call('SearchRoute_v2', {'routetext': x})
            self.sent[0] += 1
            if c == 429 or c >= 500:
                self.pause_until = time.time() + 600
                log(f'MTC answered {c}: pausing 10 min')
                return
            for d in (j or {}).get('data') or []:
                found[d['routeparentid']] = (d.get('active_fleets') or 0, (d.get('routeno') or '').split('-')[0].strip())
            time.sleep(1.0 / self.a.rate)
        with self.slock:
            for k, (n, rno) in found.items():
                d = self.dirs.setdefault(k, {'polled': 0, 'same': 0, 'busy': 0})
                d['n'], d['rno'] = n, rno
        log(f'{len(found)} directions, {sum(1 for n, _ in found.values() if n)} with buses '
            f'({sum(n for n, _ in found.values())})')

    def score(self, k, d, now, hot, live_dirs=()):
        if now - d.get('busy', 0) < 60:
            return None
        since = now - d['polled']
        if hot and since >= HOT_EVERY:
            return 2e9 + since
        if not d.get('n') and k not in live_dirs:
            return None
        if since < MINGAP:
            return None
        # overdue (or never asked): oldest first. Each direction's own limit, 0.7-1.3 x MAXGAP, so they
        # do not all fall due together (after a restart they did, every 10 min, starving moving buses)
        live = any(now - self.bus[b]['t'] < LIVE_FIX for b in d.get('buses', ()) if b in self.bus)
        if since >= (MAXGAP_LIVE if live else MAXGAP) * (0.7 + 0.6 * ((k * 2654435761) % 1000) / 1000):
            return 1e9 + since
        # a bus standing at its terminus around its trip start is about to leave: no same-answer
        # backoff, and it counts as moving (a parked bus otherwise waits up to MAXGAP to be re-read)
        leaving = [b for b in d.get('buses', ()) if b in self.bus and
                   DEPART[0] <= now - self.bus[b].get('start', -1e12) <= DEPART[1] and self.bus[b].get('v', 0) < FLOOR]
        # an unchanged answer: back off, briefly while a bus is transmitting (its next fix is on the way;
        # up to 900 s here held moving buses ~10 min old while MTC had them seconds old)
        if d['same'] and not leaving and since < min(SAME_CAP_LIVE if live else 900, 30 * 2 ** (d['same'] - 1)):
            return None
        tot, due = 0.0, False
        for b in d.get('buses', ()):
            s = self.bus.get(b)
            if not s:
                continue
            if now >= s['t'] + s.get('lag', 0) + s.get('cad', CAD0) + SLACK:
                due = True
            v = DEPART_V if b in leaving else max(FLOOR, s.get('v', 0))
            tot += (v + AGEW) * (now - max(s['t'], s.get('ct', 0)))      # ct: a newer fix from Chalo
        return tot if due else None

    def poll_dir(self, k):
        c, j = call('SearchByRouteDetails_v4', {'routeid': k, 'servicetypeid': 0})
        self.sent[0] += 1
        self.dir_t.append(time.time())
        now = time.time()
        # 429: MTC asks us to slow down - 10 min. A 5xx is usually one bad answer: back off 30 s,
        # doubling while they keep coming (to 10 min); one good answer resets it
        if c == 429 or c >= 500:
            self.errs = getattr(self, 'errs', 0) + 1
            wait = 600 if c == 429 else min(600, 30 * 2 ** (self.errs - 1))
            self.pause_until = max(self.pause_until, now + wait)
            log(f'MTC answered {c}: pausing {wait} s')
        elif c == 200:
            self.errs = 0
        with self.slock:
            d = self.dirs[k]
            d['busy'], d['polled'] = 0, now
        if c != 200 or not isinstance(j, dict):
            return
        seen = {}
        stations = []
        # side -> its last station: a bus that has covered it has finished its trip
        last = {sd: ((j.get(sd) or {}).get('data') or [{}])[-1].get('stationid') for sd in ('up', 'down')}
        first = {sd: ((j.get(sd) or {}).get('data') or [{}])[0].get('stationid') for sd in ('up', 'down')}
        for side in ('up', 'down'):
            for st in (j.get(side) or {}).get('data') or []:
                stations.append(st)
                for v in st.get('vehicleDetails') or []:
                    r = seen.setdefault(v['vehiclenumber'], {'v': v, 'eta': [], 'rno': st.get('routeno'), 'sch': [], 'side': side})
                    sid = st.get('stationid')
                    r['sch'].append((sid, v.get('sch_arrivaltime'), v.get('sch_departuretime')))
                    e = ist(v.get('eta'), '%Y-%m-%d %H:%M:%S')
                    if e:
                        r['eta'].append((sid, e))
                    if v.get('stopCoveredStatus') == 1 and v.get('actual_arrivaltime'):
                        r.setdefault('passed', []).append((sid, v.get('sch_arrivaltime'), v['actual_arrivaltime']))
                        if sid == last.get(side):
                            r['done'] = True
        ll = {st.get('stationid'): (st.get('centerlat'), st.get('centerlong')) for st in stations}
        rows, newer, stops, trips = [], 0, [], []
        for reg, r in seen.items():
            v = r['v']
            lat, lon = v.get('centerlat'), v.get('centerlong')
            fix = ist(v.get('lastrefreshon'), '%d-%m-%Y %H:%M:%S')
            if not (lat and lon and fix and 12.3 < lat < 13.7 and 79.5 < lon < 80.5):
                continue
            rno = r['rno'] or self.dirs[k].get('rno')
            with self.slock:
                s = self.bus.get(reg)
                if s and fix > s['t']:
                    gap = fix - s['t']
                    if gap <= 1.6 * s.get('cad', CAD0):
                        s['cad'] = min(300.0, max(8.0, 0.7 * s.get('cad', CAD0) + 0.3 * gap))
                    if gap < 900:
                        s['v'] = min(25.0, hav(s['lat'], s['lon'], lat, lon) / gap)
                    lag = now - fix
                    s['lag'] = lag if 'lag' not in s else min(lag, 0.8 * s['lag'] + 0.2 * lag)
                    newer += 1
                elif not s:
                    s = self.bus[reg] = {'cad': CAD0, 'lag': max(0.0, now - fix)}
                    newer += 1
                if fix >= s.get('t', 0):
                    if 'still' not in s or hav(s['still'][0], s['still'][1], lat, lon) > 40:
                        s['still'] = (lat, lon, fix)        # where and since when the bus has stood
                    s.update(t=fix, lat=lat, lon=lon, dir=k)
                st0 = ist(dt.datetime.now(IST).strftime('%d-%m-%Y ') + (v.get('sch_tripstarttime') or ''), '%d-%m-%Y %H:%M')
                if st0:
                    s['start'] = st0
                self.reg[reg] = {**self.reg.get(reg, {}), 'vid': v.get('vehicleid'), 'service': v.get('servicetype'),
                                 'operator': v.get('agencyname'), 'device': v.get('deviceuniqueid'), 'route': rno,
                                 'last': dt.datetime.now(IST).strftime('%Y-%m-%d %H:%M')}
                tk = (k, v.get('sch_tripstarttime'))       # MTC's timetable, one line per operated trip
                if tk[1] and tk not in self.passed and any(x[1] for x in r['sch']):
                    self.passed[tk] = 1
                    trips.append({'t': fix, 'dir': k, 'route': rno, 'start': tk[1], 'end': v.get('sch_tripendtime'),
                                  'reg': reg, 'service': v.get('servicetype'), 'stops': r['sch']})
                for sid, sch, act in r.get('passed', []):
                    key = (reg, v.get('sch_tripstarttime'), sid)
                    if key not in self.passed:
                        self.passed[key] = 1
                        stops.append({'t': fix, 'reg': reg, 'route': rno, 'trip': v.get('sch_tripstarttime'),
                                      'st': sid, 'sch': sch, 'act': act})
            # the working is the direction's (whose stops are MTC's stations), not the nearest by
            # position: "19A R2" and "19A" share a number but not a road
            code = self.working(k, rno, ll)
            eta, m, off, depot, early = [], None, False, False, False
            if code:
                idx = self.station_index(k, code, ll)
                eta = [[i, e] for i, e in sorted((idx[sid], e) for sid, e in r['eta'] if sid in idx)]
                m = self.locate(code, lat, lon)
                if self.in_depot(s, eta, code):     # standing in a depot: no stops, no times
                    eta, depot = [], True
                    self.diag['in depot'] += 1
                elif st0 and st0 > now and m is not None and self.ran_early(s, code, st0, m):
                    early = True                    # left before its timetabled start (K1244 on 14M, 17 Nov
                    self.diag['running ahead of timetable'] += 1   # 16:53-17:06: shown waiting while it ran)
                elif eta and st0 and st0 > now:     # its trip has not started: no stop before its timetabled time
                    # (MTC times a bus laying over at the start as if it had left: arrivals 20-40 min early)
                    day = dt.datetime.now(IST).strftime('%d-%m-%Y ')
                    schi = {}
                    for sid, sa, sd in r['sch']:
                        tt = ist(day + (sd or sa or ''), '%d-%m-%Y %H:%M')
                        if sid in idx and tt:
                            schi[idx[sid]] = tt
                    held = [[i, max(e, schi.get(i, e))] for i, e in eta]
                    self.diag['eta held to timetable'] += held != eta
                    # MTC does not list a waiting bus under the station it starts from, so it read as starting from
                    # the second (K0981 at Avadi B.T, "starts from Avadi Tube Products", 18 Nov): its side's first
                    # station, at the timetabled start
                    i0 = idx.get(first.get(r['side']))
                    if i0 is not None and i0 < held[0][0]:
                        held = [[i0, max(st0, now)]] + held
                        self.diag['first station added'] += 1
                    eta = held
                if eta:
                    fit = self.fit_eta(code, eta, m, lat, lon, fix)
                    self.diag['eta trimmed'] += fit is not None and len(fit) < len(eta)
                    if fit is None:               # MTC's trip progress is stuck elsewhere: no stops, no times
                        eta, off = [], True
                        self.diag['eta far'] += 1
                    else:
                        eta = self.cap_speed(code, fit, m, fix)
                if m and self.model is not None:
                    with self.mlock:
                        self.learn(reg, code, m[0], m[1], fix)
            else:
                m0 = self.match(rno, lat, lon, v.get('heading'), s.get('v', 0) < 0.5)
                code = m0[0] if m0 else 'c:' + (rno or '?')
            waiting = (ist(dt.datetime.now(IST).strftime('%d-%m-%Y ') + (v.get('sch_tripstarttime') or ''),
                                                    '%d-%m-%Y %H:%M') or 0) > now and not early or r.get('done')
            # no usable MTC times (all behind the bus, or its trip progress elsewhere) but driving forward along
            # its working: our travel model's times, so the map moves it on instead of freezing it at a fix 2-3
            # min old (185 of 2,400 shown buses, 18 Nov)
            fw = code and m is not None and self.forward(s, code, m, fix)
            if fw and not eta and not depot and not waiting:
                eta = self.model_eta((code, m[0], m[1]), fix)
                self.diag['eta from model'] += bool(eta)
            row = [reg, code, round(lat, 5), round(lon, 5), v.get('heading') or 0, fix,
                   flags_of(v.get('servicetype'), rno, self.fleet.get(reg, '')) + ('u' if waiting else '') + ('o' if off else '') + ('p' if depot else ''), eta]
            if not code.startswith('c:') and self.key(rno) == 'P' + norm(rno):
                row.append(rno)
            rows.append(row)
            self.diag['bus'] += 1
            self.diag['mtc eta'] += bool(r['eta'])
            self.diag['matched'] += bool(m)
            self.diag['eta out'] += bool(eta)
        with self.slock:
            d = self.dirs[k]
            if stations:                        # MTC's stop list for this direction (route validation)
                d['stations'] = [[st.get('stationid'), st.get('stationname'), st.get('centerlat'), st.get('centerlong')]
                                 for st in stations]
                d['seen'] = int(now)
            d['buses'] = list(seen)
            d['n'] = len(seen)
            d['same'] = 0 if newer else d['same'] + 1
        if self.a.archive:
            self.record(rows)
            day = dt.datetime.now(IST).strftime('%Y-%m-%d')
            for name, xs in (('stops', stops), ('trips', trips)):
                if xs:
                    with self.alock, open(os.path.join(self.a.archive, f'{name}-{day}.jsonl'), 'a') as fh:
                        fh.write(''.join(json.dumps(x, separators=(',', ':')) + '\n' for x in xs))
        with self.lock:
            self.state[k] = (now, rows)
            for r in rows:
                self.state.pop(('loose', r[0]), None)

    def working(self, k, rno, ll):
        """Our working for direction k: of its route number's workings whose stops pair up with at least 3
        and a third of MTC's stations, the one with most pairs plus half the stations its pairs span. Pairs
        alone chose a short working inside a long direction (18D Keelkattalai-Royapuram got Saidapet-Royapuram,
        17 Nov: L0217 in Madipakkam was off its working, drawn frozen at old fixes); 38 of 3,526 changed.
        Searched under every route number the name may stand for, plus M<k> (work_mtc's working on this
        direction's stations, which wins ties; built on an older station list it can pair fewer): "55K CT4"
        Airport Metro-Keerapakkam searched 55K alone and got Tambaram West-Guduvanchery, so L0016 waiting at
        the airport was shown to start at Tambaram West (18 Nov)."""
        if ('w', k) in self.stmap:
            return self.stmap[('w', k)]
        sts, cand, own = list(ll), [], f'M{k}'
        for cd in dict.fromkeys(self.codes(rno) + ([own] if own in self.stopll else [])):
            idx = self.station_index(k, cd, ll)
            if len(idx) >= max(3, len(ll) / 3):
                pos = [sts.index(sid) for sid in idx]
                cand.append((len(idx) + (max(pos) - min(pos) + 1) / 2, cd == own, len(idx), cd))
        out = max(cand)[3] if cand else None
        self.stmap[('w', k)] = out
        return out

    def cap_speed(self, code, eta, m, fix):
        """No stop sooner than the bus could reach it from its fix at MAX_V: the map moves a bus along through
        these times, and an early one raced buses ahead of where they stood (L0130, 17 Nov: fix 148 m from
        MTC's live GPS, drawn 1.4 km off)."""
        ds = self.sdist.get(code)
        if not ds or m is None or not eta:
            return eta
        out, t, here = [], fix, m[1]
        for i, e in eta:
            if i < len(ds):
                t = max(t, fix) + max(0.0, ds[i] - here) / MAX_V
                here = max(here, ds[i])
                e = max(e, round(t))
                t = e
            out.append([i, e])
        if out != eta:
            self.diag['eta capped to 12 m/s'] += 1
        return out

    def model_eta(self, m0, fix):
        """Our own arrival estimates (travel model) for a moving bus with no MTC trip, so the map moves it along
        its working instead of freezing it at an old fix: 37% of shown buses were frozen, p50 345 m / p90 1.7 km
        off MTC's live GPS against 189 / 510 m for those moved by ETAs (17 Nov, fresh2.py)."""
        if self.model is None or not m0 or m0[0] not in self.sdist:
            return []
        code, k, d = m0
        with self.mlock:
            return [[i, round(t)] for i, t in self.model.eta(self.stop_ids[code], self.sdist[code], d, k, fix)]

    @staticmethod
    def forward(s, code, m, fix):
        """Moved FWD_M or more forward along this working since its last fix on it (a bus on the same road the
        other way, or standing, does not)."""
        prev = s.get('fw')
        if prev and prev[0] == code and fix == prev[2]:
            return prev[3]                      # the same fix polled again: the same answer
        ok = bool(prev and prev[0] == code and 0 < fix - prev[2] <= 600 and m[1] - prev[1] >= FWD_M)
        s['fw'] = (code, m[1], fix, ok)
        return ok

    @staticmethod
    def ran_early(s, code, st0, m):
        """A bus whose trip starts later is already running it: EARLY_M or more along this working and moving
        forward on it (>= 50 m since the last fix). Distance alone would take a bus finishing its previous trip
        on the same road the other way. Once seen, it holds for that trip."""
        if s.get('early') == (code, st0):
            return True
        prev, s['al'] = s.get('al'), (code, st0, m[1])
        if m[1] > EARLY_M and prev and prev[:2] == (code, st0) and m[1] - prev[2] >= 50:
            s['early'] = (code, st0)
            return True
        return False

    def locate(self, code, lat, lon):
        """(next stop index, metres along) of a position on one working, or None if off it."""
        ll, ds = self.stopll.get(code), self.sdist.get(code)
        if not ll or not ds or len(ll) < 2:
            return None
        cx = math.cos(math.radians(lat)) * 111320
        best = None
        for i in range(len(ll) - 1):
            (ay, ax), (by, bx) = ll[i], ll[i + 1]
            vx, vy = (bx - ax) * cx, (by - ay) * 111320
            px, py = (lon - ax) * cx, (lat - ay) * 111320
            L2 = vx * vx + vy * vy
            t = max(0.0, min(1.0, (px * vx + py * vy) / L2)) if L2 else 0.0
            off = math.hypot(px - t * vx, py - t * vy)
            if off <= 250 and (best is None or off < best[0]):
                best = (off, i + 1, ds[i] + t * (ds[i + 1] - ds[i]))
        return best[1:] if best else None

    def fit_eta(self, code, eta, m, lat, lon, fix):
        """MTC's ETAs checked against the bus's GPS. Stops the bus has already passed (MTC marks them a
        stop or two late) are dropped; None when MTC's trip progress cannot be where the bus is: it
        stalled (J1614: only the first stop 'Covered', "next stop Vandalur Zoo" at Foreshore Estate) or
        is another trip's. On this working's road: over 3 km behind the bus, or the next stop further
        ahead than 2 km plus 25 m/s until MTC's time for it. Off the road (heading for its first stop,
        a diversion): the next stop further away than that, in a straight line."""
        ll, ds = self.stopll.get(code), self.sdist.get(code)
        if not ll or not ds or any(i >= len(ll) for i, _ in eta):
            return eta
        gap = None
        if m is not None:
            gap = ds[eta[0][0]] - m[1]
            if gap < -3000:
                bad = True
            else:
                eta = [x for x in eta if ds[x[0]] >= m[1] - 200]
                if not eta:
                    return eta
                gap = ds[eta[0][0]] - m[1]
                bad = gap > 2000 + 25 * max(0, eta[0][1] - fix)
        i0, e0 = eta[0]
        far = hav(lat, lon, ll[i0][0], ll[i0][1])
        if m is None:
            bad = far > 2000 + 25 * max(0, e0 - fix)
        if bad and len(self.far) < 40:
            self.far.append(f'{code} i{i0} {"off" if gap is None else round(gap / 1000, 1)}/{round(far / 1000, 1)} in {round((e0 - fix) / 60)}m')
        return None if bad else eta

    def in_depot(self, s, eta, code):
        """A bus standing in a depot is not running its trip, whatever MTC's trip data says (J1383 and J1041 on
        A51 R1, 16 Nov: MTC gave arrival times while both stood in Chrompet depot for over an hour). In a depot:
        within DEPOT_M of one (depots.json: where buses park at night, mtcits_depots.py), still for DEPOT_STILL
        seconds (within 40 m), and not at the stop MTC says it serves next (a depot that is also a terminus:
        a bus waiting there to start its trip is in service)."""
        try:
            mt = os.path.getmtime(self.depot_path)
            if mt != self.depot_mtime:
                self.depots, self.depot_mtime = json.load(open(self.depot_path)), mt
        except (OSError, ValueError):
            pass
        near = min((hav(s['lat'], s['lon'], d[0], d[1]) for d in self.depots), default=1e9)
        if near >= DEPOT_M:
            s.pop('dep', None)
            return False
        ll = self.stopll.get(code) or []
        if eta and eta[0][0] < len(ll) and hav(s['lat'], s['lon'], ll[eta[0][0]][0], ll[eta[0][0]][1]) < 300:
            s.pop('dep', None)
            return False
        if s.get('dep'):                        # once in, shunting about the yard does not take it out;
            if s.get('v', 0) > 4:               # driving off does (over 4 m/s between fixes)
                s.pop('dep', None)
                return False
            return True
        # the outer ring can be the road past the gate (a bus held at a signal there for 5-7 min was marked, 14-16
        # Nov audit): there it takes twice as long
        if fix_still(s) >= (DEPOT_STILL if near < DEPOT_CORE else 2 * DEPOT_STILL):
            s['dep'] = True
        return bool(s.get('dep'))

    def station_index(self, k, code, ll):
        """MTC station id -> index of our working's stop at the same place (within 300 m), in order."""
        key = (k, code)
        if key in self.stmap:                   # a direction's stations do not change
            return self.stmap[key]
        ours = self.stopll.get(code) or []
        sts = [(sid, la, lo) for sid, (la, lo) in ll.items() if la is not None and lo is not None]
        # align the two stop lists in order (either may start or end elsewhere, or skip stops):
        # most stations paired within 300 m, then least total distance
        n, m = len(sts), len(ours)
        best = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
        for i in range(1, n + 1):
            _, la, lo = sts[i - 1]
            for j in range(1, m + 1):
                b = max(best[i - 1][j], best[i][j - 1])
                dd = hav(la, lo, ours[j - 1][0], ours[j - 1][1])
                if dd < 300:
                    p = best[i - 1][j - 1]
                    b = max(b, (p[0] + 1, p[1] - dd))
                best[i][j] = b
        out, i, j = {}, n, m
        while i and j:
            if best[i][j] == best[i - 1][j]:
                i -= 1
            elif best[i][j] == best[i][j - 1]:
                j -= 1
            else:
                out[sts[i - 1][0]] = j - 1
                i, j = i - 1, j - 1
        self.stmap[key] = out
        return out

    def snapshot(self):
        """One row per bus: MTC's (a direction's, else the trip-less sweep's) for route, trip, flags and
        ETAs, at the newest position either source has; Chalo's alone for a bus MTC has no fresh fix for."""
        now = time.time()
        mtc, cha = {}, {}
        with self.lock:
            items = list(self.state.items())
        for key, (polled, rows) in items:
            if now - polled > GONE:
                continue
            src = cha if isinstance(key, tuple) and key[0] == 'chalo' else mtc
            for r in rows:
                if r[7] and r[7][-1][1] < now - 300:
                    continue
                p = src.get(r[0])
                if p is None or r[5] > p[5]:
                    src[r[0]] = r
        out, nc, nm, rec = [], 0, 0, []
        for reg in set(mtc) | set(cha):
            m, c = mtc.get(reg), cha.get(reg)
            if m and now - m[5] < GONE:
                if c and c[5] > m[5] and hav(m[2], m[3], c[2], c[3]) < CHALO_NEAR:
                    m = m[:2] + c[2:6] + m[6:]
                    nm += 1
                    rec.append(m[:6] + [m[6] + 'h'] + m[7:])
                r = m
            elif c and now - c[5] < CHALO_FRESH:
                r, nc = c, nc + 1
                rec.append(c)
            else:
                continue
            flat = []
            for i, t in [e for e in r[7] if e[1] >= now - 120][:ETA_KEEP]:
                flat += [i, int(t - r[5])]
            out.append(r[:7] + [flat] + r[8:9])
        if rec and self.a.archive:               # Chalo's positions go in the archive too (MTC's own rows are
            self.record(rec)                     # recorded as they come; record() keeps only newer fixes)
        self.diag['snap chalo only'] = nc
        self.diag['snap chalo newer'] = nm
        return {'t': int(now), 'fmt': 2, 'polled': self.cycle_n, 'cycle': round(self.cycle_s), 'v': out,
                'src': 'mtcbusits+chalo' if self.cvar else 'mtcbusits'}

    def save_fleet(self):
        if not self.fleet_path:
            return
        with self.slock:
            data = json.dumps(self.reg, indent=0, sort_keys=True)
        tmp = self.fleet_path + '.tmp'
        with open(tmp, 'w') as fh:
            fh.write(data)
        os.replace(tmp, self.fleet_path)
        with self.slock:
            dirs = {k: {'rno': d.get('rno'), 'n': d.get('n'), 'seen': d.get('seen'), 'stations': d['stations']}
                    for k, d in self.dirs.items() if d.get('stations')}
        p = os.path.join(self.a.archive, 'directions.json')
        try:
            old = json.load(open(p))                # keep directions seen on earlier days
        except (OSError, ValueError):
            old = {}
        old.update({str(k): v for k, v in dirs.items()})
        with open(p + '.tmp', 'w') as fh:
            json.dump(old, fh, separators=(',', ':'))
        os.replace(p + '.tmp', p)

    def hot(self):
        try:
            w = json.load(open(self.a.watch)) if self.a.watch else {}
        except (OSError, ValueError):
            return set()
        now = time.time()
        sns = {norm(s) for s, t in (w.get('sn') or {}).items() if now - t < (w.get('ttl') or 150)}
        out = {k for k, d in self.dirs.items() if sns and (norm(d.get('rno')) in sns or 'P' + norm(d.get('rno')) in sns)}
        return set(sorted(out)[:HOT_MAX])

    def read_vids(self):
        """Every vehicle MTC tracks (ListVehicles matches a substring: digits 0-9 cover all)."""
        out = {}
        for x in string.digits:
            c, j = call('ListVehicles', {'vehicleRegNo': x})
            self.sent[0] += 1
            for v in (j or {}).get('data') or []:
                if v.get('vehicleregno') and v.get('vehicleid'):
                    out[v['vehicleregno']] = v['vehicleid']
            time.sleep(2)
        if len(out) > 1000:
            with self.slock:
                self.vids.update(out)
            log(f'{len(out)} vehicles in MTC\'s list')

    def loose_one(self, reg):
        """Read one bus that no direction reported; show it if it has a fresh fix and a route number."""
        c, j = call('VehicleTripDetails_v2', {'vehicleId': self.vids[reg]})
        self.sent[0] += 1
        now = time.time()
        if c == 429 or c >= 500:
            self.pause_until = max(self.pause_until, now + (600 if c == 429 else 60))
            return LOOSE_STILL
        L = ((j or {}).get('LiveLocation') or [{}])[0]
        lat, lon, rno = L.get('latitude'), L.get('longitude'), (L.get('routeno') or '').strip()
        fix = ist(L.get('lastrefreshon'), '%d-%m-%Y %H:%M:%S')
        self.diag['loose asked'] += 1
        if not (lat and lon and fix and 12.3 < lat < 13.7 and 79.5 < lon < 80.5) or now - fix > LOOSE_FRESH:
            return LOOSE_IDLE
        with self.slock:
            s = self.bus.get(reg)
            if s and s.get('dir') is not None and now - s['t'] < 600:
                return LOOSE_IDLE                  # a direction has it after all
            prev = self.loose.get(reg, {})
            step = hav(prev['ll'][0], prev['ll'][1], lat, lon) if prev.get('ll') else 0
            moved = step > 30
            self.loose.setdefault(reg, {})['ll'] = (lat, lon)
        if not rno:
            return LOOSE_STILL if moved else LOOSE_IDLE
        still = not moved
        m0 = self.match(rno, lat, lon, L.get('heading'), still)
        code = m0[0] if m0 else 'c:' + rno
        # n: no trip in MTC's system - position and route number only, no destination, no ETAs
        row = [reg, code, round(lat, 5), round(lon, 5), L.get('heading') or 0, fix,
               flags_of(L.get('servicetype') or (self.reg.get(reg) or {}).get('service'), rno, self.fleet.get(reg, '')) + 'n',
               self.model_eta(m0, fix) if step > 100 else []]      # shunting in a yard is not a trip
        if not code.startswith('c:') and self.key(rno) == 'P' + norm(rno):
            row.append(rno)
        with self.lock:
            self.state[('loose', reg)] = (now, [row])
        if self.a.archive:
            self.record([row])
        self.diag['loose shown'] += 1
        return LOOSE_MOVING if moved else LOOSE_STILL

    def chalo_row(self, vid, reg, lat, lon, bearing, halted, fix):
        """One Chalo bus as a feed row (None if unusable)."""
        v = self.cvar.get(vid, {}).get('v')
        if not v:
            return None
        for sx in ('AC', 'LF', 'E'):                  # K5421ELFAC = K5421
            if reg.endswith(sx) and len(reg) > len(sx) + 3:
                reg = reg[:-len(sx)]
        with self.slock:
            s = self.bus.get(reg)
            if s and fix > s.get('ct', 0):
                s['ct'] = fix
        rno = v['route_name'].strip()
        m0 = self.match(rno, lat, lon, bearing, halted)
        code = m0[0] if m0 else 'c:' + rno
        # h: position from Chalo. A moving bus on one of our workings is running Chalo's route (shown in
        # service, with our model's ETAs); n (drawn as a ghost, "not on a trip") only for one standing or
        # off our workings - every Chalo-only bus used to be a ghost, running ones included
        run = bool(m0) and not halted
        row = [reg, code, round(lat, 5), round(lon, 5), bearing or 0, fix,
               flags_of((self.reg.get(reg) or {}).get('service'), rno, self.fleet.get(reg, '')) + ('h' if run else 'hn'),
               self.model_eta(m0, fix) if run else []]
        if not code.startswith('c:') and self.key(rno) == 'P' + norm(rno):
            row.append(rno)
        return row

    def chalo_reader(self):
        """Chalo's positions come from chalo_poller.py (its own process: as threads here, each request queued
        for the GIL behind the MTC threads, 1-1.8 s instead of 0.08 s). Every few seconds, when its file has
        changed: each bus becomes a row, worked out again only when its fix is new."""
        mtime, cache = 0, {}
        while True:
            time.sleep(3)
            try:
                mt = os.path.getmtime(self.a.chalo_raw)
                if mt == mtime:
                    continue
                mtime = mt
                buses = json.load(open(self.a.chalo_raw))['b']
            except (OSError, ValueError, KeyError):
                continue
            now, by_vid, seen = time.time(), collections.defaultdict(list), {}
            for vid, reg, lat, lon, bearing, halted, fix in buses:
                k = (vid, reg, fix)
                row = cache[k] if k in cache else self.chalo_row(vid, reg, lat, lon, bearing, halted, fix)
                seen[k] = row
                if row:
                    by_vid[vid].append(row)
            cache = seen
            with self.lock:
                for key in [k for k in self.state if isinstance(k, tuple) and k[0] == 'chalo' and k[1] not in by_vid]:
                    del self.state[key]
                for vid, rows in by_vid.items():
                    self.state[('chalo', vid)] = (now, rows)
            self.diag['chalo buses'] = len(buses)

    def sweeper(self):
        last_vids = 0
        while True:
            t0 = time.time()
            if t0 < self.pause_until:
                time.sleep(5)
                continue
            if t0 - last_vids > VIDS_EVERY:
                last_vids = t0
                try:
                    self.read_vids()
                except Exception as e:
                    log(f'vehicle list: {type(e).__name__} {str(e)[:100]}')
            with self.slock:
                seen = {r for r, s in self.bus.items() if s.get('dir') is not None and t0 - s['t'] < 600}
                # two queues: buses being shown (moving or standing, re-asked often) and the rest (never
                # asked, offline, at a depot). Every other request goes to the rest: else ~200 moving
                # trip-less buses take every request and a bus that starts running on no trip is never found
                live, rest = [], []
                for r in self.vids:
                    if r in seen:
                        continue
                    q = self.loose.get(r)
                    if not q or 'asked' not in q:      # never asked: buses last seen on a trip latest first
                        rest.append((1e9, (self.reg.get(r) or {}).get('last', ''), r))
                    elif t0 - q['asked'] >= q['every']:
                        (live if q['every'] < LOOSE_IDLE else rest).append(((t0 - q['asked']) / q['every'], '', r))
            self.turn = getattr(self, 'turn', 0) + 1
            due = (rest or live) if self.turn % 2 else (live or rest)
            if not due:
                time.sleep(2)
                continue
            reg = max(due)[-1]
            try:
                every = self.loose_one(reg)
            except Exception as e:
                log(f'bus {reg}: {type(e).__name__} {str(e)[:100]}')
                every = LOOSE_IDLE
            with self.slock:
                self.loose.setdefault(reg, {}).update(asked=time.time(), every=every)
            # the MTC budget is --rate + --sweep-rate; what directions leave unused (at night, most of
            # it) goes to the sweep
            t1 = time.time()
            dir_rate = sum(1 for x in list(self.dir_t) if t1 - x < 60) / 60   # list(): poll_dir appends from other threads
            rate = max(self.a.sweep_rate, self.a.rate + self.a.sweep_rate - dir_rate)
            time.sleep(max(0.05, 1.0 / rate - (t1 - t0)))

    def run(self):
        threading.Thread(target=self.writer, daemon=True).start()
        if self.a.sweep_rate > 0:
            threading.Thread(target=self.sweeper, daemon=True).start()
        if self.cvar and self.a.chalo_raw:
            threading.Thread(target=self.chalo_reader, daemon=True).start()
        last_disc = last_log = last_save = 0
        budget, tick = 0.0, 0.25
        inflight = [0]
        ilock = threading.Lock()

        def go(k):
            try:
                if self.model is not None:
                    with self.mlock:
                        self.model.reload()
                self.poll_dir(k)
            except Exception as e:
                log(f'direction {k}: {type(e).__name__} {str(e)[:100]}')
            finally:
                with ilock:
                    inflight[0] -= 1

        while True:
            t0 = time.time()
            if t0 < self.pause_until:
                time.sleep(5)
                continue
            if t0 - last_disc > DISCOVER and not getattr(self, 'discovering', False):
                last_disc = t0
                today = dt.datetime.now(IST).date()
                if today != self.day:                  # stops already archived: per IST day
                    self.day, self.passed = today, {}
                if self.dirs:
                    # 36 searches take minutes: in the background, or every direction waits on them
                    # (fix age swung 3.5 <-> 8 min every 5 min while discovery blocked this loop)
                    self.discovering = True

                    def disc():
                        try:
                            self.discover()
                        except Exception as e:
                            log(f'discover: {type(e).__name__} {str(e)[:100]}')
                        finally:
                            self.discovering = False
                    threading.Thread(target=disc, daemon=True).start()
                else:
                    self.discover()                    # first time: nothing to poll without it
            budget = min(budget + self.a.rate * tick, self.a.rate)
            with ilock:
                free = self.a.workers - inflight[0]
            n = min(int(budget), free)
            if n > 0:
                hot = self.hot()
                with self.slock:
                    cand = []
                    live_dirs = {b['dir'] for b in self.bus.values() if t0 - b['t'] < GONE}
                    for k, d in self.dirs.items():
                        sc = self.score(k, d, t0, k in hot, live_dirs)
                        if sc is not None:
                            cand.append((sc, k))
                    cand.sort(reverse=True)
                    batch = [k for _, k in cand[:n]]
                    for k in batch:
                        self.dirs[k]['busy'] = t0
                for k in batch:
                    with ilock:
                        inflight[0] += 1
                    threading.Thread(target=go, args=(k,), daemon=True).start()
                budget -= len(batch)
            if t0 - last_save > 300:
                last_save = t0
                self.save_fleet()
            if t0 - last_log > 300:
                last_log = t0
                with self.slock:
                    live = [s for s in self.bus.values() if t0 - s['t'] < GONE]
                    ages = sorted(t0 - s['t'] for s in live)
                    od = sum(1 for d in self.dirs.values() if d.get('n') and t0 - d['polled'] >= MAXGAP)
                rate = self.sent[0] / max(1, t0 - self.sent[1])
                self.sent = [0, t0]
                p50 = f', fix age p50 {ages[len(ages) // 2] / 60:.1f} min' if ages else ''
                log(f'{len(live)} buses live{p50}, {od} directions overdue, {rate:.2f} req/s; {dict(self.diag)}')
                if self.far:
                    log('eta far (working, MTC next stop index, km to it along the route/straight, MTC due in): ' + '; '.join(self.far[:12]))
                self.diag.clear()
                self.far = []
            time.sleep(max(0.02, tick - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stops', required=True)
    ap.add_argument('--timings', required=True)
    ap.add_argument('--stop-dist', required=True)
    ap.add_argument('--archive', default='')
    ap.add_argument('--out', required=True)
    ap.add_argument('--rate', type=float, default=2.0, help='requests per second, at most')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--write', type=float, default=10)
    ap.add_argument('--model', default='', help='model.json from travel_model.py (used for live probes only)')
    ap.add_argument('--chalo-routes', default='', help="Chalo's variant list (chalo_routes.json); none = MTC only")
    ap.add_argument('--chalo-raw', default='', help="chalo_poller.py's output (Chalo's positions); none = MTC only")
    ap.add_argument('--sweep-rate', type=float, default=2.0,
                    help='requests per second for buses on no trip (read one by one); 0 = off')
    ap.add_argument('--watch', default='', help='watch.json from report_api.py: routes riders have open')
    a = ap.parse_args()
    if a.archive:
        os.makedirs(a.archive, exist_ok=True)
    ItsCollector(a).run()


if __name__ == '__main__':
    # hand the GIL over more often: with a dozen I/O threads, each network step of a request otherwise
    # queued ~5 ms per busy thread (Chalo fetches took 1.6 s here vs 0.08 s alone, 17 Nov)
    sys.setswitchinterval(0.001)
    main()
