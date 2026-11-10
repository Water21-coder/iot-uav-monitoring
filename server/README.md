# Server: MQTT, Database and Web Dashboard

Receives, stores and displays data from the UAV and the ground truth station, and computes the research metrics for RQ1 and RQ2.

## Architecture

```
UAV (LoRa32) --LoRa--> Ground station (LoRa32) --WiFi--> MQTT broker (Mosquitto)
Ground truth (ESP32 + BME280) --WiFi----------------------> MQTT broker
                                                               |
                                                        subscriber.py
                                                               |
                                                         SQLite database
                                                               |
                                                     app.py (Flask web)
```

## Files

| File | Purpose |
|---|---|
| `subscriber.py` | Subscribes to MQTT topics, parses packets, detects flights, stores data in SQLite |
| `app.py` | Web dashboard, GPS map, RQ1 and RQ2 results pages, CSV export |
| `simulator.py` | Simulates UAV packets (Fixed or Adaptive sampling) for testing without hardware |
| `ground_sim.py` | Simulates the ground truth station for testing without hardware |
| `requirements.txt` | Python dependencies |

## Requirements

- Python 3.9 or newer
- Mosquitto MQTT broker 2.x

Install Python packages:

```
pip install -r requirements.txt
```

Mosquitto config (add to `mosquitto.conf`, then restart the service) so other devices on the network can connect:

```
listener 1883 0.0.0.0
allow_anonymous true
```

## How to run

Open separate terminals in the `server` folder and start in this order:

```
python subscriber.py
python app.py
```

Then start the data sources (real devices, or simulators for testing):

```
python ground_sim.py
python simulator.py --mode F --duration 300
python simulator.py --mode A --duration 300
```

Open the dashboard at `http://localhost:5000`.

| Page | Content |
|---|---|
| `/` | Live dashboard: sensor values, charts, GPS flight path, flight selector |
| `/results` | RQ2: PDR, Packet Loss, Latency, AoI, Packets/min, Packet Reduction |
| `/rq1` | RQ1: sensor error vs ground truth, MAE, RMSE, Linear Regression calibration, error by altitude |
| `/export?flight=ID` | Download one flight as CSV |
| `/export_ground` | Download all ground truth data as CSV |

## Data format

### UAV packet

Topic: `uav/01/sensor`

```
seq,gps_time,t1,h1,p1,t2,h2,p2,lat,lon,alt,battery,mode,rssi,snr
```

| Field | Meaning | Unit |
|---|---|---|
| seq | Packet sequence number, restarts at 1 each boot | - |
| gps_time | Send time from GPS, Unix timestamp | s |
| t1, h1, p1 | BME280 with shield: temperature, humidity, pressure | C, %, hPa |
| t2, h2, p2 | BME280 without shield: temperature, humidity, pressure | C, %, hPa |
| lat, lon | GPS position | degree |
| alt | Altitude | m |
| battery | Battery voltage | V |
| mode | `F` = Fixed sampling, `A` = Adaptive sampling | - |
| rssi, snr | Added by the ground station on receive (optional) | dBm, dB |

Example:

```
125,1731234567,31.2,68.5,1008.3,31.8,67.9,1008.1,10.9876,106.6543,25.4,11.8,F,-90.2,7.5
```

### Ground truth packet

Topic: `ground/ref`

```
seq,timestamp,temp,hum,pres
```

Example:

```
57,1731234567,31.05,68.12,1010.02
```

## Flight detection

A new flight is created automatically when the sequence number restarts, the sampling mode changes, or no UAV packet is received for more than 30 seconds.

## Metrics

| Metric | Definition |
|---|---|
| PDR | received packets / expected packets, expected = max(seq) - min(seq) + 1 |
| Packet Loss | 100% - PDR |
| Latency | receive time - GPS send time |
| AoI | time-average age of the latest received data at the server |
| Packet Reduction | (packets/min Fixed - packets/min Adaptive) / packets/min Fixed |
| MAE, RMSE | error of UAV sensor vs ground truth matched by nearest timestamp (within 5 s) |
| Calibration | Linear Regression `ref = a * raw + b`, fitted on even rows, evaluated on odd rows |

## Notes

- Timestamps are whole seconds, so Latency and AoI have about 1 second resolution.
- Ground truth is measured at ground level. Errors at higher altitude include the real vertical difference in temperature and humidity, not only sensor error.
- Chart.js and the map tiles load from the internet. Without internet the charts need a local copy of Chart.js, and the map background will be blank.
