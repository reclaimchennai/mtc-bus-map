# Chennai MTC bus map — live buses, observed traffic, open timetables

The code and data behind **[mtc.reclaimchennai.city](https://mtc.reclaimchennai.city)**: every MTC
bus route on one map, every live bus moving on it, how fast buses are actually moving road by
road, and the next bus at every stop. The GTFS feeds built from the same source are published in
[reclaimchennai/ChennaiGTFS](https://github.com/reclaimchennai/ChennaiGTFS/tree/chennai-one-feeds/data/chennai-one)
(MTC buses, CMRL Metro, Southern Railway suburban + MRTS).

Routes, stops and timetables come from **Chennai One**, the CUMTA multimodal app. Live bus
positions come from the **official MTC app's feed (Chalo)**, read by one collector on our server at
a polite rate — the map and dashboard read our copy, so visitors never call either service. Fares
are MTC's own published stage table.

## What is on the map

| | |
|---|---|
| **Routes** | 970 MTC services over 3,713 scheduled workings, each road-matched (OSRM over OpenStreetMap). Tap a line for its workings, stops and timings. |
| **Departures by hour** | Two 12-hour dials per working: day (6 am–6 pm) and night (6 pm–6 am). |
| **Live buses** | Every bus the MTC app is tracking, as a 3D model in its real livery — Switch EiV12 electric AC (red) and non-AC (cyan/purple), standard-floor diesel (sky blue), diesel AC, premium — glowing while it moves, a snowflake over AC buses, passengers behind the glass at the estimated crowd level. Drawn on a WebGL layer, only for buses in view. Between GPS fixes each bus moves along its route on estimated stop times (its distance to each stop at city bus speed); it never leaves before it must and is never drawn going backwards. |
| **Live arrivals** | Every stop card lists the tracked buses due in the next 90 minutes; low-floor buses carry a wheelchair ramp pill. Tap a next stop on a bus card to fly there. |
| **Watching a route** | While a rider has a bus or a route open, the page tells the server once a minute (`POST /api/watch`, route number only) and polls every 10 s; the collector polls watched routes every 30 s instead of once a cycle, at most 16 route variants at once, so the extra load on the source is capped however many riders watch. |
| **Rider reports** | A Report button on bus and stop cards: how full, on time or not, did the ramp work — a few taps, no typing. Served by `server/report_api.py` (`server/mtc-reports.sh`); fixed answers only, no free text, IP addresses never stored. |
| **Crowding (estimated)** | MTC publishes no hourly ticket counts; crowding is CMRL's hourly ticket profile (when Chennai travels) against how many buses each working runs that hour. Labelled as an estimate everywhere. |
| **Dashboard** | [`/db`](https://mtc.reclaimchennai.city/db/): buses on the road by hour, observed speeds, slowest corridors, bus bunching by hour / route / place, crowding, the day's roster, fares. Rebuilt hourly from the archive (`server/db_build.py`). |
| **Traffic (observed)** | Bus speed per stop-to-stop road segment from the live fleet's GPS — last hour, or typical by hour over the past week. Not a timetable estimate. |

## Layout

```
web/       the app as served (MapLibre GL, no build step)
  index.html app.js        route explorer
  buses.js iso.js          livery artwork and the 3D bus models (canvas, no image files)
  db/index.html            the dashboard (data.json is generated on the server)
  live.js                  live buses + live arrivals
  traffic.js               observed-speed layer
  data/                    routes, stops, timings, segment geometry (see below)
server/    runs on the web host
  chalo_collector.py       live buses from the MTC app's feed (Chalo), route variant by variant;
                           writes live/vehicles.json every 20 s, archives every GPS fix and the
                           road speed between fixes (chalo_routes.json: Chalo's route list)
  live_collector.py        the same from Chennai One (used until 20 Oct 2026; needs a session)
  live_speeds.py           per-segment speed medians -> live/speeds.json (every 10 min)
  daily_harvest.py         new network snapshots + the day's bus roster (05:30 IST)
  db_build.py              dashboard data: fleet, speeds, bunching, crowding, roster
  report_api.py            rider reports (POST /api/report, GET /api/recent)
  fare_harvest.py          every class's fare per stop pair (/frfs/search quotes all classes at once)
  harvest_fast.py api.py   timetable harvest for a new snapshot
  live_sweep.py            one raw fleet-wide capture
  systemd/                 the units as deployed (user services; %h = home)
pipeline/  snapshot -> GTFS + map layers
  refresh.sh               the whole chain for one snapshot date
  build_gtfs_clean.py      GTFS for MTC / CMRL / suburban
  snap_routes.py snap_rail.py   road and track matching
  build_map_layers.py build_timings.py build_live_assets.py build_pr_bundle.py
  build_fare_tables.py     fare tables published in ChennaiGTFS data/chennai-one/fares
  build_official_fares.py  MTC's published stage fares (mtcbus.tn.gov.in/Home/fares) and the
                           official fare stage of every stop
```

## Running it

The live collector needs no login. Keep the rate low: one process per deployment, serving
everyone from its output.

```bash
cd server
python3 chalo_collector.py --stops ../web/data/route_stops.json --timings ../web/data/timings.json \
  --stop-dist ../web/data/stop_dist.json --routes chalo_routes.json --archive archive \
  --out ../web/live/vehicles.json --rate 2
python3 live_speeds.py --stops ../web/data/route_stops.json --archive archive --out ../web/live/speeds.json
```

Serve `web/` with any static server. To rebuild for a new network snapshot: harvest it with
`server/harvest_fast.py`, then `pipeline/refresh.sh <YYYYMMDD>` (needs `shapely`, `pyproj`).

## Method notes

- **One route variant per call, politely.** The MTC app's feed answers live buses per route
  variant (2,817 of them). The collector polls the variants of routes inside their service
  window at 2 requests/s, looks at variants that came back empty three times only every third
  cycle, sleeps 00:30–04:30, and pauses 10 minutes on any 429 or server error. A full pass takes
  about 24 minutes, so a bus's GPS fix can be several minutes old.
- **Matching and ETAs are ours.** The feed's route ids are its own, so each bus is matched to one
  of our workings by route number, position along the working's stops and heading. The feed has
  no stop-by-stop predictions: arrival times are estimated from the bus's distance along the
  route at 4.4 m/s plus 12 s a stop. Fleet suffixes carry the bus type: `E` electric, then `LF`
  low floor (`K5008ELF` is bus K5008).
- **Observed speed** is distance along the bus's own route between two consecutive GPS fixes,
  divided by the time between them — stops, signals and congestion included, i.e. the speed a
  rider gets. Each sample is credited to every stop-to-stop segment it spans and pooled across all
  routes using that road; medians, so one bus held at a depot gate does not paint a road red.
- **Bus timetables** need only each route's first stop: every stop's time is the first-stop time
  plus the route's cumulative stop-to-stop run times, 15 s dwell — identical to the per-stop
  timetables on all 6,351 trip-stops sampled.
- **Liveries** come from the fleet suffixes, the route's features (AC, Express) and the flags archived per bus from Chennai One. Premium buses are booked
  separately in the app and have not appeared in the live feed yet.
- **What is not here:** buses that do not report GPS are not on the live layer, and the stop list
  shows only tracked buses. State (TNSTC/SETC) and private buses are not in either feed.

## Data in `web/data`

`mtc_routes.geojson` (route lines), `route_stops.json` (stops per working, keyed by route code),
`timings.json` (first/last/frequency/hourly departures per working), `bus_stops.geojson`,
`bus_termini.geojson`, `metro_lines.geojson` (one shape per rail trip, read back by the GTFS
build), `metro_map.geojson` (its distinct shapes, what the map draws), `metro_stations.geojson`, `stop_dist.json`
(each stop's distance along its route), `seg_geo.json` (stop-to-stop road segments).
Built from the Chennai One network snapshot of 17 October 2026.

## Licence and attribution

Code: MIT (see `LICENSE`). Data: timetables, stops and routes originate from CUMTA / MTC / CMRL /
Southern Railway via Chennai One; live positions from MTC via its app (Chalo); fares from MTC; route and track geometry is derived from © OpenStreetMap
contributors (ODbL), so the derived data is published under ODbL. Basemap © OpenFreeMap /
OpenMapTiles / OpenStreetMap. Attribute the source agencies if you use it.

Made by [Reclaim Chennai](https://reclaimchennai.city).
