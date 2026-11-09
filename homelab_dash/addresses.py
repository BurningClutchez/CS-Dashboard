"""LAN addresses card: free IPs left on Docker macvlan/ipvlan networks, and the
local domains in use (default *.home.arpa) as clickable links.

Built after the other cards have run, from data they already fetched (Docker
networks, Pi-hole local DNS) plus two local files (/etc/hosts and the kernel
neighbor table /proc/net/arp). It sends nothing on the network itself.
"""

import ipaddress
import os
import re

from .util import Card, CheckError, read_text

MAX_POOL = 65536          # don't enumerate pools bigger than a /16
MAX_RANGES = 40           # free ranges listed per network (the count is always exact)
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _ip(text):
    try:
        return ipaddress.ip_address((text or "").split("/")[0])
    except ValueError:
        return None


def arp_neighbors(cfg):
    """IPv4 neighbors this server has recently seen (complete ARP entries)."""
    seen = set()
    for line in read_text(os.path.join(cfg.proc, "net/arp"), "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[2] != "0x0" and p[3] != "00:00:00:00:00:00":
            ip = _ip(p[0])
            if ip:
                seen.add(ip)
    return seen


def compress(ips):
    """Sorted IPs -> ["a.b.c.d", "a.b.c.e-a.b.c.f", ...]."""
    out, start, prev = [], None, None
    for ip in ips:
        if start is None:
            start = prev = ip
        elif int(ip) == int(prev) + 1:
            prev = ip
        else:
            out.append(str(start) if start == prev else "%s-%s" % (start, prev))
            start = prev = ip
    if start is not None:
        out.append(str(start) if start == prev else "%s-%s" % (start, prev))
    return out


def free_ips(net, host_ips, neighbors, warn_free):
    """Work out free addresses for one macvlan/ipvlan network from Docker's data."""
    subnet = ipaddress.ip_network(net["subnet"], strict=False)
    if subnet.version != 4:
        raise CheckError("%s: IPv6 subnets are not listed" % net["name"])
    pool = ipaddress.ip_network(net["ip_range"], strict=False) if net.get("ip_range") else subnet
    if pool.num_addresses > MAX_POOL:
        raise CheckError("%s: pool %s is too large to list" % (net["name"], pool))
    used, reserved = [], set()
    for c in net.get("containers") or []:
        ip = _ip(c.get("ip"))
        if ip and ip in subnet:
            reserved.add(ip)
            used.append({"ip": str(ip), "name": c.get("name"),
                         "note": "" if c.get("running") else "stopped, IP reserved"})
    gw = _ip(net.get("gateway"))
    if gw:
        reserved.add(gw)
    for a in net.get("aux") or []:
        ip = _ip(a)
        if ip:
            reserved.add(ip)
    own = {ip for ip in host_ips if ip in subnet}
    lan = {ip for ip in neighbors if ip in pool} - reserved - own
    hosts = list(pool.hosts()) if pool.num_addresses > 2 else list(pool)
    free = [ip for ip in hosts if ip not in reserved and ip not in own and ip not in lan]
    ranges = compress(free)
    status = "crit" if not free else ("warn" if len(free) < warn_free else "ok")
    notes = []
    if not net.get("ip_range"):
        notes.append("no ip_range set: Docker may hand out addresses your router's DHCP also uses")
    if lan:
        notes.append("%d address(es) in the pool are used by other LAN devices (seen by this server)" % len(lan))
    return {
        "name": net["name"], "driver": net.get("driver"), "parent": net.get("parent"),
        "subnet": str(subnet), "pool": str(pool), "gateway": str(gw) if gw else None,
        "pool_size": len(hosts), "free_count": len(free),
        "free_ranges": ranges[:MAX_RANGES], "more_ranges": max(0, len(ranges) - MAX_RANGES),
        "used": sorted(used, key=lambda u: int(ipaddress.ip_address(u["ip"]))),
        "lan_seen": len(lan), "status": status, "notes": notes,
    }


def hosts_file_names(path):
    out = []
    for line in read_text(path, "").splitlines():
        line = line.split("#", 1)[0].split()
        if len(line) >= 2:
            for name in line[1:]:
                out.append({"name": name, "target": line[0], "type": "hosts"})
    return out


def domain_list(cfg, records):
    suffix = (cfg.get("domains", "suffix", "home.arpa") or "home.arpa").strip().strip(".").lower()
    scheme = (cfg.get("domains", "scheme", "http") or "http").strip().lower()
    if scheme not in ("http", "https"):
        raise CheckError("[domains] scheme must be http or https")
    links = {}
    for item in cfg.getlist("domains", "links"):
        name, sep, url = item.partition("=")
        if sep and re.match(r"^https?://", url):
            links[name.lower()] = url
    hide = {h.lower() for h in cfg.getlist("domains", "hide")}
    by_name = {}
    for r in records:
        name = (r.get("name") or "").strip().rstrip(".").lower()
        if not (name == suffix or name.endswith("." + suffix)) or name in hide or not _NAME_RE.match(name):
            continue
        d = by_name.setdefault(name, {"name": name, "targets": [], "sources": []})
        if r.get("target") and r["target"] not in d["targets"]:
            d["targets"].append(r["target"])
        if r["type"] not in d["sources"]:
            d["sources"].append(r["type"])
    out = []
    for name in sorted(by_name):
        d = by_name[name]
        d["url"] = links.get(name, "%s://%s/" % (scheme, name))
        out.append(d)
    return suffix, out


def build(cfg, cards, state):
    """Return the card dict, or None when there is nothing to show."""
    mode = cfg.tristate("addresses", "enabled")
    if mode == "no":
        return None
    by_id = {c["id"]: c for c in cards}
    docker = by_id.get("docker") or {}
    nets = (docker.get("data") or {}).get("vlan_networks") or []
    pihole = by_id.get("pihole")
    want_domains = cfg.tristate("domains", "enabled") != "no"
    if mode == "auto" and not nets and not want_domains:
        return None  # nothing could appear: skip the work
    card = Card("addresses", "addresses", cfg.get("addresses", "label", "LAN addresses"))
    t = cfg.check_timeout

    # ---- free IPs on macvlan / ipvlan networks
    if docker.get("status") == "error":
        card.errors.append({"check": "macvlan", "error": "Docker data unavailable this run"})
    host_ips = set()
    for i in ((by_id.get("network") or {}).get("data") or {}).get("interfaces") or []:
        for a in i.get("addrs") or []:
            ip = _ip(a)
            if ip:
                host_ips.add(ip)
    neighbors = card.run("neighbor table", arp_neighbors, t, cfg) or set() if nets else set()
    warn_free = cfg.getint("addresses", "warn_free", 5)
    rows = []
    for net in nets:
        if not net.get("subnet"):
            continue
        res = card.run("free IPs " + net["name"], free_ips, t, net, host_ips, neighbors, warn_free)
        if res:
            card.status(res["status"])
            rows.append(res)
    card.data["networks"] = rows

    # ---- local domains (clickable)
    if want_domains:
        records = []
        if pihole is not None:
            if pihole.get("status") == "error":
                card.errors.append({"check": "domains", "error": "Pi-hole data unavailable this run"})
            records += (pihole.get("data") or {}).get("local_dns") or []
        hosts_path = cfg.get("domains", "hosts_file", "") or os.path.join(cfg.etc, "hosts")
        records += card.run("hosts file", hosts_file_names, t, hosts_path) or []
        records += [{"name": n, "target": None, "type": "config"} for n in cfg.getlist("domains", "extra")]
        res = card.run("domains", domain_list, t, cfg, records)
        if res:
            card.data["suffix"], card.data["domains"] = res
    card.data["domains_enabled"] = want_domains
    if mode == "auto" and not rows and not card.data.get("domains") and not card.errors:
        return None  # no macvlan networks and no local domains: no card
    return card.to_dict()
