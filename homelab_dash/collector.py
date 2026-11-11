"""Collector: run every check once, write status.json and history.json, exit.

Started by homelab-dash-collector.timer about every 60 s. Every card runs in
its own thread with a deadline, so one hung check (a dead NFS mount, a
smartctl stuck on a USB bridge) only blanks its own card.
"""

import argparse
import json
import os
import sys
import threading
import time

from . import SCHEMA, __version__, addresses, apps, disks, network, security, services, system
from .config import ConfigError, default_config_path, load
from .util import atomic_write_json, worst

HISTORY_FIELDS = ["t", "cpu", "mem", "load", "temp", "disk", "rx", "tx"]


def card_jobs(cfg):
    jobs = [("system", "System", system.collect),
            ("disks", "Disks", disks.collect)]
    if cfg.sections("service"):
        jobs.append(("services", "Services", services.collect_services))
    if services.docker_enabled(cfg):
        jobs.append(("docker", "Docker", services.collect_docker))
    jobs.append(("network", "Network", network.collect))
    if cfg.has("pihole"):
        jobs.append(("pihole", "Pi-hole", apps.collect_pihole))
    if cfg.sections("media"):
        jobs.append(("media", "Media", apps.collect_media))
    if cfg.sections("game"):
        jobs.append(("games", "Game servers", apps.collect_games))
    if cfg.tristate("security", "enabled") != "no":
        jobs.append(("security", "Security", security.collect))
    return jobs


def error_card(card_id, title, message):
    return {"id": card_id, "type": "error", "title": title, "status": "error",
            "errors": [{"check": card_id, "error": message}], "data": {}}


def run_cards(cfg, state, deadline):
    jobs = card_jobs(cfg)
    results = {}
    threads = []

    def worker(card_id, title, func):
        try:
            results[card_id] = func(cfg, state)
        except Exception as exc:  # noqa: BLE001 - never let one card kill the run
            results[card_id] = error_card(card_id, title, "%s: %s" % (type(exc).__name__, exc))

    for card_id, title, func in jobs:
        th = threading.Thread(target=worker, args=(card_id, title, func), daemon=True,
                              name="card-" + card_id)
        th.start()
        threads.append(th)
    end = time.monotonic() + deadline
    for th in threads:
        th.join(max(0.0, end - time.monotonic()))

    cards = []
    for card_id, title, _ in jobs:
        res = results.get(card_id)
        if res is None:
            cards.append(error_card(card_id, title, "check did not finish within %ss" % deadline))
        elif isinstance(res, list):
            cards.extend(res)
        else:
            cards.append(res)
    return cards


def history_sample(now, cards):
    by = {c["id"]: c.get("data") or {} for c in cards}
    s, d, n = by.get("system", {}), by.get("disks", {}), by.get("network", {})

    def r(v, nd=1):
        return round(v, nd) if isinstance(v, (int, float)) else None

    return [int(now), r(s.get("cpu_pct")), r((s.get("mem") or {}).get("pct")),
            r((s.get("load") or [None])[0], 2), r(s.get("temp_max")), r(d.get("max_pct")),
            n.get("rx_bps"), n.get("tx_bps")]


def update_history(path, sample, hours):
    hist = {"fields": HISTORY_FIELDS, "samples": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("fields") == HISTORY_FIELDS:
            hist["samples"] = old.get("samples") or []
    except (OSError, ValueError, AttributeError):
        pass  # missing or corrupt: start over rather than fail
    cutoff = sample[0] - hours * 3600
    hist["samples"] = [x for x in hist["samples"] if isinstance(x, list) and x and cutoff < x[0] < sample[0]]
    hist["samples"].append(sample)
    hist["hours"] = hours
    return hist


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def snapshot(obj):
    # threads that missed the deadline may still touch state; retry the copy
    for _ in range(5):
        try:
            return json.loads(json.dumps(obj))
        except RuntimeError:
            time.sleep(0.05)
    return {}


def collect_once(cfg):
    started = time.monotonic()
    now = time.time()
    data_dir = cfg.data_dir
    state_path = os.path.join(data_dir, "state.json")
    state = load_state(state_path)
    deadline = cfg.getint("general", "deadline", 30)
    cards = run_cards(cfg, state, deadline)
    # Built from the other cards' data, so it runs after them.
    try:
        extra = addresses.build(cfg, cards, state)
    except Exception as exc:  # noqa: BLE001
        extra = error_card("addresses", "LAN addresses", "%s: %s" % (type(exc).__name__, exc))
    if extra:
        cards.append(extra)
    host = next((c["data"].get("hostname") for c in cards if c["id"] == "system"), None)
    status = {
        "schema": SCHEMA,
        "version": __version__,
        "generated_at": round(now, 3),
        "hostname": host or "server",
        "interval": cfg.getint("general", "interval", 60),
        "stale_after": cfg.getint("general", "stale_after", 180),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "status": worst([c["status"] for c in cards if c["status"] != "unknown"]),
        "cards": cards,
    }
    hist = update_history(os.path.join(data_dir, "history.json"), history_sample(now, cards),
                          cfg.getint("general", "history_hours", 24))
    return status, hist, snapshot(state)


def print_summary(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError) as exc:
        print("cannot read %s: %s" % (path, exc))
        return 1
    age = time.time() - st.get("generated_at", 0)
    print("%s: written %.0f s ago by v%s in %d ms, overall %s%s"
          % (path, age, st.get("version"), st.get("duration_ms", 0), st.get("status"),
             "  <-- STALE" if age > st.get("stale_after", 180) else ""))
    for c in st.get("cards", []):
        print("  %-16s %-7s %s" % (c["id"], c["status"], "%d error(s)" % len(c["errors"]) if c["errors"] else ""))
        for e in c["errors"]:
            print("      - %s: %s" % (e["check"], e["error"]))
    return 0 if age <= st.get("stale_after", 180) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-dash collector (read-only)")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--dry-run", action="store_true",
                    help="run all checks and print the JSON; write nothing")
    ap.add_argument("--check-config", action="store_true",
                    help="only validate the config file and list what will be checked")
    ap.add_argument("--summary", action="store_true",
                    help="print a short summary of the last status.json (age, cards, errors)")
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    if args.check_config:
        print("config OK: %s" % args.config)
        for card_id, title, _ in card_jobs(cfg):
            print("  card: %s" % title)
        for prefix in ("service", "media", "game"):
            for name, sec in cfg.sections(prefix):
                print("    %s:%s (%s)" % (prefix, name, sec.get("type") or sec.get("protocol")))
        if cfg.has("pihole"):
            print("    pihole: %s" % cfg.get("pihole", "url"))
        return 0
    if args.summary:
        return print_summary(os.path.join(cfg.data_dir, "status.json"))
    status, hist, state = collect_once(cfg)
    if args.dry_run:
        json.dump(status, sys.stdout, indent=2)
        sys.stdout.write("\n")
        bad = [c for c in status["cards"] if c["errors"]]
        print("\n# dry run: %d cards, %d with errors, %d ms, nothing written"
              % (len(status["cards"]), len(bad), status["duration_ms"]), file=sys.stderr)
        for c in bad:
            for e in c["errors"]:
                print("#   [%s] %s: %s" % (c["id"], e["check"], e["error"]), file=sys.stderr)
        return 0
    d = cfg.data_dir
    try:
        atomic_write_json(os.path.join(d, "state.json"), state, mode=0o600)
        atomic_write_json(os.path.join(d, "history.json"), hist, mode=0o644)
        atomic_write_json(os.path.join(d, "status.json"), status, mode=0o644)
    except OSError as exc:
        print("cannot write to %s: %s" % (d, exc), file=sys.stderr)
        return 1
    errs = sum(len(c["errors"]) for c in status["cards"])
    print("ok: %d cards, %d check errors, overall %s, %d ms"
          % (len(status["cards"]), errs, status["status"], status["duration_ms"]))
    return 0
