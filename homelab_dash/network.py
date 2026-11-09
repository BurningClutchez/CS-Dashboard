"""Network card: interfaces, traffic rates, default gateway, DNS and Tailscale."""

import json
import os
import shutil
import socket
import struct
import time

from .util import CHILD_ENV, Card, CheckError, read_int, read_text, run_cmd, run_with_timeout

VIRTUAL_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet", "cni", "flannel",
                    "kube", "macvlan", "mac", "dummy", "tun", "tap")
KEEP_PREFIXES = ("bond", "wg", "tailscale", "br0", "eth", "en", "wl")


def list_interfaces(cfg):
    wanted = cfg.getlist("network", "interfaces")
    base = os.path.join(cfg.sys, "class/net")
    names = sorted(os.listdir(base))
    if wanted and wanted != ["auto"]:
        return [n for n in wanted]
    out = []
    for n in names:
        physical = os.path.exists(os.path.join(base, n, "device"))
        if n.startswith(VIRTUAL_PREFIXES) and not physical:
            continue
        if physical or n.startswith(KEEP_PREFIXES):
            out.append(n)
    return out


def ip_addresses(cfg):
    text, _ = run_cmd(["ip", "-j", "addr", "show"], timeout=cfg.check_timeout,
                      path_extra=cfg.bin_path)
    out = {}
    for link in json.loads(text or "[]"):
        addrs = []
        for a in link.get("addr_info", []):
            if a.get("scope") == "host":
                continue
            if a.get("family") == "inet6" and a.get("scope") == "link":
                continue
            addrs.append("%s/%s" % (a.get("local"), a.get("prefixlen")))
        out[link.get("ifname")] = addrs
    return out


def iface_stats(cfg, name, state, now):
    base = os.path.join(cfg.sys, "class/net", name)
    if not os.path.isdir(base):
        raise CheckError("interface %s not found" % name)
    rx = read_int(os.path.join(base, "statistics/rx_bytes"), 0)
    tx = read_int(os.path.join(base, "statistics/tx_bytes"), 0)
    speed = read_int(os.path.join(base, "speed"))
    row = {
        "name": name,
        "state": read_text(os.path.join(base, "operstate"), "unknown").strip(),
        "speed_mbps": speed if speed and speed > 0 else None,
        "rx_bytes": rx,
        "tx_bytes": tx,
        "rx_bps": None,
        "tx_bps": None,
    }
    prev = state.get(name)
    if prev and 0 < now - prev["t"] < 900 and rx >= prev["rx"] and tx >= prev["tx"]:
        dt = now - prev["t"]
        row["rx_bps"] = int((rx - prev["rx"]) * 8 / dt)
        row["tx_bps"] = int((tx - prev["tx"]) * 8 / dt)
    state[name] = {"t": now, "rx": rx, "tx": tx}
    # "unknown" is normal for tunnels (wg, tailscale) that have no carrier
    row["status"] = "ok" if row["state"] in ("up", "unknown") else "crit"
    return row


def default_gateway(cfg):
    for line in read_text(os.path.join(cfg.proc, "net/route")).splitlines()[1:]:
        p = line.split()
        if len(p) >= 3 and p[1] == "00000000" and int(p[3], 16) & 2:
            gw = socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
            return {"iface": p[0], "gateway": gw}
    return None


def dns_lookup(name):
    start = time.monotonic()
    try:
        infos = socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise CheckError("DNS lookup of %s failed: %s" % (name, exc.strerror or exc))
    ms = int((time.monotonic() - start) * 1000)
    return {"name": name, "ok": True, "ms": ms, "addr": infos[0][4][0] if infos else None}


def tailscale_available(cfg):
    path = (cfg.bin_path + ":" if cfg.bin_path else "") + CHILD_ENV["PATH"]
    return shutil.which("tailscale", path=path) is not None


def tailscale(cfg):
    text, _ = run_cmd(["tailscale", "status", "--json"], timeout=cfg.check_timeout,
                      ok_codes=(0, 1), path_extra=cfg.bin_path)
    try:
        data = json.loads(text)
    except ValueError:
        raise CheckError("tailscale status returned no JSON (is tailscaled running?)")
    me = data.get("Self") or {}
    peers = list((data.get("Peer") or {}).values())
    state = data.get("BackendState", "unknown")
    out = {
        "state": state,
        "online": bool(me.get("Online")),
        "hostname": me.get("HostName"),
        "dns_name": (me.get("DNSName") or "").rstrip("."),
        "ips": me.get("TailscaleIPs") or [],
        "peers_total": len(peers),
        "peers_online": sum(1 for p in peers if p.get("Online")),
        "health": data.get("Health") or [],
    }
    if state != "Running":
        out["status"] = "crit"
    elif out["health"]:
        out["status"] = "warn"
    else:
        out["status"] = "ok"
    return out


def collect(cfg, state):
    card = Card("network", "network", "Network")
    t = cfg.check_timeout
    now = time.time()
    names = card.run("interfaces", list_interfaces, t, cfg) or []
    addrs = card.run("ip addresses", ip_addresses, t, cfg) or {}
    nstate = state.setdefault("net", {})
    rows = []
    for n in names:
        row = card.run("iface " + n, iface_stats, t, cfg, n, nstate, now)
        if row:
            row["addrs"] = addrs.get(n, [])
            card.status(row["status"])
            rows.append(row)
    for gone in [k for k in nstate if k not in names]:
        nstate.pop(gone, None)
    card.data["interfaces"] = rows
    card.data["gateway"] = card.run("gateway", default_gateway, t, cfg)

    name = (cfg.get("network", "dns_test") or "").strip()
    if name:
        ok, res, _ = run_with_timeout(dns_lookup, min(t, 5), name)
        if ok:
            card.data["dns"] = res
        else:
            card.data["dns"] = {"name": name, "ok": False, "error": res}
            card.status("crit")

    ts = cfg.tristate("network", "tailscale")
    if ts == "yes" or (ts == "auto" and tailscale_available(cfg)):
        info = card.run("tailscale", tailscale, t, cfg)
        if info:
            card.status(info["status"])
        card.data["tailscale"] = info
    # totals for the history graph (sum over physical/listed interfaces, not tunnels)
    card.data["rx_bps"] = sum(r["rx_bps"] or 0 for r in rows if not r["name"].startswith(("tailscale", "wg")))
    card.data["tx_bps"] = sum(r["tx_bps"] or 0 for r in rows if not r["name"].startswith(("tailscale", "wg")))
    return card.to_dict()
