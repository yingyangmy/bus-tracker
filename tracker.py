#!/usr/bin/env python3
"""
Headless bus tracker, built to run on GitHub Actions (no computer of yours needed).

  python tracker.py --check         test the setup: finds your route and stop, tries the live feed
  python tracker.py --minutes 275   record departures for N minutes, then rebuild timetable.md
  python tracker.py --report        only rebuild timetable.md from data/departures.csv

Which buses and stops to watch is set in watches.json.
"""
import argparse, csv, io, json, math, os, re, statistics, tempfile, time, urllib.request, zipfile
from datetime import datetime, timezone, timedelta
from google.transit import gtfs_realtime_pb2

API = "https://api.data.gov.my"
HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "data", "departures.csv")
TZ = timezone(timedelta(hours=8))  # Malaysia time
POLL = 30
CACHE = os.path.join(tempfile.gettempdir(), "bus_static")
os.makedirs(CACHE, exist_ok=True)


def get(url, timeout=60, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 bus-schedule-tracker"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5)


def rows(cat, name):
    p = os.path.join(CACHE, cat + ".zip")
    if not os.path.exists(p):
        data = get("%s/gtfs-static/prasarana?category=%s" % (API, cat), 180)
        with open(p, "wb") as f:
            f.write(data)
    with zipfile.ZipFile(p) as z, z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).split()


def dist(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(a))


def resolve(w):
    """Turn a watches.json entry into a route id, trip ids and stop coordinates."""
    cat, want = w["cat"], w["route"].lower()
    routes = list(rows(cat, "routes.txt"))
    # The route number (e.g. T821) may sit in route_short_name, route_long_name or route_id.
    hit = [r for r in routes if want in ([r["route_id"].lower()] + norm(r.get("route_short_name") or "")
                                         + norm(r.get("route_long_name") or ""))]
    if not hit:
        listing = ["%s | %s | %s" % (r["route_id"], r.get("route_short_name") or "", r.get("route_long_name") or "")
                   for r in routes[:60]]
        raise SystemExit("Route %r was not found in %s (%d routes there). First routes, as id | short name | long name:\n%s"
                         % (w["route"], cat, len(routes), "\n".join(listing)))
    rid = hit[0]["route_id"]
    trips = {r["trip_id"] for r in rows(cat, "trips.txt") if r["route_id"] == rid}
    order, seen = [], set()
    for r in rows(cat, "stop_times.txt"):
        if r["trip_id"] in trips and r["stop_id"] not in seen:
            seen.add(r["stop_id"])
            order.append(r["stop_id"])
    info = {r["stop_id"]: r for r in rows(cat, "stops.txt") if r["stop_id"] in seen}
    stops = [info[i] for i in order if i in info]
    words = norm(w["stop"])
    cand = [s for s in stops if all(x in norm(s["stop_name"]) for x in words)]
    if not cand:
        raise SystemExit("No stop matching %r on route %s. Stops on this route: %s"
                         % (w["stop"], w["route"], [s["stop_name"] for s in stops]))
    s = cand[0]
    return {"name": w.get("name") or "%s - %s" % (w["route"], s["stop_name"]), "cat": cat, "rid": rid,
            "trips": trips, "stop": s["stop_name"], "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"]),
            "radius": float(w.get("radius", 60)), "cands": [x["stop_name"] for x in cand], "n": len(stops)}


def fetch(cat):
    fm = gtfs_realtime_pb2.FeedMessage()
    fm.ParseFromString(get("%s/gtfs-realtime/vehicle-position/prasarana?category=%s" % (API, cat), 30))
    out = []
    for e in fm.entity:
        if e.HasField("vehicle") and e.vehicle.HasField("position"):
            v = e.vehicle
            out.append((v.trip.route_id, v.trip.trip_id, v.vehicle.id or v.vehicle.label or e.id,
                        v.position.latitude, v.position.longitude, v.timestamp or int(time.time())))
    return out


def check(ws):
    for w in ws:
        r = resolve(w)
        print("== %s ==" % r["name"])
        print("Route id %s, %d stops. Watching stop: %s (%.5f, %.5f), radius %d m"
              % (r["rid"], r["n"], r["stop"], r["lat"], r["lon"], r["radius"]))
        if len(r["cands"]) > 1:
            print("Other stops also matched (the first is used):", r["cands"][1:])
        vs, now = fetch(w["cat"]), time.time()
        mine = [v for v in vs if v[0] == r["rid"] or v[1] in r["trips"]]
        print("Live feed works: %d vehicles in this category, %d on this route right now." % (len(vs), len(mine)))
        for v in mine:
            print("  bus %s is %d m from the stop (position is %d s old)" % (v[2], dist(v[3], v[4], r["lat"], r["lon"]), now - v[5]))
        if not mine:
            print("  No buses matched. Zero is normal outside service hours (about 6 am to 11 pm).")
            print("  Route ids seen in the feed, for troubleshooting:", sorted({v[0] for v in vs})[:15])


def track(ws, minutes):
    rs = [resolve(w) for w in ws]
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    new = not os.path.exists(CSV_PATH)
    f = open(CSV_PATH, "a", newline="")
    wr = csv.writer(f)
    if new:
        wr.writerow(["watch", "departed_at_myt", "vehicle", "ts"])
    state, end, cats = {}, time.time() + minutes * 60, {r["cat"] for r in rs}
    print("Tracking %d watch(es) for %d minutes" % (len(rs), minutes), flush=True)
    while time.time() < end:
        t0, feeds = time.time(), {}
        for c in cats:
            try:
                feeds[c] = fetch(c)
            except Exception as e:
                print("feed error", c, e, flush=True)
        now = time.time()
        for r in rs:
            for rid, tid, vid, la, lo, ts in feeds.get(r["cat"], []):
                if rid != r["rid"] and tid not in r["trips"]:
                    continue
                if now - ts > 180:  # stale position
                    continue
                d = dist(la, lo, r["lat"], r["lon"])
                st = state.setdefault((r["name"], vid), {"in": False, "last": 0})
                if d <= r["radius"]:
                    st["in"] = True
                elif d > r["radius"] * 1.5 and st["in"]:  # bus has left the stop area
                    st["in"] = False
                    if ts - st["last"] > 300:
                        st["last"] = ts
                        wr.writerow([r["name"], datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S"), vid, ts])
                        f.flush()
        time.sleep(max(1, POLL - (time.time() - t0)))
    f.close()


def sched(dd):
    """dd: {date: [minutes since midnight]} -> (typical times, per-hour stats)."""
    n = len(dd)
    pts = sorted((m, day) for day, ms in dd.items() for m in ms)
    cl = []
    for m, day in pts:
        if cl and m - cl[-1][-1][0] <= 6 and m - cl[-1][0][0] <= 12:
            cl[-1].append((m, day))
        else:
            cl.append([(m, day)])
    need, times = max(1, math.ceil(n * 0.4)), []
    for c in cl:
        seen = len({d for _, d in c})
        if seen >= need:
            times.append(("%02d:%02d" % divmod(int(statistics.median(m for m, _ in c)), 60), seen))
    gaps, cnt = {}, {}
    for ms in dd.values():
        ms.sort()
        for i, m in enumerate(ms):
            cnt[m // 60] = cnt.get(m // 60, 0) + 1
            if i and 0 < m - ms[i - 1] <= 120:
                gaps.setdefault(m // 60, []).append(m - ms[i - 1])
    return times, [(h, round(cnt[h] / n, 1), round(statistics.median(gaps[h])) if h in gaps else None) for h in sorted(cnt)]


def report():
    if not os.path.exists(CSV_PATH):
        return
    data = {}
    with open(CSV_PATH, newline="") as f:
        for r in csv.DictReader(f):
            data.setdefault(r["watch"], set()).add((r["vehicle"], int(r["ts"])))
    out = ["# Observed bus timetable", "",
           "Built from live GPS data. Times are Malaysia time and can lag the real departure by up to a minute.", ""]
    for name, items in sorted(data.items()):
        by = {}
        for _, t in sorted(items, key=lambda x: x[1]):
            d = datetime.fromtimestamp(t, TZ)
            k = "Weekday" if d.weekday() < 5 else ("Saturday", "Sunday")[d.weekday() - 5]
            by.setdefault(k, {}).setdefault(d.date().isoformat(), []).append(d.hour * 60 + d.minute)
        out += ["## " + name, ""]
        for k in ("Weekday", "Saturday", "Sunday"):
            if k not in by:
                continue
            n = len(by[k])
            times, hours = sched(by[k])
            out.append("### %s (%d day%s of data)" % (k, n, "" if n == 1 else "s"))
            out.append("")
            out.append("Typical departures: " + (", ".join("%s (%d/%d days)" % (t, s, n) for t, s in times) or "not enough repeat data yet"))
            out += ["", "| Hour | Buses per day | Typical gap |", "|---|---|---|"]
            out += ["| %02d:00 | %s | %s |" % (h, c, ("%d min" % g) if g else "-") for h, c, g in hours]
            out.append("")
    with open(os.path.join(HERE, "timetable.md"), "w") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--minutes", type=int, default=0)
    a = ap.parse_args()
    with open(os.path.join(HERE, "watches.json")) as f:
        watches = json.load(f)
    if a.check:
        check(watches)
    elif a.minutes:
        try:
            track(watches, a.minutes)
        finally:
            report()
    else:
        report()
