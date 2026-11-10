import sqlite3
import time

import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
UAV_TOPIC = "uav/01/sensor"
GROUND_TOPIC = "ground/ref"
DB_FILE = "uav_data.db"
FLIGHT_GAP_SECONDS = 30

# UAV packet (13 fields, ground station may append rssi,snr -> 15 fields):
# seq,gps_time,t1,h1,p1,t2,h2,p2,lat,lon,alt,battery,mode[,rssi,snr]
# Ground truth packet (5 fields):
# seq,timestamp,temp,hum,pres


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time REAL,
            mode TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seq INTEGER,
            gps_time INTEGER,
            t1 REAL, h1 REAL, p1 REAL,
            t2 REAL, h2 REAL, p2 REAL,
            lat REAL, lon REAL, alt REAL,
            battery REAL,
            mode TEXT,
            rssi REAL,
            snr REAL,
            recv_time REAL,
            flight_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ground_ref (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seq INTEGER,
            ts INTEGER,
            temp REAL,
            hum REAL,
            pres REAL,
            recv_time REAL
        )
    """)
    columns = [c[1] for c in conn.execute("PRAGMA table_info(sensor_data)").fetchall()]
    if "flight_id" not in columns:
        conn.execute("ALTER TABLE sensor_data ADD COLUMN flight_id INTEGER")
    conn.commit()
    return conn


def parse_uav(text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) not in (13, 15):
        raise ValueError(f"expected 13 or 15 fields, got {len(parts)}")

    seq = int(parts[0])
    gps_time = int(parts[1])
    values = [float(x) for x in parts[2:12]]
    mode = parts[12]

    rssi = None
    snr = None
    if len(parts) == 15:
        rssi = float(parts[13])
        snr = float(parts[14])

    return [seq, gps_time] + values + [mode, rssi, snr]


def parse_ground(text):
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 5:
        raise ValueError(f"expected 5 fields, got {len(parts)}")
    return [int(parts[0]), int(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])]


def is_new_flight(state, seq, mode, recv_time):
    if state["flight_id"] is None:
        return True
    if seq <= state["last_seq"]:
        return True
    if mode != state["mode"]:
        return True
    if recv_time - state["last_recv"] > FLIGHT_GAP_SECONDS:
        return True
    return False


def handle_uav(conn, state, text, recv_time):
    try:
        row = parse_uav(text)
    except ValueError as e:
        print(f"Skip bad UAV packet: {text} ({e})")
        return

    seq = row[0]
    mode = row[12]

    if is_new_flight(state, seq, mode, recv_time):
        cur = conn.execute("INSERT INTO flights (start_time, mode) VALUES (?, ?)",
                           (recv_time, mode))
        state["flight_id"] = cur.lastrowid
        state["mode"] = mode
        print(f"=== New flight id={state['flight_id']} mode={mode} ===")

    state["last_seq"] = seq
    state["last_recv"] = recv_time

    conn.execute("""
        INSERT INTO sensor_data
        (seq, gps_time, t1, h1, p1, t2, h2, p2,
         lat, lon, alt, battery, mode, rssi, snr, recv_time, flight_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, row + [recv_time, state["flight_id"]])
    conn.commit()

    print(f"UAV    flight={state['flight_id']} seq={seq} t1={row[2]} alt={row[10]} mode={mode}")


def handle_ground(conn, text, recv_time):
    try:
        row = parse_ground(text)
    except ValueError as e:
        print(f"Skip bad ground packet: {text} ({e})")
        return

    conn.execute("""
        INSERT INTO ground_ref (seq, ts, temp, hum, pres, recv_time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, row + [recv_time])
    conn.commit()

    print(f"GROUND seq={row[0]} temp={row[2]} hum={row[3]}")


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"Connected to broker {BROKER}:{PORT}")
        client.subscribe(UAV_TOPIC)
        client.subscribe(GROUND_TOPIC)
        print(f"Subscribed to {UAV_TOPIC} and {GROUND_TOPIC}")
    else:
        print(f"Connect failed: {reason_code}")


def on_message(client, userdata, msg):
    recv_time = time.time()
    text = msg.payload.decode("utf-8", errors="replace").strip()

    if msg.topic == UAV_TOPIC:
        handle_uav(userdata["conn"], userdata["state"], text, recv_time)
    elif msg.topic == GROUND_TOPIC:
        handle_ground(userdata["conn"], text, recv_time)


def main():
    conn = init_db()
    state = {"flight_id": None, "last_seq": 0, "last_recv": 0.0, "mode": None}

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="uav-subscriber")
    client.user_data_set({"conn": conn, "state": state})
    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(BROKER, PORT, keepalive=60)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("Stopped")
    finally:
        client.disconnect()
        conn.close()


if __name__ == "__main__":
    main()
