import argparse
import math
import random
import time

import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883
TOPIC = "ground/ref"


def true_environment(now):
    temp = 31.0 + 0.3 * math.sin(now / 40)
    hum = 68.0 + 0.5 * math.sin(now / 50)
    pres = 1010.0
    return temp, hum, pres


def main():
    parser = argparse.ArgumentParser(description="Ground truth station simulator")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Send interval in seconds")
    parser.add_argument("--duration", type=float, default=0,
                        help="Run time in seconds, 0 = run until Ctrl+C")
    args = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ground-simulator")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()

    print(f"Ground truth simulator started, topic={TOPIC}")

    start = time.time()
    seq = 0

    try:
        while True:
            now = time.time()
            if args.duration and now - start >= args.duration:
                break

            temp, hum, pres = true_environment(now)
            temp += random.gauss(0, 0.05)
            hum += random.gauss(0, 0.1)
            pres += random.gauss(0, 0.03)

            seq += 1
            packet = f"{seq},{int(now)},{temp:.2f},{hum:.2f},{pres:.2f}"
            client.publish(TOPIC, packet)
            print(f"seq={seq} sent temp={temp:.2f} hum={hum:.2f}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"Stopped. sent={seq}")
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
