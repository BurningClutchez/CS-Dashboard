"""Read-only auto-detection, run once during installation (sudo python3 detect.py).

It looks at this host (OS, CPU, RAM, drives, sensors, firewall, network,
Tailscale, systemd services, Docker stacks/sidecars/add-ons/macvlan,
LinuxGSM game servers) and prints a report plus a *draft* config.ini.
It changes nothing on the system; you review the draft before using it.
"""

import argparse
import glob
import ipaddress
import json
import os
import platform
import pwd
import re
import shutil
import socket
import sys

from .config import REAL_FS
from .util import CHILD_ENV, CheckError, read_text, run_cmd, unix_http_get_json

KNOWN_UNITS = [
    # (unit(s), section name, label, group)
    (["smbd", "nmbd"], "samba", "Samba file shares", "File sharing"),
    (["nfs-server"], "nfs", "NFS server", "File sharing"),
    (["jellyfin"], "jellyfin", "Jellyfin", "Media"),
    (["plexmediaserver"], "plex", "Plex", "Media"),
    (["emby-server"], "emby", "Emby", "Media"),
    (["minidlna"], "minidlna", "MiniDLNA", "Media"),
    (["pihole-FTL"], "pihole", "Pi-hole", "Network"),
    (["AdGuardHome"], "adguard", "AdGuard Home", "Network"),
    (["unbound"], "unbound", "Unbound DNS", "Network"),
    (["tailscaled"], "tailscale", "Tailscale", "Network"),
    (["ssh", "sshd"], "ssh", "SSH", "System"),
    (["docker"], "docker", "Docker engine", "System"),
    (["smartmontools", "smartd"], "smartd", "SMART monitor (smartd)", "System"),
    (["zfs-zed"], "zed", "ZFS event daemon", "System"),
    (["fail2ban"], "fail2ban", "fail2ban", "System"),
    (["cron", "crond"], "cron", "cron", "System"),
    (["uptime-kuma"], "uptime-kuma", "Uptime Kuma", "Monitoring"),
    (["netdata"], "netdata", "Netdata", "Monitoring"),
    (["grafana-server"], "grafana", "Grafana", "Monitoring"),
    (["prometheus"], "prometheus", "Prometheus", "Monitoring"),
    (["home-assistant", "homeassistant"], "home-assistant", "Home Assistant", "Home"),
    (["mosquitto"], "mosquitto", "Mosquitto MQTT", "Home"),
    (["zigbee2mqtt"], "zigbee2mqtt", "Zigbee2MQTT", "Home"),
    (["qbittorrent-nox"], "qbittorrent", "qBittorrent", "Downloads"),
    (["transmission-daemon"], "transmission", "Transmission", "Downloads"),
    (["sonarr"], "sonarr", "Sonarr", "Downloads"),
    (["radarr"], "radarr", "Radarr", "Downloads"),
    (["prowlarr"], "prowlarr", "Prowlarr", "Downloads"),
    (["sabnzbd"], "sabnzbd", "SABnzbd", "Downloads"),
    (["nginx"], "nginx", "nginx", "Web"),
    (["caddy"], "caddy", "Caddy", "Web"),
    (["apache2", "httpd"], "apache", "Apache", "Web"),
    (["postgresql"], "postgresql", "PostgreSQL", "Databases"),
    (["mariadb", "mysql"], "mariadb", "MariaDB/MySQL", "Databases"),
    (["redis-server"], "redis", "Redis", "Databases"),
]

# Docker images we know how to get stats from: (regex, kind, default container port)
KNOWN_IMAGES = [
    (r"(^|/)jellyfin(:|$)|linuxserver/jellyfin|jellyfin/jellyfin", "jellyfin", 8096),
    (r"plexinc/pms-docker|linuxserver/plex|(^|/)plex(:|$)", "plex", 32400),
    (r"emby/embyserver|linuxserver/emby", "emby", 8096),
    (r"pihole/pihole", "pihole", 80),
    (r"louislam/uptime-kuma", "uptime-kuma", 3001),
]

MINECRAFT_LGSM = {"mcserver", "pmcserver", "vpmcserver", "wmcserver"}
NO_QUERY_LGSM = {"mcbserver"}  # Bedrock (UDP, no Steam query)
QUERY_OFFSET_LGSM = {"vhserver": 1}  # Valheim answers Steam queries on port+1


def sh(args, timeout=10, ok=(0,)):
    try:
        return run_cmd(args, timeout=timeout, ok_codes=ok)[0]
    except CheckError:
        return None


def have(cmd):
    return shutil.which(cmd, path=CHILD_ENV["PATH"]) is not None


# ------------------------------------------------------------------- host

def host_facts():
    osr = {}
    for line in read_text("/etc/os-release", "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip().strip('"')
    found, cores = {}, 0
    for line in read_text("/proc/cpuinfo", "").splitlines():
        k, _, v = line.partition(":")
        k = k.strip().lower()
        if k == "processor":
            cores += 1
        elif k in ("model name", "hardware", "model") and k not in found:
            found[k] = v.strip()
    model = found.get("model name") or found.get("hardware") or found.get("model")
    mem = 0
    for line in read_text("/proc/meminfo", "").splitlines():
        if line.startswith("MemTotal:"):
            mem = int(line.split()[1]) * 1024
    sd = sh(["systemctl", "--version"]) or ""
    m = re.search(r"systemd (\d+)", sd)
    return {
        "os": osr.get("PRETTY_NAME", "unknown"), "os_id": osr.get("ID", ""),
        "kernel": platform.release(), "arch": platform.machine(),
        "cpu": model or "unknown", "cores": cores or os.cpu_count(),
        "ram": mem, "python": platform.python_version(),
        "systemd": int(m.group(1)) if m else None,
        "hostname": socket.gethostname(),
    }


def drives():
    out = {"disks": [], "smartctl": None, "smart_devices": [], "usb": [], "nvme": False}
    text = sh(["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,TYPE,ROTA,TRAN,MODEL"])
    if text:
        for d in json.loads(text).get("blockdevices", []):
            if d.get("type") != "disk" or d.get("name", "").startswith(("zram", "loop")):
                continue
            out["disks"].append(d)
            if d.get("tran") == "usb":
                out["usb"].append("/dev/" + d["name"])
            if d.get("name", "").startswith("nvme"):
                out["nvme"] = True
    ver = sh(["smartctl", "--version"], ok=None)
    if ver:
        m = re.search(r"smartctl (\d+\.\d+)", ver)
        out["smartctl"] = m.group(1) if m else "?"
        scan = sh(["smartctl", "--scan", "--json=c"], ok=None)
        try:
            out["smart_devices"] = [d["name"] + (":" + d["type"] if d.get("type") not in (None, "scsi", "nvme", "ata", "sat") else "")
                                    for d in json.loads(scan or "{}").get("devices", [])]
        except ValueError:
            pass
    return out


def mounts():
    rows = []
    for line in read_text("/proc/self/mounts", "").splitlines():
        p = line.split()
        if len(p) >= 3 and p[2] in REAL_FS and not p[1].startswith(("/snap/", "/var/lib/docker", "/run/", "/boot/efi")):
            rows.append({"device": p[0], "mount": p[1].replace("\\040", " "), "fs": p[2]})
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: len(r["mount"])):
        if (r["device"], r["fs"]) in seen:
            continue
        seen.add((r["device"], r["fs"]))
        uniq.append(r)
    return sorted(uniq, key=lambda r: r["mount"])


def sensors():
    return len(glob.glob("/sys/class/hwmon/hwmon*/temp*_input")) or len(glob.glob("/sys/class/thermal/thermal_zone*/temp"))


# ---------------------------------------------------------------- network

def network():
    out = {"iface": None, "ip": None, "subnet": None, "tailscale": None, "wg": [], "macvlan_shim": []}
    for line in read_text("/proc/net/route", "").splitlines()[1:]:
        p = line.split()
        if len(p) > 2 and p[1] == "00000000":
            out["iface"] = p[0]
            break
    text = sh(["ip", "-j", "addr", "show"])
    links = json.loads(text) if text else []
    for link in links:
        name = link.get("ifname", "")
        kind = ((link.get("linkinfo") or {}).get("info_kind"))
        if name.startswith("wg"):
            out["wg"].append(name)
        if name == out["iface"]:
            for a in link.get("addr_info", []):
                if a.get("family") == "inet":
                    out["ip"] = a["local"]
                    out["subnet"] = str(ipaddress.ip_interface("%s/%s" % (a["local"], a["prefixlen"])).network)
                    break
    if not out["ip"]:
        # no iproute2 output: find our address with a UDP "connect" (sends nothing)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                out["ip"] = s.getsockname()[0]
            out["subnet"] = str(ipaddress.ip_interface(out["ip"] + "/24").network)
            out["subnet_guessed"] = True
        except OSError:
            pass
    dl = sh(["ip", "-j", "-d", "link", "show"])
    for link in json.loads(dl) if dl else []:
        if ((link.get("linkinfo") or {}).get("info_kind")) in ("macvlan", "ipvlan"):
            out["macvlan_shim"].append(link.get("ifname"))
    if have("tailscale"):
        ts = sh(["tailscale", "status", "--json"], ok=(0, 1))
        try:
            d = json.loads(ts or "")
            me = d.get("Self") or {}
            out["tailscale"] = {"state": d.get("BackendState"), "ips": me.get("TailscaleIPs") or [],
                                "dns_name": (me.get("DNSName") or "").rstrip(".")}
        except ValueError:
            out["tailscale"] = {"state": "not running", "ips": [], "dns_name": ""}
    return out


def listening_tcp_ports():
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in read_text(f, "").splitlines()[1:]:
            p = line.split()
            if len(p) > 3 and p[3] == "0A":  # LISTEN
                ports.add(int(p[1].rsplit(":", 1)[1], 16))
    return ports


def pick_port(preferred=8088):
    used = listening_tcp_ports()
    for port in [preferred] + list(range(8089, 8100)):
        if port not in used:
            return port, used
    return None, used


def firewall():
    out = {"ufw": None, "firewalld": None, "nft_rules": 0, "active": None}
    if have("ufw"):
        st = sh(["ufw", "status"], ok=None) or ""
        out["ufw"] = "active" if "Status: active" in st else "inactive"
    if have("firewall-cmd"):
        st = sh(["systemctl", "is-active", "firewalld"], ok=None) or ""
        out["firewalld"] = st.strip() or "inactive"
    if have("nft"):
        rs = sh(["nft", "list", "ruleset"], ok=None) or ""
        out["nft_rules"] = sum(1 for line in rs.splitlines() if line.strip().startswith(("ip ", "tcp ", "iif", "ct ", "accept", "drop", "counter")))
    if out["ufw"] == "active":
        out["active"] = "ufw"
    elif out["firewalld"] == "active":
        out["active"] = "firewalld"
    return out


# ---------------------------------------------------------------- systemd

def systemd_services():
    text = sh(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"]) or ""
    loaded = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].endswith(".service"):
            loaded[p[0][:-8]] = {"load": p[1], "active": p[2], "sub": p[3]}
    files = sh(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"]) or ""
    enabled = {line.split()[0][:-8] for line in files.splitlines()
               if len(line.split()) >= 2 and line.split()[1] == "enabled"}
    found = []
    for units, key, label, group in KNOWN_UNITS:
        present = [u for u in units if (u in loaded and loaded[u]["load"] == "loaded") and (u in enabled or loaded[u]["active"] == "active")]
        if present:
            found.append({"key": key, "label": label, "group": group, "units": present,
                          "active": all(loaded[u]["active"] == "active" for u in present)})
    for name, st in sorted(loaded.items()):  # templated tunnels / syncthing
        if name.startswith(("wg-quick@", "syncthing@", "openvpn@", "openvpn-server@")) and st["active"] == "active":
            found.append({"key": name.replace("@", "-"), "label": name, "group": "Network", "units": [name], "active": True})
    failed = [n for n, st in loaded.items() if st["active"] == "failed"]
    return found, failed


# ----------------------------------------------------------------- docker

def docker(sock="/run/docker.sock"):
    if not os.path.exists(sock):
        return None
    try:
        containers = unix_http_get_json(sock, "/containers/json?all=1", timeout=5)
        networks = unix_http_get_json(sock, "/networks", timeout=5)
    except CheckError as exc:
        return {"error": str(exc)}
    vlan = {n["Id"]: n for n in networks if n.get("Driver") in ("macvlan", "ipvlan")}
    by_id = {c["Id"]: (c.get("Names") or ["/?"])[0].lstrip("/") for c in containers}
    rows = []
    for c in containers:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        labels = c.get("Labels") or {}
        mode = (c.get("HostConfig") or {}).get("NetworkMode") or ""
        role, parent = "main", None
        if mode.startswith("container:"):
            role = "sidecar"
            pid = mode.split(":", 1)[1]
            parent = by_id.get(pid) or next((v for k, v in by_id.items() if k.startswith(pid)), pid)
        if labels.get("io.hass.type") == "addon" or name.startswith("addon_"):
            role = "add-on"
        macvlan_ips = []
        for net, n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
            if n.get("NetworkID") in vlan:
                macvlan_ips.append(n.get("IPAddress"))
        ports = {}
        for p in c.get("Ports") or []:
            if p.get("PublicPort") and p.get("Type") == "tcp":
                ports[p["PrivatePort"]] = p["PublicPort"]
        rows.append({
            "name": name, "image": c.get("Image", ""), "state": c.get("State"),
            "stack": labels.get("com.docker.compose.project") or
                     ("Home Assistant add-ons" if role == "add-on" else None),
            "role": role, "parent": parent, "host_net": mode == "host",
            "macvlan_ips": [ip for ip in macvlan_ips if ip], "ports": ports,
        })
    nets = [{"name": n["Name"], "driver": n["Driver"], "parent": (n.get("Options") or {}).get("parent"),
             "subnet": ((n.get("IPAM") or {}).get("Config") or [{}])[0].get("Subnet"),
             "ip_range": ((n.get("IPAM") or {}).get("Config") or [{}])[0].get("IPRange")} for n in vlan.values()]
    return {"containers": rows, "vlan_networks": nets}


def container_url(c, default_port):
    """Best URL from the host to reach a container's web port."""
    if c["host_net"]:
        return "http://127.0.0.1:%d" % default_port, "host network"
    if default_port in c["ports"]:
        return "http://127.0.0.1:%d" % c["ports"][default_port], "published port"
    if c["macvlan_ips"]:
        return "http://%s:%d" % (c["macvlan_ips"][0], default_port), "macvlan IP"
    return None, "no published port"


def reachable(url, timeout=2):
    m = re.match(r"https?://([^/:]+):(\d+)", url or "")
    if not m:
        return False
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------- LinuxGSM

def _read_lgsm_vars(paths):
    vals = {}
    for p in paths:
        for line in read_text(p, "").splitlines():
            m = re.match(r'^\s*(port|queryport|executable|gamename)\s*=\s*"?([^"#]*)"?', line)
            if m:
                vals[m.group(1)] = m.group(2).strip()
    return vals


def linuxgsm():
    found = []
    homes = set()
    for pw in pwd.getpwall():
        if pw.pw_dir and os.path.isdir(pw.pw_dir) and pw.pw_dir not in ("/", "/root"):
            homes.add((pw.pw_dir, pw.pw_name))
    for home, user in sorted(homes):
        for cfgdir in sorted(glob.glob(os.path.join(home, "lgsm/config-lgsm/*/"))):
            server = os.path.basename(cfgdir.rstrip("/"))  # e.g. vhserver
            for inst_cfg in sorted(glob.glob(os.path.join(cfgdir, "*.cfg"))):
                inst = os.path.basename(inst_cfg)[:-4]
                if inst.startswith("_") or inst == "common":
                    continue
                vals = _read_lgsm_vars([os.path.join(cfgdir, "_default.cfg"),
                                        os.path.join(cfgdir, "common.cfg"), inst_cfg])
                exe = os.path.basename(vals.get("executable", "").split()[0]) if vals.get("executable") else ""
                port = int(vals["port"]) if vals.get("port", "").isdigit() else None
                q = int(vals["queryport"]) if vals.get("queryport", "").isdigit() else None
                if server in MINECRAFT_LGSM:
                    proto, qport = "minecraft", port
                elif server in NO_QUERY_LGSM or port is None:
                    proto, qport = "none", None
                else:
                    proto = "a2s"
                    qport = q or (port + QUERY_OFFSET_LGSM.get(server, 0))
                found.append({"instance": inst, "server": server, "user": user, "home": home,
                              "game": vals.get("gamename") or server, "executable": exe,
                              "protocol": proto, "port": qport})
    return found


# ------------------------------------------------------------------ output

def local_domains(suffix="home.arpa", hosts="/etc/hosts"):
    names = set()
    for line in read_text(hosts, "").splitlines():
        for name in line.split("#", 1)[0].split()[1:]:
            name = name.lower().rstrip(".")
            if name.endswith("." + suffix):
                names.add(name)
    return sorted(names)


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return "%.0f %s" % (n, u)
        n /= 1024.0
    return "%.1f PiB" % n


def gather():
    facts = {"host": host_facts(), "drives": drives(), "mounts": mounts(), "sensors": sensors(),
             "network": network(), "firewall": firewall()}
    facts["port"], _ = pick_port()
    facts["services"], facts["failed_units"] = systemd_services()
    facts["docker"] = docker()
    facts["linuxgsm"] = linuxgsm()
    facts["local_domains"] = local_domains()
    facts["home_separate"] = any(m["mount"] in ("/home",) for m in facts["mounts"])
    return facts


def report(f):
    h, d, n, fw = f["host"], f["drives"], f["network"], f["firewall"]
    w = sys.stdout.write
    w("== Host\n")
    w("  %s, kernel %s (%s)\n  CPU: %s, %s cores\n  RAM: %s\n  Python %s, systemd %s\n"
      % (h["os"], h["kernel"], h["arch"], h["cpu"], h["cores"], human(h["ram"]), h["python"], h["systemd"]))
    problems = []
    pv = tuple(int(x) for x in h["python"].split(".")[:2])
    if pv < (3, 8):
        problems.append("Python %s is too old (need 3.8+)" % h["python"])
    if h["systemd"] and h["systemd"] < 247:
        problems.append("systemd %s is older than 247: some sandbox options will be ignored" % h["systemd"])
    w("== Drives\n")
    for disk in d["disks"]:
        w("  /dev/%-8s %9s  %-4s %-5s %s\n" % (disk["name"], human(int(disk.get("size") or 0)),
                                               "HDD" if disk.get("rota") in (True, "1", 1) else "SSD",
                                               disk.get("tran") or "", disk.get("model") or ""))
    w("  smartctl: %s\n" % (d["smartctl"] or "NOT INSTALLED (install smartmontools for SMART health)"))
    if d["smartctl"] and float(d["smartctl"]) < 7.0:
        problems.append("smartctl %s has no --json; install smartmontools 7.0+" % d["smartctl"])
    if d["usb"]:
        w("  USB drives: %s (if SMART fails for them, use  smart = /dev/sdX:sat)\n" % ", ".join(d["usb"]))
    w("  Temperature sensors: %d\n" % f["sensors"])
    w("== Filesystems\n")
    for m in f["mounts"]:
        w("  %-24s %-8s %s\n" % (m["mount"], m["fs"], m["device"]))
    if f["home_separate"]:
        w("  NOTE: /home is its own filesystem; see 'Separate /home' in INSTALL.md\n")
    w("== Network\n")
    w("  default interface %s, address %s, LAN subnet %s%s\n" % (n["iface"], n["ip"], n["subnet"],
      " (GUESSED /24 - check with: ip -4 addr)" if n.get("subnet_guessed") else ""))
    if n["tailscale"]:
        t = n["tailscale"]
        w("  Tailscale: %s %s %s\n" % (t["state"], " ".join(t["ips"]), t["dns_name"]))
    else:
        w("  Tailscale: not installed\n")
    if n["wg"]:
        w("  WireGuard: %s\n" % ", ".join(n["wg"]))
    w("== Firewall\n")
    w("  ufw: %s   firewalld: %s   active: %s\n" % (fw["ufw"] or "not installed", fw["firewalld"] or "not installed",
                                                     fw["active"] or "NONE"))
    w("== Dashboard port\n  %s\n" % (f["port"] or "no free port between 8088 and 8099!"))
    w("== systemd services found\n")
    for s in f["services"]:
        w("  %-22s %-28s %s\n" % (s["key"], " ".join(s["units"]), "active" if s["active"] else "NOT active"))
    if f["failed_units"]:
        w("  failed units right now: %s\n" % ", ".join(f["failed_units"]))
    w("== Docker\n")
    dk = f["docker"]
    if dk is None:
        w("  not installed\n")
    elif "error" in dk:
        w("  error: %s\n" % dk["error"])
    else:
        stacks = {}
        for c in dk["containers"]:
            stacks.setdefault(c["stack"] or "(standalone)", []).append(c)
        for name in sorted(stacks):
            w("  stack %s\n" % name)
            for c in stacks[name]:
                extra = []
                if c["role"] == "sidecar":
                    extra.append("sidecar, network via %s" % c["parent"])
                if c["role"] == "add-on":
                    extra.append("add-on")
                if c["macvlan_ips"]:
                    extra.append("macvlan %s" % ",".join(c["macvlan_ips"]))
                if c["host_net"]:
                    extra.append("host network")
                w("    %-24s %-8s %s %s\n" % (c["name"], c["state"], c["image"][:40],
                                             ("(" + "; ".join(extra) + ")") if extra else ""))
        for vn in dk["vlan_networks"]:
            w("  %s network %s on %s (%s%s)%s\n" % (vn["driver"], vn["name"], vn["parent"], vn["subnet"],
              ", ip_range " + vn["ip_range"] if vn.get("ip_range") else ", NO ip_range",
              "" if n["macvlan_shim"] else " - the host cannot reach these IPs (no macvlan shim found)"))
    w("== Local domains (*.home.arpa in /etc/hosts)\n")
    w("  %s\n" % (", ".join(f["local_domains"]) if f.get("local_domains") else "none found (Pi-hole local DNS is read at run time)"))
    w("== LinuxGSM\n")
    for g in f["linuxgsm"] or []:
        w("  %-16s user %-12s game %-14s query %s port %s\n" % (g["instance"], g["user"], g["game"], g["protocol"], g["port"]))
    if not f["linuxgsm"]:
        w("  none found\n")
    if problems:
        w("== PROBLEMS\n")
        for p in problems:
            w("  ! %s\n" % p)
    return problems


def _sec(lines, header, items, comment=None, enabled=True):
    pre = "" if enabled else "# "
    lines.append("")
    if comment:
        for c in comment.splitlines():
            lines.append("# " + c)
    lines.append(pre + "[%s]" % header)
    for k, v in items:
        lines.append(pre + "%s = %s" % (k, v))


def draft_config(f):
    h, n = f["host"], f["network"]
    L = ["# homelab-dash config: DRAFT written by detect.py on %s" % h["hostname"],
         "# Review every section, delete what you don't want, then copy to",
         "# /etc/homelab-dash/config.ini. Lines starting with # are ignored.",
         "# Secrets never go in this file: only paths to root-only files."]
    names = ["auto"]
    if n["tailscale"] and n["tailscale"].get("dns_name"):
        names.append(n["tailscale"]["dns_name"])
    _sec(L, "general", [("hostname", h["hostname"]), ("interval", "60"), ("check_timeout", "8")])
    _sec(L, "web", [("bind", "0.0.0.0"), ("port", str(f["port"] or 8088)),
                    ("allowed_hosts", " ".join(names))],
         "Host names the page may be opened with (IP addresses always work).")
    smart = "auto" if f["drives"]["smartctl"] else "off"
    if f["drives"]["usb"] and f["drives"]["smart_devices"]:
        smart = " ".join(f["drives"]["smart_devices"])
    _sec(L, "disks", [("mounts", "auto"), ("smart", smart), ("smart_interval", "1800"),
                      ("smart_skip_standby", "yes")],
         "smart = auto | off | list like  /dev/sda /dev/sdb:sat /dev/nvme0\n"
         "SMART is read every smart_interval seconds and never wakes sleeping disks.")
    _sec(L, "network", [("interfaces", "auto"), ("dns_test", "example.com"),
                        ("tailscale", "yes" if n["tailscale"] else "no")])

    dk0 = f.get("docker") or {}
    vnets = dk0.get("vlan_networks") or [] if "error" not in dk0 else []
    note = "LAN addresses card: free IPs on Docker macvlan/ipvlan networks and local domains as links."
    if vnets:
        note += "\nmacvlan/ipvlan found: " + ", ".join(
            "%s (%s%s)" % (v["name"], v.get("subnet"), ", ip_range " + v["ip_range"] if v.get("ip_range") else ", no ip_range")
            for v in vnets)
    else:
        note += "\nNo macvlan/ipvlan networks found; the card still lists local domains if there are any."
    _sec(L, "addresses", [("enabled", "auto"), ("warn_free", "5")], note)
    dom = f.get("local_domains") or []
    _sec(L, "domains", [("enabled", "auto"), ("suffix", "home.arpa"), ("scheme", "http"),
                        ("extra", ""), ("links", ""), ("hide", "")],
         "Local domains, from Pi-hole local DNS, /etc/hosts and 'extra'. Each is a link.\n"
         "links = name=https://name:port/ ... overrides the link for one name.\n"
         "Found in /etc/hosts: " + (", ".join(dom) if dom else "none"))

    dk = f["docker"]
    if dk and "error" not in dk:
        running = sorted(c["name"] for c in dk["containers"] if c["state"] == "running")
        _sec(L, "docker", [("enabled", "yes"), ("socket", "/run/docker.sock"),
                           ("must_run", " ".join(running)), ("hide", "")],
             "Every container is shown, grouped by Compose stack. Containers in must_run\n"
             "turn red when stopped; others just show grey 'stopped'.")

    L.append("")
    L.append("# ---------------------------------------------------------------- services")
    L.append("# type = systemd (unit =) | http (url =) | tcp (port =) | process (match =, user =) | docker (container =)")
    for s in f["services"]:
        if s["key"] == "docker":
            continue
        _sec(L, "service:" + s["key"], [("type", "systemd"), ("unit", " ".join(s["units"])),
                                        ("label", s["label"]), ("group", s["group"])],
             enabled=s["active"])
    stats = {}
    if dk and "error" not in dk:
        for c in dk["containers"]:
            for rx, kind, port in KNOWN_IMAGES:
                if re.search(rx, c["image"]):
                    url, how = container_url(c, port)
                    stats.setdefault(kind, (c, url, how))
        if "uptime-kuma" in stats:
            c, url, how = stats["uptime-kuma"]
            ok = reachable(url)
            _sec(L, "service:uptime-kuma-web", [("type", "http"), ("url", url or "http://127.0.0.1:3001/"),
                                                ("label", "Uptime Kuma (web)"), ("group", "Monitoring"),
                                                ("link", (url or "").replace("127.0.0.1", n["ip"] or "127.0.0.1"))],
                 "container %s, %s%s" % (c["name"], how, "" if ok else " - NOT reachable from the host"),
                 enabled=bool(url and ok))
    if "uptime-kuma" not in stats and any(s["key"] == "uptime-kuma" for s in f["services"]):
        _sec(L, "service:uptime-kuma-web", [("type", "http"), ("url", "http://127.0.0.1:3001/"),
                                            ("label", "Uptime Kuma (web)"), ("group", "Monitoring")])

    L.append("")
    L.append("# ---------------------------------------------------------------- stats")
    pi = stats.get("pihole")
    if pi or any(s["key"] == "pihole" for s in f["services"]):
        url = pi[1] if pi else "http://127.0.0.1"
        how = ("container %s, %s" % (pi[0]["name"], pi[2])) if pi else "installed as a package"
        ok = reachable(url if re.search(r":\d+", url or "") else (url or "") + ":80")
        note = "" if ok else ("\nNOT reachable from the host. A macvlan container needs a host shim\n"
                              "interface or another address; see INSTALL.md 'macvlan'.")
        _sec(L, "pihole", [("url", url or "http://PIHOLE-IP"), ("version", "auto"),
                           ("password_file", "/etc/homelab-dash/secrets/pihole.pass")],
             "Pi-hole (%s).%s\nv6: the web/app password.  v5: the API token (Settings > API)." % (how, note),
             enabled=ok)
    for kind in ("jellyfin", "emby", "plex"):
        hit = stats.get(kind)
        pkg = any(s["key"] == kind for s in f["services"])
        if not hit and not pkg:
            continue
        port = {"jellyfin": 8096, "emby": 8096, "plex": 32400}[kind]
        url = hit[1] if hit else "http://127.0.0.1:%d" % port
        ok = reachable(url)
        _sec(L, "media:" + kind, [("type", kind), ("url", url),
                                  ("key_file", "/etc/homelab-dash/secrets/%s.key" % kind),
                                  ("show_titles", "no")],
             "%s stream counts%s. show_titles = yes also shows what is playing and who."
             % (kind.capitalize(), "" if ok else " (NOT reachable from the host - check url)"),
             enabled=ok)

    L.append("")
    L.append("# ---------------------------------------------------------------- game servers")
    L.append("# protocol = a2s (Steam query) | minecraft | tcp | none (process check only)")
    for g in f["linuxgsm"] or []:
        items = [("label", "%s (%s)" % (g["game"], g["instance"])), ("protocol", g["protocol"])]
        if g["port"]:
            items += [("host", "127.0.0.1"), ("port", str(g["port"]))]
        if g["executable"]:
            items += [("process", g["executable"]), ("user", g["user"])]
        _sec(L, "game:" + g["instance"], items, "LinuxGSM %s in %s" % (g["server"], g["home"]))
    L.append("")
    return "\n".join(L)


def web_dropin(f, port=None):
    n = f["network"]
    allow = []
    if n["subnet"]:
        allow.append(n["subnet"])
    if n["tailscale"]:
        allow += ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    lines = ["# Written by detect.py: networks allowed to reach the dashboard.",
             "# This is enforced by systemd (cgroup BPF) in addition to the firewall.",
             "[Service]",
             "IPAddressAllow=%s" % " ".join(allow or ["localhost"])]
    p = port or f["port"] or 8088
    if p != 8088:
        lines.append("SocketBindAllow=tcp:%d" % p)
    return "\n".join(lines) + "\n"


def firewall_commands(f, port=None):
    n, fw = f["network"], f["firewall"]
    p = port or f["port"] or 8088
    sub = n["subnet"] or "192.168.1.0/24"
    out = []
    if fw["active"] == "ufw" or (fw["active"] is None and fw["ufw"]):
        out.append("sudo ufw allow from %s to any port %d proto tcp comment 'homelab-dash LAN'" % (sub, p))
        if n["tailscale"]:
            out.append("sudo ufw allow in on tailscale0 to any port %d proto tcp comment 'homelab-dash tailscale'" % p)
    elif fw["active"] == "firewalld":
        out.append("sudo firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"%s\" "
                   "port port=\"%d\" protocol=\"tcp\" accept'" % (sub, p))
        if n["tailscale"]:
            out.append("sudo firewall-cmd --permanent --add-rich-rule='rule family=\"ipv4\" source address=\"100.64.0.0/10\" "
                       "port port=\"%d\" protocol=\"tcp\" accept'" % p)
        out.append("sudo firewall-cmd --reload")
    else:
        out.append("# No active firewall found. The systemd IPAddressAllow= drop-in already limits the")
        out.append("# dashboard to %s%s. See INSTALL.md step 'Firewall' for enabling ufw." % (sub, " and Tailscale" if n["tailscale"] else ""))
    return "\n".join(out) + "\n"


def collector_dropin(f):
    lines = ["# Written by detect.py", "[Service]"]
    if f["home_separate"]:
        lines.append("# /home is its own filesystem: allow read-only access so its free space can be measured")
        lines.append("ProtectHome=read-only")
    if not f["drives"]["smartctl"]:
        lines.append("# smartctl not installed: SMART capabilities not needed")
        lines.append("CapabilityBoundingSet=")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="homelab-dash host detection (read-only)")
    ap.add_argument("--write", metavar="FILE", help="write the draft config here (refuses to overwrite)")
    ap.add_argument("--web-dropin", action="store_true", help="print the web unit network drop-in")
    ap.add_argument("--collector-dropin", action="store_true", help="print the collector unit drop-in")
    ap.add_argument("--firewall", action="store_true", help="print the firewall commands to run")
    ap.add_argument("--port", type=int, help="dashboard port (default: from detection)")
    ap.add_argument("--json", action="store_true", help="print raw facts as JSON")
    args = ap.parse_args(argv)
    if os.geteuid() != 0:
        print("note: not running as root; Docker, SMART and some details may be missing "
              "(run with sudo)", file=sys.stderr)
    f = gather()
    if args.port:
        f["port"] = args.port
    if args.json:
        json.dump(f, sys.stdout, indent=2, default=str)
        print()
        return 0
    if args.web_dropin:
        sys.stdout.write(web_dropin(f, args.port))
        return 0
    if args.collector_dropin:
        sys.stdout.write(collector_dropin(f))
        return 0
    if args.firewall:
        sys.stdout.write(firewall_commands(f, args.port))
        return 0
    problems = report(f)
    if args.write:
        if os.path.exists(args.write):
            print("\n%s already exists; not overwriting. Remove it or pick another name." % args.write, file=sys.stderr)
            return 1
        fd = os.open(args.write, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(draft_config(f))
        print("\nDraft config written to %s - review it before use." % args.write)
    return 1 if problems else 0
