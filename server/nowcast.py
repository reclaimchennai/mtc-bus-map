"""Where a bus is now, and when it reaches each stop: a one-dimensional nowcast
along its route.

A GPS fix is already old when it reaches us (Delhi: ~38 s median), and the next
one is ~20 s away (Chennai's Chalo feed: ~13 min, polled route by route), so showing the fix shows where the bus *was*. Instead each bus
is a point moving along its route line, and its future is played forward from
the last fix through a travel model:

  segment   each stop-to-stop segment at its effective speed (metres over seconds,
            pooled, standing at stops included - so dwell is in by construction),
            best evidence first:
            buses that drove it in the last LIVE_S seconds (live probes: every bus
            in the fleet is a traffic sensor for the others), else its usual speed
            at this hour (travel_model.py, from our archive), else its all-day
            usual, else the city's for this hour, else DEFAULT_V;
  pace      each bus's own driver/vehicle/load factor: an exponentially weighted
            ratio of how far it actually went between fixes to how far the model
            said it would, clamped to [0.6, 1.6]; it matters most in the next
            minute or two, so it fades towards 1 with time ahead (PACE_T).

The arrival time at each of the next stops is the output (the map moves the bus
along those knots in real time, so its drawn position *is* the nowcast). On a
new fix the model restarts from the observed position, so prediction error
cannot accumulate: the map's catch-up animation hides the correction.
"""
import collections
import json
import math
import os
import time

DEFAULT_V = 4.2          # m/s when nothing is known
LIVE_S = 900             # live probes count for 15 minutes
MAX_V = 22.0
PACE_T = 170             # s: a bus's own pace fades towards the model's with time ahead


def faded(pace, ahead):
    """The per-bus pace correction, weaker the further ahead we look."""
    return 1.0 + (pace - 1.0) * math.exp(-max(0.0, ahead) / PACE_T)


class Model:
    def __init__(self, path=None):
        self.path, self.m, self.loaded = path, {}, 0
        self.probe = collections.defaultdict(collections.deque)      # seg key -> (t, v)
        self.reload()

    def reload(self, force=False):
        if not self.path or (not force and time.time() - self.loaded < 600):
            return
        self.loaded = time.time()
        try:
            self.m = json.load(open(self.path))
        except (OSError, ValueError):
            pass

    # ── evidence ──
    def add_probe(self, key, t, v, dt_=1.0):
        """A bus just drove this segment at v m/s over dt_ seconds."""
        q = self.probe[key]
        q.append((t, v * dt_, dt_))
        while q and q[0][0] < t - LIVE_S:
            q.popleft()
        while len(q) > 30:
            q.popleft()

    def run_v(self, key, hour, now):
        q = self.probe.get(key)
        if q:
            live = [(m, s_) for t, m, s_ in q if t >= now - LIVE_S]
            if len(live) >= 2:                                  # pooled, like the model
                return min(MAX_V, max(1.0, sum(m for m, _ in live) / sum(s_ for _, s_ in live)))
        row = (self.m.get('seg') or {}).get(key)
        if row:
            v = row[2][hour] if row[2][hour] > 0 else row[1]
            if v and v > 0:
                return min(MAX_V, max(1.0, v))
        cv = ((self.m.get('city') or {}).get('v') or [-1] * 24)[hour]
        return cv if cv and cv > 0 else DEFAULT_V

    def dwell(self, stop, hour):
        """Standing time at stops is inside the effective segment speeds; this is 0 for
        the forward model and kept only so a caller can ask (dwell_stats)."""
        return 0.0

    def dwell_stats(self, stop, hour):
        """Expected standing time at a stop (chance of stopping x how long)."""
        row = (self.m.get('dwell') or {}).get(stop)
        city = self.m.get('city') or {}
        if row and row[0] >= 8:
            d = row[3][hour] if row[3][hour] > 0 else row[2]
            if d and d > 0:
                return row[1] * d
        return (city.get('p_stop') or 0.5) * (city.get('dwell') or 20)

    # ── forward ──
    def eta(self, ids, ds, s, k, t0, pace=1.0, n=25):
        """Arrival time at stops k, k+1, ... from position s (metres along) at time t0."""
        hour = time.gmtime(t0 + 19800).tm_hour
        out, t, here = [], t0, s
        for i in range(k, min(len(ds), k + n)):
            if i >= 1:
                v = self.run_v(f'{ids[i - 1]}|{ids[i]}', hour, t0) * faded(pace, t - t0)
                t += max(0.0, ds[i] - here) / max(v, 0.5)
            out.append([i, t])
            if i < len(ds) - 1:
                t += self.dwell(ids[i], hour)
            here = ds[i]
        return out

    def advance(self, ids, ds, s, t0, t1, pace=1.0):
        """Where a bus at s (metres) at t0 will be at t1, under the same model."""
        hour = time.gmtime(t0 + 19800).tm_hour
        t, here = t0, s
        i = next((j for j in range(1, len(ds)) if ds[j] > s), len(ds) - 1)
        while t < t1 and i < len(ds):
            v = self.run_v(f'{ids[i - 1]}|{ids[i]}', hour, t0) * faded(pace, t - t0)
            need = max(0.0, ds[i] - here) / max(v, 0.5)
            if t + need >= t1:
                return here + v * (t1 - t)
            t += need
            here = ds[i]
            if i < len(ds) - 1:
                t += self.dwell(ids[i], hour)
                if t >= t1:
                    return here
            i += 1
        return here

    def expected_time(self, ids, ds, s0, s1, hour, now):
        """Seconds the model expects for s0 -> s1 (running + dwell), pace 1."""
        t, here = 0.0, s0
        i = next((j for j in range(1, len(ds)) if ds[j] > s0), len(ds) - 1)
        while here < s1 and i < len(ds):
            v = self.run_v(f'{ids[i - 1]}|{ids[i]}', hour, now)
            end = min(ds[i], s1)
            t += max(0.0, end - here) / max(v, 0.5)
            here = end
            if here >= ds[i] and here < s1 and i < len(ds) - 1:
                t += self.dwell(ids[i], hour)
            i += 1
        return t


class Pace:
    """Per bus: how its progress compares with the model's, smoothed."""
    def __init__(self, alpha=0.3):
        self.a, self.p = alpha, {}

    def update(self, reg, expected_s, actual_s):
        if actual_s < 15 or expected_s <= 0:
            return self.get(reg)
        r = max(0.5, min(2.0, expected_s / actual_s))
        old = self.p.get(reg, 1.0)
        self.p[reg] = old + self.a * (r - old)
        return self.p[reg]

    def get(self, reg):
        p = self.p.get(reg, 1.0)
        return max(0.6, min(1.6, 1.0 + (p - 1.0) * 0.8))
