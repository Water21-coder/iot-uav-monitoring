import bisect
import csv
import io
import math
import sqlite3

from flask import Flask, Response, jsonify, render_template_string, request

DB_FILE = "uav_data.db"
MAX_POINTS = 300

app = Flask(__name__)


def query(sql, args=()):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_flight_id():
    rows = query("SELECT MAX(id) AS fid FROM flights")
    return rows[0]["fid"] if rows else None


def compute_metrics(rows):
    if not rows:
        return None

    rows = sorted(rows, key=lambda r: r["recv_time"])
    seqs = [r["seq"] for r in rows]
    received = len(rows)
    expected = max(seqs) - min(seqs) + 1
    pdr = received / expected * 100
    loss = 100 - pdr

    latencies = [r["recv_time"] - r["gps_time"] for r in rows]
    latency = sum(latencies) / len(latencies)

    duration = rows[-1]["recv_time"] - rows[0]["recv_time"]

    aoi = None
    if received >= 2 and duration > 0:
        area = 0.0
        for a, b in zip(rows, rows[1:]):
            age_start = a["recv_time"] - a["gps_time"]
            age_end = b["recv_time"] - a["gps_time"]
            area += (age_end ** 2 - age_start ** 2) / 2
        aoi = area / duration

    rate = expected / (duration / 60) if duration > 0 else None

    rssi_values = [r["rssi"] for r in rows if r["rssi"] is not None]
    rssi = sum(rssi_values) / len(rssi_values) if rssi_values else None

    return {
        "expected": expected,
        "received": received,
        "lost": expected - received,
        "pdr": round(pdr, 2),
        "loss": round(loss, 2),
        "latency": round(latency, 3),
        "aoi": round(aoi, 3) if aoi is not None else None,
        "duration": round(duration, 1),
        "rate": round(rate, 2) if rate is not None else None,
        "rssi": round(rssi, 1) if rssi is not None else None,
    }


@app.route("/api/flights")
def api_flights():
    rows = query("""
        SELECT f.id, f.start_time, f.mode, COUNT(s.id) AS packets
        FROM flights f LEFT JOIN sensor_data s ON s.flight_id = f.id
        GROUP BY f.id ORDER BY f.id DESC
    """)
    return jsonify(rows)


@app.route("/api/data")
def api_data():
    fid = request.args.get("flight", type=int) or latest_flight_id()
    if fid is None:
        return jsonify([])
    rows = query("SELECT * FROM sensor_data WHERE flight_id = ? ORDER BY id DESC LIMIT ?",
                 (fid, MAX_POINTS))
    rows.reverse()
    return jsonify(rows)


@app.route("/api/results")
def api_results():
    flights = query("SELECT * FROM flights ORDER BY id")
    results = []
    for f in flights:
        rows = query("SELECT seq, gps_time, recv_time, rssi FROM sensor_data WHERE flight_id = ?",
                     (f["id"],))
        m = compute_metrics(rows)
        if m:
            m["flight_id"] = f["id"]
            m["mode"] = f["mode"]
            m["start_time"] = f["start_time"]
            results.append(m)
    return jsonify(results)


SYNC_TOLERANCE = 5


def fit_line(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 1.0, my - mx
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    a = sxy / sxx
    b = my - a * mx
    return a, b


def error_stats(pred, ref):
    errs = [p - r for p, r in zip(pred, ref)]
    n = len(errs)
    mae = sum(abs(e) for e in errs) / n
    rmse = math.sqrt(sum(e * e for e in errs) / n)
    bias = sum(errs) / n
    return {"mae": round(mae, 3), "rmse": round(rmse, 3), "bias": round(bias, 3)}


def r_squared(pred, ref):
    mr = sum(ref) / len(ref)
    ss_tot = sum((r - mr) ** 2 for r in ref)
    ss_res = sum((r - p) ** 2 for p, r in zip(pred, ref))
    return round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None


def sync_with_ground(uav_rows, ground_rows):
    ground_rows = sorted(ground_rows, key=lambda g: g["ts"])
    times = [g["ts"] for g in ground_rows]
    pairs = []
    for u in uav_rows:
        i = bisect.bisect_left(times, u["gps_time"])
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(times):
                d = abs(times[j] - u["gps_time"])
                if best is None or d < best[0]:
                    best = (d, ground_rows[j])
        if best and best[0] <= SYNC_TOLERANCE:
            pairs.append((u, best[1]))
    return pairs


def analyze_sensor(raw, ref):
    stats = {"raw": error_stats(raw, ref)}
    train = list(range(0, len(raw), 2))
    test = list(range(1, len(raw), 2))
    if len(train) >= 2 and len(test) >= 1:
        a, b = fit_line([raw[i] for i in train], [ref[i] for i in train])
        cal_test = [a * raw[i] + b for i in test]
        raw_test = [raw[i] for i in test]
        ref_test = [ref[i] for i in test]
        stats["raw_test"] = error_stats(raw_test, ref_test)
        stats["calibrated_test"] = error_stats(cal_test, ref_test)
        stats["a"] = round(a, 4)
        stats["b"] = round(b, 4)
        stats["r2"] = r_squared(cal_test, ref_test)
        stats["calibrated_all"] = [round(a * x + b, 3) for x in raw]
    return stats


ALT_BINS = [(0, 10, "0-10 m"), (10, 30, "10-30 m"), (30, 10000, "above 30 m")]


@app.route("/api/rq1")
def api_rq1():
    fid = request.args.get("flight", type=int) or latest_flight_id()
    if fid is None:
        return jsonify({"error": "no flight"})

    uav = query("SELECT seq, gps_time, t1, h1, t2, h2, alt FROM sensor_data "
                "WHERE flight_id = ? ORDER BY gps_time", (fid,))
    if not uav:
        return jsonify({"error": "no UAV data for this flight"})

    t_min = uav[0]["gps_time"] - SYNC_TOLERANCE
    t_max = uav[-1]["gps_time"] + SYNC_TOLERANCE
    ground = query("SELECT ts, temp, hum FROM ground_ref WHERE ts BETWEEN ? AND ?",
                   (t_min, t_max))

    pairs = sync_with_ground(uav, ground)
    if len(pairs) < 4:
        return jsonify({"error": "not enough matched ground truth data", "matched": len(pairs)})

    ref_t = [g["temp"] for u, g in pairs]
    ref_h = [g["hum"] for u, g in pairs]

    sensors = {
        "t1": analyze_sensor([u["t1"] for u, g in pairs], ref_t),
        "t2": analyze_sensor([u["t2"] for u, g in pairs], ref_t),
        "h1": analyze_sensor([u["h1"] for u, g in pairs], ref_h),
        "h2": analyze_sensor([u["h2"] for u, g in pairs], ref_h),
    }

    by_alt = []
    for lo, hi, label in ALT_BINS:
        sel = [(u, g) for u, g in pairs if lo <= u["alt"] < hi]
        if not sel:
            continue
        rt = [g["temp"] for u, g in sel]
        rh = [g["hum"] for u, g in sel]
        by_alt.append({
            "label": label,
            "n": len(sel),
            "t1": error_stats([u["t1"] for u, g in sel], rt)["mae"],
            "t2": error_stats([u["t2"] for u, g in sel], rt)["mae"],
            "h1": error_stats([u["h1"] for u, g in sel], rh)["mae"],
            "h2": error_stats([u["h2"] for u, g in sel], rh)["mae"],
        })

    series = {
        "seq": [u["seq"] for u, g in pairs],
        "ref_t": ref_t,
        "t1": [u["t1"] for u, g in pairs],
        "t2": [u["t2"] for u, g in pairs],
        "t1_cal": sensors["t1"].get("calibrated_all"),
        "t2_cal": sensors["t2"].get("calibrated_all"),
        "ref_h": ref_h,
        "h1": [u["h1"] for u, g in pairs],
        "h2": [u["h2"] for u, g in pairs],
    }
    for k in sensors:
        sensors[k].pop("calibrated_all", None)

    return jsonify({
        "flight_id": fid,
        "uav_packets": len(uav),
        "matched": len(pairs),
        "sensors": sensors,
        "by_alt": by_alt,
        "series": series,
    })


STYLE = """
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f2f4f7; color: #222; }
  header { background: #1f3b57; color: #fff; padding: 14px 20px; font-size: 20px;
           display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }
  header a { color: #cfe3ff; font-size: 14px; text-decoration: none; }
  header select { font-size: 14px; padding: 4px; }
  .wrap { padding: 16px; max-width: 1200px; margin: auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
  .card { background: #fff; border-radius: 8px; padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .card .label { font-size: 12px; color: #666; }
  .card .value { font-size: 22px; font-weight: bold; margin-top: 4px; }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 12px; margin-top: 16px; }
  .box { background: #fff; border-radius: 8px; padding: 12px; height: 280px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .box h3 { margin: 0 0 8px; font-size: 14px; }
  .box canvas { height: 230px !important; }
  table { width: 100%; border-collapse: collapse; background: #fff; margin-top: 16px; font-size: 13px; }
  th, td { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: right; }
  th { background: #1f3b57; color: #fff; }
  .tablebox { overflow-x: auto; }
  .panel { background: #fff; border-radius: 8px; padding: 16px; margin-top: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .panel select { font-size: 14px; padding: 4px; margin: 0 8px; }
  .big { font-size: 26px; font-weight: bold; margin-top: 10px; }
</style>
"""


DASHBOARD = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UAV Monitoring Dashboard</title>
<script src="/static/chart.umd.min.js"></script>
<link rel="stylesheet" href="/static/leaflet.css">
<script src="/static/leaflet.js"></script>
""" + STYLE + """
</head>
<body>
<header>
  <span>UAV Environmental Monitoring Dashboard</span>
  <select id="flightSel"><option value="">Latest flight (live)</option></select>
  <a href="/results">Results (RQ2)</a>
  <a href="/rq1">Results (RQ1)</a>
  <a id="exportLink" href="/export">Export CSV</a>
</header>
<div class="wrap">

  <div class="cards">
    <div class="card"><div class="label">Flight</div><div class="value" id="c_flight">-</div></div>
    <div class="card"><div class="label">Packet seq</div><div class="value" id="c_seq">-</div></div>
    <div class="card"><div class="label">Temp shielded (C)</div><div class="value" id="c_t1">-</div></div>
    <div class="card"><div class="label">Temp unshielded (C)</div><div class="value" id="c_t2">-</div></div>
    <div class="card"><div class="label">Humidity shielded (%)</div><div class="value" id="c_h1">-</div></div>
    <div class="card"><div class="label">Pressure (hPa)</div><div class="value" id="c_p1">-</div></div>
    <div class="card"><div class="label">Altitude (m)</div><div class="value" id="c_alt">-</div></div>
    <div class="card"><div class="label">Battery (V)</div><div class="value" id="c_bat">-</div></div>
    <div class="card"><div class="label">RSSI (dBm)</div><div class="value" id="c_rssi">-</div></div>
    <div class="card"><div class="label">Mode</div><div class="value" id="c_mode">-</div></div>
  </div>

  <div class="charts">
    <div class="box"><h3>Temperature (C)</h3><canvas id="chTemp"></canvas></div>
    <div class="box"><h3>Humidity (%)</h3><canvas id="chHum"></canvas></div>
    <div class="box"><h3>Altitude (m)</h3><canvas id="chAlt"></canvas></div>
    <div class="box"><h3>RSSI (dBm)</h3><canvas id="chRssi"></canvas></div>
  </div>

  <div class="panel">
    <b>Flight path (GPS)</b>
    <div id="map" style="height:400px;margin-top:8px"></div>
  </div>

  <div class="tablebox">
    <table>
      <thead>
        <tr>
          <th>seq</th><th>received</th><th>t1</th><th>h1</th><th>t2</th><th>h2</th>
          <th>p1</th><th>lat</th><th>lon</th><th>alt</th><th>bat</th><th>rssi</th><th>mode</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

</div>

<script>
const COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"];

function makeChart(id, names) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: {
      labels: [],
      datasets: names.map((n, i) => ({
        label: n, data: [], borderColor: COLORS[i], backgroundColor: COLORS[i],
        tension: 0.2, pointRadius: 1
      }))
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      scales: { x: { title: { display: true, text: "seq" } } }
    }
  });
}

const chTemp = makeChart("chTemp", ["t1 shielded", "t2 unshielded"]);
const chHum = makeChart("chHum", ["h1 shielded", "h2 unshielded"]);
const chAlt = makeChart("chAlt", ["altitude"]);
const chRssi = makeChart("chRssi", ["rssi"]);

const map = L.map("map").setView([10.9876, 106.6543], 16);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "OpenStreetMap"
}).addTo(map);
const path = L.polyline([], { color: "#d62728", weight: 3 }).addTo(map);
const marker = L.circleMarker([10.9876, 106.6543], {
  radius: 7, color: "#1f3b57", fillColor: "#1f77b4", fillOpacity: 1
}).addTo(map);
let fittedFlight = null;

function updateMap(rows, last) {
  const pts = rows.filter(r => r.lat && r.lon).map(r => [r.lat, r.lon]);
  if (pts.length === 0) return;
  path.setLatLngs(pts);
  marker.setLatLng(pts[pts.length - 1]);
  marker.bindTooltip("seq " + last.seq + " | alt " + last.alt + " m");
  if (fittedFlight !== last.flight_id) {
    map.fitBounds(path.getBounds(), { padding: [20, 20] });
    fittedFlight = last.flight_id;
  }
}

function setChart(chart, labels, series) {
  chart.data.labels = labels;
  series.forEach((s, i) => { chart.data.datasets[i].data = s; });
  chart.update();
}

function show(v) { return (v === null || v === undefined) ? "-" : v; }

function fmtTime(t) {
  if (!t) return "-";
  return new Date(t * 1000).toLocaleTimeString();
}

async function loadFlights() {
  const sel = document.getElementById("flightSel");
  const current = sel.value;
  const res = await fetch("/api/flights");
  const flights = await res.json();
  sel.innerHTML = '<option value="">Latest flight (live)</option>' + flights.map(f =>
    '<option value="' + f.id + '">Flight ' + f.id + ' - mode ' + f.mode + ' - ' +
    f.packets + ' pkts - ' + fmtTime(f.start_time) + '</option>'
  ).join("");
  sel.value = current;
}

async function refresh() {
  try {
    const fid = document.getElementById("flightSel").value;
    const res = await fetch("/api/data" + (fid ? "?flight=" + fid : ""));
    const rows = await res.json();
    if (rows.length === 0) return;

    const last = rows[rows.length - 1];
    document.getElementById("c_flight").textContent = show(last.flight_id);
    document.getElementById("c_seq").textContent = show(last.seq);
    document.getElementById("c_t1").textContent = show(last.t1);
    document.getElementById("c_t2").textContent = show(last.t2);
    document.getElementById("c_h1").textContent = show(last.h1);
    document.getElementById("c_p1").textContent = show(last.p1);
    document.getElementById("c_alt").textContent = show(last.alt);
    document.getElementById("c_bat").textContent = show(last.battery);
    document.getElementById("c_rssi").textContent = show(last.rssi);
    document.getElementById("c_mode").textContent = show(last.mode);

    const labels = rows.map(r => r.seq);
    setChart(chTemp, labels, [rows.map(r => r.t1), rows.map(r => r.t2)]);
    setChart(chHum, labels, [rows.map(r => r.h1), rows.map(r => r.h2)]);
    setChart(chAlt, labels, [rows.map(r => r.alt)]);
    setChart(chRssi, labels, [rows.map(r => r.rssi)]);
    updateMap(rows, last);
    document.getElementById("exportLink").href = "/export?flight=" + last.flight_id;

    const recent = rows.slice(-10).reverse();
    document.getElementById("tbody").innerHTML = recent.map(r =>
      "<tr><td>" + r.seq + "</td><td>" + fmtTime(r.recv_time) + "</td><td>" + r.t1 +
      "</td><td>" + r.h1 + "</td><td>" + r.t2 + "</td><td>" + r.h2 + "</td><td>" + r.p1 +
      "</td><td>" + r.lat + "</td><td>" + r.lon + "</td><td>" + r.alt + "</td><td>" + r.battery +
      "</td><td>" + show(r.rssi) + "</td><td>" + r.mode + "</td></tr>"
    ).join("");
  } catch (e) {
    console.log("refresh error", e);
  }
}

document.getElementById("flightSel").addEventListener("change", refresh);
loadFlights();
refresh();
setInterval(refresh, 2000);
setInterval(loadFlights, 5000);
</script>
</body>
</html>
"""


RESULTS = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RQ2 Results</title>
""" + STYLE + """
</head>
<body>
<header>
  <span>RQ2 Results - Fixed vs Adaptive Sampling</span>
  <a href="/">Back to dashboard</a>
  <a href="/rq1">Results (RQ1)</a>
</header>
<div class="wrap">

  <div class="tablebox">
    <table>
      <thead>
        <tr>
          <th>flight</th><th>mode</th><th>duration (s)</th><th>expected</th><th>received</th>
          <th>lost</th><th>PDR (%)</th><th>Packet Loss (%)</th><th>Latency (s)</th>
          <th>AoI (s)</th><th>Packets/min</th><th>Avg RSSI</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div class="panel">
    <b>Packet Reduction</b><br><br>
    Fixed flight: <select id="selF"></select>
    Adaptive flight: <select id="selA"></select>
    <div class="big" id="reduction">-</div>
    <div style="font-size:12px;color:#666;margin-top:6px">
      Reduction = (Packets/min Fixed - Packets/min Adaptive) / Packets/min Fixed
    </div>
  </div>

</div>

<script>
let results = [];

function show(v) { return (v === null || v === undefined) ? "-" : v; }

function calcReduction() {
  const f = results.find(r => r.flight_id == document.getElementById("selF").value);
  const a = results.find(r => r.flight_id == document.getElementById("selA").value);
  const out = document.getElementById("reduction");
  if (!f || !a || !f.rate || !a.rate) { out.textContent = "-"; return; }
  const red = (f.rate - a.rate) / f.rate * 100;
  out.textContent = red.toFixed(1) + " %";
}

async function load() {
  const res = await fetch("/api/results");
  results = await res.json();

  document.getElementById("tbody").innerHTML = results.map(r =>
    "<tr><td>" + r.flight_id + "</td><td>" + r.mode + "</td><td>" + r.duration +
    "</td><td>" + r.expected + "</td><td>" + r.received + "</td><td>" + r.lost +
    "</td><td>" + r.pdr + "</td><td>" + r.loss + "</td><td>" + r.latency +
    "</td><td>" + show(r.aoi) + "</td><td>" + show(r.rate) + "</td><td>" + show(r.rssi) + "</td></tr>"
  ).join("");

  const opt = list => list.map(r =>
    '<option value="' + r.flight_id + '">Flight ' + r.flight_id + ' (' + r.received + ' pkts)</option>'
  ).join("");
  document.getElementById("selF").innerHTML = opt(results.filter(r => r.mode === "F"));
  document.getElementById("selA").innerHTML = opt(results.filter(r => r.mode === "A"));
  calcReduction();
}

document.getElementById("selF").addEventListener("change", calcReduction);
document.getElementById("selA").addEventListener("change", calcReduction);
load();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD)


@app.route("/results")
def results_page():
    return render_template_string(RESULTS)


RQ1 = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RQ1 Results</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
""" + STYLE + """
</head>
<body>
<header>
  <span>RQ1 Results - Sensor Error, Shield and Calibration</span>
  <select id="flightSel"></select>
  <a href="/">Dashboard</a>
  <a href="/results">Results (RQ2)</a>
</header>
<div class="wrap">

  <div class="panel" id="info">Loading...</div>

  <div class="panel">
    <b>Error vs ground truth</b>
    <div style="font-size:12px;color:#666;margin:4px 0 8px">
      Raw = all matched packets. Calibration fitted on even rows, evaluated on odd rows.
    </div>
    <div class="tablebox">
      <table>
        <thead>
          <tr>
            <th>sensor</th><th>MAE raw</th><th>RMSE raw</th><th>Bias raw</th>
            <th>MAE test before cal</th><th>MAE test after cal</th>
            <th>RMSE test before cal</th><th>RMSE test after cal</th>
            <th>a</th><th>b</th><th>R2</th>
          </tr>
        </thead>
        <tbody id="tbErr"></tbody>
      </table>
    </div>
  </div>

  <div class="panel">
    <b>MAE by altitude (flight condition)</b>
    <div class="tablebox">
      <table>
        <thead>
          <tr><th>altitude</th><th>n</th><th>t1 shielded</th><th>t2 unshielded</th>
          <th>h1 shielded</th><th>h2 unshielded</th></tr>
        </thead>
        <tbody id="tbAlt"></tbody>
      </table>
    </div>
  </div>

  <div class="charts">
    <div class="box"><h3>Temperature vs ground truth (C)</h3><canvas id="chT"></canvas></div>
    <div class="box"><h3>Humidity vs ground truth (%)</h3><canvas id="chH"></canvas></div>
  </div>

</div>

<script>
const COLORS = ["#000000", "#1f77b4", "#d62728", "#2ca02c", "#ff7f0e"];
const NAMES = { t1: "t1 shielded", t2: "t2 unshielded", h1: "h1 shielded", h2: "h2 unshielded" };

function makeChart(id, names, dashed) {
  return new Chart(document.getElementById(id), {
    type: "line",
    data: { labels: [], datasets: names.map((n, i) => ({
      label: n, data: [], borderColor: COLORS[i], backgroundColor: COLORS[i],
      borderDash: dashed.includes(i) ? [5, 4] : [], tension: 0.2, pointRadius: 0
    })) },
    options: { animation: false, responsive: true, maintainAspectRatio: false,
      scales: { x: { title: { display: true, text: "seq" } } } }
  });
}

const chT = makeChart("chT", ["ground truth", "t1 shielded", "t2 unshielded", "t1 calibrated", "t2 calibrated"], [3, 4]);
const chH = makeChart("chH", ["ground truth", "h1 shielded", "h2 unshielded"], []);

function show(v) { return (v === null || v === undefined) ? "-" : v; }

function fmtTime(t) { return t ? new Date(t * 1000).toLocaleTimeString() : "-"; }

async function loadFlights() {
  const res = await fetch("/api/flights");
  const flights = await res.json();
  const sel = document.getElementById("flightSel");
  sel.innerHTML = flights.map(f =>
    '<option value="' + f.id + '">Flight ' + f.id + ' - mode ' + f.mode + ' - ' +
    f.packets + ' pkts - ' + fmtTime(f.start_time) + '</option>').join("");
}

async function load() {
  const fid = document.getElementById("flightSel").value;
  const res = await fetch("/api/rq1" + (fid ? "?flight=" + fid : ""));
  const d = await res.json();
  const info = document.getElementById("info");

  if (d.error) {
    info.textContent = "Cannot analyze: " + d.error;
    document.getElementById("tbErr").innerHTML = "";
    document.getElementById("tbAlt").innerHTML = "";
    return;
  }

  info.innerHTML = "Flight <b>" + d.flight_id + "</b>: " + d.uav_packets +
    " UAV packets, <b>" + d.matched + "</b> matched with ground truth";

  document.getElementById("tbErr").innerHTML = Object.keys(d.sensors).map(k => {
    const s = d.sensors[k];
    const rt = s.raw_test || {}, ct = s.calibrated_test || {};
    return "<tr><td>" + NAMES[k] + "</td><td>" + s.raw.mae + "</td><td>" + s.raw.rmse +
      "</td><td>" + s.raw.bias + "</td><td>" + show(rt.mae) + "</td><td>" + show(ct.mae) +
      "</td><td>" + show(rt.rmse) + "</td><td>" + show(ct.rmse) + "</td><td>" + show(s.a) +
      "</td><td>" + show(s.b) + "</td><td>" + show(s.r2) + "</td></tr>";
  }).join("");

  document.getElementById("tbAlt").innerHTML = d.by_alt.map(b =>
    "<tr><td>" + b.label + "</td><td>" + b.n + "</td><td>" + b.t1 + "</td><td>" + b.t2 +
    "</td><td>" + b.h1 + "</td><td>" + b.h2 + "</td></tr>").join("");

  const s = d.series;
  chT.data.labels = s.seq;
  [s.ref_t, s.t1, s.t2, s.t1_cal, s.t2_cal].forEach((v, i) => { chT.data.datasets[i].data = v || []; });
  chT.update();
  chH.data.labels = s.seq;
  [s.ref_h, s.h1, s.h2].forEach((v, i) => { chH.data.datasets[i].data = v; });
  chH.update();
}

document.getElementById("flightSel").addEventListener("change", load);
loadFlights().then(load);
</script>
</body>
</html>
"""


@app.route("/export")
def export_csv():
    fid = request.args.get("flight", type=int) or latest_flight_id()
    if fid is None:
        return "No flight data", 404
    rows = query("SELECT * FROM sensor_data WHERE flight_id = ? ORDER BY id", (fid,))
    if not rows:
        return "No data for this flight", 404

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=flight_{fid}.csv"})


@app.route("/export_ground")
def export_ground_csv():
    rows = query("SELECT * FROM ground_ref ORDER BY id")
    if not rows:
        return "No ground truth data", 404

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ground_truth.csv"})


@app.route("/rq1")
def rq1_page():
    return render_template_string(RQ1)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
