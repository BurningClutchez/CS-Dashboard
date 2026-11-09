"""Services card (systemd, http, tcp, process, single containers) and the Docker card
(every container, grouped by Compose stack, with sidecars, add-ons and macvlan)."""

import os
import re
import time
import urllib.parse

from .util import (Card, CheckError, http_request, read_text, run_cmd, tcp_probe,
                   unix_http_get_json, worst)

_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


# ------------------------------------------------------------------ systemd

def parse_systemctl_show(text):
    units, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        units.append(cur)
    return units


def systemd_units(cfg, names):
    text, _ = run_cmd(["systemctl", "show", "--no-pager",
                       "-p", "Id,LoadState,ActiveState,SubState,Result,ActiveEnterTimestamp"]
                      + names, timeout=cfg.check_timeout, path_extra=cfg.bin_path)
    return parse_systemctl_show(text)


def unit_status(u):
    if u.get("LoadState") == "not-found":
        return "error", "unit not found"
    active, sub = u.get("ActiveState", "unknown"), u.get("SubState", "")
    detail = "%s (%s)" % (active, sub) if sub else active
    if active == "active":
        return "ok", detail
    if active in ("activating", "reloading", "deactivating"):
        return "warn", detail
    return "crit", detail


def check_systemd(cfg, sec):
    names = [n if "." in n else n + ".service" for n in re.split(r"[\s,]+", sec["unit"].strip()) if n]
    units = systemd_units(cfg, names)
    parts, statuses = [], []
    for name, u in zip(names, units):
        s, detail = unit_status(u)
        statuses.append(s)
        parts.append({"unit": u.get("Id") or name, "state": detail, "status": s})
    return {"status": worst(statuses), "units": parts,
            "detail": ", ".join("%s: %s" % (p["unit"], p["state"]) for p in parts)}


# --------------------------------------------------------------- http / tcp

def _expect_ok(code, spec):
    for part in (spec or "200-399").split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= code <= int(hi):
                return True
        elif part and int(part) == code:
            return True
    return False


def check_http(cfg, sec):
    url = sec["url"].strip()
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise CheckError("url must start with http:// or https://")
    timeout = float(sec.get("timeout", min(cfg.check_timeout, 5)))
    verify = sec.get("verify_tls", "yes").strip().lower() not in ("no", "false", "0", "off")
    start = time.monotonic()
    code, _body, _h = http_request(url, timeout=timeout, verify_tls=verify, max_bytes=65536)
    ms = int((time.monotonic() - start) * 1000)
    ok = _expect_ok(code, sec.get("expect"))
    return {"status": "ok" if ok else "crit", "detail": "HTTP %d in %d ms" % (code, ms), "ms": ms}


def check_tcp(cfg, sec):
    host = sec.get("host", "127.0.0.1").strip()
    port = int(sec["port"])
    ms = tcp_probe(host, port, timeout=float(sec.get("timeout", min(cfg.check_timeout, 3))))
    return {"status": "ok", "detail": "port %d open (%d ms)" % (port, ms), "ms": ms}


# ------------------------------------------------------------------ process

def _uid_of(user):
    if user is None or user == "":
        return None
    if str(user).isdigit():
        return int(user)
    import pwd
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        raise CheckError("unknown user %s" % user)


def _proc_uid(cfg, pid):
    for line in read_text(os.path.join(cfg.proc, pid, "status"), "").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    return None


def find_processes(cfg, needle, user=None):
    """PIDs whose command line contains `needle` (optionally owned by `user`).

    Reads /proc/<pid>/cmdline and /proc/<pid>/status only, which needs no
    special privileges.
    """
    uid = _uid_of(user)
    pids = []
    for entry in os.listdir(cfg.proc):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(cfg.proc, entry, "cmdline"), "rb") as fh:
                cmd = fh.read(4096).replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue  # process exited while we looked
        if needle not in cmd or "homelab-dash" in cmd or "collect.py" in cmd:
            continue
        if uid is not None and _proc_uid(cfg, entry) != uid:
            continue
        pids.append(int(entry))
    return sorted(pids)


def check_process(cfg, sec):
    pids = find_processes(cfg, sec["match"].strip(), sec.get("user", "").strip() or None)
    if not pids:
        return {"status": "crit", "detail": "not running"}
    return {"status": "ok", "detail": "running (%d process%s)" % (len(pids), "" if len(pids) == 1 else "es")}


# ------------------------------------------------------------------- docker

def docker_socket(cfg):
    return cfg.get("docker", "socket", "/run/docker.sock")


def docker_enabled(cfg):
    mode = cfg.tristate("docker", "enabled")
    if mode == "no":
        return False
    if mode == "yes":
        return True
    return os.path.exists(cfg.fsroot + docker_socket(cfg)) or os.path.exists(docker_socket(cfg))


def container_state(state, status_text, health=None):
    """Map Docker state to (status, short text)."""
    m = re.search(r"\((healthy|unhealthy|health: starting)\)", status_text or "")
    health = health or (m.group(1).replace("health: ", "") if m else None)
    if state == "running":
        if health == "unhealthy":
            return "crit", "unhealthy"
        if health == "starting":
            return "warn", "starting"
        return "ok", "healthy" if health == "healthy" else "running"
    if state == "restarting":
        return "crit", "restart loop"
    if state == "paused":
        return "warn", "paused"
    if state == "dead":
        return "crit", "dead"
    if state == "exited":
        m = re.search(r"Exited \((\-?\d+)\)", status_text or "")
        code = int(m.group(1)) if m else None
        if code in (0, 137, 143, None):
            return "unknown", "stopped"
        return "crit", "crashed (exit %d)" % code
    return "unknown", state or "unknown"


def check_container(cfg, sec):
    name = sec["container"].strip()
    if not _CONTAINER_RE.match(name):
        raise CheckError("bad container name")
    data = unix_http_get_json(docker_socket(cfg), "/containers/%s/json" % name,
                              timeout=cfg.check_timeout)
    st = data.get("State") or {}
    health = (st.get("Health") or {}).get("Status")
    text = "Exited (%s)" % st.get("ExitCode") if st.get("Status") == "exited" else ""
    status, short = container_state(st.get("Status"), text, health)
    if status == "unknown":
        status = "crit"  # a container you listed explicitly is expected to run
    return {"status": status, "detail": short}


def docker_overview(cfg):
    sock = docker_socket(cfg)
    containers = unix_http_get_json(sock, "/containers/json?all=1", timeout=cfg.check_timeout)
    networks = unix_http_get_json(sock, "/networks", timeout=cfg.check_timeout)
    hide = set(cfg.getlist("docker", "hide"))
    must_run = set(cfg.getlist("docker", "must_run"))

    vlan_nets = {}
    for n in networks:
        if n.get("Driver") in ("macvlan", "ipvlan"):
            cfgs = (n.get("IPAM") or {}).get("Config") or [{}]
            v4 = next((c for c in cfgs if ":" not in (c.get("Subnet") or "")), cfgs[0] if cfgs else {})
            vlan_nets[n.get("Id")] = {
                "name": n.get("Name"), "driver": n.get("Driver"),
                "parent": (n.get("Options") or {}).get("parent"),
                "subnet": v4.get("Subnet"),
                "gateway": v4.get("Gateway"),
                "ip_range": v4.get("IPRange"),
                "aux": sorted(set((v4.get("AuxiliaryAddresses") or {}).values())),
                "containers": [],
            }

    by_id, items = {}, []
    for c in containers:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        labels = c.get("Labels") or {}
        status, short = container_state(c.get("State"), c.get("Status"))
        item = {
            "name": name,
            "image": (c.get("Image") or "").split("@")[0],
            "state": c.get("State"),
            "detail": short,
            "uptime": c.get("Status"),
            "status": status,
            "stack": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "role": "main",
            "macvlan": [],
        }
        if labels.get("io.hass.type") == "addon" or name.startswith("addon_"):
            item["role"] = "add-on"
            item["stack"] = item["stack"] or "Home Assistant add-ons"
        elif labels.get("io.hass.type") or name.startswith("hassio_"):
            item["stack"] = item["stack"] or "Home Assistant Supervisor"
        mode = (c.get("HostConfig") or {}).get("NetworkMode") or ""
        if mode.startswith("container:"):
            item["role"] = "sidecar"
            item["_net_parent"] = mode.split(":", 1)[1]
        for net_name, n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
            vn = vlan_nets.get(n.get("NetworkID"))
            if vn:
                static = (n.get("IPAMConfig") or {}).get("IPv4Address") or None
                ip = n.get("IPAddress") or static
                item["macvlan"].append({"network": net_name, "driver": vn["driver"], "ip": ip})
                # a stopped container with a static IP still "owns" it: starting it would clash
                vn["containers"].append({"name": name, "ip": ip, "static": bool(static),
                                         "running": c.get("State") == "running"})
        if name in must_run and item["status"] == "unknown":
            item["status"] = "crit"
            item["detail"] = "stopped (must run)"
        by_id[c.get("Id")] = item
        by_id[name] = item
        if name not in hide:
            items.append(item)

    # Sidecars share a network namespace with another container (e.g. gluetun VPN).
    # If that container is down, the sidecar has no network even if it "runs".
    for item in items:
        pid = item.pop("_net_parent", None)
        if not pid:
            continue
        parent = by_id.get(pid) or next((v for k, v in by_id.items() if k.startswith(pid)), None)
        item["network_via"] = parent["name"] if parent else pid[:12]
        if parent:
            parent.setdefault("provides_network_to", []).append(item["name"])
            if parent["state"] != "running" and item["state"] == "running":
                item["status"] = "crit"
                item["detail"] = "no network (%s is down)" % parent["name"]

    stacks = {}
    for item in items:
        stacks.setdefault(item["stack"] or "Standalone containers", []).append(item)
    groups = []
    for name in sorted(stacks, key=lambda s: (s == "Standalone containers", s.lower())):
        members = sorted(stacks[name], key=lambda i: ({"main": 0, "add-on": 1, "sidecar": 2}[i["role"]], i["name"]))
        running = sum(1 for m in members if m["state"] == "running")
        groups.append({
            "name": name,
            # stopped containers are grey; they don't turn a stack yellow
            "status": worst([m["status"] for m in members if m["status"] != "unknown"] or ["unknown"]),
            "running": running,
            "total": len(members),
            "containers": members,
        })
    return {"stacks": groups, "vlan_networks": list(vlan_nets.values()),
            "running": sum(g["running"] for g in groups),
            "total": sum(g["total"] for g in groups)}


# -------------------------------------------------------------------- cards

CHECKS = {"systemd": check_systemd, "http": check_http, "tcp": check_tcp,
          "process": check_process, "docker": check_container}


def collect_services(cfg, state):
    card = Card("services", "services", "Services")
    items = []
    for name, sec in cfg.sections("service"):
        kind = sec.get("type", "").strip()
        label = sec.get("label", name)
        res = card.run(label, CHECKS[kind], float(sec.get("timeout", cfg.check_timeout)) + 2, cfg, sec)
        item = {"name": name, "label": label, "type": kind,
                "group": sec.get("group", "").strip() or None,
                "link": sec.get("link", "").strip() or None}
        if res:
            item.update(res)
        else:
            item.update({"status": "error", "detail": card.errors[-1]["error"]})
        card.status(item["status"])
        items.append(item)
    card.data["items"] = items
    card.data["up"] = sum(1 for i in items if i["status"] == "ok")
    card.data["total"] = len(items)
    return card.to_dict()


def collect_docker(cfg, state):
    card = Card("docker", "docker", "Docker")
    data = card.run("docker", docker_overview, cfg.check_timeout + 2, cfg)
    if data:
        card.data.update(data)
        for g in data["stacks"]:
            if g["status"] != "unknown":
                card.status(g["status"])
    return card.to_dict()
