import argparse
import math
import random
import time

import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
TOPIC = "uav/01/sensor"

LAT0 = 10.9876
LON0 = 106.6543
ORBIT_DEG = 0.003          # about 330 m radius
FLIGHT_PERIOD = 300        # seconds for one climb-hover-descend cycle
MAX_ALT = 60.0


def read_sensors(t):
    alt = MAX_ALT * (1 - math.cos(2 * math.pi * t / FLIGHT_PERIOD)) / 2

    now = time.time()
    base_t = 31.0 - alt * 0.01 + 0.3 * math.sin(now / 40)
    t1 = base_t + random.gauss(0, 0.1)
    t2 = base_t + 0.6 + random.gauss(0, 0.3)

    base_h = 68.0 + alt * 0.02 + 0.5 * math.sin(now / 50)
    h1 = base_h + random.gauss(0, 0.2)
    h2 = base_h - 1.0 + random.gauss(0, 0.5)

    base_p = 1010.0 - alt * 0.12
    p1 = base_p + random.gauss(0, 0.05)
    p2 = base_p + random.gauss(0, 0.1)

    angle = t / 50
    lat = LAT0 + ORBIT_DEG * math.cos(angle)
    lon = LON0 + ORBIT_DEG * math.sin(angle)

    battery = max(10.5, 12.6 - t * 0.002)

    horizontal_m = ORBIT_DEG * 111000
    dist = math.sqrt(horizontal_m ** 2 + alt ** 2)
    rssi = -40 - 20 * math.log10(max(dist, 1)) + random.gauss(0, 2)
    snr = max(-15.0, min(12.0, 10 - (-rssi - 80) * 0.5 + random.gauss(0, 1)))

    return {
        "t1": t1, "h1": h1, "p1": p1,
        "t2": t2, "h2": h2, "p2": p2,
        "lat": lat, "lon": lon, "alt": alt,
        "battery": battery, "rssi": rssi, "snr": snr,
    }


def is_lost(rssi):
    loss_prob = 0.02 + max(0.0, -rssi - 95) * 0.05
    return random.random() < min(loss_prob, 0.9)


def build_packet(seq, r, mode):
    gps_time = int(time.time())
    return (
        f"{seq},{gps_time},"
        f"{r['t1']:.2f},{r['h1']:.2f},{r['p1']:.2f},"
        f"{r['t2']:.2f},{r['h2']:.2f},{r['p2']:.2f},"
        f"{r['lat']:.6f},{r['lon']:.6f},{r['alt']:.1f},"
        f"{r['battery']:.2f},{mode},"
        f"{r['rssi']:.1f},{r['snr']:.1f}"
    )


def should_send_adaptive(r, last_sent, now, last_time):
    if last_sent is None:
        return True
    if abs(r["t1"] - last_sent["t1"]) >= 0.2:
        return True
    if abs(r["h1"] - last_sent["h1"]) >= 0.5:
        return True
    if now - last_time >= 10:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="UAV data simulator")
    parser.add_argument("--mode", choices=["F", "A"], default="F",
                        help="F = Fixed sampling, A = Adaptive sampling")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Fixed mode send interval in seconds")
    parser.add_argument("--duration", type=float, default=0,
                        help="Run time in seconds, 0 = run until Ctrl+C")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="uav-simulator")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    print(f"Simulator started, mode={args.mode}, topic={TOPIC}")

    start = time.time()
    seq = 0
    sent = 0
    lost = 0
    last_sent = None
    last_time = 0.0
    step = args.interval if args.mode == "F" else 1.0

    try:
        while True:
            now = time.time()
            t = now - start
            if args.duration and t >= args.duration:
                break

            r = read_sensors(t)

            if args.mode == "A" and not should_send_adaptive(r, last_sent, now, last_time):
                time.sleep(step)
                continue

            seq += 1
            last_sent = r
            last_time = now

            if is_lost(r["rssi"]):
                lost += 1
                print(f"seq={seq} LOST (simulated) rssi={r['rssi']:.1f}")
            else:
                packet = build_packet(seq, r, args.mode)
                client.publish(TOPIC, packet)
                sent += 1
                print(f"seq={seq} sent alt={r['alt']:.1f} t1={r['t1']:.2f} rssi={r['rssi']:.1f}")

            time.sleep(step)
    except KeyboardInterrupt:
        pass
    finally:
        total = sent + lost
        pdr = (sent / total * 100) if total else 0
        print(f"Stopped. total={total} sent={sent} lost={lost} PDR={pdr:.1f}%")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
