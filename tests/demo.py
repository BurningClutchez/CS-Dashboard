#!/usr/bin/env python3
"""Preview the dashboard with FAKE data on any computer (no install, no root).

    python3 tests/demo.py            # then open http://127.0.0.1:8088/
    python3 tests/demo.py --port 9000

It builds a fake server in a temp folder, runs the real collector against it
every 30 s, fills 24 h of made-up history, and starts the real web server on
127.0.0.1 only. Press Ctrl+C to stop; the temp folder is deleted.
"""

import argparse
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fakesys  # noqa: E402
from homelab_dash import collector, config, webserver  # noqa: E402
from homelab_dash.util import atomic_write_json  # noqa: E402


def fake_history(now, hours=24):
    rnd = random.Random(7)
    samples = []
    t = now - hours * 3600
    while t < now - 60:
        h = (t / 3600.0) % 24
        if now - 9 * 3600 < t < now - 9 * 3600 + 240:  # a "reboot": 4 minutes without data
            t += 60
            continue
        busy = 0.5 + 0.5 * math.sin((h - 15) / 24 * 2 * math.pi)
        cpu = max(1, 6 + 30 * busy + rnd.gauss(0, 4) + (45 if 2 <= h < 2.5 else 0))
        samples.append([int(t), round(cpu, 1), round(42 + 8 * busy + rnd.gauss(0, 1), 1),
                        round(0.3 + 1.6 * busy + abs(rnd.gauss(0, .2)), 2),
                        round(44 + 10 * busy + rnd.gauss(0, 1.2), 1), round(21.5 + (t - now + hours * 3600) / 3600 * 0.02, 1),
                        int(max(0, 2e6 + 4e7 * busy * busy + rnd.gauss(0, 3e6))),
                        int(max(0, 5e5 + 6e6 * busy + rnd.gauss(0, 8e5)))])
        t += 60
    return {"fields": collector.HISTORY_FIELDS, "samples": samples, "hours": hours}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--once", action="store_true", help="collect once and exit (prints the data folder)")
    args = ap.parse_args()
    base = tempfile.mkdtemp(prefix="homelab-dash-demo-")
    try:
        paths = fakesys.build_root(base)
        secrets = fakesys.write_secrets(base)
        sock = os.path.join(base, "docker.sock")
        fakesys.start_docker(sock)
        servers = fakesys.start_all()
        servers["webport"] = args.port
        cfg_path = fakesys.write_config(base, paths, sock, servers, secrets, check_timeout=2)
        cfg = config.load(cfg_path)
        data = cfg.data_dir
        now = time.time()
        atomic_write_json(os.path.join(data, "history.json"), fake_history(now))

        def collect():
            fakesys.bump_counters(paths)
            status, hist, state = collector.collect_once(cfg)
            atomic_write_json(os.path.join(data, "state.json"), state, 0o600)
            atomic_write_json(os.path.join(data, "history.json"), hist)
            atomic_write_json(os.path.join(data, "status.json"), status)

        collect()
        collect()  # second run so traffic rates appear
        if args.once:
            print(data)
            return 0

        def loop():
            while True:
                time.sleep(30)
                collect()

        threading.Thread(target=loop, daemon=True).start()
        srv = webserver.make_server(cfg, bind="127.0.0.1", port=args.port)
        print("demo dashboard (FAKE data): http://127.0.0.1:%d/   Ctrl+C to stop" % args.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
